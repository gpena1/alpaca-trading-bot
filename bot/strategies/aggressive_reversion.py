"""Strategy C -- Stretch Reversion.

Thesis: prices that travel unusually far from their own mean, unusually
fast, tend to come back. Size the bet by HOW FAR -- a three-sigma stretch
is a better bet than a two-sigma one, so it gets a bigger position.

FAILS IN: regime change. When a stretch is not an overreaction but the
start of a real move, this fights it and loses. That is deliberately the
mirror image of Strategy A -- A wants the move to continue, C wants it to
revert. They cannot both be wrong at the same time in the same way, which
is the entire reason to run both.

Inherited from the audit of the original mean_reversion:
  - Z-SCORE, NOT RSI BANDS. RSI 20/80 was chosen by sweeping thresholds
    over the same six-month window the result was measured on, and it was
    fit to 15-minute noise. A z-score is self-normalising: "two standard
    deviations" means the same thing on GLD and on USO without tuning.
  - MIDLINE EXIT. The original held from oversold until OVERBOUGHT --
    refusing to take a reversion profit until the move had fully reversed
    into the opposite extreme, which frequently never happened. Exit is
    now at the mean, which is where the thesis actually resolves.
  - HARD TIME STOP. The single most important addition. A failed
    reversion is a position fighting a trend, and without a time limit it
    stays on indefinitely. After N bars it closes regardless of price.
  - TAKE-PROFIT KEPT. This is the one strategy where a fixed target is
    coherent, because a bounded move back to the mean is the literal
    thesis. Trend and breakout get none.

The time stop is what makes this survivable at aggressive sizing. Scaling
INTO a stretch without one is how mean-reversion books blow up: the
position grows exactly as the loss grows.
"""

from __future__ import annotations

import pandas as pd

import config_aggressive as cfg
from bot.strategies.sizing import AggressiveSignal, Target, atr, vol_targeted_weight


class StretchReversion:
    name = "stretch_reversion"
    symbols = cfg.REVERSION_SYMBOLS

    def __init__(self, aggression: float = cfg.AGGRESSION):
        self.aggression = aggression
        self.period = cfg.ZSCORE_PERIOD
        self.entry_z = cfg.ZSCORE_ENTRY
        self.exit_z = cfg.ZSCORE_EXIT

    @property
    def min_bars(self) -> int:
        return self.period + cfg.VOL_LOOKBACK + 2

    def zscore(self, close: pd.Series) -> float | None:
        mean = close.rolling(self.period).mean().iloc[-1]
        std = close.rolling(self.period).std().iloc[-1]
        if pd.isna(mean) or pd.isna(std) or std <= 0:
            return None
        return float((close.iloc[-1] - mean) / std)

    def generate(self, bars: pd.DataFrame) -> AggressiveSignal:
        if len(bars) < self.min_bars:
            return AggressiveSignal(Target.HOLD, f"need {self.min_bars} bars, have {len(bars)}")

        close = bars["close"]
        z = self.zscore(close)
        if z is None:
            return AggressiveSignal(Target.HOLD, "z-score undefined")

        if abs(z) <= self.exit_z:
            return AggressiveSignal(Target.FLAT, f"z={z:+.2f} back at the mean -- reversion done")

        if abs(z) < self.entry_z:
            return AggressiveSignal(Target.HOLD, f"z={z:+.2f} between entry and exit bands")

        # Deeper stretch, bigger bet -- capped, because the tail of this
        # distribution is exactly where reversion stops being reliable.
        scale = min(abs(z) / self.entry_z, cfg.ZSCORE_MAX_SCALE)
        weight = vol_targeted_weight(close, aggression=self.aggression, scale=scale)
        if weight <= 0:
            return AggressiveSignal(Target.HOLD, "volatility unmeasurable -- not sizing")

        atr_value = atr(bars)
        stop_distance = atr_value * 3.0 if atr_value else None

        # Stretched DOWN means buy the dip; stretched UP means fade it.
        direction = Target.LONG if z < 0 else Target.SHORT
        return AggressiveSignal(
            direction,
            f"z={z:+.2f} beyond +/-{self.entry_z}, scale {scale:.2f}x, weight {weight:.0%}",
            weight,
            stop_distance,
        )
