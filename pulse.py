#!/usr/bin/env python3
"""
QUANT_LAB pulse — DISPLAY ONLY. Never calls bot.py, never places an order,
never touches strategy constants. Runs hourly on GitHub Actions.

v3 (2026-09-11, command center):
  1. quotes QQQ / NQ1! / SPY / BTCUSD / XAUUSD / GLD (TradingView -> Kraken/Yahoo fallback)
  2. scores yesterday's published call and publishes today's  -> data/scorecard.csv
  3. writes docs/pulse.json (state, quotes, scorecard, health, P&L from the $100k start)
  4. writes docs/sleeves.json  (last 260 closes + SMA150 per sleeve, for the price panels)
  5. writes docs/depth.json    (Kraken BTC order book, 30 levels each side — CONTEXT ONLY)
  6. appends docs/equity.json  (one mark per hour — the real traded curve)
"""
import csv, json, os, datetime as dt

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data"); DOCS = os.path.join(ROOT, "docs")
SYMS = {"QQQ": ("NASDAQ", "QQQ"), "SPY": ("AMEX", "SPY"), "GLD": ("AMEX", "GLD"),
        "BTCUSD": ("KRAKEN", "XBTUSD"), "XAUUSD": ("OANDA", "XAUUSD"), "NQ1!": ("CME_MINI", "NQ1!")}
START_EQUITY = 100000.0          # the rehearsal account's day-one value
SMA = 150                        # display only — the rule lives in bot.py

def _get(url, timeout=15):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 quantlab-pulse"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def _kraken(pair):
    d = _get("https://api.kraken.com/0/public/Ticker?pair=" + pair)
    k = list(d["result"].keys())[0]; t = d["result"][k]
    last = float(t["c"][0]); op = float(t["o"])
    return {"price": last, "chg": round((last / op - 1) * 100, 2), "src": "kraken"}

def _yahoo(sym):
    d = _get("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=1d" % sym)
    m = d["chart"]["result"][0]["meta"]
    last = m.get("regularMarketPrice"); prev = m.get("chartPreviousClose") or m.get("previousClose")
    return {"price": last, "chg": round((last / prev - 1) * 100, 2) if (last and prev) else None, "src": "yahoo"}

FALLBACK = {"BTCUSD": lambda: _kraken("XBTUSD"),
            "XAUUSD": lambda: _yahoo("GC%3DF"),
            "NQ1!":   lambda: _yahoo("NQ%3DF"),
            "QQQ":    lambda: _yahoo("QQQ"),
            "GLD":    lambda: _yahoo("GLD"),
            "SPY":    lambda: _yahoo("SPY")}

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
                           "high": a.indicators.get("high"), "low": a.indicators.get("low"),
                           "src": "tradingview"}
            except Exception:
                got = None
        if got is None and name in FALLBACK:
            try: got = FALLBACK[name]()
            except Exception as e2: got = {"error": str(e2)[:120]}
        out[name] = got if got is not None else {"error": "no source"}
    return out

def depth():
    """Kraken BTC book, 30 levels a side. Context for the eyes; the rule never reads it."""
    try:
        d = _get("https://api.kraken.com/0/public/Depth?pair=XBTUSD&count=30")
        k = list(d["result"].keys())[0]; b = d["result"][k]
        asks = [[float(p), float(v)] for p, v, _ in b["asks"]]
        bids = [[float(p), float(v)] for p, v, _ in b["bids"]]
        return {"asks": asks, "bids": bids, "src": "kraken",
                "bid_vol": round(sum(v for _, v in bids), 3), "ask_vol": round(sum(v for _, v in asks), 3)}
    except Exception as e:
        return {"error": str(e)[:120]}

def load_json(p, d=None):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return d

def closes():
    px = {}
    try:
        with open(os.path.join(DATA, "daily.csv")) as f:
            for r in csv.DictReader(f):
                px.setdefault(r["symbol"], {})[r["date"]] = float(r["close"])
    except Exception: pass
    return px

def sleeves(px, n=260):
    """Last n closes + SMA150 per sleeve, so the page can draw price vs line without daily.csv."""
    out = {}
    for sym in ("QQQ", "BTC", "GLD"):
        ds = sorted(px.get(sym, {}))
        if len(ds) < SMA + 5: continue
        cs = [px[sym][d] for d in ds]
        sma = [None] * len(cs)
        run = sum(cs[:SMA])
        sma[SMA - 1] = run / SMA
        for i in range(SMA, len(cs)):
            run += cs[i] - cs[i - SMA]; sma[i] = run / SMA
        rows = [[ds[i], round(cs[i], 2), round(sma[i], 2)] for i in range(len(cs) - n, len(cs)) if sma[i]]
        out[sym] = rows
    return out

# ---------------------------------------------------------------- scorecard
def confidence(dist):
    a = abs(dist)
    return "HIGH" if a >= 5 else ("MEDIUM" if a >= 2 else "LOW")

def scorecard(sig, px):
    path = os.path.join(DATA, "scorecard.csv")
    hdr = ["as_of", "sleeve", "call", "dist_pct", "confidence", "close",
           "next_close", "next_ret_pct", "result", "scored_on", "source"]
    rows = []
    if os.path.exists(path):
        with open(path) as f: rows = list(csv.DictReader(f))
    for r in rows:
        if r["result"]: continue
        later = sorted(d for d in px.get(r["sleeve"], {}) if d > r["as_of"])
        if not later: continue
        nc = px[r["sleeve"]][later[0]]; ret = 100 * (nc / float(r["close"]) - 1)
        if r["call"] in ("LONG", "HELD"): res = "RIGHT" if ret > 0 else "WRONG"
        else: res = "RIGHT" if ret <= 0 else "WRONG"
        r.update(next_close=round(nc, 2), next_ret_pct=round(ret, 3), result=res, scored_on=later[0])
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
    return {"n_scored": len(done), "hit_rate": hit(done), "n_live": len(liv), "live_hit_rate": hit(liv),
            "by_sleeve": {s: hit([r for r in done if r["sleeve"] == s]) for s in ("QQQ", "BTC", "GLD")},
            "by_conf": {c: hit([r for r in done if r["confidence"] == c]) for c in ("HIGH", "MEDIUM", "LOW")},
            "last30": hit(done[-90:]), "open_calls": len(rows) - len(done),
            "live_rows": [r for r in rows if r.get("source") == "LIVE"][-12:]}

def main():
    sig = load_json(os.path.join(DATA, "signals.json"), {}) or {}
    state = load_json(os.path.join(DATA, "state.json"), {}) or {}
    ledger = []
    try:
        with open(os.path.join(DATA, "ledger.csv")) as f: ledger = list(csv.DictReader(f))
    except Exception: pass
    px = closes()
    q = quotes()
    sc = scorecard(sig, px) if sig.get("as_of") else {}
    live = None
    m = {"QQQ": "QQQ", "GLD": "GLD", "BTC": "BTCUSD"}
    if state.get("pos") and all(isinstance(q.get(m[k]), dict) and q[m[k]].get("price") for k in m):
        live = state.get("cash", 0) + sum(state["pos"].get(k, 0) * q[m[k]]["price"] for k in m)
    now = dt.datetime.now(dt.timezone.utc)
    stale = (now.date() - dt.date.fromisoformat(sig["as_of"])).days if sig.get("as_of") else None
    last_close_eq = state.get("equity")
    pnl = None
    if live:
        pnl = {"since_start_usd": round(live - START_EQUITY, 2),
               "since_start_pct": round(100 * (live / START_EQUITY - 1), 3),
               "today_pct": round(100 * (live / last_close_eq - 1), 3) if last_close_eq else None,
               "from_hwm_pct": round(100 * (live / (state.get("hwm") or START_EQUITY) - 1), 3),
               "marked_at": now.strftime("%H:%M UTC")}
    out = {"generated": now.strftime("%Y-%m-%d %H:%M UTC"), "quotes": q, "signals": sig,
           "state": state, "ledger": ledger[-40:], "scorecard": sc,
           "live_equity": round(live, 2) if live else None, "start_equity": START_EQUITY, "pnl": pnl,
           "health": {"signal_age_days": stale, "signals_ok": stale is not None and stale <= 4,
                      "bot_run": sig.get("run"), "halted": bool(state.get("halted")),
                      "broker_match": (sig.get("reconcile") or {}).get("source") == "broker"}}
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "pulse.json"), "w") as f: json.dump(out, f, indent=1)
    with open(os.path.join(DOCS, "sleeves.json"), "w") as f: json.dump(sleeves(px), f)
    with open(os.path.join(DOCS, "depth.json"), "w") as f:
        json.dump({"generated": out["generated"], **depth()}, f)
    tape_p = os.path.join(DOCS, "equity.json")
    tape = load_json(tape_p, []) or []
    if live:
        stamp = now.strftime("%Y-%m-%dT%H:00Z")
        if not tape or tape[-1][0] != stamp:
            tape.append([stamp, round(live, 2), round(q.get("QQQ", {}).get("price") or 0, 2),
                         round(q.get("BTCUSD", {}).get("price") or 0, 2), round(q.get("GLD", {}).get("price") or 0, 2)])
        with open(tape_p, "w") as f: json.dump(tape[-4000:], f)
    print(json.dumps({"generated": out["generated"], "live_equity": out["live_equity"], "pnl": pnl,
                      "scorecard": {k: v for k, v in sc.items() if k != "live_rows"}, "health": out["health"]}, indent=1))

if __name__ == "__main__":
    main()
