#!/usr/bin/env python3
"""selftest.py — known-answer tests. Synthetic bars whose correct answer is worked out by hand,
plus one real-data anchor: the book engine must reproduce the money desk's reconciled figures
(ANNUALISATION_AUDIT.md: CAGR 19.3 / Sharpe 1.39 monthly / maxDD -15.4 on data/daily.csv through 2026-09-11).
Run before trusting any verdict:  python selftest.py
"""
import sys, pathlib, math
import numpy as np, pandas as pd
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import vault, rules, judge

NY = vault.NY
FAILS = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name} {detail}")
    if not ok: FAILS.append(name)


def synth_session(day, path, base=20000.0, vol=1.0):
    """1-min RTH bars for one day. `path` = list of (minute_from_open, price) anchors; linear in between."""
    t0 = pd.Timestamp(f"{day} 09:30", tz=NY)
    idx = pd.date_range(t0, periods=390, freq="1min")
    mins = np.arange(390)
    anchors_m = [m for m, _ in path]; anchors_p = [p for _, p in path]
    close = np.interp(mins, anchors_m, anchors_p)
    op = np.concatenate([[close[0]], close[:-1]])
    hi = np.maximum(op, close) + 0.25; lo = np.minimum(op, close) - 0.25
    return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close, "volume": 100.0, "symbol": "NQZ5"}, index=idx)


def flat_days(n, start="2024-01-01", base=20000.0, rng=40.0):
    """n quiet days that seed ATR14 ≈ rng (each day ranges base..base+rng and closes at base)."""
    days = pd.bdate_range(start, periods=n)
    return [synth_session(d.date(), [(0, base), (100, base + rng), (200, base), (389, base)]) for d in days]


def test_session_engine():
    print("session engine (vol-band break, target / stop / time exits)")
    pre = flat_days(16)
    base, rng = 20000.0, 40.0          # ATR14 ≈ 40.5 (range 40 + 0.5 from the +/-0.25 wicks)
    last = pre[-1].index[-1].normalize()
    d1 = (last + pd.offsets.BDay(1)).date()
    d2 = (last + pd.offsets.BDay(2)).date()
    d3 = (last + pd.offsets.BDay(3)).date()
    # day 1: break up by 0.45*ATR at minute ~30, run to +0.6 ATR target by minute 60, then flat
    up = [(0, base), (5, base), (30, base + 0.5 * 40.5), (60, base + 1.2 * 40.5), (389, base + 1.2 * 40.5)]
    # day 2: break up then reverse through the stop
    dn = [(0, base), (30, base + 0.5 * 40.5), (60, base + 0.5 * 40.5 - 0.6 * 40.5), (389, base - 40)]
    # day 3: break up, drift sideways just under target until the time exit
    tm = [(0, base), (30, base + 0.5 * 40.5), (389, base + 0.55 * 40.5)]
    df = pd.concat(pre + [synth_session(d1, up), synth_session(d2, dn), synth_session(d3, tm)])
    sessions, daily = vault.sessions_from_1m(df)
    check("sessions built (ATR14 valid from the 15th day: 2 seed days + 3 test days)", len(sessions) == 5, f"n={len(sessions)}")
    atr = [s for s in sessions if str(s["day"].date()) == str(d1)][0]["atr"]
    check("ATR14 ≈ 40.5", abs(atr - 40.5) < 0.6, f"atr={atr:.2f}")
    p = {"k": 0.45, "stop_mult": 0.25, "target_mult": 0.60, "cut_min": 660, "exit_min": 950, "min_hold": 2, "use_vwap": True}
    inst = rules.INSTRUMENTS["MNQ"]; cost = {"commission_rt": 1.22, "slip_ticks": 1.0}; sizing = {"account": 50000, "risk_pct": 1.0, "max_contracts": 20}
    tr = rules.run_session_rule(sessions, "vol_band_break", p, inst, cost, sizing)
    tr = tr[tr["day"] >= str(d1)]
    check("one trade per session, three sessions", len(tr) == 3, f"n={len(tr)}")
    t1, t2, t3 = tr.iloc[0], tr.iloc[1], tr.iloc[2]
    check("day 1 long, exits at target", t1["dir"] == 1 and t1["why"] == "target", f"{t1['why']}")
    check("day 1 gross R = target/stop = 2.4", abs(t1["R_gross"] - 2.4) < 0.02, f"R_gross={t1['R_gross']:.3f}")
    cost_R = (1.22 + 2 * 0.25 * inst["pt"]) / (t1["stop_dist"] * inst["pt"])
    check("day 1 net R = gross − costs", abs((t1["R_gross"] - t1["R"]) - cost_R) < 1e-6, f"cost {t1['R_gross']-t1['R']:.4f} vs {cost_R:.4f}")
    check("day 2 exits at stop, gross R = −1", t2["why"] == "stop" and abs(t2["R_gross"] + 1.0) < 1e-9, f"{t2['why']} {t2['R_gross']:.3f}")
    check("day 3 time exit at/after 15:50", t3["why"] == "time" and t3["min_out"] >= 950, f"{t3['why']} out={t3['min_out']}")
    qty = int(500 / (t1["stop_dist"] * inst["pt"]))
    check("sizing: 1% of $50k / (stop × $2)", t1["qty"] == max(1, min(20, qty)), f"qty={t1['qty']} expected {max(1,min(20,qty))}")
    # opening-range form: entry only after 10:00, stop at the other side of the range
    p2 = {"cut_min": 720, "exit_min": 955, "min_hold": 1, "max_stop_atr": 1.5}
    tr2 = rules.run_session_rule(sessions, "opening_range_break", p2, inst, cost, sizing)
    tr2 = tr2[tr2["day"] >= str(d1)]
    check("range form: entries after 10:00", (tr2["min_in"] >= 600).all() if len(tr2) else False, f"n={len(tr2)}")
    if len(tr2):
        r1 = tr2.iloc[0]
        check("range form: stop = entry − range low (≈ entry − 20000 + 0.25)", abs(r1["stop_dist"] - (r1["entry"] - 0.5 - (base - 0.25))) < 0.6, f"stop_dist={r1['stop_dist']:.2f}")
    # momentum check: enters at 15:30 in the direction of the first 30 minutes
    p3 = {"entry_min": 930, "exit_min": 955, "cut_min": 935, "min_hold": 0, "stop_atr": 0.5}
    tr3 = rules.run_session_rule(sessions, "intraday_momentum", p3, inst, cost, sizing)
    tr3 = tr3[tr3["day"] >= str(d1)]
    check("momentum: entry at 15:30, long on an up open", len(tr3) == 3 and (tr3["min_in"] == 930).all() and tr3.iloc[0]["dir"] == 1, f"n={len(tr3)}")
    # filters: trend disagreement blocks the trade
    state = pd.Series(False, index=[s["day"] for s in sessions])
    p4 = dict(p, filters={"trend_agree": True})
    tr4 = rules.run_session_rule(sessions, "vol_band_break", p4, inst, cost, sizing, daily_state=state)
    check("trend filter blocks longs in a declared downtrend", len(tr4[tr4["day"] >= str(d1)]) == 0, f"n={len(tr4)}")


def test_swing_pullback():
    print("swing pullback (daily closes)")
    n = 220
    idx = pd.bdate_range("2023-01-02", periods=n)
    c = np.linspace(100, 140, n)                    # steady uptrend: always above SMA150 once warm
    c = c.copy()
    # a 3-day dip at 200..202, then a rally past the 5-day high
    c[200], c[201], c[202] = 137.0, 136.5, 136.0
    c[203:] = np.linspace(139.0, 145.0, n - 203)
    px = pd.DataFrame({"QQQ": c}, index=idx)
    tr = rules.swing_pullback(px, {"sma": 150, "low_days": 3, "high_days": 5, "max_days": 7, "stop_atr": 1.5}, {"QQQ": 2.0}, {"account": 50000, "risk_pct": 1.0})
    check("at least one trade, all long", len(tr) >= 1 and (tr["dir"] == 1).all(), f"n={len(tr)}")
    t = tr[tr["day"] == str(idx[203].date())]
    check("the dip is bought at the NEXT close after the 3-day low (day 203)", len(t) == 1, f"entries={list(tr['day'])[:5]}")
    if len(t):
        t = t.iloc[0]
        atr = pd.Series(c).diff().abs().rolling(20).mean().iloc[202]
        check("R = 1.5 × ATR20 of closes at the signal day", abs(t["stop_dist"] - 1.5 * atr) < 1e-9, f"{t['stop_dist']:.4f} vs {1.5*atr:.4f}")
        check("exit reason target (5-day high) or time", t["why"] in ("target", "time"), t["why"])
        gross_R = (t["exit"] - t["entry"]) / t["stop_dist"]
        check("gross R arithmetic", abs(t["R_gross"] - gross_R) < 1e-9)
        check("cost = 2 bp of notional round trip", abs((t["pnl_gross"] - t["pnl"]) - (t["entry"] + t["exit"]) * t["units"] * 2e-4 / 2) < 1e-6)


def test_book_engine_anchor():
    print("book engine anchor: reproduce the money desk's reconciled figures on the real daily.csv")
    try:
        px, prov = vault.load_daily()
    except Exception as e:
        check("daily.csv reachable", False, str(e)); return
    px = px[["QQQ", "BTC", "GLD"]]
    px = px[px.index <= "2026-09-11"]
    eq, turn = rules.book_engine(px, rules.state_sma(px))
    s = judge.curve_stats(eq)
    print(f"    CAGR {s['cagr']*100:.1f}%  Sharpe(monthly) {s['sharpe_monthly']:.2f}  Sharpe(daily√252) {s['sharpe_daily252']:.2f}  maxDD {s['max_dd']*100:.1f}%  turnover/yr {turn.sum()/s['years']:.1f}x")
    check("CAGR ≈ 19.3% (±0.4)", abs(s["cagr"] * 100 - 19.3) < 0.4)
    check("Sharpe monthly ≈ 1.39 (±0.05)", abs(s["sharpe_monthly"] - 1.39) < 0.05)
    check("maxDD ≈ −15.4% (±0.3)", abs(s["max_dd"] * 100 + 15.4) < 0.3)
    # frac generalisation: 0.5 everywhere must hold half the exposure
    half = rules.state_sma(px) * 0.5
    eq2, _ = rules.book_engine(px, half)
    r1, r2 = eq.pct_change().dropna(), eq2.pct_change().dropna()
    j = r1.index.intersection(r2.index)
    ratio = r2[j].std() / r1[j].std()
    check("frac = 0.5 gives about half the daily volatility", 0.45 < ratio < 0.56, f"ratio={ratio:.3f}")


def test_ride_the_run():
    print("ride-the-run schedule")
    idx = pd.bdate_range("2024-01-01", periods=120)
    c = pd.Series(np.linspace(100, 160, 120), index=idx)            # new 20-day high every day
    s = rules.ride_the_run_schedule(c, 0.5, 0.25, 1.0, 20, 20)
    check("starts at base 0.5", abs(s.iloc[0] - 0.5) < 1e-9)
    check("climbs to the cap 1.0 and stays", abs(s.iloc[-1] - 1.0) < 1e-9 and s.max() <= 1.0 + 1e-9)
    c2 = c.copy(); c2.iloc[60:] = 100.0                              # a 20-day low ends the run
    s2 = rules.ride_the_run_schedule(c2, 0.5, 0.25, 1.0, 20, 20)
    check("a 20-day low resets to base", abs(s2.iloc[-1] - 0.5) < 1e-9, f"end={s2.iloc[-1]}")


def test_judge():
    print("judge: pass line and lesson")
    days = pd.bdate_range("2021-01-04", periods=400)
    R = np.array([2.4, -1.0] * 200)      # 50% win rate, +0.7R expectancy, PF 2.4
    tr = pd.DataFrame({"day": [str(d.date()) for d in days], "market": "NQ", "dir": 1, "pnl": R * 100, "pnl_gross": R * 100 + 3, "R": R, "R_gross": R + 0.03, "why": "target", "stop_dist": 50, "qty": 1})
    S = judge.trade_stats(tr, 50000); S["_tr"] = tr
    line = {"min_trades": 200, "min_expectancy_R": 0.05, "min_profit_factor": 1.2, "max_daily_loss_fires_pct": 1.0, "min_headroom_pct": 30, "min_positive_years_frac": 0.5}
    firm = {"account": 50000, "daily_loss": 1000, "max_loss": 2000}
    table, verdict = judge.pass_line_trades(S, S, firm, line)
    check("a clean +0.7R book PASSES", verdict == "PASS", verdict)
    tr2 = tr.iloc[:150].copy()
    S2 = judge.trade_stats(tr2, 50000); S2["_tr"] = tr2
    _, v2 = judge.pass_line_trades(S2, S2, firm, line)
    check("150 trades → INSUFFICIENT, not FAIL", v2 == "INSUFFICIENT", v2)
    tr3 = tr.copy(); tr3["R"] = tr3["R"] - 0.9; tr3["pnl"] = tr3["R"] * 100
    S3 = judge.trade_stats(tr3, 50000); S3["_tr"] = tr3
    _, v3 = judge.pass_line_trades(S3, S3, firm, line)
    L, fix = judge.lesson_trades(S3, S3, {}, line, v3, "X")
    check("costs-eat-it case names cost/venue as the fix", v3 == "FAIL" and fix == "cost_venue", f"{v3} {fix}")


if __name__ == "__main__":
    for f in [test_session_engine, test_swing_pullback, test_ride_the_run, test_judge, test_book_engine_anchor]:
        try:
            f()
        except Exception as e:
            import traceback; traceback.print_exc(); FAILS.append(f.__name__ + " crashed")
    print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)
