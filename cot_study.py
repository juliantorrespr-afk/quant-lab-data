#!/usr/bin/env python3
"""
COT STUDY — does big-player positioning (CFTC Commitments of Traders) predict our sleeves?

Declared test, written before the data was seen (2026-09-11):
  Feature : COT index = trailing-156-week percentile of net positioning / open interest,
            for speculators (non-commercial) and hedgers (commercial), gold / Nasdaq mini / bitcoin.
  Timing  : report is for Tuesday, public Friday 15:30 ET -> usable from the NEXT Monday's close.
            Anything faster is look-ahead and is not allowed here.
  T1      : forward 4 / 8 / 13-week sleeve return by COT-index quintile (mean, t-stat), full & OOS >= 2021.
  T2      : SMA150 sleeve with a positioning filter (stay flat when speculators are at a >= 80th pct
            extreme and hedgers <= 20th) vs without. Sharpe / maxDD, full & OOS.
  ADOPT only if T2 adds >= +0.10 Sharpe OOS AND >= +0.05 full-sample AND >= 20 filtered weeks OOS.
  Otherwise COT stays on the dashboard as context and the rule never reads it.
Runs on GitHub Actions (cot.yml). Writes data/cot_study.json and docs/cot.json. Never touches bot.py.
"""
import csv, io, json, os, zipfile, datetime as dt, urllib.request
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data"); DOCS = os.path.join(ROOT, "docs")
YEARS = range(2010, dt.date.today().year + 1)
MK = {"GOLD - COMMODITY EXCHANGE": ("GOLD", "GLD"), "NASDAQ MINI - CHICAGO MERCANTILE": ("NQ", "QQQ"),
      "BITCOIN - CHICAGO MERCANTILE": ("BTC", "BTC")}
LOOK, OOS = 156, pd.Timestamp("2021-01-01")

def fetch_year(y):
    url = "https://www.cftc.gov/files/dea/history/deacot%d.zip" % y
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 quantlab"})
    with urllib.request.urlopen(req, timeout=60) as r: raw = r.read()
    z = zipfile.ZipFile(io.BytesIO(raw)); name = [n for n in z.namelist() if n.lower().endswith(".txt")][0]
    return z.read(name).decode("latin-1")

def market(name):
    n = name.strip().upper()
    for k, v in MK.items():
        if n.startswith(k): return v
    return None

def load_cot():
    rows = []
    for y in YEARS:
        try: txt = fetch_year(y)
        except Exception as e: print("skip", y, e); continue
        for rec in csv.reader(txt.splitlines()):
            if not rec: continue
            m = market(rec[0]) if rec else None
            if not m: continue
            try:
                rows.append(dict(date=pd.Timestamp(rec[2].strip()), market=m[0], sleeve=m[1], oi=int(rec[7]),
                                 spec_net=int(rec[8]) - int(rec[9]), hedge_net=int(rec[11]) - int(rec[12])))
            except (ValueError, IndexError): continue
    df = pd.DataFrame(rows).drop_duplicates(["date", "market"]).sort_values(["market", "date"])
    df["spec_pos"] = df.spec_net / df.oi; df["hedge_pos"] = df.hedge_net / df.oi
    for c in ("spec_pos", "hedge_pos"):
        df[c + "_idx"] = df.groupby("market")[c].transform(
            lambda s: s.rolling(LOOK, min_periods=52).apply(lambda w: (w[:-1] < w[-1]).mean() * 100, raw=True))
    df["usable"] = df.date + pd.Timedelta(days=6)     # next Monday after the Friday release
    return df

def load_px():
    d = pd.read_csv(os.path.join(DATA, "daily.csv"), parse_dates=["date"])
    return d.pivot(index="date", columns="symbol", values="close").sort_index().ffill()

def t1(df, px):
    out = {}
    for m, g in df.groupby("market"):
        s = g.iloc[0].sleeve; p = px[s].dropna()
        g = g.dropna(subset=["spec_pos_idx"]).copy()
        res = {}
        for h in (4, 8, 13):
            fwd = []
            for _, r in g.iterrows():
                i0 = p.index.searchsorted(r.usable); i1 = i0 + 5 * h
                if i1 >= len(p): fwd.append(np.nan); continue
                fwd.append(p.iloc[i1] / p.iloc[i0] - 1)
            g["fwd%d" % h] = fwd
            for label, sub in (("full", g), ("oos", g[g.date >= OOS])):
                q = pd.qcut(sub.spec_pos_idx.rank(method="first"), 5, labels=False)
                tab = sub.groupby(q)["fwd%d" % h].agg(["mean", "count", "std"])
                tab["t"] = tab["mean"] / (tab["std"] / np.sqrt(tab["count"]))
                res["%s_h%d" % (label, h)] = {"q%d" % (k + 1): dict(mean_pct=round(v["mean"] * 100, 2), n=int(v["count"]),
                                                             t=round(float(v["t"]), 2)) for k, v in tab.iterrows()}
                res["%s_h%d_spread_q5_minus_q1_pct" % (label, h)] = round((tab["mean"].iloc[-1] - tab["mean"].iloc[0]) * 100, 2)
        out[m] = res
    return out

def sleeve_path(p, filt=None):
    sma = p.rolling(150).mean(); sig = (p > sma).shift(1).fillna(False)
    if filt is not None: sig = sig & ~filt.reindex(p.index).ffill().fillna(False).astype(bool)
    r = p.pct_change().fillna(0) * sig
    eq = (1 + r).cumprod(); return eq, r

def stats(eq, r, per_year):
    rr = r[r != 0] if False else r
    sh = rr.mean() / rr.std() * np.sqrt(per_year) if rr.std() else 0
    dd = (eq / eq.cummax() - 1).min(); yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    return dict(sharpe=round(float(sh), 3), maxdd_pct=round(float(dd) * 100, 2),
                cagr_pct=round(((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1) * 100, 2))

def t2(df, px):
    out = {}
    for m, g in df.groupby("market"):
        s = g.iloc[0].sleeve; p = px[s].dropna(); per = 365 if s == "BTC" else 252
        ext = ((g.spec_pos_idx >= 80) & (g.hedge_pos_idx <= 20))
        filt = pd.Series(ext.values, index=g.usable.values)
        base_eq, base_r = sleeve_path(p); f_eq, f_r = sleeve_path(p, filt)
        res = {}
        for label, m0 in (("full", p.index[0]), ("oos", OOS)):
            b = stats(base_eq[base_eq.index >= m0] / base_eq[base_eq.index >= m0].iloc[0], base_r[base_r.index >= m0], per)
            f = stats(f_eq[f_eq.index >= m0] / f_eq[f_eq.index >= m0].iloc[0], f_r[f_r.index >= m0], per)
            res[label] = {"sma150": b, "sma150_plus_cot_filter": f, "delta_sharpe": round(f["sharpe"] - b["sharpe"], 3),
                          "weeks_filtered": int(ext[(g.date >= m0)].sum())}
        # random positioning can clear +0.10 OOS by luck on a short window (seen in the dry run):
        # require the full sample to agree by >= +0.05 and at least 20 filtered weeks OOS.
        res["adopt"] = bool(res["oos"]["delta_sharpe"] >= 0.10 and res["full"]["delta_sharpe"] >= 0.05
                            and res["oos"]["weeks_filtered"] >= 20)
        out[m] = res
    return out

def main():
    df = load_cot(); px = load_px()
    latest = df.sort_values("date").groupby("market").tail(1)
    res = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "rows": int(len(df)), "first": str(df.date.min().date()), "last": str(df.date.max().date()),
           "latest": [dict(market=r.market, date=str(r.date.date()), spec_idx=None if pd.isna(r.spec_pos_idx) else round(r.spec_pos_idx, 1),
                           hedge_idx=None if pd.isna(r.hedge_pos_idx) else round(r.hedge_pos_idx, 1),
                           spec_net=int(r.spec_net), hedge_net=int(r.hedge_net), oi=int(r.oi)) for _, r in latest.iterrows()],
           "T1_quintiles": t1(df, px), "T2_filter": t2(df, px),
           "rule": "adopt only if T2 delta_sharpe OOS >= +0.10, full >= +0.05, >= 20 filtered weeks OOS; otherwise context only"}
    res["verdict"] = {m: ("ADOPT" if v["adopt"] else "CONTEXT ONLY") for m, v in res["T2_filter"].items()}
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DATA, "cot_study.json"), "w") as f: json.dump(res, f, indent=1)
    hist = df[["date", "market", "spec_pos_idx", "hedge_pos_idx", "spec_net", "hedge_net", "oi"]].tail(3 * 160)
    with open(os.path.join(DOCS, "cot.json"), "w") as f:
        json.dump({"generated": res["generated"], "latest": res["latest"], "verdict": res["verdict"],
                   "series": [[str(r.date.date()), r.market, None if pd.isna(r.spec_pos_idx) else round(r.spec_pos_idx, 1),
                               None if pd.isna(r.hedge_pos_idx) else round(r.hedge_pos_idx, 1)] for _, r in hist.iterrows()]}, f)
    print(json.dumps({k: res[k] for k in ("rows", "first", "last", "latest", "verdict")}, indent=1))
    for m, v in res["T2_filter"].items(): print(m, json.dumps(v))

if __name__ == "__main__":
    main()
