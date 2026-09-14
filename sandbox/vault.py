#!/usr/bin/env python3
"""vault.py — the readers. Every dataset the challenger desk can test on, loaded the same way.

The vault is /srv/vault on the Hetzner box (raw price history, private by license).
The public repo carries only daily closes (data/daily.csv) and derived results.
Nothing here fetches from the internet except the repo's own daily.csv (public, GitHub raw).

Every loader returns a pandas object plus a `provenance` dict (files, first/last stamp, rows, sha256)
so a verdict can say exactly which bytes it was computed from.
"""
import os, io, glob, json, hashlib, zipfile, datetime as dt, pathlib
import numpy as np, pandas as pd

VAULT = pathlib.Path(os.environ.get("QL_VAULT", "/srv/vault"))
RAW = VAULT / "raw"
REPO_RAW = "https://raw.githubusercontent.com/juliantorrespr-afk/quant-lab-data/main/"
NY = "America/New_York"


def sha256_file(p, limit_mb=None):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _prov(files, index, extra=None):
    files = [str(f) for f in files]
    d = {"files": files, "rows": int(len(index)),
         "first": str(index.min())[:19] if len(index) else None,
         "last": str(index.max())[:19] if len(index) else None,
         "sha256": [sha256_file(f) for f in files if os.path.exists(f)]}
    if extra: d.update(extra)
    return d


# ---------------------------------------------------------------- daily closes (repo)
def load_daily(local=None, fetch=True):
    """The repo's data/daily.csv: date,symbol,close for QQQ, BTC, GLD (PAXG since Aug-2026).
    Looks for a local copy first (./daily.csv or $QL_DAILY), else fetches from GitHub raw."""
    path = local or os.environ.get("QL_DAILY") or ("daily.csv" if os.path.exists("daily.csv") else None)
    if path is None and (VAULT / "derived" / "daily.csv").exists():
        path = str(VAULT / "derived" / "daily.csv")
    if path is None:
        if not fetch:
            raise FileNotFoundError("daily.csv not found and fetch disabled")
        import urllib.request
        data = urllib.request.urlopen(REPO_RAW + "data/daily.csv", timeout=30).read()
        (VAULT / "derived").mkdir(parents=True, exist_ok=True) if VAULT.exists() else None
        path = str(VAULT / "derived" / "daily.csv") if VAULT.exists() else "daily.csv"
        open(path, "wb").write(data)
    df = pd.read_csv(path, parse_dates=["date"])
    px = df.pivot(index="date", columns="symbol", values="close").sort_index()
    keep = [c for c in ["QQQ", "BTC", "GLD"] if c in px.columns]
    px = px[keep].ffill().dropna()
    return px, _prov([path], px.index, {"source": "repo data/daily.csv (fetch.py: Yahoo QQQ/GLD, Kraken BTC)", "symbols": keep})


# ---------------------------------------------------------------- Databento futures 1-min
def _databento_files(symbol):
    """symbol like 'NQ' or 'ES' -> parquet files written by vault_pull.py (NQ_v_0_ohlcv-1m_*.parquet)."""
    pat = str(RAW / "databento" / f"{symbol}_v_0_ohlcv-1m_*.parquet")
    return sorted(glob.glob(pat))


def load_futures_1m(symbol):
    """Continuous front-month (volume roll), UNADJUSTED, UTC ts_event = bar open. Returns 1-min bars in NY time
    with columns open, high, low, close, volume, symbol. Raises FileNotFoundError when the vault lacks it."""
    files = _databento_files(symbol)
    if not files:
        raise FileNotFoundError(f"vault has no Databento {symbol} 1-min bars ({RAW/'databento'})")
    parts = []
    for f in files:
        d = pd.read_parquet(f)
        if "ts_event" in d.columns:
            d = d.set_index("ts_event")
        parts.append(d[["open", "high", "low", "close", "volume"] + (["symbol"] if "symbol" in d.columns else [])])
    df = pd.concat(parts).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(NY)
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    return df, _prov(files, df.index, {"source": "Databento GLBX.MDP3 ohlcv-1m continuous .v.0 (volume roll), unadjusted"})


def sessions_from_1m(df1m, start="09:30", end="16:00", bar="5min", min_bars=20):
    """Regular-hours sessions as the challenge engine expects them:
    5-min bars 09:30-16:00 NY, session VWAP, ATR(14) of prior sessions (shifted), volume.
    Sessions whose continuous symbol changes mid-day (a roll) are dropped."""
    d = df1m.between_time(start, end, inclusive="left").copy()
    if "symbol" in d.columns:
        day = d.index.normalize()
        nsym = d.groupby(day)["symbol"].nunique()
        bad = nsym[nsym > 1].index
        if len(bad):
            d = d[~day.isin(bad)]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    b = d.resample(bar, label="left", closed="left").agg(agg).dropna(subset=["close"])
    b = b.between_time(start, end, inclusive="left")
    b["day"] = b.index.normalize()
    tp = (b["high"] + b["low"] + b["close"]) / 3
    cum_pv = (tp * b["volume"]).groupby(b["day"]).cumsum()
    cum_v = b["volume"].groupby(b["day"]).cumsum().replace(0, np.nan)
    b["vwap"] = (cum_pv / cum_v).fillna(tp.groupby(b["day"]).cumsum() / (b.groupby("day").cumcount() + 1))
    daily = b.groupby("day").agg(h=("high", "max"), l=("low", "min"), c=("close", "last"), v=("volume", "sum"))
    pc = daily["c"].shift(1)
    tr = pd.concat([daily["h"] - daily["l"], (daily["h"] - pc).abs(), (daily["l"] - pc).abs()], axis=1).max(axis=1)
    daily["atr14"] = tr.rolling(14).mean().shift(1)
    daily["medvol20"] = daily["v"].rolling(20).median().shift(1)
    mins = (b.index.hour * 60 + b.index.minute).values
    out = []
    for day, g in b.groupby("day"):
        a = daily.at[day, "atr14"] if day in daily.index else np.nan
        if not np.isfinite(a) or a <= 0 or len(g) < min_bars:
            continue
        idx = b.index.get_indexer(g.index)
        out.append({"day": day, "atr": float(a), "medvol20": float(daily.at[day, "medvol20"]) if np.isfinite(daily.at[day, "medvol20"]) else np.nan,
                    "O": g["open"].values, "H": g["high"].values, "L": g["low"].values, "C": g["close"].values,
                    "V": g["vwap"].values, "VOL": g["volume"].values.astype(float), "M": mins[idx]})
    return out, daily


def daily_from_1m(df1m):
    """Daily RTH closes from 1-min futures bars, back-adjusted at symbol changes (adjacent-bar difference)
    so multi-day rules can be run on a continuous series. Returns Series of closes (NY dates)."""
    d = df1m.between_time("09:30", "16:00", inclusive="left")
    daily = d.groupby(d.index.normalize())["close"].last()
    if "symbol" in df1m.columns:
        sym = d.groupby(d.index.normalize())["symbol"].last()
        adj = 0.0
        out = daily.copy()
        prev = None
        # walk backwards: when the symbol changes between day i and i+1, shift everything before by the gap
        vals = daily.values.copy()
        syms = sym.values
        for i in range(len(vals) - 1, 0, -1):
            if syms[i] != syms[i - 1]:
                # gap = first close of new contract minus last close of old (approximation of basis)
                gap = vals[i] - vals[i - 1]
                vals[:i] += gap
        out[:] = vals
        return out
    return daily


# ---------------------------------------------------------------- Dukascopy (gold m5)
def load_dukascopy(instrument="xauusd", tf="m5"):
    files = sorted(glob.glob(str(RAW / "dukascopy" / f"{instrument}-{tf}-*.csv")))
    if not files:
        raise FileNotFoundError(f"vault has no Dukascopy {instrument} {tf} bars")
    parts = []
    for f in files:
        d = pd.read_csv(f)
        ts = d["timestamp"]
        unit = "ms" if ts.iloc[0] > 1e11 else "s"
        d.index = pd.to_datetime(ts, unit=unit, utc=True)
        parts.append(d[["open", "high", "low", "close", "volume"]])
    df = pd.concat(parts).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.index = df.index.tz_convert(NY)
    return df, _prov(files, df.index, {"source": f"Dukascopy {instrument} {tf} bid (dukascopy-node), private license"})


# ---------------------------------------------------------------- Binance BTC 1-min
def load_binance_1m():
    files = sorted(glob.glob(str(RAW / "binance" / "BTCUSDT-1m-*.zip")))
    if not files:
        raise FileNotFoundError("vault has no Binance BTCUSDT 1-min zips")
    parts = []
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "qv", "n", "tbb", "tbq", "ig"]
    for f in files:
        with zipfile.ZipFile(f) as z:
            name = z.namelist()[0]
            d = pd.read_csv(z.open(name), header=None, names=cols)
        first = str(d["open_time"].iloc[0])
        if not first.replace(".", "").isdigit():
            d = d.iloc[1:]
        t = d["open_time"].astype("int64")
        unit = "us" if t.iloc[0] > 1e14 else "ms"
        d.index = pd.to_datetime(t, unit=unit, utc=True)
        parts.append(d[["open", "high", "low", "close", "volume"]].astype(float))
    df = pd.concat(parts).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df, _prov(files, df.index, {"source": "Binance public data BTCUSDT 1m monthly zips"})


# ---------------------------------------------------------------- Yahoo daily OHLCV (vault)
def load_yahoo_daily(ticker):
    files = sorted(glob.glob(str(RAW / "yahoo" / f"{ticker.replace('=', '_').replace('-', '_')}_daily_*.csv")))
    if not files:
        raise FileNotFoundError(f"vault has no Yahoo daily file for {ticker}")
    f = files[-1]
    raw = open(f).read().splitlines()
    # yfinance >= 0.2.40 writes a 3-line header (Price/Ticker/Date). Find the first line that starts with a date.
    hdr = raw[0].split(",")
    start = 0
    for i, line in enumerate(raw[1:], 1):
        if line[:4].isdigit():
            start = i; break
    d = pd.read_csv(io.StringIO("\n".join([",".join(["Date"] + hdr[1:])] + raw[start:])), parse_dates=["Date"], index_col="Date")
    d.columns = [c.lower().replace(" ", "_") for c in d.columns]
    return d, _prov([f], d.index, {"source": "yfinance daily OHLCV, personal-use license"})


# ---------------------------------------------------------------- news calendar
def load_calendar():
    p = VAULT / "derived" / "calendar.csv"
    if not p.exists():
        raise FileNotFoundError("vault has no derived/calendar.csv (run calendar_pull.py on the server)")
    d = pd.read_csv(p, parse_dates=["release_time_utc"])
    return d, _prov([p], d["release_time_utc"], {"source": "BLS/Fed schedules via calendar_pull.py"})


# ---------------------------------------------------------------- inventory (docs/vault.json)
def inventory():
    """What the vault holds, dataset by dataset, with first/last stamps. Never reads the .env."""
    inv = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "vault": str(VAULT), "datasets": []}

    def add(name, files, first=None, last=None, rows=None, public=False, note=""):
        size = sum(os.path.getsize(f) for f in files if os.path.exists(f)) / 1e6
        inv["datasets"].append({"name": name, "files": len(files), "size_mb": round(size, 1),
                                "first": first, "last": last, "rows": rows, "public_ok": public, "note": note})

    for sym in ["NQ", "ES", "MNQ", "MES"]:
        files = _databento_files(sym)
        if files:
            try:
                df, p = load_futures_1m(sym)
                add(f"{sym} 1-min (Databento)", files, p["first"], p["last"], p["rows"], True, "futures, continuous, unadjusted")
            except Exception as e:
                add(f"{sym} 1-min (Databento)", files, note=f"unreadable: {e}")
    for inst in ["xauusd", "usatechidxusd", "usa500idxusd"]:
        files = sorted(glob.glob(str(RAW / "dukascopy" / f"{inst}-m5-*.csv")))
        if files:
            try:
                df, p = load_dukascopy(inst)
                add(f"{inst} 5-min (Dukascopy)", files, p["first"], p["last"], p["rows"], False, "CFD/spot basis, private")
            except Exception as e:
                add(f"{inst} 5-min (Dukascopy)", files, note=f"unreadable: {e}")
    files = sorted(glob.glob(str(RAW / "binance" / "BTCUSDT-1m-*.zip")))
    if files:
        names = [os.path.basename(f)[12:19] for f in files]
        add("BTCUSDT 1-min (Binance)", files, names[0], names[-1], None, False, f"{len(files)} monthly zips")
    files = sorted(glob.glob(str(RAW / "yahoo" / "*_daily_*.csv")))
    if files:
        add("daily OHLCV (Yahoo)", files, None, None, None, False, ", ".join(sorted({os.path.basename(f).split('_daily')[0] for f in files})))
    files = sorted(glob.glob(str(RAW / "alpaca" / "*.parquet")))
    if files:
        add("QQQ/SPY 1-min (Alpaca)", files, None, None, None, False, f"{len(files)} yearly files")
    try:
        px, p = load_daily(fetch=False)
        add("daily closes (repo daily.csv)", p["files"], p["first"][:10], p["last"][:10], p["rows"], True, "QQQ / BTC / GLD — the money desk's own file")
    except Exception:
        pass
    cal = VAULT / "derived" / "calendar.csv"
    if cal.exists():
        d = pd.read_csv(cal)
        add("news calendar (CPI/FOMC/NFP)", [cal], str(d.iloc[0, 0])[:10], str(d.iloc[-1, 0])[:10], len(d), True, "")
    return inv


if __name__ == "__main__":
    print(json.dumps(inventory(), indent=1))
