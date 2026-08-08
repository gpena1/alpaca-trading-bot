"""Parameters for the cross-sectional momentum strategy.

Deliberately kept in its own file rather than merged into config.py: this
is a separate strategy with a separate backtest, and keeping it isolated
means it can be evaluated, changed, or thrown away without touching the
live per-symbol bot.

Design note on knob count. The per-symbol strategies got into trouble
because parameters were swept until the in-sample numbers looked good.
This file has deliberately few: three momentum lookbacks, a holding
count, a rebalance cadence, and a regime filter. No inverse-volatility
weighting, no per-position stops, no skip-month. Every knob added here is
another dimension to overfit, and the walk-forward split is the only
thing standing between you and a repeat of RSI 20/80.
"""

# Liquid, long-history ETFs across sectors, regions, and asset classes.
# The point of a wide universe is that ranking becomes meaningful --
# ranking SPY against QQQ is nearly a coin flip because they correlate
# ~0.95, which is exactly why the current two-symbol trend sleeve is
# really one bet rather than two.
UNIVERSE = [
    # Broad equity
    "SPY", "QQQ", "IWM", "EFA", "EEM", "VGK", "EWJ",
    # US sectors
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLY", "XLB",
    # Industry / thematic
    "SMH", "XBI", "KRE", "ITB", "VNQ",
    # Commodities and metals
    "GLD", "SLV", "DBC", "USO",
    # Fixed income
    "TLT", "IEF", "LQD", "HYG",
]

# Momentum is scored as the average total return across these lookbacks,
# in trading days: roughly 3, 6, and 12 months. Averaging several
# horizons is more robust than picking one -- a single lookback that
# happens to fit the sample is the classic overfit.
MOMENTUM_LOOKBACKS = [63, 126, 252]

# How many names to hold. Fewer = more concentrated = more aggressive,
# and this is the natural aggression dial for this strategy, far more so
# than loosening an entry threshold.
TOP_N = 5

# Equal weight across holdings, scaled so the book never exceeds this.
TARGET_GROSS_EXPOSURE = 0.90

# Rebalance cadence in trading days. 21 ~= monthly. Daily rebalancing on
# a momentum rank produces enormous turnover for very little signal.
REBALANCE_EVERY_N_BARS = 21

# Regime filter: only hold anything while the market proxy is above its
# long moving average. This is the single cheapest protection an
# aggressive long book can have -- concentrated momentum portfolios do
# most of their damage in sustained declines.
REGIME_SYMBOL = "SPY"
REGIME_SMA_PERIOD = 200
USE_REGIME_FILTER = True

# Absolute-momentum gate (dual momentum): a name must have a positive
# momentum score in its own right to be held, not merely rank highly
# among a set of falling assets.
REQUIRE_POSITIVE_MOMENTUM = True

# Backtest settings
XS_BACKTEST_YEARS = 10
XS_STARTING_CASH = 100_000.0
XS_SLIPPAGE_PCT = 0.0005
TRADING_DAYS_PER_YEAR = 252
