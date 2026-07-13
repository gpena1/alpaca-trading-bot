"""SMA crossover trend-following strategy.

BUY when the fast SMA crosses above the slow SMA (golden cross).
SELL when the fast SMA crosses below the slow SMA (death cross).
Used for broad, liquid index ETFs (SPY, QQQ) that tend to trend.
"""

import pandas as pd

import config
from bot.strategies.base import Signal, SignalAction, Strategy


class TrendFollowingStrategy(Strategy):
    name = "trend_following"

    def __init__(
        self,
        fast_period: int = config.TREND_FAST_SMA,
        slow_period: int = config.TREND_SLOW_SMA,
    ):
        self.fast_period = fast_period
        self.slow_period = slow_period

    def generate_signal(self, bars: pd.DataFrame) -> Signal:
        if len(bars) < self.slow_period + 1:
            return Signal(SignalAction.HOLD, "insufficient history for slow SMA")

        close = bars["close"]
        fast = close.rolling(self.fast_period).mean()
        slow = close.rolling(self.slow_period).mean()

        prev_fast, prev_slow = fast.iloc[-2], slow.iloc[-2]
        curr_fast, curr_slow = fast.iloc[-1], slow.iloc[-1]

        crossed_up = prev_fast <= prev_slow and curr_fast > curr_slow
        crossed_down = prev_fast >= prev_slow and curr_fast < curr_slow

        if crossed_up:
            return Signal(
                SignalAction.BUY,
                f"fast SMA({self.fast_period}) crossed above slow SMA({self.slow_period})",
            )
        if crossed_down:
            return Signal(
                SignalAction.SELL,
                f"fast SMA({self.fast_period}) crossed below slow SMA({self.slow_period})",
            )
        return Signal(SignalAction.HOLD, "no crossover")
