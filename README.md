# MarketPulse Bot Stack

Cloud-ready trading and research bot workspace for:
- India equity
- India FNO
- US equities
- Crypto
- Polymarket paper research

This repo now includes a Railway-friendly US cloud stack with:
- one long-running US execution bot
- one US/Crypto intel bot
- one father-bot supervisor
- one internal scheduler for research, supervision refresh, and EOD workbook generation

## Recommended hosting

Use `Railway` for the current US cloud deployment.

Why:
- long-running Python worker support
- persistent volume support
- simple GitHub-driven deploy flow
- better fit than Netlify or Expo for a stateful bot runtime

Do not use Netlify or Expo as the primary bot host for this repo:
- Netlify is function-oriented and better for short-lived serverless tasks
- Expo EAS Hosting is for web app hosting, not long-running Python workers

## Main US cloud files

- [launcher/launch_us_cloud.py](/D:/MarketPulseBot/launcher/launch_us_cloud.py)
- [bot_us_crypto_v4.py](/D:/MarketPulseBot/bot_us_crypto_v4.py)
- [bot_us_crypto_intel.py](/D:/MarketPulseBot/bot_us_crypto_intel.py)
- [bot_us_research.py](/D:/MarketPulseBot/bot_us_research.py)
- [bot_us_eod_report.py](/D:/MarketPulseBot/bot_us_eod_report.py)
- [bot_father.py](/D:/MarketPulseBot/bot_father.py)
- [us_supervisor.py](/D:/MarketPulseBot/us_supervisor.py)
- [vm_us_scheduler.py](/D:/MarketPulseBot/vm_us_scheduler.py)
- [docs/railway-us-deploy.md](/D:/MarketPulseBot/docs/railway-us-deploy.md)

## Railway architecture

Run one Railway `Worker` service with one mounted volume.

The worker launches:
- US execution bot
- US intel bot
- father bot
- VM scheduler

The persistent volume stores:
- `/data/briefings`
- `/data/logs`
- `/data/reports`

This lets the bot keep:
- paper-trade state
- research outputs
- supervision state
- logs
- the auto-generated `MarketPulse_TradeLog.xlsx`

even when your laptop is off.

## Required Railway environment variables

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

FINNHUB_KEY=...

ALPACA_KEY=...
ALPACA_SECRET=...
ALPACA_PAPER=true

US_CAPITAL=10000
CRYPTO_CAPITAL=500
```

## First Railway deploy

1. Push this repo to a private GitHub repository.
2. In Railway, create a new project from that repo.
3. Add a `Worker` service.
4. Set the start command to:
   - `python -m launcher.launch_us_cloud`
5. Attach a persistent volume at `/data`.
6. Add the required environment variables.
7. Deploy.

Then manually test once in the Railway shell:

```bash
python us_supervisor.py
python bot_us_research.py
```

Confirm these outputs appear:
- `/data/briefings/us_weekly_brief.json`
- `/data/briefings/us_supervision.json`
- `/data/briefings/father_opinion.json`
- `/data/briefings/bot_state.json`
- `/data/briefings/vm_scheduler_state.json`
- `/data/reports/MarketPulse_TradeLog.xlsx`

## Automatic workbook behavior

The US paper-trading workbook is updated in the cloud automatically.

How it works:
- trade CSV logs are written by the US bot
- bot state is persisted to shared JSON
- after US close, the scheduler runs the EOD workbook builder
- the workbook is rebuilt into `/data/reports/MarketPulse_TradeLog.xlsx`

This does not require your laptop to be on.

## Security

- keep all exchange API keys in paper mode first
- never enable withdrawal permissions on bot keys during testing
- do not commit `config/config.env`
- rotate any Telegram or exchange secrets that were ever pasted into chat or shared unsafely

## Verification status

Implemented and locally verified:
- US cloud launcher
- Railway volume-aware state/log/report paths
- US supervisor refresh scheduling
- US weekly research scheduling
- US EOD workbook generation
- scheduler timing tests
- US cloud architecture tests

Not yet verified live:
- Railway runtime behavior
- mounted volume behavior in production
- live Telegram delivery from Railway
- live exchange connectivity

## Next step

Create an empty private GitHub repo, then connect Railway to it and deploy the worker using the Railway guide in [docs/railway-us-deploy.md](/D:/MarketPulseBot/docs/railway-us-deploy.md:1).
