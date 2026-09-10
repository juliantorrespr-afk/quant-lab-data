# quant-lab-data — feed + execution bot (P7-VT3)

Runs by itself every day at 00:10 UTC on GitHub Actions. No alerts, no clicks.

1. `fetch.py` pulls yesterday's closes (Kraken BTC/PAXG, Yahoo QQQ/GLD) into `data/daily.csv` (seeded 2015-01-02 → 2026-09-04).
2. `bot.py` computes the three signals, sizes, and orders, and executes them:
   - `MODE=DRY` (default): paper ledger only → `data/ledger.csv`, `data/state.json`, `data/signals.json`. This is the paper desk.
   - `MODE=ALPACA`: same orders sent to an Alpaca **paper** account (fake money, real API). Set repo variable `MODE=ALPACA` and secrets `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`.
   - `MODE=LIVE`: refused unless `data/LIVE_APPROVED_BY_JAE.txt` exists and `APCA_LIVE=1`. Only Jae creates that file.

## Setup (5 minutes)
1. GitHub → New repository → `quant-lab-data`, **Private**. Upload every file in this zip (keep the folders).
2. Settings → Actions → General → Workflow permissions → **Read and write** → Save.
3. Actions tab → `quant-lab-daily` → **Run workflow** once. You should see `data/signals.json` and `data/ledger.csv` update.
4. (Optional, when allowed) Alpaca paper keys → Settings → Secrets and variables → Actions: secrets `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`; variable `MODE` = `ALPACA`.

The Claude desk reads `data/signals.json` and `data/ledger.csv` nightly and only messages you on a flip, a kill-switch, or an outage.
