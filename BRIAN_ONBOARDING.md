# Onboarding: running your own copy of this bot

This repo is Gabriel's Alpaca paper-trading bot plus a private read-only web
dashboard. You (Brian) are getting your own copy to run on your own server,
trading against the **same Alpaca account** Gabriel uses. Read the whole
file before running anything — the concurrency warning below matters.

If you're handing this file to Claude Code, just say "follow
BRIAN_ONBOARDING.md to set this up" and it can run the commands below
directly. Stop and ask Gabriel if any step fails rather than guessing.

## 0. Prerequisites

- Python 3.9+ and `git` installed.
- GitHub access to `gpena1/alpaca-trading-bot` already accepted (Gabriel
  added you as a collaborator) and you've forked it to your own account.
- The Alpaca API key/secret and dashboard credentials, which Gabriel is
  sharing with you through a password manager vault — **not** through this
  file or email. If you don't have vault access yet, get that from him
  before step 3.

## 1. Clone your fork

```bash
git clone https://github.com/<your-github-username>/alpaca-trading-bot.git
cd alpaca-trading-bot
```

## 2. Set up the Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 3. Fill in `.env`

Open `.env` and fill in the values from the vault. You need at minimum:

```
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets
DASHBOARD_USERNAME=
DASHBOARD_PASSWORD=
```

`ALPACA_GROWTH_*` / `ALPACA_AGGRESSIVE_*` are optional sub-account keys for
two of the dashboard's tiers — only fill those in if Gabriel gave you
values for them. Leave `ALPACA_BASE_URL` pointed at the paper endpoint
unless you and Gabriel have explicitly agreed to trade live together.

Pick your own `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` — those are just
for your own copy of the web dashboard and don't need to match Gabriel's.

## 4. IMPORTANT — read before running the trading bot

You and Gabriel share one Alpaca account. `bot/main.py` sizes every order
from the account's *current* equity each cycle — it has no idea a second
bot exists. If you **both** run `python3 -m bot.main` at the same time,
you can end up with two bots placing orders against the same capital in
the same window, blowing past the exposure caps in `config.py`.

**Only run the trading loop if you and Gabriel have explicitly agreed you're
the one running it right now.** Otherwise, skip straight to the dashboard
in step 6 — it's read-only and safe to run in parallel with anything.

## 5. Verify it works (dry check, no trading)

```bash
python3 -m bot.main --once
```

This runs one polling cycle and logs what it sees/would do, then exits —
confirms your API keys and environment are wired up correctly.

## 6. Run the web dashboard (safe to run alongside Gabriel's)

```bash
gunicorn -w 1 --threads 4 -b 0.0.0.0:8000 web.app:app
```

Visit `http://localhost:8000`, log in with the `DASHBOARD_USERNAME` /
`DASHBOARD_PASSWORD` you set in step 3. If you're exposing this on a real
server, put it behind nginx/Caddy for HTTPS rather than serving gunicorn
directly to the internet.

## 7. Running the trading bot long-term (only if you're the one running it)

`python3 -m bot.main` polls forever — run it under `systemd`, `tmux`,
`screen`, or `pm2` so it survives you disconnecting from the server.

## Questions / something doesn't work

Message Gabriel (brian, you know his info) rather than guessing on
anything involving live order submission or shared credentials.
