"""SMA trend-following strategy, state-based and long/short.

LONG while the fast SMA is above the slow SMA.
SHORT while it is below (or FLAT if the symbol can't be shorted).

Two changes from the original:

1. State, not event. The old version emitted BUY only on the single bar
   where the fast SMA crossed above the slow. Miss that bar for any
   reason -- stop-out, exposure cap, market closed, restart -- and the
   position could not be re-established until the next full crossover,
   potentially months later. This version answers "what should I be
   holding right now", which is recoverable on every cycle.

2. Short side enabled. A long-only trend system is flat during every
   downtrend, so half of every trend the strategy correctly identifies is
   unmonetisable.
"""

import pandas as pd

import config
from bot.strategies.base import Signal, Strategy, TargetPosition


class TrendFollowingStrategy(Strategy):
    name = "trend_following"

    def __init__(
        self,
        allow_short: bool = False,
        fast_period: int = config.TREND_FAST_SMA,
        slow_period: int = config.TREND_SLOW_SMA,
    ):
        super().__init__(allow_short=allow_short)
        self.fast_period = fast_period
        self.slow_period = slow_period

    def generate_signal(self, bars: pd.DataFrame) -> Signal:
        if len(bars) < self.slow_period + 1:
            return Signal(
                TargetPosition.HOLD,
                f"insufficient history: {len(bars)} bars < {self.slow_period + 1}",
            )

        close = bars["close"]
        fast = close.rolling(self.fast_period).mean().iloc[-1]
        slow = close.rolling(self.slow_period).mean().iloc[-1]

        if pd.isna(fast) or pd.isna(slow):
            return Signal(TargetPosition.HOLD, "SMA not yet defined")

        spread_pct = (fast - slow) / slow * 100

        if fast > slow:
            return Signal(
                TargetPosition.LONG,
                f"SMA({self.fast_period})={fast:.2f} above SMA({self.slow_period})={slow:.2f} "
                f"({spread_pct:+.2f}%)",
            )
        if fast < slow:
            return self._short_or_flat(
                f"SMA({self.fast_period})={fast:.2f} below SMA({self.slow_period})={slow:.2f} "
                f"({spread_pct:+.2f}%)"
            )
        return Signal(TargetPosition.HOLD, "SMAs equal")
