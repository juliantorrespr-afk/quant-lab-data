#!/usr/bin/env python3
"""judge.py — turns a trade list or an equity curve into the pass-line table, the verdict and the lesson.

The pass line (SANDBOX_PROMPT_v2, rail 3) for trade-style tests:
  >= 200 out-of-sample trades after costs · expectancy >= +0.05R · profit factor >= 1.2 ·
  sized so the firm's daily-loss rule would have fired on < 1% of days · max drawdown inside the firm's
  total-loss rule with >= 30% headroom · walk-forward holds (at least half the OOS years positive).
Book-style tests (an overlay or a rival signal for the trend book) are judged against the live book:
  the declared test says which of Sharpe / maxDD / return-over-drawdown / turnover must improve, by how much.
"""
import math
import numpy as np, pandas as pd


# ----------------------------------------------------------------------------------------- trade stats
def trade_stats(tr, account=50_000.0):
    """Expectancy, profit factor, drawdown and the funded-account view of a trade list."""
    n = len(tr)
    if n == 0:
        return {"n": 0}
    R = tr["R"].values.astype(float)
    Rg = tr["R_gross"].values.astype(float)
    p = tr["pnl"].values.astype(float)
    w, l = p[p > 0], p[p <= 0]
    pf = float(w.sum() / abs(l.sum())) if len(l) and l.sum() != 0 else (99.0 if len(w) else 0.0)
    daily = tr.groupby("day")["pnl"].sum()
    eq = account + daily.cumsum()
    path = pd.concat([pd.Series([account]), eq])
    dd = (path - path.cummax())
    sqn = float(R.mean() / R.std(ddof=1) * math.sqrt(n)) if n > 1 and R.std(ddof=1) > 0 else 0.0
    cumR = np.concatenate([[0.0], np.cumsum(R)])
    max_dd_R = float((cumR - np.maximum.accumulate(cumR)).min())
    se = float(R.std(ddof=1) / math.sqrt(n)) if n > 1 else float("nan")
    return {
        "n": int(n), "expectancy_R": float(R.mean()), "expectancy_R_gross": float(Rg.mean()),
        "cost_R": float(Rg.mean() - R.mean()), "se_R": se, "t_stat": float(R.mean() / se) if se and se > 0 else 0.0,
        "sqn": sqn, "win_rate": float((p > 0).mean()), "profit_factor": pf, "max_dd_R": max_dd_R,
        "avg_win_R": float(R[R > 0].mean()) if (R > 0).any() else 0.0, "avg_loss_R": float(R[R <= 0].mean()) if (R <= 0).any() else 0.0,
        "net_usd": float(p.sum()), "max_dd_usd": float(dd.min()), "days": int(len(daily)),
        "worst_day_usd": float(daily.min()), "best_day_usd": float(daily.max()),
        "trades_per_year": float(n / max((pd.to_datetime(tr["day"]).max() - pd.to_datetime(tr["day"]).min()).days / 365.25, 1e-9)),
        "by_exit": tr.groupby("why")["R"].agg(["count", "mean"]).round(3).to_dict("index") if "why" in tr else {},
    }


def by_year(tr):
    if len(tr) == 0:
        return {}
    y = pd.to_datetime(tr["day"]).dt.year
    g = tr.groupby(y)["R"].agg(["count", "mean", "sum"])
    return {int(k): {"n": int(v["count"]), "expectancy_R": round(float(v["mean"]), 3), "sum_R": round(float(v["sum"]), 2)} for k, v in g.iterrows()}


def by_market(tr):
    if len(tr) == 0 or "market" not in tr:
        return {}
    g = tr.groupby("market")["R"].agg(["count", "mean"])
    pf = tr.groupby("market")["pnl"].apply(lambda s: s[s > 0].sum() / abs(s[s <= 0].sum()) if (s <= 0).any() and s[s <= 0].sum() != 0 else 99.0)
    return {k: {"n": int(v["count"]), "expectancy_R": round(float(v["mean"]), 3), "profit_factor": round(float(pf[k]), 2)} for k, v in g.iterrows()}


def funded_check(tr, firm):
    """Would the firm's rules have fired? daily loss firing rate, trailing end-of-day drawdown vs max loss."""
    if len(tr) == 0:
        return {"daily_loss_fires_pct": None, "max_dd_usd": None, "headroom_pct": None}
    daily = tr.groupby("day")["pnl"].sum()
    fires = float((daily <= -firm["daily_loss"]).mean() * 100)
    eq = firm["account"] + daily.cumsum()
    path = pd.concat([pd.Series([firm["account"]]), eq])
    mdd = float((path - path.cummax()).min())
    headroom = float((1 - abs(mdd) / firm["max_loss"]) * 100)
    return {"daily_loss_fires_pct": round(fires, 2), "max_dd_usd": round(mdd, 0), "headroom_pct": round(headroom, 1),
            "days": int(len(daily)), "worst_day_usd": round(float(daily.min()), 0)}


def surviving_size(tr, firm, line, base_risk_pct):
    """The largest risk-per-trade at which the firm's rules would have held: daily-loss rule fired on fewer
    than the allowed share of days AND max drawdown left the required headroom. P&L scales linearly with
    risk (units = risk$/R), so the grid is a rescale of one trade list. Returns (risk_pct or None, detail)."""
    if len(tr) == 0:
        return None, {}
    daily = tr.groupby("day")["pnl"].sum()
    yrs = max((pd.to_datetime(tr["day"]).max() - pd.to_datetime(tr["day"]).min()).days / 365.25, 1e-9)
    best = None
    for r in [x / 100.0 for x in range(200, 4, -1)]:          # 2.00% down to 0.05% in 0.01 steps
        k = r / base_risk_pct
        d = daily * k
        fires = float((d <= -firm["daily_loss"]).mean() * 100)
        path = pd.concat([pd.Series([firm["account"]]), firm["account"] + d.cumsum()])
        mdd = float((path - path.cummax()).min())
        headroom = (1 - abs(mdd) / firm["max_loss"]) * 100
        if fires < line["max_daily_loss_fires_pct"] and headroom >= line["min_headroom_pct"]:
            best = r
            det = {"risk_pct": r, "net_per_year_usd": float(d.sum() / yrs), "return_per_year_pct": float(d.sum() / yrs / firm["account"] * 100),
                   "max_dd_usd": mdd, "headroom_pct": headroom, "daily_loss_fires_pct": fires, "worst_day_usd": float(d.min())}
            break
    return best, (det if best else {"risk_pct": None, "note": "no size down to 0.05% risk survives the firm's rules"})


# ----------------------------------------------------------------------------------------- pass line (trades)
def pass_line_trades(oos, is_, firm, line, base_risk_pct=1.0):
    """Every criterion as a row: name, threshold, value, ok. `line` from the declared test."""
    tr = oos.get("_tr", pd.DataFrame())
    years = by_year(tr) if len(tr) else {}
    pos_years = sum(1 for v in years.values() if v["expectancy_R"] > 0)
    size, sd = surviving_size(tr, firm, line, base_risk_pct)
    min_ret = line.get("min_return_at_surviving_size_pct", 10.0)
    rows = [
        ("OOS trades", f">= {line['min_trades']}", oos.get("n", 0), oos.get("n", 0) >= line["min_trades"]),
        ("OOS expectancy (net)", f">= {line['min_expectancy_R']:+.2f}R", round(oos.get("expectancy_R", float('nan')), 3), oos.get("expectancy_R", -9) >= line["min_expectancy_R"]),
        ("OOS profit factor", f">= {line['min_profit_factor']}", round(oos.get("profit_factor", 0), 2), oos.get("profit_factor", 0) >= line["min_profit_factor"]),
        ("a size survives the firm's rules", f"daily-loss fires < {line['max_daily_loss_fires_pct']}% of days and >= {line['min_headroom_pct']}% headroom on the loss limit",
         (f"{size:.2f}% risk per trade" if size else "none down to 0.05%"), size is not None),
        ("return at the surviving size", f">= {min_ret:.0f}% of the account per year", (round(sd.get("return_per_year_pct"), 1) if size else None), size is not None and sd.get("return_per_year_pct", -9) >= min_ret),
        ("walk-forward: OOS years positive", f">= {line['min_positive_years_frac']*100:.0f}%", f"{pos_years}/{len(years)}", len(years) > 0 and pos_years / len(years) >= line["min_positive_years_frac"]),
        ("in-sample agrees (sign)", "IS expectancy > 0", round(is_.get("expectancy_R", float('nan')), 3), is_.get("expectancy_R", -9) > 0),
    ]
    table = [{"criterion": a, "threshold": b, "value": c, "ok": bool(d)} for a, b, c, d in rows]
    table.append({"criterion": "surviving-size detail", "threshold": "", "value": sd, "ok": size is not None})
    hard = [r["ok"] for r in table[1:6]]
    enough = table[0]["ok"]
    if all(hard) and enough:
        verdict = "PASS"
    elif all(hard) and not enough:
        verdict = "INSUFFICIENT"        # everything passes but there are not enough trades to believe it yet
    else:
        verdict = "FAIL"
    return table, verdict


def lesson_trades(oos, is_, mk, line, verdict, name, table=None):
    """One or two honest lines: where the loss comes from, which of the three fixes is worth declaring."""
    e, g, c = oos.get("expectancy_R", float("nan")), oos.get("expectancy_R_gross", float("nan")), oos.get("cost_R", float("nan"))
    n = oos.get("n", 0)
    L = []
    failed = {r["criterion"] for r in (table or []) if not r["ok"]}
    edge_ok = e >= line["min_expectancy_R"] and oos.get("profit_factor", 0) >= line["min_profit_factor"]
    if verdict != "PASS" and edge_ok and ({"a size survives the firm's rules", "return at the surviving size"} & failed):
        sd = next((r["value"] for r in table if r["criterion"] == "surviving-size detail"), {})
        ddR = oos.get("max_dd_R", float("nan"))
        if sd.get("risk_pct"):
            L.append(f"The edge is real ({e:+.3f}R over {n} trades) but jagged: the worst stretch is {abs(ddR):.0f}R deep. "
                     f"The largest size that keeps a funded account alive is {sd['risk_pct']:.2f}% risk per trade, and at that size it earns about {sd['return_per_year_pct']:.1f}% a year.")
        else:
            L.append(f"The edge is real ({e:+.3f}R over {n} trades) but no size down to 0.05% risk survives the firm's loss rules — the worst stretch is {abs(ddR):.0f}R deep.")
        L.append("The lever is the drawdown, not the entry: a filter that removes the losing stretch (or the losing market) is the one declared fix worth trying.")
        good = [k for k, v in mk.items() if v["expectancy_R"] >= line["min_expectancy_R"] and v["n"] >= 50]
        bad = [k for k, v in mk.items() if v["expectancy_R"] < line["min_expectancy_R"]]
        if good and bad:
            L.append(f"By market: works on {', '.join(good)}, loses on {', '.join(bad)}.")
            return L, "market"
        return L, "filter"
    if verdict == "PASS":
        L.append(f"{name} clears the line out of sample: {e:+.3f}R net over {n} trades (gross {g:+.3f}R, costs {c:.3f}R).")
        L.append("Next: 30 forward paper days on the venue simulator, tracking the engine within tolerance. No fix needed.")
        fix = None
    elif n == 0:
        L.append("No trades were produced — the rule never triggered on this data. Check the filters before calling it dead.")
        fix = "filter"
    elif not np.isfinite(g) or g <= 0:
        L.append(f"No signal out of sample: gross expectancy {g:+.3f}R before any cost over {n} trades. This is the rule, not the venue.")
        L.append("The only honest fix is a filter (one declared condition that removes the losing trades) — or close the family.")
        fix = "filter"
    elif g >= line["min_expectancy_R"] and e < line["min_expectancy_R"]:
        L.append(f"The signal is there ({g:+.3f}R gross, {n} trades) and costs eat it ({c:.3f}R per trade → {e:+.3f}R net).")
        L.append("The lever is cost/venue: a cheaper contract, fewer ticks of slippage, or a wider stop that makes each tick smaller in R.")
        fix = "cost_venue"
    else:
        good = [k for k, v in mk.items() if v["expectancy_R"] >= line["min_expectancy_R"] and v["n"] >= 50]
        bad = [k for k, v in mk.items() if v["expectancy_R"] < line["min_expectancy_R"]]
        if good and bad:
            L.append(f"It works on {', '.join(good)} and not on {', '.join(bad)} — a market fix (drop the losers) is the declared next step.")
            fix = "market"
        elif verdict == "INSUFFICIENT":
            L.append(f"Everything passes but only {n} out-of-sample trades — too rare to believe yet. Needs more years or more markets, not a new rule.")
            fix = "market"
        else:
            L.append(f"Weak signal out of sample: {g:+.3f}R gross, {e:+.3f}R net over {n} trades — below the +{line['min_expectancy_R']:.2f}R line but not zero.")
            L.append("A filter is the first declared fix; if the gross number does not rise past +0.10R the family closes.")
            fix = "filter"
    if verdict != "PASS" and is_.get("expectancy_R", 0) > 0.10 and (not np.isfinite(e) or e < 0):
        L.append(f"In-sample looked good ({is_['expectancy_R']:+.3f}R) and out-of-sample did not — the classic shape of a fitted number.")
    return L, fix


# ----------------------------------------------------------------------------------------- book stats
def curve_stats(eq):
    eq = eq.dropna()
    if len(eq) < 30:
        return {}
    r = eq.pct_change().dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    r_mo = eq.resample("ME").last().pct_change().dropna()
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    return {"cagr": float(cagr), "sharpe_daily252": float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0,
            "sharpe_monthly": float(r_mo.mean() / r_mo.std() * np.sqrt(12)) if len(r_mo) > 3 and r_mo.std() > 0 else 0.0,
            "max_dd": float(dd), "ret_over_dd": float(cagr / abs(dd)) if dd < 0 else float("inf"), "years": float(yrs),
            "worst_year": float(eq.resample("YE").last().pct_change().dropna().min()) if yrs > 2 else None}


def yearly(eq):
    y = eq.resample("YE").last().pct_change().dropna()
    return {int(k.year): round(float(v) * 100, 1) for k, v in y.items()}


def pass_line_book(cand, base, line, turn_cand=None, turn_base=None):
    rows = []
    def r(name, thr, val, ok):
        rows.append({"criterion": name, "threshold": thr, "value": val, "ok": bool(ok)})
    if "min_sharpe_gain" in line:
        gain = cand["sharpe_monthly"] - base["sharpe_monthly"]
        r("OOS Sharpe vs live book (monthly)", f">= live {line['min_sharpe_gain']:+.2f}", round(gain, 3), gain >= line["min_sharpe_gain"])
    if "max_dd_worse_pp" in line:
        worse = (base["max_dd"] - cand["max_dd"]) * 100     # positive = candidate drawdown is deeper
        r("OOS max drawdown vs live book", f"no deeper than {line['max_dd_worse_pp']} pp", round(worse, 2), worse <= line["max_dd_worse_pp"])
    if "max_turnover_ratio" in line and turn_cand is not None and turn_base is not None:
        ratio = float(turn_cand.sum() / max(turn_base.sum(), 1e-9))
        r("turnover vs live book", f"<= {line['max_turnover_ratio']}x", round(ratio, 2), ratio <= line["max_turnover_ratio"])
    if line.get("ret_over_dd_must_improve"):
        r("return / drawdown improves (OOS)", "candidate > baseline", f"{cand['ret_over_dd']:.2f} vs {base['ret_over_dd']:.2f}", cand["ret_over_dd"] > base["ret_over_dd"])
    if line.get("dd_must_not_worsen"):
        r("max drawdown not worse (OOS)", "candidate >= baseline", f"{cand['max_dd']*100:.1f}% vs {base['max_dd']*100:.1f}%", cand["max_dd"] >= base["max_dd"] - 1e-9)
    verdict = "PASS" if rows and all(x["ok"] for x in rows) else "FAIL"
    return rows, verdict


def lesson_book(cand, base, verdict, name, extra=""):
    L = []
    dS = cand["sharpe_monthly"] - base["sharpe_monthly"]
    dC = (cand["cagr"] - base["cagr"]) * 100
    dD = (cand["max_dd"] - base["max_dd"]) * 100
    L.append(f"{name} vs the live book out of sample: Sharpe {dS:+.2f}, CAGR {dC:+.1f} pts, max drawdown {dD:+.1f} pts (positive = shallower).")
    if verdict == "PASS":
        L.append("It clears its declared line. Next: a second engine reproduces it before anything is proposed to the money desk.")
    else:
        if dS < 0 and dD < 0:
            L.append("Worse on both quality and hole — the change removes nothing and adds nothing. Closed unless a declared fix names a mechanism.")
        elif dS >= 0 and dD < 0:
            L.append("Any return it adds is bought with a deeper hole — leverage in a costume, the shape the lab has rejected before.")
        elif dS < 0 and dD >= 0:
            L.append("Shallower hole, weaker return — it is a risk dial, not an edge; the vol target already does this job.")
        else:
            L.append("Better but not by the declared margin — inside noise; not adopted.")
    if extra:
        L.append(extra)
    return L
