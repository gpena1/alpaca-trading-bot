"""Central configuration loaded from environment variables (.env)."""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
IS_PAPER = "paper" in ALPACA_BASE_URL
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "no").strip().lower() == "yes"

if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    sys.exit(
        "ALPACA_API_KEY / ALPACA_SECRET_KEY are not set. "
        "Copy .env.example to .env and fill in your Alpaca credentials."
    )

if not IS_PAPER and not CONFIRM_LIVE_TRADING:
    sys.exit(
        "ALPACA_BASE_URL points at a non-paper endpoint but CONFIRM_LIVE_TRADING "
        "is not 'yes'. Refusing to start to avoid accidentally placing live orders. "
        "Set CONFIRM_LIVE_TRADING=yes in .env only if you deliberately intend to "
        "trade with real money."
    )

# Equity symbols trade through the stock API and respect market hours.
EQUITY_SYMBOLS = ["SPY", "QQQ", "GLD", "USO"]
# Crypto symbols trade through the crypto API, 24/7, no market-hours gate.
CRYPTO_SYMBOLS = ["BTC/USD"]
SYMBOLS = EQUITY_SYMBOLS + CRYPTO_SYMBOLS

# Which strategy drives each symbol. Broad, liquid index ETFs trend well;
# range-bound commodities mean-revert; BTC/USD gets a breakout strategy to
# capture its higher volatility regime.
STRATEGY_MAP = {
    "SPY": "trend_following",
    "QQQ": "trend_following",
    "GLD": "mean_reversion",
    "USO": "mean_reversion",
    "BTC/USD": "momentum_breakout",
}

# --- Strategy parameters ---
TREND_FAST_SMA = 20
TREND_SLOW_SMA = 50

MEAN_REVERSION_RSI_PERIOD = 14
# Backtested 2026-07-14 over a 6-month window: RSI 20/80 beat the original
# 30/70 for both GLD (Sharpe -0.48 -> +0.71) and USO (Sharpe 5.33 -> 7.17)
# with no tradeoff between the two -- see bot/backtest.py.
MEAN_REVERSION_OVERSOLD = 20
MEAN_REVERSION_OVERBOUGHT = 80

# Backtested 2026-07-14: 20-bar (5hr) channel on BTC/USD's 15-min bars was
# pure noise -- 180 trades, 26% win rate, Sharpe -3.95. 80-bar is the local
# optimum across the full range tested (20-250): Sharpe improves to -0.94,
# max drawdown 12.8% -> 4.2%. Still net negative -- longer lookback reduces
# whipsaw losses but can't fix a long-only strategy trading through a real
# downtrend. See bot/backtest.py.
BREAKOUT_LOOKBACK = 80
# Swept lookback x trend-filter-period jointly (2026-07-14): (80, 150) is the
# best combination found, Sharpe -0.94 -> -0.75. The filter barely narrows
# trade count (50 -> 48) since an 80-bar breakout is usually already above a
# 100-250-bar trend SMA by the time it fires -- breakout and trend correlate
# at this lookback, so the filter can't add much selectivity. Still net
# negative; see bot/backtest.py and the conversation this was tuned in.
BREAKOUT_TREND_FILTER_PERIOD = 150

# --- Data / loop settings ---
BAR_TIMEFRAME_MINUTES = 15
LOOKBACK_BARS = 200  # bars of history fetched per decision cycle
POLL_INTERVAL_SECONDS = 300  # how often the main loop re-evaluates each symbol

# --- Risk management ---
# Equal fixed-fractional allocation across the 5 instruments by default.
MAX_ALLOCATION_PCT_PER_SYMBOL = 0.20  # cap on (position value / account equity)
STOP_LOSS_PCT = 0.05  # exit if price falls 5% below entry
TAKE_PROFIT_PCT = 0.10  # exit if price rises 10% above entry
MAX_TOTAL_EXPOSURE_PCT = 0.90  # cap on (sum of all positions / account equity)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
