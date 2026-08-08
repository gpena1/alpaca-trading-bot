"""Strategy B -- Volatility-Expansion Breakout.

Thesis: volatility is mean-reverting even when price is not. Quiet
periods precede violent ones. So instead of buying any new high, wait for
the range to COMPRESS into the bottom of its own recent distribution,
then take the direction it breaks.

FAILS IN: false breakouts and gap reversals. Price escapes the squeeze,
triggers the entry, then immediately reverses. That is a different
failure from Strategy A's chop -- A loses when there is no direction at
all, B loses when direction appears and then lies.

Why this replaces the old momentum_breakout rather than tuning it. The
original bought any 80-bar Donchian high with a trend filter, and lost
money at every lookback from 20 to 250. Two structural defects, neither
fixable by parameters:

  1. LONG-ONLY. Its own docstring conceded a long-only breakout cannot
     profit through a sustained decline, only avoid buying into one. Here
     the short side is symmetric.
  2. NO SELECTIVITY. Every new high is a breakout, and most new highs go
     nowhere. The squeeze condition is the selectivity the original never
     had -- it demands that the breakout emerge from unusual quiet, which
     is a genuinely different (and far rarer) setup.

Entries use CLOSES, not intraday extremes. On the free IEX feed, highs
and lows come from ~2% of consolidated volume and are the values most
corrupted by the partial tape. ATR still uses them, but only to set a
stop distance, never to fire a signal.

Sizing is inverse to entry volatility, so the biggest positions go on
when the setup is quietest -- which is the whole point of the thesis.
"""

from __future__ import annotations

import pandas as pd

import config_aggressive as cfg
from bot.strategies.sizing import AggressiveSignal, Target, atr, vol_targeted_weight


class VolatilitySqueezeBreakout:
    name = "squeeze_breakout"
    symbols = cfg.BREAKOUT_SYMBOLS

    def __init__(self, aggression: float = cfg.AGGRESSION):
        self.aggression = aggression
        self.range_period = cfg.SQUEEZE_RANGE_PERIOD
        self.percentile = cfg.SQUEEZE_PERCENTILE
        self.history = cfg.SQUEEZE_HISTORY

    @property
    def min_bars(self) -> int:
        return self.history + self.range_period + 2

    def _range_series(self, close: pd.Series) -> pd.Series:
        """Rolling close-to-close range, normalised by price so it's
        comparable across time and instruments."""
        rolling_max = close.rolling(self.range_period).max()
        rolling_min = close.rolling(self.range_period).min()
        return (rolling_max - rolling_min) / close

    def generate(self, bars: pd.DataFrame) -> AggressiveSignal:
        if len(bars) < self.min_bars:
            return AggressiveSignal(Target.HOLD, f"need {self.min_bars} bars, have {len(bars)}")

        close = bars["close"]
        # The squeeze must be measured on the bars BEFORE the potential
        # breakout. Including the current bar is self-defeating: a genuine
        # break widens the range, so the range test would always fail at
        # exactly the moment it needs to pass. (The first smoke test
        # caught this -- the strategy could never fire.)
        prior_closes = close.iloc[:-1]
        ranges = self._range_series(prior_closes).dropna()
        if len(ranges) < self.history:
            return AggressiveSignal(Target.HOLD, "insufficient range history")

        current_range = float(ranges.iloc[-1])
        threshold = float(ranges.iloc[-self.history:].quantile(self.percentile))
        in_squeeze = current_range <= threshold

        # The channel is measured on the bars BEFORE the current one, so
        # we never test a value against a window containing it.
        prior = close.iloc[-(self.range_period + 1):-1]
        channel_high, channel_low = float(prior.max()), float(prior.min())
        current_close = float(close.iloc[-1])

        broke_up = current_close > channel_high
        broke_down = current_close < channel_low

        if not (broke_up or broke_down):
            return AggressiveSignal(
                Target.FLAT,
                f"inside {self.range_period}-bar range"
                + (" (squeezed, waiting)" if in_squeeze else ""),
            )

        if not in_squeeze:
            return AggressiveSignal(
                Target.FLAT,
                f"break ignored: range {current_range:.3f} above squeeze threshold {threshold:.3f}",
            )

        weight = vol_targeted_weight(close, aggression=self.aggression)
        if weight <= 0:
            return AggressiveSignal(Target.HOLD, "volatility unmeasurable -- not sizing")

        atr_value = atr(bars)
        stop_distance = atr_value * cfg.BREAKOUT_ATR_STOP_MULT if atr_value else None

        direction = Target.LONG if broke_up else Target.SHORT
        edge = channel_high if broke_up else channel_low
        return AggressiveSignal(
            direction,
            f"squeeze break {'up' if broke_up else 'down'}: close {current_close:.2f} "
            f"vs {edge:.2f}, range {current_range:.3f} <= {threshold:.3f}, weight {weight:.0%}",
            weight,
            stop_distance,
        )
