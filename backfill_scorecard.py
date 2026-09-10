#!/usr/bin/env python3
"""One-off: backfill data/scorecard.csv with the calls the rule WOULD have
published, 2015-2026. No lookahead (call uses close t, scored on close t+1).
Rows are marked source=BACKFILL; the hourly pulse writes source=LIVE."""
import csv, collections, os, sys
DATA=os.path.join(os.path.dirname(os.path.abspath(__file__)),"data")
px=collections.defaultdict(dict)
for r in csv.DictReader(open(os.path.join(DATA,"daily.csv"))):
    px[r["symbol"]][r["date"]]=float(r["close"])
dates=sorted(px["QQQ"])
def ser(s):
    o=[];l=None
    for d in dates:
        v=px[s].get(d,l);l=v;o.append(v)
    return o
P={s:ser(s) for s in ("QQQ","BTC","GLD")}
TREND={"QQQ":1,"BTC":1,"GLD":0}
def conf(d):
    a=abs(d); return "HIGH" if a>=5 else ("MEDIUM" if a>=2 else "LOW")
rows=[]
for i in range(150,len(dates)-1):
    for s in ("QQQ","BTC","GLD"):
        a=P[s]; m=sum(a[i-149:i+1])/150
        dist=100*(a[i]/m-1)
        call=("LONG" if a[i]>m else "FLAT") if TREND[s] else "HELD"
        ret=100*(a[i+1]/a[i]-1)
        res=("RIGHT" if ret>0 else "WRONG") if call in ("LONG","HELD") else ("RIGHT" if ret<=0 else "WRONG")
        rows.append(dict(as_of=dates[i],sleeve=s,call=call,dist_pct=round(dist,2),
            confidence=conf(dist),close=round(a[i],2),next_close=round(a[i+1],2),
            next_ret_pct=round(ret,3),result=res,scored_on=dates[i+1],source="BACKFILL"))
hdr=["as_of","sleeve","call","dist_pct","confidence","close","next_close","next_ret_pct","result","scored_on","source"]
with open(os.path.join(DATA,"scorecard.csv"),"w",newline="") as f:
    w=csv.DictWriter(f,hdr); w.writeheader()
    for r in rows: w.writerow(r)
def hit(sub): return round(100*sum(1 for r in sub if r["result"]=="RIGHT")/len(sub),1) if sub else None
print("rows",len(rows),"overall hit",hit(rows))
for s in ("QQQ","BTC","GLD"): print(" ",s,hit([r for r in rows if r["sleeve"]==s]))
for c in ("HIGH","MEDIUM","LOW"): print(" conf",c,hit([r for r in rows if r["confidence"]==c]),len([r for r in rows if r["confidence"]==c]))
for c in ("LONG","FLAT","HELD"): print(" call",c,hit([r for r in rows if r["call"]==c]),len([r for r in rows if r["call"]==c]))
print(" since2021",hit([r for r in rows if r["as_of"]>="2021"]))
