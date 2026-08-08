"""RSI mean-reversion strategy, state-based and long/short.

LONG when RSI is oversold, SHORT when overbought, FLAT once RSI returns to
the midline band, HOLD in between.

The exit band matters. The original held a long from oversold all the way
until RSI became *overbought* -- i.e. it refused to take a mean-reversion
profit until the move had fully reversed into the opposite extreme, which
frequently never happened. Exiting near the midline is what the strategy's
own thesis implies: the bet is on reversion to the mean, so the mean is
where the bet resolves.

Unlike the trend and breakout strategies, this one keeps a take-profit
(config.TAKE_PROFIT_PCT_BY_STRATEGY) -- mean reversion is explicitly a
bounded-move bet, so a fixed target is coherent here.
"""

import pandas as pd

import config
from bot.strategies.base import Signal, Strategy, TargetPosition


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)  # neutral when no losses/gains yet


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"

    def __init__(
        self,
        allow_short: bool = False,
        period: int = config.MEAN_REVERSION_RSI_PERIOD,
        oversold: float = config.MEAN_REVERSION_OVERSOLD,
        overbought: float = config.MEAN_REVERSION_OVERBOUGHT,
        exit_low: float = config.MEAN_REVERSION_EXIT_LOW,
        exit_high: float = config.MEAN_REVERSION_EXIT_HIGH,
    ):
        super().__init__(allow_short=allow_short)
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        self.exit_low = exit_low
        self.exit_high = exit_high

    def generate_signal(self, bars: pd.DataFrame) -> Signal:
        if len(bars) < self.period + 1:
            return Signal(
                TargetPosition.HOLD,
                f"insufficient history: {len(bars)} bars < {self.period + 1}",
            )

        rsi = _rsi(bars["close"], self.period).iloc[-1]

        if rsi < self.oversold:
            return Signal(TargetPosition.LONG, f"RSI={rsi:.1f} below oversold {self.oversold}")
        if rsi > self.overbought:
            return self._short_or_flat(f"RSI={rsi:.1f} above overbought {self.overbought}")
        if self.exit_low <= rsi <= self.exit_high:
            return Signal(TargetPosition.FLAT, f"RSI={rsi:.1f} back at midline -- reversion complete")
        return Signal(TargetPosition.HOLD, f"RSI={rsi:.1f} between entry and exit bands")
