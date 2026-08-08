"""Channel breakout strategy, state-based and long/short where permitted.

LONG while the last close is above the highest close of the prior N bars
and above the longer trend average. SHORT on a break below the lowest
close of the prior N bars (FLAT instead for symbols that can't be shorted,
i.e. crypto on Alpaca).

Two changes from the original:

1. Channel measured on CLOSES, not intraday highs/lows. On the free IEX
   feed, a bar's high and low come from ~2% of consolidated volume, so
   extremes are precisely the values most corrupted by the partial tape --
   channel boundaries came out systematically too narrow and fired false
   breakouts. Closes are far more stable under sampling. On daily bars
   this matters less than it did at 15 minutes, but closes remain the
   better input.

2. Short side enabled for equities. The original's own docstring
   identified the core defect: a long-only breakout strategy structurally
   cannot profit through a sustained decline, only avoid buying into one.
   That is why widening the lookback moved Sharpe from -3.95 to -0.75 and
   no further -- the parameter was never the binding constraint.

   BTC/USD stays long-or-flat because Alpaca does not support shorting
   crypto. That is a venue limitation, not a strategy choice, and it means
   the crypto sleeve retains the original structural weakness.
"""

import pandas as pd

import config
from bot.strategies.base import Signal, Strategy, TargetPosition


class MomentumBreakoutStrategy(Strategy):
    name = "momentum_breakout"

    def __init__(
        self,
        allow_short: bool = False,
        lookback: int = config.BREAKOUT_LOOKBACK,
        trend_filter_period: int = config.BREAKOUT_TREND_FILTER_PERIOD,
    ):
        super().__init__(allow_short=allow_short)
        self.lookback = lookback
        self.trend_filter_period = trend_filter_period

    def generate_signal(self, bars: pd.DataFrame) -> Signal:
        min_bars = max(self.lookback, self.trend_filter_period) + 1
        if len(bars) < min_bars:
            return Signal(
                TargetPosition.HOLD,
                f"insufficient history: {len(bars)} bars < {min_bars}",
            )

        close = bars["close"]
        # Exclude the current bar from the channel so we compare against the
        # prior N bars, not a window containing the value being tested.
        prior_closes = close.iloc[-(self.lookback + 1):-1]
        channel_high = prior_closes.max()
        channel_low = prior_closes.min()
        current_close = float(close.iloc[-1])
        trend_sma = close.rolling(self.trend_filter_period).mean().iloc[-1]

        if pd.isna(trend_sma):
            return Signal(TargetPosition.HOLD, "trend filter not yet defined")

        if current_close > channel_high:
            if current_close > trend_sma:
                return Signal(
                    TargetPosition.LONG,
                    f"close {current_close:.2f} above {self.lookback}-bar high close "
                    f"{channel_high:.2f}, above {self.trend_filter_period}-bar trend {trend_sma:.2f}",
                )
            return Signal(
                TargetPosition.FLAT,
                f"upside break filtered: {current_close:.2f} below trend {trend_sma:.2f}",
            )

        if current_close < channel_low:
            if current_close < trend_sma:
                return self._short_or_flat(
                    f"close {current_close:.2f} below {self.lookback}-bar low close "
                    f"{channel_low:.2f}, below {self.trend_filter_period}-bar trend {trend_sma:.2f}"
                )
            return Signal(
                TargetPosition.FLAT,
                f"downside break filtered: {current_close:.2f} above trend {trend_sma:.2f}",
            )

        return Signal(TargetPosition.HOLD, f"close {current_close:.2f} inside channel")
