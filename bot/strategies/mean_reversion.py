"""RSI-based mean-reversion strategy.

BUY when RSI drops into oversold territory (expecting a bounce).
SELL when RSI rises into overbought territory (expecting a pullback).
Used for range-bound commodity ETFs (GLD, USO).
"""

import pandas as pd

import config
from bot.strategies.base import Signal, SignalAction, Strategy


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
        period: int = config.MEAN_REVERSION_RSI_PERIOD,
        oversold: float = config.MEAN_REVERSION_OVERSOLD,
        overbought: float = config.MEAN_REVERSION_OVERBOUGHT,
    ):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def generate_signal(self, bars: pd.DataFrame) -> Signal:
        if len(bars) < self.period + 1:
            return Signal(SignalAction.HOLD, "insufficient history for RSI")

        rsi = _rsi(bars["close"], self.period)
        current_rsi = rsi.iloc[-1]

        if current_rsi < self.oversold:
            return Signal(SignalAction.BUY, f"RSI={current_rsi:.1f} below oversold threshold {self.oversold}")
        if current_rsi > self.overbought:
            return Signal(SignalAction.SELL, f"RSI={current_rsi:.1f} above overbought threshold {self.overbought}")
        return Signal(SignalAction.HOLD, f"RSI={current_rsi:.1f} within neutral range")
