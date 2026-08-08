# Start here, Brian

## 1. Read `STRATEGY_EVALUATION.md` before you read anything else

That document is the honest summary of where this project actually stands, and
it changes what you should expect from the rest of it.

Four strategy families were built and tested: per-symbol trend/reversion/breakout,
cross-sectional momentum across a 29-ETF universe, three aggressive strategies over
a volatility-targeted sizing layer, and a Carver-style multi-speed continuous
forecast. **None of them is recommended for funding.** Every one of them lost to
buying and holding SPY out-of-sample, and several lost money outright.

That is a real finding rather than an unfinished job. Most of the early numbers
that looked good turned out to be measurement error — a backtest with no
walk-forward split, a Sharpe ratio annualized against wall-clock time instead of
trading sessions, and a benchmark measured on prices that excluded dividends. All
three flattered the results, all three were found and corrected, and the results
got worse each time.

So: what's worth your attention here is the infrastructure — the execution, risk
accounting, and validation harness. The strategies are worth your attention only
as an example of what the harness is for.

Please don't spend your time tuning parameters to make the numbers improve.
`STRATEGY_EVALUATION.md` has a section on what would need to be true before
revisiting any of this, and "sweep the knobs until it looks good" is specifically
the failure mode the whole setup was built to detect.

## 2. Setup

Open this folder in Claude Code and say: **"follow BRIAN_ONBOARDING.md to set this
up."** That file has PowerShell-specific commands for your Windows machine and
walks through the whole process.

## 3. Credentials — not in this folder

**There is no `.env` file here, and that is deliberate.** Credentials are never
committed to the repository or passed around in a shared folder.

What you do instead:

- Copy `.env.example` to `.env` and fill in the blanks yourself.
- Get the actual values — Alpaca API key and secret, dashboard username and
  password — from the **password manager vault**. Not from email, not from chat,
  not from this file.
- If you don't have vault access yet, ask me for it before you get to the
  environment setup step.
- Once your `.env` exists, don't forward it or paste its contents anywhere.
  `.gitignore` already excludes it, so git won't commit it by accident.

The GitHub login for cloning comes through the vault as well.

## 4. Concurrency warning — read this before running the trading loop

**You and I share one Alpaca account.**

`bot/main.py` sizes every order from the account's *current* equity on each cycle.
It has no idea a second bot exists. If we both run `python3 -m bot.main` at the
same time, two bots end up placing orders against the same capital in the same
window, and the exposure caps in `config.py` get blown straight past — each bot
thinks it's the only one spending.

**Only run the trading loop if you and I have explicitly agreed that you're the
one running it right now.**

If we haven't had that conversation, skip to the dashboard instead. It's read-only
— it never calls the risk manager and never submits an order — so it's safe to run
in parallel with anything.

`python3 -m bot.main --once` is also safe to run any time: it executes a single
cycle, logs what it sees and what it would do, and exits without trading. That's
the right way to confirm your keys and environment are wired up.

## 5. If something breaks

Message me rather than guessing, particularly on anything touching live order
submission or shared credentials. A wrong guess on either of those costs real
money or leaks real keys.

— Gabriel
