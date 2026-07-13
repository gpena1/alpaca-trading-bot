"""Donchian channel breakout strategy.

BUY when price closes above the highest high of the lookback window
(new high momentum). SELL when price closes below the lowest low of the
lookback window (new low momentum / breakdown). Used for BTC/USD, which
tends to move in strong directional bursts rather than mean-revert.
"""

import pandas as pd

import config
from bot.strategies.base import Signal, SignalAction, Strategy


class MomentumBreakoutStrategy(Strategy):
    name = "momentum_breakout"

    def __init__(self, lookback: int = config.BREAKOUT_LOOKBACK):
        self.lookback = lookback

    def generate_signal(self, bars: pd.DataFrame) -> Signal:
        if len(bars) < self.lookback + 1:
            return Signal(SignalAction.HOLD, "insufficient history for breakout channel")

        # Exclude the current (not-yet-closed-relative-to-decision) bar from
        # the channel so we're comparing against the prior N bars, not
        # including today's own high/low.
        prior = bars.iloc[-(self.lookback + 1):-1]
        channel_high = prior["high"].max()
        channel_low = prior["low"].min()
        current_close = bars["close"].iloc[-1]

        if current_close > channel_high:
            return Signal(
                SignalAction.BUY,
                f"close {current_close:.2f} broke above {self.lookback}-bar high {channel_high:.2f}",
            )
        if current_close < channel_low:
            return Signal(
                SignalAction.SELL,
                f"close {current_close:.2f} broke below {self.lookback}-bar low {channel_low:.2f}",
            )
        return Signal(SignalAction.HOLD, "price within breakout channel")
