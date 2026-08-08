"""Strategy A -- Aggressive Trend.

Thesis: hold the direction the market is already going, sized by
volatility rather than by a flat percentage.

FAILS IN: chop. When price oscillates around the moving averages, this
whipsaws -- entering long near local highs and short near local lows.
That is its signature weakness and it is different from B and C.

Inherited from the audit of the original trend_following:
  - STATE, not events. The old version emitted BUY only on the exact bar
    of a crossover; miss it and the position was unrecoverable until the
    next cross, possibly months later.
  - NO TAKE-PROFIT. Trend systems earn from a few very large winners.
    Capping every winner at +10% while letting losers run to the stop
    inverts the payoff, and that single setting is the likeliest reason
    the original returned ~2% out-of-sample.
  - SHORTS ENABLED. A long-only trend system is structurally flat through
    every downtrend it correctly identifies.
  - Slow MA widened 50 -> 100 days. On daily bars, 20/50 flips often
    enough to bleed on turnover; the wider pair takes fewer, larger
    positions, which is what "aggressive" should mean here.
"""

from __future__ import annotations

import pandas as pd

import config_aggressive as cfg
from bot.strategies.sizing import AggressiveSignal, Target, atr, vol_targeted_weight


class AggressiveTrend:
    name = "aggressive_trend"
    symbols = cfg.TREND_SYMBOLS

    def __init__(self, aggression: float = cfg.AGGRESSION):
        self.aggression = aggression
        self.fast = cfg.TREND_FAST
        self.slow = cfg.TREND_SLOW

    @property
    def min_bars(self) -> int:
        return self.slow + cfg.VOL_LOOKBACK + 2

    def generate(self, bars: pd.DataFrame) -> AggressiveSignal:
        if len(bars) < self.min_bars:
            return AggressiveSignal(Target.HOLD, f"need {self.min_bars} bars, have {len(bars)}")

        close = bars["close"]
        fast = close.rolling(self.fast).mean().iloc[-1]
        slow = close.rolling(self.slow).mean().iloc[-1]
        if pd.isna(fast) or pd.isna(slow):
            return AggressiveSignal(Target.HOLD, "moving averages not yet defined")

        weight = vol_targeted_weight(close, aggression=self.aggression)
        if weight <= 0:
            return AggressiveSignal(Target.HOLD, "volatility unmeasurable -- not sizing")

        atr_value = atr(bars)
        stop_distance = atr_value * cfg.TREND_ATR_STOP_MULT if atr_value else None
        spread = (fast - slow) / slow * 100

        if fast > slow:
            return AggressiveSignal(
                Target.LONG,
                f"trend up: MA{self.fast} {spread:+.2f}% over MA{self.slow}, weight {weight:.0%}",
                weight,
                stop_distance,
            )
        return AggressiveSignal(
            Target.SHORT,
            f"trend down: MA{self.fast} {spread:+.2f}% under MA{self.slow}, weight {weight:.0%}",
            weight,
            stop_distance,
        )
