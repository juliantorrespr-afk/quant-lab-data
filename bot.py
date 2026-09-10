"""QUANT_LAB execution bot — P7-VT3 book. Runs on GitHub Actions right after fetch.py (~00:15 UTC), every day.

THE WHOLE STRATEGY (never tuned, same on every market):
  QQQ, BTC : own it when yesterday's close is ABOVE its 150-day average. Cash when BELOW. That is the stop loss.
  Gold     : always owned (timing gold was tested and rejected).
  Size     : weight x min(1, target_vol / realized_vol_20d). Weights QQQ 40 / BTC 20 / gold 40. Targets 15 / 40 / 15 %.
             Sizes are re-read every Monday; the book is re-balanced on the first 3 weekdays of each month.
  Kill     : if the book is 25% below its high-water mark -> everything to cash, bot stops, human reviews.

MODES (env MODE):
  DRY     (default)  -> no broker. Writes the orders it WOULD send to data/ledger.csv and marks the book to market. This is the paper desk.
  ALPACA             -> sends the same orders to an Alpaca PAPER account (APCA_API_KEY_ID / APCA_API_SECRET_KEY secrets, paper endpoint). Fake money, real API.
  LIVE               -> refused unless data/LIVE_APPROVED_BY_JAE.txt exists AND APCA_LIVE=1. Nothing in this repo creates that file.
"""
import csv, os, sys, json, math, datetime as dt, urllib.request, statistics as st

MODE = os.environ.get("MODE", "DRY").upper()
BOOK = 100_000.0                      # paper book size (DRY mode) — matches the TradingView demo
W    = {"QQQ": .40, "BTC": .20, "GLD": .40}
VT   = {"QQQ": .15, "BTC": .40, "GLD": .15}
SMA  = 150
HELD = {"GLD"}
KILL = -0.25
DISASTER = -0.15                      # hard stop 15% below entry on every sleeve: never fired 2015-26, pure gap insurance
MAX_ORDER_FRAC = 0.45                 # no single order may exceed 45% of the book (rail against bad data)
DATA, LEDGER, STATE, SIGNALS = "data/daily.csv", "data/ledger.csv", "data/state.json", "data/signals.json"
ALPACA_SYM = {"QQQ": "QQQ", "GLD": "GLD", "BTC": "BTC/USD"}

# ---------- 1. load closes ----------
px = {}
for r in csv.DictReader(open(DATA)):
    px.setdefault(r["symbol"], {})[r["date"]] = float(r["close"])
last_date = min(max(px[s]) for s in W)           # only act on a date every market has closed
def series(s):
    ks = sorted(k for k in px[s] if k <= last_date); return ks, [px[s][k] for k in ks]

# ---------- 2. signals + sizes ----------
sig = {"as_of": last_date, "run": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")}
state = json.load(open(STATE)) if os.path.exists(STATE) else {"hwm": BOOK, "cash": BOOK, "pos": {s: 0.0 for s in W}, "sizes": {}, "halted": False}
d = dt.date.fromisoformat(last_date)
monday = d.weekday() == 0
first3 = sum(1 for i in range(1, d.day + 1) if dt.date(d.year, d.month, i).weekday() < 5) <= 3 and d.weekday() < 5
for s in W:
    ks, c = series(s)
    sma = st.fmean(c[-SMA:]); close = c[-1]
    rets = [c[i] / c[i - 1] - 1 for i in range(len(c) - 20, len(c))]
    vol = st.pstdev(rets) * math.sqrt(252)
    size = min(1.0, VT[s] / vol) if vol > 0 else 1.0
    on = True if s in HELD else close > sma
    ent = state.get("entry", {}).get(s)
    if ent and state["pos"][s] > 0 and close / ent - 1 < DISASTER: on = False; sig.setdefault("notes", []).append(f"{s}: DISASTER STOP hit ({(close/ent-1)*100:.1f}% from entry) -> flat, re-enter on a new 20-day high")
    sig[s] = {"close": close, "sma150": round(sma, 2), "dist_pct": round((close / sma - 1) * 100, 2), "state": "HELD" if (s in HELD and on) else ("LONG" if on else "FLAT"),
              "vol20_pct": round(vol * 100, 1), "size": round(size, 3), "weight": W[s]}
    if monday or s not in state["sizes"] or not state["sizes"].get(s): state["sizes"][s] = size   # sizes re-read weekly
    sig[s]["size_in_use"] = state["sizes"][s]

# ---------- 3. mark book, kill switch ----------
def mtm(): return state["cash"] + sum(state["pos"][s] * sig[s]["close"] for s in W)
equity = mtm(); state["hwm"] = max(state["hwm"], equity); dd = equity / state["hwm"] - 1
sig["book"] = {"equity": round(equity, 2), "hwm": round(state["hwm"], 2), "dd_pct": round(dd * 100, 2), "mode": MODE}
if state.get("halted"):
    sig["book"]["note"] = "HALTED by kill switch — human review required"; json.dump(sig, open(SIGNALS, "w"), indent=1); print(json.dumps(sig, indent=1)); sys.exit(0)

# ---------- 4. targets -> orders ----------
orders = []
for s in W:
    on = sig[s]["state"] != "FLAT"
    target_dollars = equity * W[s] * state["sizes"][s] if on else 0.0
    have = state["pos"][s] * sig[s]["close"]
    flip = (on and have == 0) or (not on and have > 0)
    if flip or first3 or (monday and abs(target_dollars - have) > 0.02 * equity):
        delta = target_dollars - have
        if abs(delta) > 0.005 * equity: orders.append((s, delta))
if dd <= KILL and not state.get("halted"):
    orders = [(s, -state["pos"][s] * sig[s]["close"]) for s in W if state["pos"][s] > 0]; state["halted"] = True
    sig["book"]["note"] = f"KILL SWITCH: book {dd*100:.1f}% below high-water mark -> all to cash, bot halted"
for s, delta in orders:
    if abs(delta) > MAX_ORDER_FRAC * equity: raise SystemExit(f"REFUSED: order {s} {delta:.0f} exceeds {MAX_ORDER_FRAC:.0%} of book — check data")

# ---------- 5. execute ----------
def alpaca(path, method="GET", body=None):
    base = "https://api.alpaca.markets" if os.environ.get("APCA_LIVE") == "1" else "https://paper-api.alpaca.markets"
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"], "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))

if MODE == "LIVE" and not (os.path.exists("data/LIVE_APPROVED_BY_JAE.txt") and os.environ.get("APCA_LIVE") == "1"):
    raise SystemExit("LIVE refused: needs data/LIVE_APPROVED_BY_JAE.txt (written by Jae, by hand) and APCA_LIVE=1")

new = not os.path.exists(LEDGER)
with open(LEDGER, "a", newline="") as f:
    w = csv.writer(f)
    if new: w.writerow(["as_of", "symbol", "side", "dollars", "price", "reason", "mode", "broker_id"])
    for s, delta in orders:
        side = "buy" if delta > 0 else "sell"; price = sig[s]["close"]
        reason = sig[s]["state"] if (sig[s]["state"] == "FLAT" or state["pos"][s] == 0) else ("rebalance" if first3 else "resize")
        if sig["book"].get("note", "").startswith("KILL"): reason = "kill-switch"
        bid = ""
        if MODE in ("ALPACA", "LIVE"):
            body = {"symbol": ALPACA_SYM[s], "notional": f"{abs(delta):.2f}", "side": side, "type": "market", "time_in_force": "gtc" if s == "BTC" else "day"}
            if side == "sell" and s != "BTC":   # equities: close by qty to avoid fractional-short rejections
                q = min(state["pos"][s], abs(delta) / price); body = {"symbol": ALPACA_SYM[s], "qty": f"{q:.4f}", "side": "sell", "type": "market", "time_in_force": "day"}
            try: bid = alpaca("/v2/orders", "POST", body).get("id", "")
            except Exception as e: bid = f"ERROR {e}"
        # paper ledger fill at the close (DRY) / expected fill (ALPACA); cost model: 0.05% ETF, 0.26% BTC
        fee = abs(delta) * (0.0026 if s == "BTC" else 0.0005)
        was = state["pos"][s]; state["pos"][s] = max(0.0, was + delta / price); state["cash"] -= delta + fee
        state.setdefault("entry", {})
        if was == 0 and delta > 0: state["entry"][s] = price
        if state["pos"][s] == 0: state["entry"][s] = None
        w.writerow([last_date, s, side, round(abs(delta), 2), price, reason, MODE, bid])

state["equity"] = round(mtm(), 2); state["as_of"] = last_date
json.dump(state, open(STATE, "w"), indent=1); json.dump(sig, open(SIGNALS, "w"), indent=1)
print(json.dumps(sig, indent=1)); print("orders:", [(s, round(x)) for s, x in orders])
