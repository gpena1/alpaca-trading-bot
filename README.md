# Alpaca multi-instrument trading bot

Trades SPY, QQQ, BTC/USD, GLD, and USO simultaneously against the Alpaca
Markets API, one strategy per symbol:

| Symbol  | Strategy           | Rationale                                   |
|---------|---------------------|----------------------------------------------|
| SPY     | trend_following     | broad index, tends to trend                   |
| QQQ     | trend_following     | broad index, tends to trend                   |
| GLD     | mean_reversion       | range-bound commodity                         |
| USO     | mean_reversion       | range-bound commodity                         |
| BTC/USD | momentum_breakout    | high-volatility, directional bursts, 24/7      |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and add your Alpaca **paper trading** API key/secret from
https://app.alpaca.markets (Paper Trading tab). `ALPACA_BASE_URL` already
defaults to the paper endpoint — leave it alone unless you deliberately
intend to trade with real money, in which case you must also set
`CONFIRM_LIVE_TRADING=yes` or the bot refuses to start.

## Run

```bash
python3 -m bot.main
```

The bot polls every `POLL_INTERVAL_SECONDS` (default 300s), pulls recent
bars for each symbol, runs its assigned strategy, and routes any BUY/SELL
signal through the risk manager before submitting a market order. Equities
are skipped while the market is closed; BTC/USD trades around the clock.

## Risk controls (`config.py`)

- **Position sizing**: fixed-fractional — each symbol gets up to
  `MAX_ALLOCATION_PCT_PER_SYMBOL` (default 20%) of account equity.
- **Total exposure cap**: `MAX_TOTAL_EXPOSURE_PCT` (default 90%) across all
  five instruments combined.
- **Stop-loss / take-profit**: positions auto-close at
  `STOP_LOSS_PCT` (default -5%) or `TAKE_PROFIT_PCT` (default +10%),
  overriding whatever the strategy says that cycle.
- **No shorting**: a SELL signal only closes an existing long; it never
  opens a short position.

Tune these in `config.py` before running with real capital.

## Project layout

```
config.py                       symbols, strategy assignment, risk parameters
bot/broker.py                   Alpaca API wrapper (market data + orders)
bot/portfolio.py                account equity / positions / exposure
bot/risk_manager.py             position sizing, exposure caps, stop-loss/take-profit
bot/strategies/base.py          Strategy interface + Signal type
bot/strategies/trend_following.py    SMA crossover
bot/strategies/mean_reversion.py     RSI oversold/overbought
bot/strategies/momentum_breakout.py  Donchian channel breakout
bot/main.py                     polling loop that wires it all together
```

## Important caveats

- **This is not investment advice and is not a validated profitable
  strategy.** The three strategies here are common, simple baselines
  provided as a working scaffold, not a tuned or backtested trading system.
- Backtest and paper-trade extensively before considering live capital.
- The bot has no persistence across restarts — it re-derives state from
  Alpaca's account/position API each cycle, so it's safe to restart, but it
  keeps no memory of past signals beyond current SMA/RSI/channel state.
- Network or API errors on one symbol are caught and logged per-symbol so
  they don't take down the whole cycle; check logs regularly regardless.
