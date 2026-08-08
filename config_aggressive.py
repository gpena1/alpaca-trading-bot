"""Parameters for the three aggressive strategies.

Isolated from config.py on purpose: these are experimental and must not
be reachable from the live bot path until the harness says they earn it.
Same pattern as config_xs.py.

WHAT THE LAST TWO ROUNDS TAUGHT US, encoded here:

1. Aggression belongs in SIZING, not in entry thresholds. Loosening an
   entry just takes worse trades. Volatility targeting makes aggression a
   single dial with a known unit: "how much annualised volatility am I
   willing to run".
2. Fixed take-profits destroy trend and breakout systems. Only the
   reversion strategy gets an exit target, because a bounded move is
   literally its thesis.
3. Knobs are how you overfit. Each strategy below has three or four
   parameters, not a dozen, and the aggression dial is shared rather than
   re-tuned per strategy.
4. Benchmark against SPY TOTAL RETURN. The price-only comparison made a
   losing strategy look like a winner by 14 points.
5. The regime filter cost money over 2020-2026, a window with one bear
   market. It is available but defaults OFF, and any claim about it needs
   a sample containing more than one sustained decline.
"""

# --- Shared aggression dial ---------------------------------------------
# PER-POSITION annualised volatility budget. This is NOT the portfolio's
# target volatility -- it is how much each individual position is allowed
# to contribute. With ~10-15 positions open, a 2.5% per-position budget
# builds to a portfolio running roughly 8-12% annualised at 1.0x.
#
# This number was wrong on the first pass (0.30) and the smoke test
# caught it: at that budget every ETF's raw weight exceeded the position
# cap, so the cap bound at every aggression level and 1x, 2x and 3x
# produced identical books. A dial that does nothing is worse than no
# dial, because it looks like evidence that aggression doesn't matter.
TARGET_ANNUAL_VOL = 0.025

# Multiplier applied on top of the vol budget. This is the single number
# the backtest sweeps. 1.0 = as configured above.
AGGRESSION = 1.0

# Hard ceiling on any one position, regardless of how quiet it is.
# Binds for genuinely low-volatility instruments (IEF, LQD), which is
# exactly when it should -- without it, vol targeting sizes a 5%-vol bond
# ETF into absurd leverage.
MAX_WEIGHT_PER_POSITION = 0.35

# Gross exposure scales WITH aggression -- that is what being aggressive
# means at the book level -- up to a hard ceiling that no setting passes.
BASE_GROSS_EXPOSURE = 1.0
ABSOLUTE_MAX_GROSS = 3.0

# Realised volatility lookback, in trading days.
VOL_LOOKBACK = 20
# Below this, treat vol as unmeasurable and skip rather than size huge.
MIN_ANNUAL_VOL = 0.03

# --- Portfolio kill switch ----------------------------------------------
# Aggressive systems do not fail slowly. If equity falls this far below
# its running peak, flatten everything and stop opening new positions for
# COOLDOWN_BARS.
MAX_PORTFOLIO_DRAWDOWN = 0.25
KILL_SWITCH_COOLDOWN_BARS = 21

# --- Optional regime filter ---------------------------------------------
USE_REGIME_FILTER = False
REGIME_SYMBOL = "SPY"
REGIME_SMA_PERIOD = 200

# --- Strategy A: aggressive trend ---------------------------------------
# Same thesis as the old trend_following, but state-based, long/short, no
# take-profit, and vol-targeted. Fails in chop.
TREND_FAST = 20
TREND_SLOW = 100
TREND_ATR_STOP_MULT = 4.0  # wide trailing stop; trends need room

# --- Strategy B: volatility-expansion breakout --------------------------
# NOT the old Donchian channel. Enters when volatility CONTRACTS and then
# price breaks the range -- compression precedes expansion. Sized inversely
# to entry volatility, so the biggest positions go on when the setup is
# quietest. Fails on false breakouts and gap reversals.
SQUEEZE_RANGE_PERIOD = 20  # range window that must be narrow
SQUEEZE_PERCENTILE = 0.30  # range must be in the bottom 30% of its history
SQUEEZE_HISTORY = 120  # window the percentile is measured against
BREAKOUT_ATR_STOP_MULT = 2.5

# --- Strategy C: stretch reversion --------------------------------------
# NOT RSI thresholds. Z-score of price against its own mean, with position
# size SCALING with the size of the stretch, and a hard time stop so a
# failed reversion cannot become an open-ended loss. Fails when a stretch
# turns into a trend -- the exact opposite failure mode of Strategy A.
ZSCORE_PERIOD = 20
ZSCORE_ENTRY = 2.0  # standard deviations from the mean to enter
ZSCORE_EXIT = 0.5  # exit once price is back near the mean
ZSCORE_MAX_SCALE = 2.0  # cap on how much a deeper stretch scales the size
REVERSION_TIME_STOP_BARS = 15  # exit regardless after this many bars
REVERSION_TAKE_PROFIT = 0.08  # the one strategy where a target is coherent

# --- Universe assignment ------------------------------------------------
# Each strategy runs on instruments suited to its thesis. All shortable
# ETFs; crypto is excluded entirely -- Alpaca can't short it, and a
# long-only sleeve inside an aggressive book is a known dead end.
TREND_SYMBOLS = ["SPY", "QQQ", "IWM", "EFA", "TLT"]
BREAKOUT_SYMBOLS = ["XLE", "XLK", "SMH", "GLD", "SLV"]
REVERSION_SYMBOLS = ["XLU", "XLP", "IEF", "LQD", "USO"]

ALL_SYMBOLS = sorted(set(TREND_SYMBOLS + BREAKOUT_SYMBOLS + REVERSION_SYMBOLS + ["SPY"]))

# --- Backtest -----------------------------------------------------------
BACKTEST_YEARS = 10  # Alpaca's free plan will cap this; report what came back
STARTING_CASH = 100_000.0
SLIPPAGE_PCT = 0.0005
TRADING_DAYS_PER_YEAR = 252
AGGRESSION_SWEEP = [1.0, 1.5, 2.0]
