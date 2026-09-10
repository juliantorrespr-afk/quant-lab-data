"""QUANT_LAB desk — MCP server for Claude Desktop.

Lets any Claude chat read the book, the signals, the ledger and TradingView quotes, run the bot, and (when keys exist)
place PAPER orders — without anyone driving a browser. Install once (see README_MCP.md); then in Claude Desktop:
  "what's the desk saying today?"  ->  desk_status
  "run the bot"                    ->  bot_run(mode="DRY")
  "quote QQQ / BTC / gold"         ->  tv_quote
  "buy $5,000 QQQ on paper"        ->  paper_order  (Alpaca paper keys required; refuses LIVE)

Rails: this server can never place a live order (no LIVE mode exposed), never changes strategy parameters,
and every order it sends is appended to data/ledger.csv with mode=MCP.
Run:  python quantlab_mcp.py   (stdio)   — from the quant-lab-data repo folder.
"""
import os, json, csv, subprocess, sys, datetime as dt, urllib.request
from mcp.server.fastmcp import FastMCP

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
mcp = FastMCP("quantlab-desk")

TV_SYMBOLS = {"QQQ": ("NASDAQ", "QQQ", "america"), "BTC": ("KRAKEN", "BTCUSD", "crypto"), "GLD": ("AMEX", "GLD", "america"),
              "XAUUSD": ("OANDA", "XAUUSD", "cfd"), "SPY": ("AMEX", "SPY", "america"), "NQ": ("CME_MINI", "NQ1!", "futures")}

def _read_json(name):
    p = os.path.join(DATA, name)
    return json.load(open(p)) if os.path.exists(p) else None

@mcp.tool()
def desk_status() -> dict:
    """Today's read: state (LONG/FLAT/HELD), flip line, distance, vol, size per sleeve, book equity and drawdown, bot mode."""
    sig = _read_json("signals.json"); st = _read_json("state.json")
    if not sig: return {"error": "no signals.json yet — run bot_run first"}
    out = {"as_of": sig.get("as_of"), "book": sig.get("book"), "sleeves": {}}
    for s in ("QQQ", "BTC", "GLD"):
        x = sig.get(s, {})
        out["sleeves"][s] = {"state": x.get("state"), "close": x.get("close"), "flip_line": x.get("sma150"), "distance_pct": x.get("dist_pct"),
                             "vol20_pct": x.get("vol20_pct"), "size": x.get("size_in_use"), "weight": x.get("weight")}
    if st: out["positions"] = st.get("pos"); out["cash"] = round(st.get("cash", 0), 2); out["entries"] = st.get("entry")
    if sig.get("notes"): out["notes"] = sig["notes"]
    return out

@mcp.tool()
def ledger(last_n: int = 20) -> list:
    """The last N orders the bot made (paper or MCP), newest first."""
    p = os.path.join(DATA, "ledger.csv")
    if not os.path.exists(p): return []
    rows = list(csv.DictReader(open(p)))
    return rows[-last_n:][::-1]

@mcp.tool()
def bot_run(mode: str = "DRY") -> str:
    """Run fetch.py then bot.py. mode = DRY (ledger only) or ALPACA (paper broker). LIVE is refused here."""
    mode = mode.upper()
    if mode not in ("DRY", "ALPACA"): return "refused: this server only runs DRY or ALPACA (paper)."
    env = dict(os.environ, MODE=mode)
    out = []
    for script in ("fetch.py", "bot.py"):
        r = subprocess.run([sys.executable, os.path.join(ROOT, script)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        out.append(f"== {script} (rc={r.returncode}) ==\n{r.stdout[-3000:]}\n{r.stderr[-1500:]}")
    return "\n".join(out)

@mcp.tool()
def tv_quote(symbol: str = "QQQ", interval: str = "1d") -> dict:
    """TradingView quote + indicator snapshot (close, SMA150-ish via SMA200/SMA100, RSI, recommendation). symbol: QQQ, BTC, GLD, XAUUSD, SPY, NQ. interval: 1d, 4h, 1h."""
    from tradingview_ta import TA_Handler, Interval
    iv = {"1d": Interval.INTERVAL_1_DAY, "4h": Interval.INTERVAL_4_HOURS, "1h": Interval.INTERVAL_1_HOUR, "1w": Interval.INTERVAL_1_WEEK}.get(interval, Interval.INTERVAL_1_DAY)
    ex, sym, scr = TV_SYMBOLS.get(symbol.upper(), ("NASDAQ", symbol.upper(), "america"))
    h = TA_Handler(symbol=sym, exchange=ex, screener=scr, interval=iv)
    a = h.get_analysis(); i = a.indicators
    return {"symbol": f"{ex}:{sym}", "interval": interval, "close": i.get("close"), "open": i.get("open"), "high": i.get("high"), "low": i.get("low"),
            "volume": i.get("volume"), "SMA100": i.get("SMA100"), "SMA200": i.get("SMA200"), "EMA50": i.get("EMA50"), "RSI": i.get("RSI"), "ATR": i.get("ATR"),
            "tv_summary": a.summary, "note": "TradingView's own SMA150 is not exposed here; the desk computes SMA150 from closes in data/daily.csv (desk_status)."}

def _alpaca(path, method="GET", body=None):
    base = "https://paper-api.alpaca.markets"           # paper only — this server never touches the live endpoint
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
        headers={"APCA-API-KEY-ID": os.environ.get("APCA_API_KEY_ID", ""), "APCA-API-SECRET-KEY": os.environ.get("APCA_API_SECRET_KEY", ""), "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))

@mcp.tool()
def paper_account() -> dict:
    """Alpaca PAPER account equity, cash and open positions (needs APCA_API_KEY_ID / APCA_API_SECRET_KEY in the environment)."""
    if not os.environ.get("APCA_API_KEY_ID"): return {"error": "no Alpaca paper keys in environment"}
    acct = _alpaca("/v2/account"); pos = _alpaca("/v2/positions")
    return {"equity": acct.get("equity"), "cash": acct.get("cash"), "positions": [{"symbol": p["symbol"], "qty": p["qty"], "market_value": p["market_value"], "unrealized_plpc": p["unrealized_plpc"]} for p in pos]}

@mcp.tool()
def paper_order(symbol: str, dollars: float, side: str = "buy") -> dict:
    """Place a PAPER market order on Alpaca (QQQ, GLD, or BTC -> BTC/USD). Max 45% of a $100k book per order. Logged to data/ledger.csv with mode=MCP."""
    if not os.environ.get("APCA_API_KEY_ID"): return {"error": "no Alpaca paper keys in environment"}
    if abs(dollars) > 45_000: return {"error": "refused: order exceeds the 45%-of-book rail"}
    sym = {"BTC": "BTC/USD", "GLD": "GLD", "QQQ": "QQQ"}.get(symbol.upper(), symbol.upper())
    body = {"symbol": sym, "notional": f"{abs(dollars):.2f}", "side": side.lower(), "type": "market", "time_in_force": "gtc" if "/" in sym else "day"}
    r = _alpaca("/v2/orders", "POST", body)
    with open(os.path.join(DATA, "ledger.csv"), "a", newline="") as f:
        csv.writer(f).writerow([dt.date.today().isoformat(), symbol.upper(), side.lower(), round(abs(dollars), 2), "", "manual-mcp", "MCP", r.get("id", "")])
    return {"id": r.get("id"), "status": r.get("status"), "symbol": sym, "notional": body["notional"], "side": body["side"]}

@mcp.tool()
def rulebook() -> str:
    """The whole strategy in one paragraph — so any chat can explain a signal without guessing."""
    return ("QQQ and BTC: own it when the daily close is above the 150-day average, cash when below (that is the stop). Gold: always held. "
            "Size = weight × min(1, target vol / 20-day realized vol); weights QQQ 40 / BTC 20 / gold 40; targets 15 / 40 / 15 %. "
            "Sizes re-read Mondays; rebalance on the first 3 weekdays of the month. Rails: −15% disaster stop per sleeve (gap insurance), "
            "−25% book kill switch, no order > 45% of book. No take-profit (tested, loses). No shorts (tested, loses). No intraday (1,492 configs, 0 survive). "
            "Nothing here is tuned; the only way this loses is overriding it.")

if __name__ == "__main__":
    mcp.run()
