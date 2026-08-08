"""Carver-style multi-speed trend with CONTINUOUS forecasts.

This is architecturally different from every strategy built so far, in
two ways that matter:

1. CONTINUOUS, NOT BINARY. Everything up to now emitted long / short /
   flat at full size. A barely-positive trend and an overwhelming one
   produced the same position. Here the forecast is a number, and
   position size is proportional to it -- weak signal, small position.
   That alone changes the return distribution: you are no longer betting
   the same amount on your best and worst ideas.

2. MULTI-SPEED. Instead of picking one lookback pair (which is a
   parameter waiting to be overfit -- see the old 20/50, then 20/100),
   it runs several simultaneously and averages them. A fast pair catches
   short swings, a slow pair rides long ones. Averaging across speeds is
   diversification across time horizons, and it removes the "which
   lookback?" question rather than answering it.

Source: Rob Carver, "Systematic Trading" / pysystemtrade. The forecast
scalars below are the published values from that work, NOT fitted here.
That matters: fitting scalars to this sample is exactly the mistake that
produced RSI 20/80.

HONEST LIMITATION, stated up front. Carver's system is designed for 40+
futures markets across equities, bonds, FX, commodities and volatility.
The diversification across instruments is a large part of where its
return comes from. Running it on five equity ETFs is a diminished
version of the idea, and should be expected to perform worse than the
published results for the full system. That is a limitation of what
Alpaca gives us access to, not of the method.

FAILS IN: sustained choppy markets, same as any trend system -- but it
degrades more gracefully, because a weak signal produces a small
position rather than a full one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config_aggressive as cfg
from bot.strategies.sizing import AggressiveSignal, Target, annualised_vol, atr

# (fast, slow) EWMA pairs with their PUBLISHED forecast scalars. The
# scalar normalises each speed so its average absolute forecast is ~10,
# which is what makes different speeds comparable and combinable.
SPEEDS = [
    (2, 8, 10.6),
    (4, 16, 7.5),
    (8, 32, 5.3),
    (16, 64, 3.75),
    (32, 128, 2.65),
]

# Forecasts are capped at +/-20, i.e. twice the average. Uncapped
# forecasts let a single extreme reading dominate the whole book.
FORECAST_CAP = 20.0
AVERAGE_FORECAST = 10.0

# Combining correlated speeds shrinks the combined forecast, so it is
# scaled back up. 1.4 is Carver's approximate value for ~5 correlated
# trend speeds. Published, not fitted.
FORECAST_DIVERSIFICATION_MULTIPLIER = 1.4

# Equal weight across speeds. Optimising these weights on this sample
# would be overfitting with extra steps.
SPEED_WEIGHTS = [1.0 / len(SPEEDS)] * len(SPEEDS)


class CarverMultiSpeedTrend:
    name = "carver_trend"
    symbols = cfg.TREND_SYMBOLS

    def __init__(self, aggression: float = cfg.AGGRESSION):
        self.aggression = aggression

    @property
    def min_bars(self) -> int:
        slowest = max(s for _, s, _ in SPEEDS)
        return slowest * 2 + cfg.VOL_LOOKBACK + 2

    def _raw_forecast(self, close: pd.Series, fast: int, slow: int) -> float | None:
        """EWMAC: the fast/slow EWMA gap, normalised by price volatility.

        Dividing by volatility is what makes the number comparable across
        instruments and across time -- a 2-point gap means something very
        different on a quiet bond ETF than on a volatile semis ETF.
        """
        if len(close) < slow + 2:
            return None
        ewmac = close.ewm(span=fast).mean().iloc[-1] - close.ewm(span=slow).mean().iloc[-1]
        daily_returns = close.pct_change().dropna()
        if len(daily_returns) < cfg.VOL_LOOKBACK:
            return None
        daily_vol = float(daily_returns.iloc[-cfg.VOL_LOOKBACK:].std())
        price_vol = daily_vol * float(close.iloc[-1])  # volatility in price units
        if price_vol <= 0:
            return None
        return float(ewmac / price_vol)

    def combined_forecast(self, close: pd.Series) -> tuple[float | None, dict]:
        """Weighted average of scaled, capped per-speed forecasts."""
        scaled = {}
        for (fast, slow, scalar), weight in zip(SPEEDS, SPEED_WEIGHTS):
            raw = self._raw_forecast(close, fast, slow)
            if raw is None:
                continue
            value = float(np.clip(raw * scalar, -FORECAST_CAP, FORECAST_CAP))
            scaled[f"{fast}/{slow}"] = (value, weight)

        if len(scaled) < 2:
            return None, {}

        total_weight = sum(w for _, w in scaled.values())
        combined = sum(v * w for v, w in scaled.values()) / total_weight
        combined *= FORECAST_DIVERSIFICATION_MULTIPLIER
        combined = float(np.clip(combined, -FORECAST_CAP, FORECAST_CAP))
        return combined, {k: round(v, 1) for k, (v, _) in scaled.items()}

    def generate(self, bars: pd.DataFrame) -> AggressiveSignal:
        if len(bars) < self.min_bars:
            return AggressiveSignal(Target.HOLD, f"need {self.min_bars} bars, have {len(bars)}")

        close = bars["close"]
        forecast, detail = self.combined_forecast(close)
        if forecast is None:
            return AggressiveSignal(Target.HOLD, "forecast undefined")

        realised = annualised_vol(close)
        if realised is None or realised < cfg.MIN_ANNUAL_VOL:
            return AggressiveSignal(Target.HOLD, "volatility unmeasurable -- not sizing")

        # Position scales with BOTH forecast strength and inverse
        # volatility. forecast/10 is the conviction term: a forecast of 10
        # (the average) gives the standard vol-targeted size, 20 gives
        # double, 5 gives half.
        conviction = abs(forecast) / AVERAGE_FORECAST
        weight = self.aggression * conviction * (cfg.TARGET_ANNUAL_VOL / realised)
        weight = float(min(weight, cfg.MAX_WEIGHT_PER_POSITION))

        # A near-zero forecast is a genuine "no opinion", not a weak long.
        # Holding a tiny position because the forecast is 0.3 just pays
        # spread for no reason.
        if abs(forecast) < 1.0 or weight <= 0.01:
            return AggressiveSignal(Target.FLAT, f"forecast {forecast:+.1f} too weak to act on")

        atr_value = atr(bars)
        stop_distance = atr_value * cfg.TREND_ATR_STOP_MULT if atr_value else None
        direction = Target.LONG if forecast > 0 else Target.SHORT

        return AggressiveSignal(
            direction,
            f"forecast {forecast:+.1f} (speeds {detail}), conviction {conviction:.2f}x, "
            f"weight {weight:.0%}",
            weight,
            stop_distance,
        )
