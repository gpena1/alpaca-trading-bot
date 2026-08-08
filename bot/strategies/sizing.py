"""Shared sizing machinery for the aggressive strategies.

The central idea, taken from Rob Carver's systematic-trading framework:
aggression should be expressed as a VOLATILITY TARGET, not as looser
entry rules. A position is sized so that its own recent volatility,
scaled up or down, produces the annualised volatility you asked for.

Why this beats the alternative. The old bot had a flat 20% allocation per
symbol regardless of instrument. That means a 20% slug of TLT and a 20%
slug of SMH contribute wildly different amounts of risk -- the portfolio's
actual risk is whatever the most volatile holding happens to be doing.
Vol targeting equalises that, which is what makes a single AGGRESSION
number meaningful across a mixed universe.

It also means "more aggressive" has an honest definition: you are
choosing to run more volatility, with the drawdown that implies, rather
than pretending a looser threshold found you more opportunities.

Deliberately kept separate from bot/strategies/base.py so the live bot's
interface is untouched. These strategies are experimental until the
harness says otherwise -- same treatment cross_sectional.py got.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

import config_aggressive as cfg


class Target(Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"
    HOLD = "hold"  # no opinion -- keep whatever is currently held


@dataclass
class AggressiveSignal:
    """A target state plus how big the position should be.

    `weight` is the fraction of equity to put at risk, already
    vol-targeted and capped. It is ignored for FLAT and HOLD.
    `stop_distance` is in price units (from ATR), for a trailing stop.
    """

    target: Target
    reason: str
    weight: float = 0.0
    stop_distance: float | None = None


def annualised_vol(closes: pd.Series, lookback: int = cfg.VOL_LOOKBACK) -> float | None:
    """Annualised realised volatility from daily returns."""
    if len(closes) < lookback + 2:
        return None
    returns = closes.pct_change().dropna().iloc[-lookback:]
    if len(returns) < lookback // 2:
        return None
    daily = float(returns.std())
    if daily <= 0:
        return None
    return daily * np.sqrt(cfg.TRADING_DAYS_PER_YEAR)


def atr(bars: pd.DataFrame, period: int = 14) -> float | None:
    """Average true range. Uses highs and lows, which are noisier on the
    IEX feed than closes -- acceptable here because ATR is only setting a
    stop distance, not generating the entry signal. The lesson from the
    old Donchian channel was that ENTRIES must not depend on extremes."""
    if len(bars) < period + 1:
        return None
    high, low, close = bars["high"], bars["low"], bars["close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    value = float(true_range.rolling(period).mean().iloc[-1])
    return value if value > 0 else None


def vol_targeted_weight(
    closes: pd.Series,
    aggression: float = cfg.AGGRESSION,
    target_vol: float = cfg.TARGET_ANNUAL_VOL,
    scale: float = 1.0,
) -> float:
    """Fraction of equity for this position.

    weight = aggression * scale * (target_vol / realised_vol), capped.

    A quiet instrument gets a bigger slug, a violent one a smaller slug,
    so each contributes comparable risk. `scale` lets a strategy express
    conviction (the reversion strategy sizes up on deeper stretches)
    without touching the vol logic.
    """
    realised = annualised_vol(closes)
    if realised is None or realised < cfg.MIN_ANNUAL_VOL:
        return 0.0
    raw = aggression * scale * (target_vol / realised)
    return float(min(raw, cfg.MAX_WEIGHT_PER_POSITION))


def gross_cap(aggression: float = cfg.AGGRESSION) -> float:
    """Gross exposure ceiling for a given aggression setting.

    Scaling this with aggression is what makes the dial mean something at
    the BOOK level. If only per-position weights scaled, a fixed gross cap
    would truncate the extra size and every setting would converge to the
    same portfolio -- which is exactly the bug the first smoke test found.
    """
    return float(min(cfg.BASE_GROSS_EXPOSURE * aggression, cfg.ABSOLUTE_MAX_GROSS))


def regime_is_risk_on(spy_closes: pd.Series | None) -> bool:
    """Optional. Defaults OFF in config -- the ablation showed it cost
    money over 2020-2026, though that window holds only one sustained
    decline, so the finding is weak rather than conclusive."""
    if not cfg.USE_REGIME_FILTER:
        return True
    if spy_closes is None or len(spy_closes) < cfg.REGIME_SMA_PERIOD:
        return False
    sma = spy_closes.rolling(cfg.REGIME_SMA_PERIOD).mean().iloc[-1]
    if pd.isna(sma):
        return False
    return float(spy_closes.iloc[-1]) > float(sma)


class KillSwitch:
    """Portfolio-level drawdown cutout.

    Aggressive systems do not degrade gracefully; they fail at once. This
    is the one piece of risk machinery that gets MORE important as the
    aggression dial goes up, and the only one that is not optional.
    """

    def __init__(
        self,
        max_drawdown: float = cfg.MAX_PORTFOLIO_DRAWDOWN,
        cooldown_bars: int = cfg.KILL_SWITCH_COOLDOWN_BARS,
    ):
        self.max_drawdown = max_drawdown
        self.cooldown_bars = cooldown_bars
        self.peak = None
        self.cooldown_remaining = 0
        self.trips = 0

    def update(self, equity: float) -> bool:
        """Feed current equity. Returns True if trading is ALLOWED."""
        if self.peak is None or equity > self.peak:
            self.peak = equity

        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            if self.cooldown_remaining == 0:
                self.peak = equity  # reset the high-water mark on resume
            return False

        if self.peak and self.peak > 0:
            drawdown = (self.peak - equity) / self.peak
            if drawdown >= self.max_drawdown:
                self.cooldown_remaining = self.cooldown_bars
                self.trips += 1
                return False
        return True
