#!/usr/bin/env python3
"""
QUANT_LAB pulse — DISPLAY ONLY. Never calls bot.py, never places an order,
never touches strategy constants. Runs hourly on GitHub Actions.

Does three things:
  1. quotes QQQ / NQ1! / SPY / BTCUSD / XAUUSD / GLD from TradingView (tradingview-ta)
  2. scores yesterday's published call and publishes today's  -> data/scorecard.csv
  3. writes docs/pulse.json for the dashboard
"""
import csv, json, os, datetime as dt

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data"); DOCS = os.path.join(ROOT, "docs")
SYMS = {"QQQ": ("NASDAQ", "QQQ"), "SPY": ("AMEX", "SPY"), "GLD": ("AMEX", "GLD"),
        "BTCUSD": ("KRAKEN", "XBTUSD"), "XAUUSD": ("OANDA", "XAUUSD"), "NQ1!": ("CME_MINI", "NQ1!")}

def _kraken(pair):
    """Free public Kraken ticker - no key, works from any runner."""
    import urllib.request
    u = "https://api.kraken.com/0/public/Ticker?pair=" + pair
    with urllib.request.urlopen(u, timeout=15) as r:
        d = json.load(r)
    k = list(d["result"].keys())[0]
    t = d["result"][k]
    last = float(t["c"][0]); op = float(t["o"])
    return {"price": last, "chg": round((last / op - 1) * 100, 2), "src": "kraken"}

def _stooq(sym):
    """Free daily CSV, no key. sym e.g. xauusd, nq.f"""
    import urllib.request, csv as _csv, io
    u = "https://stooq.com/q/l/?s=%s&f=sd2t2ohlcv&h&e=csv" % sym
    with urllib.request.urlopen(u, timeout=15) as r:
        rows = list(_csv.DictReader(io.StringIO(r.read().decode())))
    if not rows or rows[0].get("Close") in (None, "N/D", ""):
        raise ValueError("stooq no data")
    c = float(rows[0]["Close"]); o = float(rows[0]["Open"])
    return {"price": c, "chg": round((c / o - 1) * 100, 2), "src": "stooq"}

def _yahoo(sym):
    """Free Yahoo chart endpoint, no key."""
    import urllib.request
    u = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=1d" % sym)
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.load(r)
    m = d["chart"]["result"][0]["meta"]
    last = m.get("regularMarketPrice"); prev = m.get("chartPreviousClose") or m.get("previousClose")
    return {"price": last, "chg": round((last / prev - 1) * 100, 2) if (last and prev) else None,
            "src": "yahoo"}

FALLBACK = {"BTCUSD": lambda: _kraken("XBTUSD"),
            "XAUUSD": lambda: _yahoo("GC%3DF"),
            "NQ1!":   lambda: _yahoo("NQ%3DF")}

def quotes():
    out = {}
    try:
        from tradingview_ta import TA_Handler, Interval
        have_tv = True
    except Exception as e:
        out["_tv_error"] = str(e)[:120]; have_tv = False
    for name, (ex, tk) in SYMS.items():
        got = None
        if have_tv:
            try:
                h = TA_Handler(symbol=tk, exchange=ex, screener="crypto" if ex == "KRAKEN" else
                               ("forex" if ex == "OANDA" else "america"), interval=Interval.INTERVAL_1_DAY)
                a = h.get_analysis()
                if a.indicators.get("close"):
                    got = {"price": a.indicators.get("close"), "chg": a.indicators.get("change"),
                           "sma200": a.indicators.get("SMA200"), "rsi": a.indicators.get("RSI"),
                           "src": "tradingview"}
            except Exception as e:
                got = None
                err = str(e)[:120]
        if got is None and name in FALLBACK:
            try:
                got = FALLBACK[name]()
            except Exception as e2:
                got = {"error": str(e2)[:120]}
        out[name] = got if got is not None else {"error": "no source"}
    return out

def load_json(p, d=None):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return d

# ---------------------------------------------------------------- scorecard
# A "call" is the desk's published read for a sleeve: LONG / FLAT / HELD plus a
# confidence taken from how far price sits from the 150-day line. It is scored
# the next session: the call was RIGHT if the sleeve's next-day move went the
# way the call implied (LONG/HELD -> up, FLAT -> the sleeve avoided a down day).
# This is the win rate that is honest to watch daily. It never affects trading.
def confidence(dist):
    a = abs(dist)
    return "HIGH" if a >= 5 else ("MEDIUM" if a >= 2 else "LOW")

def closes():
    px = {}
    try:
        with open(os.path.join(DATA, "daily.csv")) as f:
            for r in csv.DictReader(f):
                px.setdefault(r["symbol"], {})[r["date"]] = float(r["close"])
    except Exception: pass
    return px

def scorecard(sig):
    path = os.path.join(DATA, "scorecard.csv")
    hdr = ["as_of", "sleeve", "call", "dist_pct", "confidence", "close",
           "next_close", "next_ret_pct", "result", "scored_on", "source"]
    rows = []
    if os.path.exists(path):
        with open(path) as f: rows = list(csv.DictReader(f))
    px = closes()
    # score any open rows whose next close now exists
    for r in rows:
        if r["result"]: continue
        sym = {"QQQ": "QQQ", "BTC": "BTC", "GLD": "GLD"}[r["sleeve"]]
        later = sorted(d for d in px.get(sym, {}) if d > r["as_of"])
        if not later: continue
        nc = px[sym][later[0]]; ret = 100 * (nc / float(r["close"]) - 1)
        if r["call"] in ("LONG", "HELD"): res = "RIGHT" if ret > 0 else "WRONG"
        else: res = "RIGHT" if ret <= 0 else "WRONG"
        r.update(next_close=round(nc, 2), next_ret_pct=round(ret, 3),
                 result=res, scored_on=later[0])
    # publish today's call
    have = {(r["as_of"], r["sleeve"]) for r in rows}
    for sleeve in ("QQQ", "BTC", "GLD"):
        s = sig.get(sleeve)
        if not s or (sig["as_of"], sleeve) in have: continue
        rows.append({"as_of": sig["as_of"], "sleeve": sleeve, "call": s["state"],
                     "dist_pct": round(s["dist_pct"], 2), "confidence": confidence(s["dist_pct"]),
                     "close": s["close"], "next_close": "", "next_ret_pct": "",
                     "result": "", "scored_on": "", "source": "LIVE"})
    rows.sort(key=lambda r: (r["as_of"], r["sleeve"]))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, hdr); w.writeheader()
        for r in rows: w.writerow({k: r.get(k, "") for k in hdr})
    done = [r for r in rows if r["result"]]
    def hit(sub):
        return round(100 * sum(1 for r in sub if r["result"] == "RIGHT") / len(sub), 1) if sub else None
    liv = [r for r in done if r.get("source") == "LIVE"]
    return {"n_scored": len(done), "hit_rate": hit(done), "n_live": len(liv),
            "live_hit_rate": hit(liv),
            "by_sleeve": {s: hit([r for r in done if r["sleeve"] == s]) for s in ("QQQ", "BTC", "GLD")},
            "by_conf": {c: hit([r for r in done if r["confidence"] == c]) for c in ("HIGH", "MEDIUM", "LOW")},
            "last30": hit(done[-90:]), "open_calls": len(rows) - len(done)}

def main():
    sig = load_json(os.path.join(DATA, "signals.json"), {}) or {}
    state = load_json(os.path.join(DATA, "state.json"), {}) or {}
    ledger = []
    try:
        with open(os.path.join(DATA, "ledger.csv")) as f: ledger = list(csv.DictReader(f))
    except Exception: pass
    q = quotes()
    sc = scorecard(sig) if sig.get("as_of") else {}
    # live mark-to-market from the quotes we just pulled
    live = None
    m = {"QQQ": "QQQ", "GLD": "GLD", "BTC": "BTCUSD"}
    if state.get("pos") and all(isinstance(q.get(m[k]), dict) and q[m[k]].get("price") for k in m):
        live = state.get("cash", 0) + sum(state["pos"].get(k, 0) * q[m[k]]["price"] for k in m)
    now = dt.datetime.now(dt.timezone.utc)
    stale = None
    if sig.get("as_of"):
        stale = (now.date() - dt.date.fromisoformat(sig["as_of"])).days
    out = {"generated": now.strftime("%Y-%m-%d %H:%M UTC"), "quotes": q, "signals": sig,
           "state": state, "ledger": ledger[-40:], "scorecard": sc,
           "live_equity": round(live, 2) if live else None,
           "health": {"signal_age_days": stale, "signals_ok": stale is not None and stale <= 4,
                      "bot_run": sig.get("run"), "halted": bool(state.get("halted"))}}
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "pulse.json"), "w") as f: json.dump(out, f, indent=1)
    # --- live equity tape: one point per pulse, this is the REAL traded curve ---
    tape_p = os.path.join(DOCS, "equity.json")
    try:
        tape = json.load(open(tape_p))
    except Exception:
        tape = []
    if live:
        stamp = now.strftime("%Y-%m-%dT%H:00Z")
        if not tape or tape[-1][0] != stamp:
            tape.append([stamp, round(live, 2),
                         round(q.get("QQQ", {}).get("price") or 0, 2),
                         round(q.get("BTCUSD", {}).get("price") or 0, 2),
                         round(q.get("GLD", {}).get("price") or 0, 2)])
        with open(tape_p, "w") as f: json.dump(tape[-4000:], f)
    print(json.dumps({"generated": out["generated"], "live_equity": out["live_equity"],
                      "scorecard": sc, "health": out["health"]}, indent=1))

if __name__ == "__main__":
    main()
