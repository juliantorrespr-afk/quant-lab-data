# quant-lab-data — read this first

This folder is Jae's QUANT_LAB execution desk. It runs by itself; sessions here are for reading, auditing and (rarely) fixing plumbing.

## What runs
- `fetch.py` — nightly closes (Kraken BTC/PAXG, Yahoo QQQ/GLD) into `data/daily.csv`; self-seeds full daily history if the file is missing or not daily.
- `bot.py` — the whole strategy, ~115 lines, commented at the top. Modes: DRY (paper ledger, default) / ALPACA (paper broker) / LIVE (refused unless `data/LIVE_APPROVED_BY_JAE.txt` exists AND `APCA_LIVE=1`).
- `quantlab_mcp.py` — MCP server for Claude Desktop: desk_status, ledger, bot_run(DRY|ALPACA), tv_quote, paper_account, paper_order (paper only), rulebook.
- `.github/workflows/daily.yml` — GitHub Actions, 00:10 UTC daily: fetch → bot → commit `data/`.

## The strategy (do not change in a session)
QQQ, BTC: own when the daily close is above the 150-day SMA, cash below. Gold: always held. Size = weight × min(1, target vol / 20-day vol); weights 40/20/40; targets 15/40/15 %. Sizes re-read Mondays; rebalance 1st–3rd weekday of the month. Rails: −15% disaster stop per sleeve, −25% book kill switch, no order > 45% of book. No take-profit, no shorts, no intraday — all tested and rejected (see the Trading project docs).

## Rules for any Claude session in this folder
- Never place a live order. Never create `LIVE_APPROVED_BY_JAE.txt`. Never put keys in files (GitHub secrets / Claude Desktop env only).
- Never edit strategy constants (W, VT, SMA, KILL, DISASTER). A new idea gets the standard test in the Trading project first (select 2015–20, judge 2021–26, real costs).
- `data/` is written by the bot; humans and chats only read it. If something looks wrong, open an issue-style note in the Trading project (claude/LAB_STATUS.md), don't hand-edit the ledger.
- Sanity checks before trusting `data/signals.json`: as_of within 4 days, sma150 within 40% of close, daily.csv > 5,000 rows.
