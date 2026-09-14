#!/usr/bin/env python3
"""rules.py — every rule the challenger desk knows how to test, in one place.

Two kinds:
  * session rules  — run on 5-minute regular-hours sessions (from vault.sessions_from_1m): the Opening Break
                     family, the Momentum Check, the Volume Break, Gold After the News.
  * daily rules    — run on daily closes: the Swing Pullback, Gold Ride-the-Run, Two-Speed Trend.

Conventions (never silently changed — they are part of every declared test):
  * a signal is read at a bar's close; the fill is at that close ± slippage (session rules) or at the NEXT
    close (daily rules: execution lag >= 1 day, the money desk's own convention);
  * costs are charged on every fill: commission per round turn + slippage ticks per side (session rules),
    or basis points of notional per round turn (daily rules);
  * R = the distance from entry to the stop, in price. Expectancy is mean P&L in R, net of costs.
"""
import math
import numpy as np, pandas as pd

# ----------------------------------------------------------------------------------------- instruments
INSTRUMENTS = {
    # point value $, tick size, name of the contract the economics belong to
    "MNQ": {"pt": 2.0, "tick": 0.25, "note": "Micro Nasdaq, priced off NQ bars"},
    "MES": {"pt": 5.0, "tick": 0.25, "note": "Micro S&P, priced off ES bars"},
    "NQ":  {"pt": 20.0, "tick": 0.25, "note": "E-mini Nasdaq"},
    "ES":  {"pt": 50.0, "tick": 0.25, "note": "E-mini S&P"},
    "MGC": {"pt": 10.0, "tick": 0.10, "note": "Micro gold, priced off XAUUSD bars"},
}


# ----------------------------------------------------------------------------------------- session engine
def run_session_rule(sessions, rule, p, inst, cost, sizing, daily_state=None):
    """Generic one-trade-per-session engine. `rule` picks the entry; the engine manages stop/target/time.

    sessions: list of dicts from vault.sessions_from_1m (O,H,L,C,V,VOL,M arrays + atr, day)
    rule:     'vol_band_break' | 'opening_range_break' | 'intraday_momentum'
    p:        rule parameters (see the declared test JSON)
    inst:     {'pt','tick'}   cost: {'commission_rt','slip_ticks'}   sizing: {'account','risk_pct','max_contracts'}
    daily_state: optional Series(bool) indexed by day for the trend filter (True = uptrend)
    Returns a trades DataFrame (one row per trade) with gross and net P&L, R-multiples and the reason for exit.
    """
    PT, TICK = inst["pt"], inst["tick"]
    slip = cost.get("slip_ticks", 1.0) * TICK
    comm = cost.get("commission_rt", 1.22)
    risk_usd = sizing["account"] * sizing["risk_pct"] / 100.0
    maxc = sizing.get("max_contracts", 20)
    cut_min, exit_min, min_hold = p.get("cut_min", 660), p.get("exit_min", 950), p.get("min_hold", 2)
    filters = p.get("filters", {})
    out = []
    first30_vols = []      # rolling record of first-30-minute volume for the relative-volume filter
    for s in sessions:
        O, H, L, C, V, VOL, M, atr = s["O"], s["H"], s["L"], s["C"], s["V"], s["VOL"], s["M"], s["atr"]
        n = len(C)
        o = O[0]
        # opening range = bars that open before 10:00 (M < 600)
        orb = M < 600
        r_hi, r_lo = (H[orb].max(), L[orb].min()) if orb.any() else (np.nan, np.nan)
        v30 = float(VOL[orb].sum()) if orb.any() else np.nan
        relvol = (v30 / np.median(first30_vols[-20:])) if len(first30_vols) >= 20 and np.isfinite(v30) else np.nan
        first30_vols.append(v30)
        trend = None
        if daily_state is not None and s["day"] in daily_state.index:
            trend = bool(daily_state.loc[s["day"]])
        # ---- declared filters (Patient Break / Volume Break)
        if filters.get("range_frac_of_atr") and np.isfinite(r_hi):
            if (r_hi - r_lo) < filters["range_frac_of_atr"] * atr:
                continue
        if filters.get("relvol_min"):
            if not (np.isfinite(relvol) and relvol >= filters["relvol_min"]):
                continue
        pos = 0; ntr = 0; ebar = -1; entry = stop = targ = 0.0; qty = 0; entry_raw = 0.0; sd = 0.0
        for i in range(1, n):
            if pos != 0:
                px = None; why = None; raw = None
                hs = (L[i] <= stop) if pos > 0 else (H[i] >= stop)
                ht = (targ is not None) and ((H[i] >= targ) if pos > 0 else (L[i] <= targ))
                if hs:
                    raw = stop; why = "stop"
                elif ht and (i - ebar) >= min_hold:
                    raw = targ; why = "target"
                elif M[i] >= exit_min:
                    raw = C[i]; why = "time"
                if raw is not None:
                    px = raw - pos * slip
                    pnl = pos * (px - entry) * PT * qty - qty * comm
                    gross = pos * (raw - entry_raw) * PT * qty
                    out.append(dict(day=str(s["day"].date()), dir=pos, entry=entry, exit=px, qty=qty, pnl=pnl, pnl_gross=gross,
                                    stop_dist=sd, why=why, min_in=int(M[ebar]), min_out=int(M[i]),
                                    R=pnl / (sd * PT * qty), R_gross=gross / (sd * PT * qty), atr=atr))
                    pos = 0
                    if ntr >= 1:
                        break
            if pos == 0 and ntr < 1 and M[i] < cut_min:
                sig = 0
                if rule == "vol_band_break":
                    up, dn = o + p["k"] * atr, o - p["k"] * atr
                    if C[i] > up and (not p.get("use_vwap", True) or C[i] > V[i]): sig = 1
                    elif C[i] < dn and (not p.get("use_vwap", True) or C[i] < V[i]): sig = -1
                    if sig:
                        sd = p["stop_mult"] * atr
                        td = p.get("target_mult")
                elif rule == "opening_range_break":
                    if M[i] < 600 or not np.isfinite(r_hi):
                        continue
                    if C[i] > r_hi: sig = 1
                    elif C[i] < r_lo: sig = -1
                    if sig:
                        sd = (C[i] - r_lo) if sig > 0 else (r_hi - C[i])
                        td = None
                        if sd <= 0 or sd > p.get("max_stop_atr", 1.5) * atr:
                            sig = 0
                elif rule == "intraday_momentum":
                    # Gao/Han/Li/Zhou (2018): the first half-hour's sign predicts the last half-hour.
                    if M[i] != p.get("entry_min", 930):
                        continue
                    k = np.searchsorted(M, 600)  # first bar at/after 10:00
                    if k >= n: continue
                    first = C[k - 1] - o if k > 0 else 0.0
                    sig = 1 if first > 0 else (-1 if first < 0 else 0)
                    sd = p.get("stop_atr", 0.5) * atr
                    td = None
                if sig != 0 and filters.get("trend_agree") and trend is not None:
                    if (sig > 0) != trend:
                        sig = 0
                if sig != 0:
                    entry_raw = C[i]                       # the signal price: stop and target are anchored here
                    entry = C[i] + sig * slip              # the fill: slippage is a cost, never a level
                    stop = entry_raw - sig * sd
                    targ = (entry_raw + sig * td * atr) if td else None
                    qty = int(max(1, min(maxc, risk_usd / (sd * PT))))
                    pos = sig; ntr += 1; ebar = i
    cols = ["day", "dir", "entry", "exit", "qty", "pnl", "pnl_gross", "stop_dist", "why", "min_in", "min_out", "R", "R_gross", "atr"]
    return pd.DataFrame(out, columns=cols)


# ----------------------------------------------------------------------------------------- event engine (gold news)
def run_event_rule(bars5, events, p, inst, cost, sizing):
    """Gold After the News. bars5: continuous 5-min bars (NY tz) with open/high/low/close.
    events: list of pd.Timestamp (NY tz) release times. Enter at the close of the bar that ends
    `window_min` minutes after the release, in the direction of the move since the release; stop at the
    window's extreme on the other side; exit at the stop or at the last bar `hold_days` sessions later."""
    PT, TICK = inst["pt"], inst["tick"]
    slip = cost.get("slip_ticks", 1.0) * TICK
    comm = cost.get("commission_rt", 1.92)
    risk_usd = sizing["account"] * sizing["risk_pct"] / 100.0
    maxc = sizing.get("max_contracts", 20)
    idx = bars5.index
    out = []
    for t in events:
        i0 = idx.searchsorted(t)
        if i0 >= len(idx): continue
        i1 = idx.searchsorted(t + pd.Timedelta(minutes=p.get("window_min", 30)))
        if i1 <= i0 or i1 >= len(idx): continue
        win = bars5.iloc[i0:i1]
        ref = bars5["open"].iloc[i0]
        c1 = bars5["close"].iloc[i1 - 1]
        sig = 1 if c1 > ref else (-1 if c1 < ref else 0)
        if sig == 0: continue
        sd = (c1 - win["low"].min()) if sig > 0 else (win["high"].max() - c1)
        if sd <= 0: continue
        entry_raw = c1; entry = c1 + sig * slip; stop = entry - sig * sd
        qty = int(max(1, min(maxc, risk_usd / (sd * PT))))
        last_day = idx[i1 - 1].normalize() + pd.Timedelta(days=p.get("hold_days", 2))
        j_end = idx.searchsorted(last_day + pd.Timedelta(hours=16, minutes=55))
        j_end = min(j_end, len(idx) - 1)
        raw = None; why = None; j_exit = j_end
        for j in range(i1, j_end + 1):
            H, L = bars5["high"].iloc[j], bars5["low"].iloc[j]
            if (sig > 0 and L <= stop) or (sig < 0 and H >= stop):
                raw = stop; why = "stop"; j_exit = j; break
        if raw is None:
            raw = bars5["close"].iloc[j_end]; why = "time"
        px = raw - sig * slip
        pnl = sig * (px - entry) * PT * qty - qty * comm
        gross = sig * (raw - entry_raw) * PT * qty
        out.append(dict(day=str(t.date()), dir=sig, entry=entry, exit=px, qty=qty, pnl=pnl, pnl_gross=gross, stop_dist=sd, why=why,
                        min_in=int(idx[i1 - 1].hour * 60 + idx[i1 - 1].minute), min_out=int(idx[j_exit].hour * 60 + idx[j_exit].minute),
                        R=pnl / (sd * PT * qty), R_gross=gross / (sd * PT * qty), atr=np.nan))
    cols = ["day", "dir", "entry", "exit", "qty", "pnl", "pnl_gross", "stop_dist", "why", "min_in", "min_out", "R", "R_gross", "atr"]
    return pd.DataFrame(out, columns=cols)


# ----------------------------------------------------------------------------------------- daily: swing pullback
def swing_pullback(px, p, costs_bps, sizing):
    """Above the 150-day line, buy a 3-day closing low; exit at a 5-day closing high, on day `max_days`,
    or at the stop (entry - stop_atr * ATR20 of closes). Signal on close t, fill at close t+1.
    px: DataFrame of closes (columns = markets). costs_bps: {market: round-trip cost in bp of notional}.
    Returns trades DataFrame with R (net of costs), R_gross, pnl in $ at the declared risk sizing."""
    risk_usd = sizing["account"] * sizing["risk_pct"] / 100.0
    lookback_low, lookback_high = p.get("low_days", 3), p.get("high_days", 5)
    max_days, stop_atr, sma_n = p.get("max_days", 7), p.get("stop_atr", 1.5), p.get("sma", 150)
    out = []
    for m in px.columns:
        c = px[m].dropna()
        sma = c.rolling(sma_n).mean()
        atr = c.diff().abs().rolling(20).mean()
        lo3 = c.rolling(lookback_low).min()
        hi5 = c.shift(1).rolling(lookback_high).max()
        v = c.values; dates = c.index
        i = sma_n + 25
        cost = costs_bps.get(m, 0.0) / 1e4
        while i < len(v) - 1:
            if v[i] > sma.iloc[i] and v[i] <= lo3.iloc[i] and np.isfinite(atr.iloc[i]):
                # enter at next close
                j = i + 1
                entry = v[j]; R = stop_atr * atr.iloc[i]
                stop = entry - R
                notional = risk_usd / (R / entry)          # $ position so that one R = risk_usd
                units = notional / entry
                k = j
                exit_px = None; why = None
                while k < len(v) - 1:
                    held = k - j
                    if v[k] <= stop: why = "stop"
                    elif v[k] >= hi5.iloc[k]: why = "target"
                    elif held >= max_days - 1: why = "time"
                    if why:
                        exit_px = v[k + 1] if k + 1 < len(v) else v[k]   # act at the next close
                        k = k + 1
                        break
                    k += 1
                if exit_px is None:
                    break
                gross = (exit_px - entry) * units
                fee = (entry + exit_px) * units * cost / 2.0
                pnl = gross - fee
                out.append(dict(market=m, day=str(dates[j].date()), exit_day=str(dates[k].date()), dir=1, entry=entry, exit=exit_px,
                                units=units, notional=notional, pnl=pnl, pnl_gross=gross, stop_dist=R, why=why, days=k - j,
                                R=pnl / (R * units), R_gross=gross / (R * units)))
                i = k + 1
            else:
                i += 1
    cols = ["market", "day", "exit_day", "dir", "entry", "exit", "units", "notional", "pnl", "pnl_gross", "stop_dist", "why", "days", "R", "R_gross"]
    return pd.DataFrame(out, columns=cols)


# ----------------------------------------------------------------------------------------- daily: book engine
W = {"QQQ": .40, "BTC": .20, "GLD": .40}
VT = {"QQQ": .15, "BTC": .40, "GLD": .15}
SMA_N, VOLN = 150, 20
COST_B = {"QQQ": 0.0005, "BTC": 0.0026, "GLD": 0.0005}
DISASTER = -0.15


def book_engine(px, frac, weights=W, vt=VT, cost=COST_B, disaster=True, min_trade=0.005, drift_rail=0.05, start_equity=100_000.0):
    """The money desk's cash-and-shares engine (recon.py variant E, the adopted one), with one generalisation:
    `frac` is a DataFrame in [0,1] per sleeve = the fraction of the vol-targeted size to hold (1/0 reproduces
    the live SMA150 book). Sizes are set weekly (Monday); rebalance window = first three weekdays of the month;
    a sleeve trades when it flips, drifts > 5% of equity, or on Monday when > 2% off target. Disaster stop -15%.
    Returns (equity Series, turnover Series [traded notional / equity per day])."""
    px = px[list(weights)].ffill().dropna()
    RET = np.log(px / px.shift(1))
    idx = px.index
    vol = {k: RET[k].rolling(VOLN).std() * np.sqrt(252) for k in weights}
    dow = idx.dayofweek
    is_wd = dow < 5
    wd_rank = pd.Series(is_wd.astype(int), index=idx).groupby([idx.year, idx.month]).cumsum()
    rebal_win = pd.Series(np.asarray(is_wd) & (wd_rank.values <= 3), index=idx)
    frac = frac.reindex(idx).fillna(0.0)
    warm = max(SMA_N, VOLN) - 1      # first day the 150-day line exists (recon.py starts here too)
    n = len(idx)
    cash = start_equity
    pos = {k: 0.0 for k in weights}
    entry = {k: None for k in weights}
    sizes = {}
    eq_out, turn_out = [], []
    prev_month = None
    for i in range(n):
        d = idx[i]
        if i < warm or any(pd.isna(vol[k].iloc[i]) for k in weights):
            eq_out.append(np.nan); turn_out.append(0.0); continue
        p = {k: px[k].iloc[i] for k in weights}
        equity = cash + sum(pos[k] * p[k] for k in weights)
        if d.dayofweek == 0 or not sizes:
            for k in weights:
                sizes[k] = min(1.0, vt[k] / max(vol[k].iloc[i], 1e-6))
        first3 = bool(rebal_win.iloc[i]) and (prev_month != (d.year, d.month))
        if first3:
            prev_month = (d.year, d.month)
        traded = 0.0
        for k in weights:
            f = float(frac[k].iloc[i])
            on = f > 0
            if disaster and entry[k] and pos[k] > 0 and p[k] / entry[k] - 1 < DISASTER:
                on = False; f = 0.0
            tgt = equity * weights[k] * sizes[k] * f
            have = pos[k] * p[k]
            flip = (on and have == 0) or (not on and have > 0)
            drift = abs(tgt - have) > drift_rail * equity
            if flip or first3 or drift or (d.dayofweek == 0 and abs(tgt - have) > 0.02 * equity):
                delta = tgt - have
                if abs(delta) > min_trade * equity:
                    fee = abs(delta) * cost[k]
                    was = pos[k]
                    pos[k] = max(0.0, was + delta / p[k])
                    cash -= delta + fee
                    traded += abs(delta)
                    if was == 0 and delta > 0: entry[k] = p[k]
                    if pos[k] == 0: entry[k] = None
        eq_out.append(cash + sum(pos[k] * p[k] for k in weights))
        turn_out.append(traded / max(equity, 1e-9))
    return pd.Series(eq_out, index=idx).dropna(), pd.Series(turn_out, index=idx)


def state_sma(px, n=SMA_N, gold_always=True):
    """The live book's state: above the n-day line = 1, below = 0; gold always held."""
    f = pd.DataFrame(index=px.index)
    for k in px.columns:
        f[k] = (px[k] > px[k].rolling(n).mean()).astype(float)
    if gold_always and "GLD" in f.columns:
        f["GLD"] = 1.0
    return f


def state_two_speed(px, fast=20, slow=250, gold_always=True):
    """Two-Speed Trend: half a position when one horizon agrees, full when both, none when neither."""
    f = pd.DataFrame(index=px.index)
    for k in px.columns:
        a = (px[k] > px[k].rolling(fast).mean()).astype(float)
        b = (px[k] > px[k].rolling(slow).mean()).astype(float)
        f[k] = (a + b) / 2.0
    if gold_always and "GLD" in f.columns:
        f["GLD"] = 1.0
    return f


def sleeve_engine(c, sched, vt=0.15, cost=0.0005, start_equity=100_000.0, min_trade=0.005, drift_rail=0.05):
    """One sleeve at weight 1.0 through the same cash engine (used by Gold Ride-the-Run).
    sched: Series in [0, cap] = fraction of the vol-target size to hold each day."""
    px = pd.DataFrame({"X": c}).dropna()
    frac = pd.DataFrame({"X": sched.reindex(px.index).fillna(0.0)})
    eq, turn = book_engine(px, frac, weights={"X": 1.0}, vt={"X": vt}, cost={"X": cost}, disaster=False,
                           min_trade=min_trade, drift_rail=drift_rail, start_equity=start_equity)
    return eq, turn


def ride_the_run_schedule(c, base=0.5, step=0.25, cap=1.0, high_days=20, low_days=20):
    """Start at `base` of the vol-target size; add `step` at each new `high_days`-day closing high while the run
    lasts; a `low_days`-day closing low ends the run and resets to `base`. Never above `cap` (the vol target)."""
    hi = c.shift(1).rolling(high_days).max()
    lo = c.shift(1).rolling(low_days).min()
    out = pd.Series(base, index=c.index)
    level = base
    for i in range(len(c)):
        if np.isfinite(hi.iloc[i]) and c.iloc[i] > hi.iloc[i]:
            level = min(cap, level + step)
        if np.isfinite(lo.iloc[i]) and c.iloc[i] < lo.iloc[i]:
            level = base
        out.iloc[i] = level
    # act on the next day (signal at close t, size from t+1)
    return out.shift(1).fillna(base)


# ----------------------------------------------------------------------------------------- post-filters (declared fixes)
def trailing_edge_filter(tr, years=3, min_trades=30):
    """Market fix without look-ahead: keep a trade only if the same market's trades that EXITED before its entry
    day, within the trailing `years`, show positive net expectancy (or fewer than `min_trades` — no evidence yet).
    Trades never interact across markets, so dropping some changes nothing else."""
    if len(tr) == 0:
        return tr
    tr = tr.copy()
    tr["_d"] = pd.to_datetime(tr["day"]); tr["_x"] = pd.to_datetime(tr["exit_day"])
    keep = []
    for i, row in tr.iterrows():
        past = tr[(tr["market"] == row["market"]) & (tr["_x"] < row["_d"]) & (tr["_x"] >= row["_d"] - pd.Timedelta(days=int(365.25 * years)))]
        keep.append(len(past) < min_trades or past["R"].mean() > 0)
    out = tr[np.array(keep)].drop(columns=["_d", "_x"])
    return out
