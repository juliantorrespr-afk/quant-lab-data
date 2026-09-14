#!/usr/bin/env python3
"""runner.py — the machine that runs a declared test and writes the verdict.

    python runner.py --list                 what is declared, what is ready, what has run
    python runner.py --next                 run the first declared test whose data exists and which has not run yet
    python runner.py --ready                run every ready, not-yet-run test in queue order
    python runner.py --run 03_swing_pullback
    python runner.py --inventory            write docs/vault.json (what the vault holds)
    python runner.py --push                 push results, ledger, docs/*.json and the MIND page to the public repo (needs GITHUB_TOKEN)
    flags: --force (re-run even if already run on the same data)  --out DIR (default: this folder's parent)

Rails it enforces by construction:
  * a test runs only if it is written down in tests/ with its expectation (nothing on the fly);
  * a dead family (families.json) is refused; an open family closes after three declared fixes;
  * scheduled runs never change a parameter or a verdict — this script only reads tests/ and writes results/;
  * every verdict records the exact data files (with hashes) it was computed from.
"""
import os, sys, json, glob, argparse, datetime as dt, pathlib, traceback, hashlib
import numpy as np, pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vault, rules, judge

OUT = pathlib.Path(os.environ.get("QL_OUT", HERE.parent))
RESULTS = OUT / "results"
DOCS = OUT / "docs"
LEDGER = HERE / "ledger.json"
FAMILIES = HERE / "families.json"
NOW = lambda: dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def jload(p, default):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except Exception:
        return default


def jdump(p, obj):
    pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(p).write_text(json.dumps(obj, indent=1, default=_json_default))


def _json_default(o):
    if isinstance(o, (np.integer,)): return int(o)
    if isinstance(o, (np.floating,)): return None if not np.isfinite(o) else float(o)
    if isinstance(o, (np.bool_,)): return bool(o)
    if isinstance(o, (pd.Timestamp, dt.datetime, dt.date)): return str(o)[:19]
    if isinstance(o, float) and not np.isfinite(o): return None
    return str(o)


# ----------------------------------------------------------------------------------------- declared tests
def load_tests():
    ts = [jload(p, None) for p in sorted(glob.glob(str(HERE / "tests" / "*.json")))]
    ts = [t for t in ts if t]
    return sorted(ts, key=lambda t: t.get("queue", 99))


def data_ready(t):
    """(ready: bool, blocker: str)"""
    for need in t["data"]["needs"]:
        if need == "daily_closes":
            try:
                vault.load_daily(fetch=True)
            except Exception as e:
                return False, f"daily closes unreachable ({e})"
        elif need.startswith("databento:"):
            if not vault._databento_files(need.split(":")[1]):
                return False, f"vault has no Databento {need.split(':')[1]} 1-min bars"
        elif need.startswith("dukascopy:"):
            if not glob.glob(str(vault.RAW / "dukascopy" / f"{need.split(':')[1]}-m5-*.csv")):
                return False, f"vault has no Dukascopy {need.split(':')[1]} 5-min bars"
        elif need == "calendar":
            if not (vault.VAULT / "derived" / "calendar.csv").exists():
                return False, "no news calendar yet (calendar_pull.py on the server)"
        else:
            return False, f"unknown data need {need}"
    return True, ""


def family_gate(t, fam):
    f = t["family"]
    if f in fam.get("dead", {}):
        return False, f"family '{f}' is dead ({fam['dead'][f]['doc']}) — never retried under a new name"
    o = fam.get("open", {}).get(f)
    if o and o.get("status") == "closed":
        return False, f"family '{f}' closed after three declared fixes"
    return True, ""


# ----------------------------------------------------------------------------------------- windows
def split_trades_by_window(tr, windows):
    a, b = windows["select"], windows["judge"]
    d = pd.to_datetime(tr["day"])
    is_ = tr[(d >= a[0]) & (d <= a[1])]
    oos = tr[(d >= b[0]) & (d <= b[1])]
    return is_, oos


def split_trades_by_fraction(tr, all_days, frac=0.6):
    days = sorted({pd.Timestamp(x).tz_localize(None) if pd.Timestamp(x).tzinfo else pd.Timestamp(x) for x in all_days})
    cut = days[int(len(days) * frac)] if days else None
    if cut is None or len(tr) == 0:
        return tr, tr, cut
    d = pd.to_datetime(tr["day"])
    return tr[d < cut], tr[d >= cut], cut


def stats_block(tr, account):
    s = judge.trade_stats(tr, account)
    s["_tr"] = tr
    return s


def clean(s):
    return {k: v for k, v in s.items() if not k.startswith("_")}


# ----------------------------------------------------------------------------------------- runners by kind
def run_session_trades(t):
    prov = []
    all_tr, all_days = [], []
    cost, sizing = t["cost"], t["sizing"]
    for market, contract in t["markets"].items():
        df1m, p = vault.load_futures_1m(market)
        prov.append({market: p})
        sessions, daily = vault.sessions_from_1m(df1m)
        all_days += [s["day"] for s in sessions]
        state = None
        if t["params"].get("filters", {}).get("trend_agree"):
            state = daily["c"] > daily["c"].rolling(150).mean()
        tr = rules.run_session_rule(sessions, t["rule"], t["params"], rules.INSTRUMENTS[contract], cost, sizing, state)
        tr["market"] = market
        all_tr.append((market, contract, sessions, state, tr))
    tr = pd.concat([x[4] for x in all_tr], ignore_index=True) if all_tr else pd.DataFrame()
    is_, oos, cut = split_trades_by_fraction(tr, all_days, 0.6)
    S_is, S_oos = stats_block(is_, sizing["account"]), stats_block(oos, sizing["account"])
    table, verdict = judge.pass_line_trades(S_oos, S_is, t["firm"], t["pass_line"], t["sizing"]["risk_pct"])
    mk = judge.by_market(oos)
    lesson, fix = judge.lesson_trades(S_oos, S_is, mk, t["pass_line"], verdict, t["name"], table)
    sens = {}
    for st in t.get("sensitivity", {}).get("slip_ticks", []):
        c2 = dict(cost, slip_ticks=st)
        rows = []
        for market, contract, sessions, state, _ in all_tr:
            r = rules.run_session_rule(sessions, t["rule"], t["params"], rules.INSTRUMENTS[contract], c2, sizing, state)
            rows.append(r)
        r = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
        _, o2, _ = split_trades_by_fraction(r, all_days, 0.6)
        so = judge.trade_stats(o2, sizing["account"])
        sens[f"slip {st} tick/side"] = {"oos_expectancy_R": so.get("expectancy_R"), "oos_pf": so.get("profit_factor"), "n": so.get("n", 0)}
    return {"verdict": verdict, "table": table, "lesson": lesson, "fix_recommended": fix, "split_date": str(cut)[:10] if cut is not None else None,
            "in_sample": clean(S_is), "out_of_sample": clean(S_oos), "by_market_oos": mk, "by_year_oos": judge.by_year(oos),
            "funded_oos": judge.funded_check(oos, t["firm"]), "sensitivity": sens, "provenance": prov, "trades": tr}


def run_daily_trades(t):
    px, p = vault.load_daily()
    px = px[[m for m in t["markets"] if m in px.columns]]
    costs = {k: v for k, v in t["costs_bps"].items() if k != "note"}
    tr = rules.swing_pullback(px, t["params"], costs, t["sizing"])
    pf = t.get("post_filter", {}).get("trailing_edge")
    if pf:
        tr = rules.trailing_edge_filter(tr, pf.get("years", 3), pf.get("min_trades", 30))
    is_, oos = split_trades_by_window(tr, t["windows"])
    S_is, S_oos = stats_block(is_, t["sizing"]["account"]), stats_block(oos, t["sizing"]["account"])
    table, verdict = judge.pass_line_trades(S_oos, S_is, t["firm"], t["pass_line"], t["sizing"]["risk_pct"])
    mk = judge.by_market(oos)
    lesson, fix = judge.lesson_trades(S_oos, S_is, mk, t["pass_line"], verdict, t["name"], table)
    sens = {}
    for x in t.get("sensitivity", {}).get("costs_x", []):
        c2 = {k: v * x for k, v in costs.items()}
        r = rules.swing_pullback(px, t["params"], c2, t["sizing"])
        if pf:
            r = rules.trailing_edge_filter(r, pf.get("years", 3), pf.get("min_trades", 30))
        _, o2 = split_trades_by_window(r, t["windows"])
        so = judge.trade_stats(o2, t["sizing"]["account"])
        sens[f"costs x{x:g}"] = {"oos_expectancy_R": so.get("expectancy_R"), "oos_pf": so.get("profit_factor"), "n": so.get("n", 0)}
    # correlation of the rule's daily P&L with the live book (does it diversify?)
    corr = None
    try:
        frac = rules.state_sma(px)
        eq_b, _ = rules.book_engine(px, frac)
        rb = eq_b.pct_change()
        daily = tr.groupby("exit_day")["pnl"].sum()
        daily.index = pd.to_datetime(daily.index)
        rs = daily.reindex(rb.index).fillna(0.0) / t["sizing"]["account"]
        j = rs.index >= t["windows"]["judge"][0]
        corr = float(np.corrcoef(rs[j], rb[j].fillna(0))[0, 1])
    except Exception:
        pass
    return {"verdict": verdict, "table": table, "lesson": lesson, "fix_recommended": fix,
            "in_sample": clean(S_is), "out_of_sample": clean(S_oos), "by_market_oos": mk, "by_year_oos": judge.by_year(oos),
            "funded_oos": judge.funded_check(oos, t["firm"]), "sensitivity": sens, "corr_with_live_book_oos": corr,
            "provenance": [p], "trades": tr}


def run_book(t):
    px, p = vault.load_daily()
    px = px[["QQQ", "BTC", "GLD"]]
    base_frac = rules.state_sma(px)
    cand_frac = rules.state_two_speed(px, t["params"]["fast"], t["params"]["slow"])
    eq_b, tb = rules.book_engine(px, base_frac)
    eq_c, tc = rules.book_engine(px, cand_frac)
    # judge both on the candidate's own valid span (its slow line needs 250 days)
    start = px.index[t["params"]["slow"] + 1]
    eq_b, eq_c = eq_b[eq_b.index >= start], eq_c[eq_c.index >= start]
    eq_b, eq_c = eq_b / eq_b.iloc[0] * 100000, eq_c / eq_c.iloc[0] * 100000
    w = t["windows"]
    seg = lambda e, a, b: e[(e.index >= a) & (e.index <= b)]
    B = {"full": judge.curve_stats(eq_b), "is": judge.curve_stats(seg(eq_b, *w["select"])), "oos": judge.curve_stats(seg(eq_b, *w["judge"]))}
    C = {"full": judge.curve_stats(eq_c), "is": judge.curve_stats(seg(eq_c, *w["select"])), "oos": judge.curve_stats(seg(eq_c, *w["judge"]))}
    j = lambda s: s[(s.index >= w["judge"][0]) & (s.index <= w["judge"][1])]
    table, verdict = judge.pass_line_book(C["oos"], B["oos"], t["pass_line"], j(tc), j(tb))
    turn = {"candidate_per_year": float(j(tc).sum() / max(C["oos"]["years"], 1e-9)), "baseline_per_year": float(j(tb).sum() / max(B["oos"]["years"], 1e-9))}
    lesson = judge.lesson_book(C["oos"], B["oos"], verdict, t["name"],
                               extra=f"Turnover out of sample: {turn['candidate_per_year']:.1f}x a year vs the book's {turn['baseline_per_year']:.1f}x.")
    sens = {}
    for slow in t.get("sensitivity", {}).get("slow", []):
        if slow == t["params"]["slow"]: continue
        f2 = rules.state_two_speed(px, t["params"]["fast"], slow)
        e2, t2 = rules.book_engine(px, f2)
        st2 = px.index[slow + 1]
        e2 = e2[e2.index >= st2]
        sens[f"slow {slow}"] = {"oos": judge.curve_stats(seg(e2, *w["judge"])), "baseline_oos_same_span": judge.curve_stats(seg(eq_b[eq_b.index >= st2], *w["judge"]))}
    curve = pd.DataFrame({"live_book": eq_b, "candidate": eq_c}).dropna()
    return {"verdict": verdict, "table": table, "lesson": lesson, "fix_recommended": None if verdict == "PASS" else "close",
            "baseline": B, "candidate": C, "turnover_oos": turn, "yearly": {"live_book": judge.yearly(eq_b), "candidate": judge.yearly(eq_c)},
            "sensitivity": sens, "provenance": [p], "curve": curve}


def run_overlay(t):
    px, p = vault.load_daily()
    c = px["GLD"]
    P = t["params"]
    ones = pd.Series(1.0, index=c.index)
    sched = rules.ride_the_run_schedule(c, P["base"], P["step"], P["cap"], P["high_days"], P["low_days"])
    eq_b, tb = rules.sleeve_engine(c, ones, P["vol_target"], P["cost"])
    eq_o, to = rules.sleeve_engine(c, sched, P["vol_target"], P["cost"])
    w = t["windows"]
    seg = lambda e, a, b: e[(e.index >= a) & (e.index <= b)]
    B = {k: judge.curve_stats(seg(eq_b, *w[k])) for k in ["select", "judge", "seat"]}
    O = {k: judge.curve_stats(seg(eq_o, *w[k])) for k in ["select", "judge", "seat"]}
    B["full"], O["full"] = judge.curve_stats(eq_b), judge.curve_stats(eq_o)
    table, verdict = judge.pass_line_book(O["seat"], B["seat"], t["pass_line"])
    ugly = {str(y): {"live_sleeve_pct": judge.yearly(eq_b).get(y), "overlay_pct": judge.yearly(eq_o).get(y)} for y in w.get("ugly_years", [])}
    lesson = judge.lesson_book(O["seat"], B["seat"], verdict, t["name"],
                               extra=f"Average size held: overlay {sched.mean():.2f} of the vol target vs 1.00 — it holds less, so lower return is arithmetic, not a finding.")
    sens = {}
    v = t.get("sensitivity", {}).get("uncapped_variant")
    if v:
        s2 = rules.ride_the_run_schedule(c, v["base"], v["step"], v["cap"], P["high_days"], P["low_days"])
        e2, _ = rules.sleeve_engine(c, s2, P["vol_target"], P["cost"])
        sens["uncapped (adds above the vol target)"] = {"seat": judge.curve_stats(seg(e2, *w["seat"])), "judge": judge.curve_stats(seg(e2, *w["judge"])), "avg_size": float(s2.mean())}
    curve = pd.DataFrame({"gold_full_size": eq_b, "ride_the_run": eq_o, "size_held": sched}).dropna()
    return {"verdict": verdict, "table": table, "lesson": lesson, "fix_recommended": None if verdict == "PASS" else "close",
            "baseline": B, "candidate": O, "ugly_years": ugly, "avg_size_held": float(sched.mean()), "sensitivity": sens,
            "yearly": {"gold_full_size": judge.yearly(eq_b), "ride_the_run": judge.yearly(eq_o)}, "provenance": [p], "curve": curve}


def run_event_trades(t):
    bars, p1 = vault.load_dukascopy("xauusd", "m5")
    cal, p2 = vault.load_calendar()
    ev = cal[cal["event"].isin(t["params"]["events"])]["release_time_utc"]
    events = [pd.Timestamp(x).tz_localize("UTC").tz_convert(vault.NY) if pd.Timestamp(x).tzinfo is None else pd.Timestamp(x).tz_convert(vault.NY) for x in ev]
    contract = list(t["markets"].values())[0]
    tr = rules.run_event_rule(bars, events, t["params"], rules.INSTRUMENTS[contract], t["cost"], t["sizing"])
    tr["market"] = "XAUUSD"
    is_, oos, cut = split_trades_by_fraction(tr, [pd.Timestamp(x).normalize() for x in events], 0.6)
    S_is, S_oos = stats_block(is_, t["sizing"]["account"]), stats_block(oos, t["sizing"]["account"])
    table, verdict = judge.pass_line_trades(S_oos, S_is, t["firm"], t["pass_line"], t["sizing"]["risk_pct"])
    lesson, fix = judge.lesson_trades(S_oos, S_is, {}, t["pass_line"], verdict, t["name"], table)
    return {"verdict": verdict, "table": table, "lesson": lesson, "fix_recommended": fix, "split_date": str(cut)[:10] if cut is not None else None,
            "in_sample": clean(S_is), "out_of_sample": clean(S_oos), "by_year_oos": judge.by_year(oos),
            "funded_oos": judge.funded_check(oos, t["firm"]), "provenance": [p1, p2], "trades": tr}


KINDS = {"session_trades": run_session_trades, "daily_trades": run_daily_trades, "book": run_book, "overlay": run_overlay, "event_trades": run_event_trades}


# ----------------------------------------------------------------------------------------- one test, start to finish
def run_test(t, force=False):
    led = jload(LEDGER, {"tests": {}, "families": {}})
    fam = jload(FAMILIES, {"dead": {}, "open": {}})
    ok, why = family_gate(t, fam)
    if not ok:
        log(f"REFUSED {t['id']}: {why}"); return {"verdict": "REFUSED", "why": why}
    ready, blocker = data_ready(t)
    if not ready:
        log(f"BLOCKED {t['id']}: {blocker}")
        led["tests"].setdefault(t["id"], {}).update({"status": "blocked", "blocker": blocker, "checked": NOW(), "name": t["name"], "family": t["family"], "queue": t["queue"]})
        jdump(LEDGER, led); return {"verdict": "BLOCKED", "why": blocker}
    log(f"RUN {t['id']} — {t['name']}")
    res = KINDS[t["kind"]](t)
    data_sha = sorted({s for p in res["provenance"] for v in ([p] if "sha256" in p else p.values()) for s in v.get("sha256", [])})
    prev = led["tests"].get(t["id"], {})
    if prev.get("status") == "run" and prev.get("data_sha") == data_sha and not force:
        log(f"already run on identical data ({t['id']}); use --force to repeat"); return {"verdict": prev.get("verdict"), "why": "unchanged"}
    # ---- persist
    d = RESULTS / t["id"]; d.mkdir(parents=True, exist_ok=True)
    if "trades" in res and len(res["trades"]):
        res["trades"].to_csv(d / "trades.csv", index=False)
    if "curve" in res:
        res["curve"].to_csv(d / "curve.csv")
    key = key_numbers(t, res)
    verdict_doc = {"id": t["id"], "name": t["name"], "family": t["family"], "kind": t["kind"], "ran_at": NOW(), "declared": t["declared"],
                   "verdict": res["verdict"], "key": key, "pass_line_table": res["table"], "lesson": res["lesson"],
                   "fix_recommended": res.get("fix_recommended"), "expectation": t["expectation"], "plain_words": t["plain_words"],
                   "data_sha": data_sha, "provenance": res["provenance"], "engine": "sandbox/runner.py + rules.py + judge.py (first run — single engine, provisional until reproduced)",
                   "detail": {k: v for k, v in res.items() if k not in ("trades", "curve", "table", "lesson", "provenance", "verdict")}}
    jdump(d / "verdict.json", verdict_doc)
    # ---- ledger + family bookkeeping
    led["tests"][t["id"]] = {"status": "run", "verdict": res["verdict"], "ran_at": verdict_doc["ran_at"], "name": t["name"], "family": t["family"],
                             "queue": t["queue"], "key": key, "lesson": res["lesson"], "fix_recommended": res.get("fix_recommended"), "data_sha": data_sha}
    f = led["families"].setdefault(t["family"], {"status": "open", "fixes_used": 0, "tests": []})
    if t["id"] not in f["tests"]: f["tests"].append(t["id"])
    if t.get("fix_type"):
        f["fixes_used"] = f.get("fixes_used", 0) + 1
        if f["fixes_used"] >= 3 and res["verdict"] != "PASS":
            f["status"] = "closed"; f["closed_at"] = NOW(); f["closed_why"] = "three declared fixes, none passed"
    led["updated"] = NOW()
    jdump(LEDGER, led)
    log(f"VERDICT {t['id']}: {res['verdict']} — {res['lesson'][0] if res['lesson'] else ''}")
    return verdict_doc


def key_numbers(t, res):
    if t["kind"] in ("session_trades", "daily_trades", "event_trades"):
        o, i = res["out_of_sample"], res["in_sample"]
        return {"oos_trades": o.get("n", 0), "oos_expectancy_R": o.get("expectancy_R"), "oos_expectancy_R_gross": o.get("expectancy_R_gross"),
                "oos_cost_R": o.get("cost_R"), "oos_profit_factor": o.get("profit_factor"), "oos_win_rate": o.get("win_rate"),
                "oos_max_dd_usd": o.get("max_dd_usd"), "oos_max_dd_R": o.get("max_dd_R"), "surviving_size": next((r["value"] for r in res["table"] if r["criterion"] == "surviving-size detail"), {}), "is_trades": i.get("n", 0), "is_expectancy_R": i.get("expectancy_R"),
                
                "trades_per_year": o.get("trades_per_year")}
    if t["kind"] == "book":
        return {"oos_sharpe_candidate": res["candidate"]["oos"].get("sharpe_monthly"), "oos_sharpe_live": res["baseline"]["oos"].get("sharpe_monthly"),
                "oos_cagr_candidate": res["candidate"]["oos"].get("cagr"), "oos_cagr_live": res["baseline"]["oos"].get("cagr"),
                "oos_maxdd_candidate": res["candidate"]["oos"].get("max_dd"), "oos_maxdd_live": res["baseline"]["oos"].get("max_dd"),
                "turnover_candidate": res["turnover_oos"]["candidate_per_year"], "turnover_live": res["turnover_oos"]["baseline_per_year"]}
    if t["kind"] == "overlay":
        return {"seat_ret_over_dd_candidate": res["candidate"]["seat"].get("ret_over_dd"), "seat_ret_over_dd_live": res["baseline"]["seat"].get("ret_over_dd"),
                "seat_cagr_candidate": res["candidate"]["seat"].get("cagr"), "seat_cagr_live": res["baseline"]["seat"].get("cagr"),
                "seat_maxdd_candidate": res["candidate"]["seat"].get("max_dd"), "seat_maxdd_live": res["baseline"]["seat"].get("max_dd"),
                "avg_size_held": res["avg_size_held"]}
    return {}


# ----------------------------------------------------------------------------------------- the MIND's node
def write_sandbox_json():
    led = jload(LEDGER, {"tests": {}, "families": {}})
    fam = jload(FAMILIES, {"dead": {}, "open": {}})
    tests = load_tests()
    queue = []
    counts = {"declared": len(tests), "run": 0, "passed": 0, "failed": 0, "insufficient": 0, "blocked": 0}
    latest = None
    for t in tests:
        e = led["tests"].get(t["id"], {})
        ready, blocker = data_ready(t)
        status = e.get("status", "declared")
        if status == "run":
            counts["run"] += 1
            v = e.get("verdict", "")
            counts["passed" if v == "PASS" else "insufficient" if v == "INSUFFICIENT" else "failed"] += 1
            if latest is None or e.get("ran_at", "") >= latest.get("ran_at", ""):
                latest = {"id": t["id"], "name": t["name"], "verdict": v, "ran_at": e.get("ran_at"), "lesson": e.get("lesson", [""])[0], "key": e.get("key", {})}
        elif not ready:
            counts["blocked"] += 1; status = "blocked"
        queue.append({"id": t["id"], "queue": t["queue"], "name": t["name"], "family": t["family"], "kind": t["kind"], "status": status,
                      "verdict": e.get("verdict"), "ran_at": e.get("ran_at"), "data_ready": ready, "blocker": blocker or e.get("blocker", ""),
                      "expectation": t["expectation"], "plain_words": t["plain_words"], "key": e.get("key"), "lesson": e.get("lesson"),
                      "fix_recommended": e.get("fix_recommended"), "fixes_used": led["families"].get(t["family"], {}).get("fixes_used", 0)})
    try:
        inv = vault.inventory()
    except Exception as e:
        inv = {"error": str(e), "datasets": []}
    doc = {"updated": NOW(), "counts": counts, "queue": queue, "latest": latest,
           "families": {"open": [k for k, v in led["families"].items() if v.get("status", "open") == "open"] + [k for k in fam.get("open", {}) if k not in led["families"]],
                        "closed": [k for k, v in led["families"].items() if v.get("status") == "closed"], "dead": len(fam.get("dead", {}))},
           "vault": {"datasets": len(inv.get("datasets", [])), "items": [{"name": d["name"], "first": d["first"], "last": d["last"], "rows": d["rows"], "size_mb": d["size_mb"]} for d in inv.get("datasets", [])]},
           "the_number": f"{counts['declared']} declared / {counts['run']} run / {counts['passed']} passed"}
    jdump(DOCS / "sandbox.json", doc)
    jdump(DOCS / "vault.json", inv)
    log(f"sandbox.json: {doc['the_number']} · vault datasets {doc['vault']['datasets']}")
    return doc


# ----------------------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true"); ap.add_argument("--next", action="store_true"); ap.add_argument("--ready", action="store_true")
    ap.add_argument("--run"); ap.add_argument("--inventory", action="store_true"); ap.add_argument("--push", action="store_true")
    ap.add_argument("--force", action="store_true"); ap.add_argument("--out")
    a = ap.parse_args()
    global OUT, RESULTS, DOCS
    if a.out:
        OUT = pathlib.Path(a.out); RESULTS, DOCS = OUT / "results", OUT / "docs"
    tests = load_tests()
    led = jload(LEDGER, {"tests": {}, "families": {}})
    if a.list or not any([a.next, a.ready, a.run, a.inventory, a.push]):
        print(f"{'#':>2} {'id':24} {'status':10} {'verdict':12} {'data':7} note")
        for t in tests:
            e = led["tests"].get(t["id"], {})
            ready, blocker = data_ready(t)
            print(f"{t['queue']:>2} {t['id']:24} {e.get('status','declared'):10} {str(e.get('verdict') or ''):12} {'ready' if ready else 'no':7} {blocker or t['expectation'][:70]}")
    ran = []
    if a.run:
        t = next((x for x in tests if x["id"] == a.run), None)
        if not t: sys.exit(f"no declared test {a.run}")
        ran.append(run_test(t, a.force))
    if a.next or a.ready:
        for t in tests:
            e = led["tests"].get(t["id"], {})
            if e.get("status") == "run" and not a.force: continue
            ready, _ = data_ready(t)
            if not ready:
                run_test(t)          # records the blocker
                continue
            ran.append(run_test(t, a.force))
            if a.next: break
        if not ran:
            log("nothing to run: every ready test has already run (use --force) — blockers recorded")
    if a.inventory or ran or a.next or a.ready:
        write_sandbox_json()
    if a.push:
        import publish
        publish.push_all(OUT, HERE)


if __name__ == "__main__":
    main()
