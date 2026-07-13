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
MEAN_REVERSION_OVERSOLD = 30
MEAN_REVERSION_OVERBOUGHT = 70

BREAKOUT_LOOKBACK = 20  # Donchian channel window

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
