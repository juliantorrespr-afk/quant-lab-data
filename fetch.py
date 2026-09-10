"""QUANT_LAB data feed — runs on GitHub Actions daily (~00:10 UTC).
Pulls completed daily closes from Kraken (XBTUSD, PAXGUSD) and Yahoo (QQQ, GLD) and appends to data/daily.csv.
Never writes today's live candle. Idempotent: existing rows are kept, mismatches are logged, not overwritten."""
import json, csv, datetime as dt, urllib.request, os, sys, time
OUT="data/daily.csv"; LOG="data/feed_log.txt"
UA={"User-Agent":"Mozilla/5.0 (quant-lab feed)"}
def get(url):
    req=urllib.request.Request(url,headers=UA)
    return json.load(urllib.request.urlopen(req,timeout=30))
today=dt.datetime.now(dt.timezone.utc).date()

# ---- first run: seed full history if data/daily.csv is missing (QQQ, GLD adjusted; BTC-USD from Yahoo as history, Kraken from here on) ----
if not os.path.exists(OUT) or sum(1 for _ in open(OUT)) < 5000:   # missing or not daily -> re-seed
    os.makedirs("data", exist_ok=True); seeded={}
    for tk,sym in [("QQQ","QQQ"),("GLD","GLD"),("BTC-USD","BTC")]:
        j=get(f"https://query1.finance.yahoo.com/v8/finance/chart/{tk}?period1=1420070400&period2={int(time.time())}&interval=1d")
        r=j["chart"]["result"][0]; ts=r["timestamp"]; adj=r["indicators"]["adjclose"][0]["adjclose"]
        for t,a in zip(ts,adj):
            if a is None: continue
            d=dt.datetime.fromtimestamp(t,dt.timezone.utc).date()
            if d>=dt.date(2015,1,1) and d<today: seeded[(d.isoformat(),sym)]=f"{a:.2f}"
    with open(OUT,"w",newline="") as f:
        w=csv.writer(f); w.writerow(["date","symbol","close"])
        for (d,s) in sorted(seeded): w.writerow([d,s,seeded[(d,s)]])
    print("seeded",len(seeded),"rows")
rows={}
# Kraken: candle open time (UTC midnight) = the day the close belongs to; last candle is live -> drop
for pair,sym in [("XBTUSD","BTC"),("PAXGUSD","PAXG")]:
    since=int(time.time())-12*86400
    j=get(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440&since={since}")
    key=[k for k in j["result"] if k!="last"][0]
    for c in j["result"][key][:-1]:
        d=dt.datetime.fromtimestamp(int(c[0]),dt.timezone.utc).date()
        if d<today: rows[(d.isoformat(),sym)]=c[4]
# Yahoo: adjusted close via chart API; drop today's bar if the US session is not closed (before 21:30 UTC)
for tk,sym in [("QQQ","QQQ"),("GLD","GLD")]:
    j=get(f"https://query1.finance.yahoo.com/v8/finance/chart/{tk}?range=1mo&interval=1d")
    r=j["chart"]["result"][0]; ts=r["timestamp"]; adj=r["indicators"]["adjclose"][0]["adjclose"]; cl=r["indicators"]["quote"][0]["close"]
    for t,a,c in zip(ts,adj,cl):
        if a is None: continue
        d=dt.datetime.fromtimestamp(t,dt.timezone.utc).date()
        if d<today: rows[(d.isoformat(),sym)]=f"{a:.2f}"
existing={}
if os.path.exists(OUT):
    for r in csv.DictReader(open(OUT)): existing[(r["date"],r["symbol"])]=r["close"]
added=0; mism=[]
for k,v in rows.items():
    if k in existing:
        if abs(float(existing[k])-float(v))>0.02*max(1,abs(float(v)))/100: mism.append((k,existing[k],v))
    else: existing[k]=v; added+=1
with open(OUT,"w",newline="") as f:
    w=csv.writer(f); w.writerow(["date","symbol","close"])
    for (d,s) in sorted(existing): w.writerow([d,s,existing[(d,s)]])
with open(LOG,"a") as f: f.write(f"{dt.datetime.now(dt.timezone.utc).isoformat()} added={added} mismatches={mism}\n")
print("added",added,"mismatches",mism)
