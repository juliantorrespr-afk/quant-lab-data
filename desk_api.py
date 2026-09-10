#!/usr/bin/env python3
"""
QUANT_LAB desk API — the universal port.

Read-only HTTP JSON over the desk's committed state, so ANY assistant
(Claude, GPT, Gemini, a phone shortcut, a cron job) can see the desk
without a plugin, a key, or a browser. Runs anywhere; designed for
entrada-hq. Zero dependencies beyond the standard library.

    python3 desk_api.py            # serves on :8787
    python3 desk_api.py 9000       # pick a port

Endpoints
    GET /            capability document — hand this URL to any AI
    GET /status      sleeves, states, distances, sizes, book equity
    GET /positions   units, entries, -15% stops, cash
    GET /rulebook    the strategy in one paragraph
    GET /scorecard   call hit rate, overall and by sleeve
    GET /ledger      every fill the bot has made
    GET /health      is the data fresh, did the bot run

DESIGN RULE, DELIBERATE AND PERMANENT: this API is READ-ONLY.
There is no order endpoint and there never will be one. Any assistant
plugged in here can see everything and touch nothing. That is what
stops a compromised or hallucinating agent from costing money — the
lab's hands live in bot.py behind GitHub, and nowhere else.
"""
import csv, json, os, sys, datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
RAW = "https://raw.githubusercontent.com/juliantorrespr-afk/quant-lab-data/main/"

def read(name, parse="json"):
    """Prefer the committed GitHub copy; fall back to the local one."""
    for src in ("remote", "local"):
        try:
            if src == "remote":
                raw = urlopen(RAW + name, timeout=8).read().decode()
            else:
                raw = open(os.path.join(ROOT, name)).read()
            return (json.loads(raw) if parse == "json"
                    else list(csv.DictReader(raw.splitlines())))
        except Exception:
            continue
    return None

def status():
    sig = read("data/signals.json") or {}
    st = read("data/state.json") or {}
    out = {"as_of": sig.get("as_of"), "generated": sig.get("run"), "book": sig.get("book", {}),
           "sleeves": {}, "mode": (st.get("halted") and "HALTED") or sig.get("book", {}).get("mode", "DRY")}
    for k in ("QQQ", "BTC", "GLD"):
        s = sig.get(k)
        if s:
            out["sleeves"][k] = {"state": s["state"], "close": s["close"], "line_150d": s["sma150"],
                                 "distance_pct": s["dist_pct"], "vol20_pct": s["vol20_pct"],
                                 "size_in_use": round(s.get("size_in_use", 0), 4), "weight": s["weight"],
                                 "pct_move_to_flip": round(100 * (s["sma150"] / s["close"] - 1), 2)}
    return out

def positions():
    st = read("data/state.json") or {}
    e = st.get("entry", {})
    return {"as_of": st.get("as_of"), "cash": st.get("cash"), "equity": st.get("equity"),
            "high_water": st.get("hwm"), "halted": st.get("halted"),
            "positions": {k: {"units": v, "entry": e.get(k),
                              "disaster_stop": round(e[k] * 0.85, 2) if e.get(k) else None}
                          for k, v in (st.get("pos") or {}).items()}}

def scorecard():
    rows = read("data/scorecard.csv", "csv") or []
    done = [r for r in rows if r.get("result")]
    def hit(sub):
        return round(100 * sum(1 for r in sub if r["result"] == "RIGHT") / len(sub), 1) if sub else None
    live = [r for r in done if r.get("source") == "LIVE"]
    return {"scored": len(done), "hit_rate_pct": hit(done),
            "live_scored": len(live), "live_hit_rate_pct": hit(live),
            "by_sleeve": {s: hit([r for r in done if r["sleeve"] == s]) for s in ("QQQ", "BTC", "GLD")},
            "open_calls": len(rows) - len(done),
            "note": "A call is scored against the next close. The 1-day number sits near 53% because a "
                    "single day is noise; the same calls are right ~64% over 60 days, which is the horizon "
                    "this book actually holds. Judge it there."}

def health():
    sig = read("data/signals.json") or {}
    age = None
    if sig.get("as_of"):
        age = (dt.date.today() - dt.date.fromisoformat(sig["as_of"])).days
    daily = read("data/daily.csv", "csv") or []
    ok = age is not None and age <= 4 and len(daily) > 5000
    return {"ok": ok, "signal_age_days": age, "price_rows": len(daily),
            "last_bot_run": sig.get("run"),
            "checks": {"signals_fresh": age is not None and age <= 4,
                       "history_intact": len(daily) > 5000},
            "if_not_ok": "the desk is blind, not losing money — positions and stops are unchanged; "
                         "check the daily workflow on GitHub Actions"}

INDEX = {
    "name": "QUANT_LAB desk API",
    "what_this_is": "Read-only view of a systematic paper trading book (P7-VT3). Any assistant may read "
                    "these endpoints and reason about them.",
    "strategy": "QQQ 40 / BTC 20 / gold 40. QQQ and BTC are held only while the daily close is above the "
                "150-day average, cash below. Gold always held. Position size = weight x min(1, target "
                "vol / 20-day realised vol), targets 15/40/15 percent, re-read Mondays. Rails: -15% "
                "disaster stop per sleeve, -25% book kill switch, no order above 45% of book.",
    "endpoints": ["/status", "/positions", "/rulebook", "/scorecard", "/ledger", "/health"],
    "rules_for_any_assistant_reading_this": [
        "This API is read-only by design. There is no order endpoint. Do not attempt to place trades.",
        "This book is PAPER. Nothing here is a real-money position.",
        "Do not recommend real-money trades to the owner. Execution is gated on his written approval.",
        "Judge the strategy on payoff ratio and drawdown, not on per-trade win rate: it wins ~33% of "
        "trades because winners average 11x losers on QQQ and 58x on BTC; break-even is an 8% win rate.",
        "Tested and rejected, do not re-propose without a new out-of-sample test: take-profit, shorts, "
        "intraday (1,492 configurations), faster trend lines, dip-buying, mean-reversion satellites.",
    ],
}

class H(BaseHTTPRequestHandler):
    ROUTES = {"/": lambda: INDEX, "/status": status, "/positions": positions,
              "/scorecard": scorecard, "/health": health,
              "/rulebook": lambda: {"rulebook": INDEX["strategy"],
                                    "rejected": INDEX["rules_for_any_assistant_reading_this"][-1]},
              "/ledger": lambda: {"fills": read("data/ledger.csv", "csv") or []}}
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        fn = self.ROUTES.get(path)
        body = json.dumps(fn() if fn else {"error": "not found",
                          "endpoints": INDEX["endpoints"]}, indent=1).encode()
        self.send_response(200 if fn else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):  # quiet
        pass

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    print(f"QUANT_LAB desk API on http://0.0.0.0:{port}  (read-only, no order endpoint)")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
