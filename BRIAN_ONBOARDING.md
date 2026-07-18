# Onboarding: running your own copy of this bot

This repo is Gabriel's Alpaca paper-trading bot plus a private read-only web
dashboard. You (Brian) are getting your own copy to run on your own server,
trading against the **same Alpaca account** Gabriel uses. Read the whole
file before running anything — the concurrency warning below matters.

If you're handing this file to Claude Code, just say "follow
BRIAN_ONBOARDING.md to set this up" and it can run the commands below
directly. Stop and ask Gabriel if any step fails rather than guessing.

## 0. Prerequisites

**You're on Windows** — commands below are given for both PowerShell and
Mac/Linux where they differ. One extra note: `gunicorn` (used in step 6)
doesn't run on Windows at all (it relies on a Unix-only feature), so that
step uses `waitress` instead — same job, cross-platform. Everything else
in this repo (the actual bot and dashboard code) is plain Python and runs
identically on Windows.

- Python 3.9+ and `git` installed.
- Gabriel's GitHub login (`gpena1`), shared with you through the password
  manager vault — **not** through this file or email. You're using his
  account directly rather than your own, so anything you push shows up
  as coming from him. Coordinate with him before pushing any code changes
  so you don't clobber each other's work.
- The Alpaca API key/secret and dashboard credentials, also in the vault.
  If you don't have vault access yet, get that from him before step 3.

## 1. Clone the repo

```bash
git clone https://github.com/gpena1/alpaca-trading-bot.git
cd alpaca-trading-bot
```

Log in with Gabriel's GitHub credentials if `git` prompts you (or set up
an SSH key / credential helper under his account first, if you'd rather
not type a password each time).

## 2. Set up the Python environment

PowerShell (Windows):
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Mac/Linux:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

(You already have a filled-in `.env` from Gabriel, so you can skip the
copy step and just drop that file in instead — see step 3.)

## 3. Fill in `.env`

Open `.env` and fill in the values from the vault. You need at minimum:

```
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_BASE_URL=https://paper-api.alpaca.markets
DASHBOARD_USERNAME=
DASHBOARD_PASSWORD=
```

Leave `ALPACA_BASE_URL` pointed at the paper endpoint unless you and Gabriel
have explicitly agreed to trade live together.

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

Windows (PowerShell) — `gunicorn` doesn't work here, use `waitress` instead:
```powershell
pip install waitress
waitress-serve --host=0.0.0.0 --port=8000 web.app:app
```

Mac/Linux:
```bash
gunicorn -w 1 --threads 4 -b 0.0.0.0:8000 web.app:app
```

Visit `http://localhost:8000`, log in with the `DASHBOARD_USERNAME` /
`DASHBOARD_PASSWORD` you set in step 3. If you're exposing this on a real
server, put it behind nginx/Caddy (or IIS on Windows) for HTTPS rather
than serving directly to the internet.

## 7. Running the trading bot long-term (only if you're the one running it)

`python3 -m bot.main` polls forever, so it needs to keep running after you
close your terminal:

- **Windows**: simplest is to leave a PowerShell window open, or use Task
  Scheduler with "Run whether user is logged on or not" so it survives
  reboots. For something closer to a real background service, look at
  [NSSM](https://nssm.cc) (wraps any command as a Windows service).
- **Mac/Linux**: run it under `systemd`, `tmux`, `screen`, or `pm2`.

If you'd rather have a real Linux environment instead of juggling Windows
equivalents, [WSL](https://learn.microsoft.com/windows/wsl/install) gives
you one inside Windows, and every Mac/Linux command above works unmodified.

## Questions / something doesn't work

Message Gabriel directly rather than guessing on anything involving live
order submission or shared credentials.
