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
EVAL_START = 100_000.0                # the balance an evaluation would be measured from
# EXPOSURE — how much of the lab's full size this venue may run. Set per venue, never tuned for return.
#   1.00  paper lab      — no external drawdown floor, so the book runs at full size
#   0.70  evaluation     — ends when the target is hit, so the 180-day result governs: 0 failures / 548
#   0.60  once funded    — never ends, so the LIFETIME drawdown governs: -9.3% against a -10% floor
# survive.py 2026-09-10: at 0.70 the 11-year drawdown is still -11.1%; only 0.60 clears the floor.
# Sharpe is flat (1.35-1.38) from 0.85 down to 0.35 — sizing down costs speed, not quality.
EXPOSURE = float(os.environ.get("EXPOSURE", "1.0"))
if not 0.1 <= EXPOSURE <= 1.0:
    raise SystemExit(f"REFUSED: EXPOSURE={EXPOSURE} outside 0.1-1.0")
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

# ---------- 2a. THE BROKER IS THE TRUTH ----------
# Our ledger models a fill at yesterday's close. The broker fills at the live market price.
# Those two numbers are never identical, and left alone the gap compounds until the bot is
# sizing a book it does not actually own — the most dangerous failure mode in this system.
# So in ALPACA/LIVE, before deciding anything, replace our memory with what the broker holds.
def _alpaca_get(path):
    base = "https://api.alpaca.markets" if os.environ.get("APCA_LIVE") == "1" else "https://paper-api.alpaca.markets"
    req = urllib.request.Request(base + path, headers={
        "APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
        "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"]})
    return json.load(urllib.request.urlopen(req, timeout=30))

BROKER_OK = MODE == "DRY"          # DRY needs no broker
BROKER_EQUITY = None               # set only when the broker actually answered
PENDING = set()                    # sleeves with an order already working at the broker
if MODE in ("ALPACA", "LIVE"):
    try:
        acct = _alpaca_get("/v2/account")
        # Alpaca reports crypto positions as "BTCUSD" but accepts orders as "BTC/USD".
        # Normalising both sides is the difference between reading the book and believing it is empty.
        _norm = lambda x: x.replace("/", "").upper()
        held = {_norm(p["symbol"]): p for p in _alpaca_get("/v2/positions")}
        was_pos, was_cash = dict(state["pos"]), state["cash"]
        entry = dict(state.get("entry") or {})
        for s in W:
            p = held.get(_norm(ALPACA_SYM[s]))
            state["pos"][s] = float(p["qty"]) if p else 0.0
            entry[s] = float(p["avg_entry_price"]) if p else None
        state["entry"] = entry
        state["cash"] = float(acct["cash"])
        BROKER_EQUITY = float(acct["equity"])
        state["hwm"] = max(state.get("hwm", BOOK), BROKER_EQUITY)
        # IN-FLIGHT ORDERS. Equity orders sent after the close sit "accepted" until the next open,
        # so the position we just read has NOT moved yet. Without this check a second run would see
        # the un-reduced position, decide it still needs to sell, and send the order twice.
        # Same class of bug as the BTC double-buy on 2026-09-10, wearing a different costume.
        try:
            for _o in _alpaca_get("/v2/orders?status=open&limit=100"):
                for _s in W:
                    if _norm(_o.get("symbol", "")) == _norm(ALPACA_SYM[_s]):
                        PENDING.add(_s)
        except Exception as _e:
            sig.setdefault("notes", []).append(f"open-order check failed ({str(_e)[:80]}) — trading suppressed")
            raise
        sig["reconcile"] = {
            "source": "broker", "broker_equity": round(float(acct["equity"]), 2),
            "qty_drift": {s: round(state["pos"][s] - was_pos.get(s, 0.0), 6) for s in W},
            "cash_drift": round(state["cash"] - was_cash, 2),
            "pending_orders": sorted(PENDING)}
        BROKER_OK = True
    except Exception as e:
        # A broker we cannot read is a broker we must not trade against. Publish signals, send nothing.
        sig["reconcile"] = {"source": "FAILED", "error": str(e)[:200],
                            "note": "could not read positions — orders suppressed for this run"}
        BROKER_OK = False

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
equity = mtm()
# The kill switch must measure the REAL account. mtm() marks positions at yesterday's close, which
# runs a percent or two away from the broker's live mark — enough to invent a drawdown that is not
# there, or hide one that is. When the broker answered, its equity is the number that governs.
mark = BROKER_EQUITY if BROKER_EQUITY is not None else equity
state["hwm"] = max(state["hwm"], mark)
dd = mark / state["hwm"] - 1
sig["book"] = {"equity": round(mark, 2), "marked_at_close": round(equity, 2),
               "hwm": round(state["hwm"], 2), "dd_pct": round(dd * 100, 2), "mode": MODE}
# ---------- 3a. EVALUATION RULES — this paper account is run as if it were funded ----------
# Our own kill switch is -25%. No evaluation firm is anywhere near that generous. Running the paper
# book under the FIRM's limits is the only way to learn, before any fee is at risk, whether this
# strategy can survive them. These lines are TIGHTER than our rails and therefore always bind first.
EV = {"start": EVAL_START, "daily": 0.05, "total": 0.10, "trailing": 0.10,
      "target": 0.10, "min_days": 4}
today = dt.datetime.now(dt.timezone.utc).date().isoformat()
ev = state.setdefault("eval", {"day": today, "day_start": mark, "attempt": 1, "days_traded": 0,
                               "worst_daily": 0.0, "worst_total": 0.0, "worst_trailing": 0.0,
                               "breaches": [], "failed": False, "passed": False})
if ev.get("day") != today:                       # a new UTC day resets the daily loss budget
    ev["day"], ev["day_start"] = today, mark
day_pl   = mark / ev["day_start"] - 1            # today's P&L, the daily-loss rule
total_pl = mark / EV["start"] - 1                # from the starting balance, the static max-loss rule
trail_pl = mark / state["hwm"] - 1               # from the high-water mark, the trailing rule
ev["worst_daily"]    = min(ev["worst_daily"], day_pl)
ev["worst_total"]    = min(ev["worst_total"], total_pl)
ev["worst_trailing"] = min(ev["worst_trailing"], trail_pl)

breach = None
if day_pl   <= -EV["daily"]:    breach = f"DAILY LOSS {day_pl*100:.2f}% (limit -{EV['daily']*100:.0f}%)"
elif total_pl <= -EV["total"]:  breach = f"MAX LOSS {total_pl*100:.2f}% from start (limit -{EV['total']*100:.0f}%)"
elif trail_pl <= -EV["trailing"]: breach = f"TRAILING DD {trail_pl*100:.2f}% from high-water (limit -{EV['trailing']*100:.0f}%)"
if breach and not ev["failed"]:
    ev["failed"] = True
    ev["breaches"].append({"date": today, "attempt": ev["attempt"], "why": breach})
if total_pl >= EV["target"] and not ev["passed"]:
    ev["passed"] = True
    ev["breaches"].append({"date": today, "attempt": ev["attempt"], "why": f"TARGET HIT +{total_pl*100:.2f}%"})

sig["eval"] = {
    "attempt": ev["attempt"], "failed": ev["failed"], "passed": ev["passed"],
    "day_pl_pct": round(day_pl * 100, 2), "total_pl_pct": round(total_pl * 100, 2),
    "trailing_pl_pct": round(trail_pl * 100, 2),
    "daily_budget_used_pct": round(min(100, max(0, -day_pl / EV["daily"] * 100)), 1),
    "total_budget_used_pct": round(min(100, max(0, -total_pl / EV["total"] * 100)), 1),
    "worst_daily_pct": round(ev["worst_daily"] * 100, 2),
    "worst_total_pct": round(ev["worst_total"] * 100, 2),
    "days_traded": ev["days_traded"], "min_days": EV["min_days"],
    "target_pct": EV["target"] * 100, "limits": {"daily": -EV["daily"] * 100, "total": -EV["total"] * 100},
    "exposure": EXPOSURE,
    "breaches": ev["breaches"][-5:]}

# A funded account is closed the moment a line is crossed. Flatten on THIS run, then halt.
EVAL_FLATTEN = bool(ev["failed"] and not state.get("halted"))
if EVAL_FLATTEN:
    sig["book"]["note"] = "EVALUATION FAILED — " + breach + " — flattening and halting"

if state.get("halted") and not EVAL_FLATTEN:      # already halted on a previous run: do nothing at all
    sig["book"].setdefault("note", "HALTED — human review required")
    json.dump(sig, open(SIGNALS, "w"), indent=1); json.dump(state, open(STATE, "w"), indent=1)
    print(json.dumps(sig, indent=1)); sys.exit(0)

# ---------- 4. targets -> orders ----------
orders = []
for s in W:
    if s in PENDING:
        sig.setdefault("notes", []).append(f"{s}: an order is already working at the broker — skipped this run")
        continue
    on = sig[s]["state"] != "FLAT"
    target_dollars = equity * W[s] * state["sizes"][s] * EXPOSURE if on else 0.0
    have = state["pos"][s] * sig[s]["close"]
    flip = (on and have == 0) or (not on and have > 0)
    # DRIFT RAIL (added 2026-09-10, after a symbol-mapping bug left the book far over target in BTC):
    # a sleeve more than 5% of the book away from its target is breakage, not a market view.
    # Correct it on any day — otherwise a bad read compounds until the next calendar slot comes round.
    drift = abs(target_dollars - have) > 0.05 * equity
    if flip or first3 or drift or (monday and abs(target_dollars - have) > 0.02 * equity):
        delta = target_dollars - have
        if abs(delta) > 0.005 * equity: orders.append((s, delta))
if EVAL_FLATTEN:                                  # the evaluation line binds before our own kill switch
    orders = [(s, -state["pos"][s] * sig[s]["close"]) for s in W if state["pos"][s] > 0 and s not in PENDING]
    state["halted"] = True
elif dd <= KILL and not state.get("halted"):
    orders = [(s, -state["pos"][s] * sig[s]["close"]) for s in W if state["pos"][s] > 0 and s not in PENDING]; state["halted"] = True
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

if orders and ev.get("last_trade_day") != today:  # evaluation firms count DAYS that traded, not trades
    ev["days_traded"] = ev.get("days_traded", 0) + 1
    ev["last_trade_day"] = today
    sig["eval"]["days_traded"] = ev["days_traded"]

new = not os.path.exists(LEDGER)
with open(LEDGER, "a", newline="") as f:
    w = csv.writer(f)
    if new: w.writerow(["as_of", "symbol", "side", "dollars", "price", "reason", "mode", "broker_id"])
    for s, delta in orders:
        side = "buy" if delta > 0 else "sell"; price = sig[s]["close"]
        reason = sig[s]["state"] if (sig[s]["state"] == "FLAT" or state["pos"][s] == 0) else ("rebalance" if first3 else "resize")
        _tgt = equity * W[s] * state["sizes"][s] * EXPOSURE if sig[s]["state"] != "FLAT" else 0.0
        if abs(_tgt - state["pos"][s] * price) > 0.05 * equity: reason = "drift-correction"
        if sig["book"].get("note", "").startswith("KILL"): reason = "kill-switch"
        if sig["book"].get("note", "").startswith("EVALUATION FAILED"): reason = "eval-breach-flatten"
        bid = ""
        if MODE in ("ALPACA", "LIVE") and BROKER_OK:
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
