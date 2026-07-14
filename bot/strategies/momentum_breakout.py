"""Donchian channel breakout strategy.

BUY when price closes above the highest high of the lookback window
(new high momentum) AND price is above a longer-term trend average. SELL
when price closes below the lowest low of the lookback window (new low
momentum / breakdown) -- this exit is never trend-filtered, since it only
ever closes an existing long (RiskManager never shorts) and is protective
regardless of trend. Used for BTC/USD, which tends to move in strong
directional bursts rather than mean-revert.

The trend filter exists because backtesting (2026-07-14, 6-month window)
showed the un-filtered version losing badly during a real downtrend --
Sharpe -3.95 at the original 20-bar lookback, still -0.94 even after
widening the lookback to 80. A long-only breakout strategy structurally
can't profit through a sustained decline; it can only avoid buying into
one. See bot/backtest.py.
"""

import pandas as pd

import config
from bot.strategies.base import Signal, SignalAction, Strategy


class MomentumBreakoutStrategy(Strategy):
    name = "momentum_breakout"

    def __init__(
        self,
        lookback: int = config.BREAKOUT_LOOKBACK,
        trend_filter_period: int = config.BREAKOUT_TREND_FILTER_PERIOD,
    ):
        self.lookback = lookback
        self.trend_filter_period = trend_filter_period

    def generate_signal(self, bars: pd.DataFrame) -> Signal:
        min_bars = max(self.lookback, self.trend_filter_period) + 1
        if len(bars) < min_bars:
            return Signal(SignalAction.HOLD, "insufficient history for breakout channel")

        # Exclude the current (not-yet-closed-relative-to-decision) bar from
        # the channel so we're comparing against the prior N bars, not
        # including today's own high/low.
        prior = bars.iloc[-(self.lookback + 1):-1]
        channel_high = prior["high"].max()
        channel_low = prior["low"].min()
        current_close = bars["close"].iloc[-1]
        trend_sma = bars["close"].rolling(self.trend_filter_period).mean().iloc[-1]

        if current_close > channel_high:
            if current_close > trend_sma:
                return Signal(
                    SignalAction.BUY,
                    f"close {current_close:.2f} broke above {self.lookback}-bar high {channel_high:.2f}, "
                    f"above {self.trend_filter_period}-bar trend {trend_sma:.2f}",
                )
            return Signal(
                SignalAction.HOLD,
                f"breakout above {channel_high:.2f} filtered: below {self.trend_filter_period}-bar trend {trend_sma:.2f}",
            )
        if current_close < channel_low:
            return Signal(
                SignalAction.SELL,
                f"close {current_close:.2f} broke below {self.lookback}-bar low {channel_low:.2f}",
            )
        return Signal(SignalAction.HOLD, "price within breakout channel")
