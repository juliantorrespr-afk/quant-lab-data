# Connect the desk to Claude Desktop (MCP) — 5 minutes, one time

This makes every Claude chat able to read the book, the signals, the ledger and TradingView quotes, run the bot, and place PAPER orders — no browser driving.

1. Put this folder (the unzipped `quant-lab-data`) somewhere permanent, e.g. `C:\quantlab` (Windows) or `~/quantlab` (Mac).
2. Install Python 3.11+ and run once in that folder:
   `pip install mcp tradingview-ta`
3. Claude Desktop → Settings → Developer → **Edit Config**. Add inside `"mcpServers"`:
```json
"quantlab-desk": {
  "command": "python",
  "args": ["C:\\quantlab\\quantlab_mcp.py"],
  "env": { "APCA_API_KEY_ID": "", "APCA_API_SECRET_KEY": "" }
}
```
   (Mac: `"command": "python3", "args": ["/Users/<you>/quantlab/quantlab_mcp.py"]`.) Leave the Alpaca keys empty until you have a paper account; the other tools work without them.
4. Restart Claude Desktop. You'll see the 🔌 tools: `desk_status`, `ledger`, `bot_run`, `tv_quote`, `paper_account`, `paper_order`, `rulebook`.
5. Try: "What is the desk saying today?" → Claude calls `desk_status`. "Quote BTC on the 4h" → `tv_quote`.

What it can never do: place a live order (no LIVE mode exists in this server), change a strategy parameter, or exceed the 45%-of-book rail. Every MCP order is appended to `data/ledger.csv` with mode `MCP`.

## What "TradingView ↔ Claude" really is
- TradingView has no public order API. Charts/scripts/paper-trading are UI only. Every "TradingView MCP" you see on Instagram reads quotes/screener data (like `tv_quote` here) or drives a browser.
- Orders leave TradingView only one way: **alerts → webhook** (Essential plan, $15/mo) → a relay → your broker. That's the path for the funded MT5 account later (webhook → EA/copier). For the ETF+crypto book the bot talks to the broker API directly — no TradingView in the loop.
