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

# --- Shorting ---
# Alpaca supports shorting equities/ETFs but NOT crypto. Any symbol not
# listed here is long-or-flat: a SHORT target degrades to FLAT rather than
# silently failing at order submission.
SHORTABLE_SYMBOLS = set(EQUITY_SYMBOLS)
ALLOW_SHORTING = True

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

# --- Data / loop settings ---
# DAILY bars. Rationale (2026-08): the free IEX feed carries ~2% of
# consolidated volume, so 15-minute bars were being built from a thin,
# unrepresentative sample -- worst of all for the breakout strategy, which
# reads highs and lows (the exact values a 2% sample misses). Daily bars
# average that sampling error away, hold positions across days instead of
# hours, and let the bot run as one scheduled job per day instead of a
# 24/7 polling loop.
BAR_TIMEFRAME = "day"
LOOKBACK_BARS = 300  # ~14 months of trading days per decision cycle
# Only used when running the legacy forever-loop. The supported mode is a
# once-a-day scheduled invocation of `python3 -m bot.main --once`.
POLL_INTERVAL_SECONDS = 86_400

# --- Strategy parameters ---
# All periods are now in DAYS, not 15-minute bars.
TREND_FAST_SMA = 20   # ~1 month
TREND_SLOW_SMA = 50   # ~2.5 months

MEAN_REVERSION_RSI_PERIOD = 14
# Reverted to the conventional 30/70. The previous 20/80 was selected by
# sweeping thresholds over the same 6-month window the result was measured
# on -- in-sample fitting, not an edge -- and it was fit to 15-minute noise
# that no longer exists on daily bars. Let walk-forward testing pick the
# real values.
MEAN_REVERSION_OVERSOLD = 30
MEAN_REVERSION_OVERBOUGHT = 70
# Exit band: close the position when RSI returns toward the midline, rather
# than holding until the opposite extreme.
MEAN_REVERSION_EXIT_LOW = 45
MEAN_REVERSION_EXIT_HIGH = 55

# Donchian-style channel, now measured on CLOSES rather than intraday
# highs/lows -- closes are far more reliable than extremes on a partial
# tape. 20/100 days are the conventional turtle-style values; the old
# 80/150 were 15-minute-bar counts and meant something entirely different.
BREAKOUT_LOOKBACK = 20
BREAKOUT_TREND_FILTER_PERIOD = 100

# --- Risk management ---
MAX_ALLOCATION_PCT_PER_SYMBOL = 0.20  # cap on (|position value| / account equity)
MAX_TOTAL_EXPOSURE_PCT = 0.90  # cap on (sum of |position values| / account equity)

# Stops widened for daily bars: a 5% stop sized for intraday moves gets hit
# by ordinary daily noise when the intended hold is weeks.
STOP_LOSS_PCT = 0.10

# Take-profit is now PER STRATEGY. A fixed take-profit is actively harmful
# to trend and breakout systems -- both earn their return from a small
# number of very large winners, and capping every winner at +10% while
# letting losers run to the stop inverts the payoff. Mean reversion is the
# one strategy where a fixed target is coherent: it is explicitly betting
# on a bounded move back to the mean.
TAKE_PROFIT_PCT_BY_STRATEGY = {
    "trend_following": None,
    "momentum_breakout": None,
    "mean_reversion": 0.08,
}

# Protective stops are submitted to Alpaca as native GTC stop orders at
# entry, so they are live between daily runs rather than only being checked
# when the bot happens to wake up. The in-process check remains as a
# backstop for crypto and for any stop that fails to register.
USE_NATIVE_STOP_ORDERS = True

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
