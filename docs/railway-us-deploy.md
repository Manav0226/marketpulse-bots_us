# Railway Deployment For US Stack

## Recommended shape
- Use one Railway `Worker` service, not four separate services.
- Attach one persistent volume to that worker.
- Run `launcher/launch_us_cloud.py` as the start command.

This is the recommended Railway design because the US execution bot, father bot,
intel bot, and scheduler all share local state files such as:
- `bot_state.json`
- `father_opinion.json`
- `us_weekly_brief.json`
- `us_supervision.json`
- `MarketPulse_TradeLog.xlsx`

With one worker and one volume, those files stay consistent and restart-safe.

## Files used
- `launcher/launch_us_cloud.py`
- `vm_us_scheduler.py`
- `bot_us_crypto_v4.py`
- `bot_us_crypto_intel.py`
- `bot_us_research.py`
- `us_supervisor.py`
- `bot_father.py`

## Railway service setup
1. Push this repo to GitHub.
2. In Railway, create a new project from the GitHub repo.
3. Add a `Worker` service from that repo.
4. Set the start command to:
   - `python launcher/launch_us_cloud.py`
5. Add a persistent volume.
6. Mount that volume at:
   - `/data`

## Required environment variables
Set these in Railway for the worker service:

```env
MARKETPULSE_STATE_DIR=/data/briefings
MARKETPULSE_LOG_DIR=/data/logs
MARKETPULSE_REPORT_DIR=/data/reports
MARKETPULSE_TZ=UTC

US_PAPER_TRADING=true
CRYPTO_PAPER_TRADING=true
POLYMARKET_PAPER_TRADING=true
AUTO_PAUSE_ONLY=true

US_INTEL_TG_TOKEN=...
US_INTEL_TG_CHAT=7973242803
US_RESEARCH_TG_TOKEN=...
US_RESEARCH_TG_CHAT=7973242803
US_EXEC_TG_TOKEN=...
US_EXEC_TG_CHAT=7973242803
FATHER_TG_TOKEN=...
FATHER_TG_CHAT=7973242803

OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.4-mini

GITHUB_TOKEN=...
GITHUB_USER=Manav-Deakin-23
GITHUB_REPO=marketpulse-bots

ALPACA_KEY=...
ALPACA_SECRET=...
ALPACA_PAPER=true

FINNHUB_KEY=...

US_CAPITAL=10000
CRYPTO_CAPITAL=500
```

## First deploy
1. Create the Railway worker.
2. Add the environment variables.
3. Attach the persistent volume at `/data`.
4. Deploy the service.
5. Open the Railway logs and confirm:
   - launcher started
   - father bot started
   - US execution bot started
   - US intel bot started
   - VM scheduler started

## First testing
After first deploy, open the Railway shell and run:

```bash
python us_supervisor.py
python bot_us_research.py
```

Then confirm these files exist under the mounted volume path:
- `/data/briefings/us_weekly_brief.json`
- `/data/briefings/us_supervision.json`
- `/data/briefings/father_opinion.json`
- `/data/briefings/bot_state.json`
- `/data/briefings/vm_scheduler_state.json`
- `/data/reports/MarketPulse_TradeLog.xlsx`

Also confirm:
- Telegram messages arrive from US intel
- Telegram messages arrive from US research
- Telegram messages arrive from father bot

For the workbook, the scheduler will rebuild it automatically after US close from
the persisted paper-trade CSV logs and bot-state snapshots. Your laptop does not
need to be on for this.

## Updating later
1. Push code to GitHub.
2. Railway redeploys automatically if auto-deploy is enabled.
3. Watch the service logs after deploy.

## Safe testing posture
- Keep Binance or any exchange API keys out until paper mode is stable.
- Keep all venue toggles in paper mode.
- Let the worker run for at least one full US session before trusting automation.
- Review `/data/briefings` artifacts before enabling anything live.
