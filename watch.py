#!/usr/bin/env python3
"""QUANT_LAB watch — leave this open on the Mac and see the desk breathing.
   python3 watch.py            (30s refresh)
   python3 watch.py --once     (single print)
DISPLAY ONLY. It never trades, never edits data/, never touches the strategy."""
import csv, json, os, sys, time, datetime as dt

R = os.path.dirname(os.path.abspath(__file__)); D = os.path.join(R, "data")
C = dict(g="\033[92m", r="\033[91m", y="\033[93m", b="\033[94m", d="\033[2m", x="\033[0m", B="\033[1m")
SY = {"QQQ": ("NASDAQ", "QQQ", "america"), "BTC": ("KRAKEN", "XBTUSD", "crypto"),
      "GLD": ("AMEX", "GLD", "america"), "NQ1!": ("CME_MINI", "NQ1!", "america")}

def quote(k):
    from tradingview_ta import TA_Handler, Interval
    ex, tk, sc = SY[k]
    return TA_Handler(symbol=tk, exchange=ex, screener=sc,
                      interval=Interval.INTERVAL_1_DAY).get_analysis().indicators

def jload(n):
    try:
        with open(os.path.join(D, n)) as f: return json.load(f)
    except Exception: return {}

def bar(dist, w=21):
    """price vs the 150-day line, -10%..+10% clipped, | is the line"""
    p = max(-10, min(10, dist)); i = int(round((p + 10) / 20 * (w - 1)))
    s = ["-"] * w; s[w // 2] = "|"; s[i] = "#"
    col = C["g"] if dist > 0 else C["r"]
    return col + "".join(s) + C["x"]

def frame():
    sig, stt = jload("signals.json"), jload("state.json")
    live = {}
    for k in ("QQQ", "BTC", "GLD", "NQ1!"):
        try: live[k] = quote(k)["close"]
        except Exception: live[k] = None
    eq = None
    if stt.get("pos") and all(live.get(k) for k in ("QQQ", "BTC", "GLD")):
        eq = stt.get("cash", 0) + sum(stt["pos"][k] * live[k] for k in ("QQQ", "BTC", "GLD"))
    now = dt.datetime.now().strftime("%H:%M:%S")
    age = ""
    if sig.get("as_of"):
        d = (dt.date.today() - dt.date.fromisoformat(sig["as_of"])).days
        age = (C["g"] + f"signals {d}d old" if d <= 4 else C["r"] + f"SIGNALS STALE {d}d") + C["x"]
    print("\033[2J\033[H", end="")
    print(f"{C['B']}QUANT_LAB desk{C['x']}  {now}   {age}   "
          f"{C['d']}book P7-VT3 · 40/20/40 · SMA150 · vol-target 15/40/15{C['x']}")
    print("-" * 78)
    print(f"{'sleeve':<7}{'state':<7}{'last':>11}{'150-line':>11}{'dist':>8}  {'position vs line':<23}{'size':>6}")
    for k in ("QQQ", "BTC", "GLD"):
        s = sig.get(k, {})
        if not s: continue
        lp = live.get(k) or s.get("close", 0)
        dist = 100 * (lp / s["sma150"] - 1) if s.get("sma150") else 0
        col = C["g"] if s["state"] in ("LONG", "HELD") else C["y"]
        print(f"{k:<7}{col}{s['state']:<7}{C['x']}{lp:>11,.2f}{s['sma150']:>11,.2f}"
              f"{dist:>+7.2f}%  {bar(dist):<23}{s.get('size_in_use',0):>6.2f}")
    print("-" * 78)
    if eq:
        pnl = eq - 100000
        col = C["g"] if pnl >= 0 else C["r"]
        dd = 100 * (eq / stt.get("hwm", 100000) - 1)
        print(f"equity {C['B']}${eq:>11,.2f}{C['x']}   P&L {col}{pnl:>+10,.2f}{C['x']}"
              f"   from HWM {dd:>+6.2f}%   cash ${stt.get('cash',0):>10,.2f}")
    if live.get("NQ1!"): print(f"{C['d']}NQ1! {live['NQ1!']:,.2f}{C['x']}")
    print(f"{C['d']}rule: no action unless a sleeve crosses its line, "
          f"the 1st-3rd weekday rebalance, or a -15% stop. Ctrl-C to exit.{C['x']}")

if __name__ == "__main__":
    once = "--once" in sys.argv
    while True:
        try: frame()
        except Exception as e: print("watch error:", e)
        if once: break
        time.sleep(30)
