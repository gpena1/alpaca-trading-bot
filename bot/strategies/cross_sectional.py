"""Cross-sectional momentum: rank a universe, hold the top N.

This is a PORTFOLIO strategy, not a per-symbol one. The existing
Strategy interface answers "what should I do with SPY?" in isolation.
Cross-sectional momentum can't be expressed that way -- whether SPY is
worth holding depends entirely on how it ranks against the other 29
names, which the per-symbol interface never gets to see.

The mechanism:
  1. Score every symbol by its average return across several lookbacks.
  2. Optionally require that score to be positive in absolute terms.
  3. Optionally require the market proxy to be above its long SMA.
  4. Hold the top N, equal weight, flat otherwise.
  5. Only re-evaluate every N bars, so the book isn't churned daily.

Why this and not "make the trend strategy more aggressive": SPY and QQQ
correlate around 0.95, so trend-following both is one bet held twice.
Ranking across 30 assets that behave differently -- sectors, regions,
bonds, metals -- is a structurally different source of return, and it
fails in different conditions (crowded momentum unwinds) than a trend
system does (chop).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

import config_xs

logger = logging.getLogger(__name__)


@dataclass
class MomentumScore:
    symbol: str
    score: float
    per_lookback: dict


class CrossSectionalMomentum:
    name = "cross_sectional_momentum"

    def __init__(
        self,
        lookbacks: list[int] | None = None,
        top_n: int = config_xs.TOP_N,
        use_regime_filter: bool = config_xs.USE_REGIME_FILTER,
        regime_symbol: str = config_xs.REGIME_SYMBOL,
        regime_sma_period: int = config_xs.REGIME_SMA_PERIOD,
        require_positive: bool = config_xs.REQUIRE_POSITIVE_MOMENTUM,
    ):
        self.lookbacks = sorted(lookbacks or config_xs.MOMENTUM_LOOKBACKS)
        self.top_n = top_n
        self.use_regime_filter = use_regime_filter
        self.regime_symbol = regime_symbol
        self.regime_sma_period = regime_sma_period
        self.require_positive = require_positive

    @property
    def min_bars(self) -> int:
        return max(max(self.lookbacks), self.regime_sma_period) + 2

    def score_symbol(self, closes: pd.Series) -> MomentumScore | None:
        """Average total return across the configured lookbacks.

        Averaging horizons rather than picking one is the robustness
        choice: a single lookback tuned to a sample is precisely the
        failure mode that produced the old breakout parameters.
        """
        per_lookback = {}
        for lb in self.lookbacks:
            if len(closes) < lb + 1:
                return None
            past = float(closes.iloc[-1 - lb])
            if past <= 0:
                return None
            per_lookback[lb] = float(closes.iloc[-1]) / past - 1.0
        if not per_lookback:
            return None
        return MomentumScore("", sum(per_lookback.values()) / len(per_lookback), per_lookback)

    def regime_is_risk_on(self, symbol_closes: dict[str, pd.Series]) -> bool:
        if not self.use_regime_filter:
            return True
        proxy = symbol_closes.get(self.regime_symbol)
        if proxy is None or len(proxy) < self.regime_sma_period:
            logger.warning("regime proxy %s unavailable -- defaulting to risk-off", self.regime_symbol)
            return False
        sma = proxy.rolling(self.regime_sma_period).mean().iloc[-1]
        if pd.isna(sma):
            return False
        return float(proxy.iloc[-1]) > float(sma)

    def select(self, symbol_bars: dict[str, pd.DataFrame]) -> tuple[list[str], dict]:
        """Return (symbols to hold, diagnostics).

        An empty list means hold nothing -- either the regime is risk-off
        or nothing cleared the absolute-momentum gate.
        """
        closes = {s: b["close"] for s, b in symbol_bars.items() if not b.empty}

        risk_on = self.regime_is_risk_on(closes)
        diagnostics = {"risk_on": risk_on, "scores": {}, "reason": ""}

        if not risk_on:
            diagnostics["reason"] = (
                f"{self.regime_symbol} below its {self.regime_sma_period}-day SMA -- flat"
            )
            return [], diagnostics

        scored: list[tuple[str, float]] = []
        for symbol, series in closes.items():
            score = self.score_symbol(series)
            if score is None:
                continue
            diagnostics["scores"][symbol] = score.score
            scored.append((symbol, score.score))

        if not scored:
            diagnostics["reason"] = "insufficient history to score any symbol"
            return [], diagnostics

        scored.sort(key=lambda x: x[1], reverse=True)
        ranked = scored[: self.top_n]

        if self.require_positive:
            ranked = [(s, v) for s, v in ranked if v > 0]

        held = [s for s, _ in ranked]
        if not held:
            diagnostics["reason"] = "no symbol cleared the positive-momentum gate -- flat"
        else:
            diagnostics["reason"] = "holding " + ", ".join(
                f"{s} ({v:+.1%})" for s, v in ranked
            )
        return held, diagnostics

    def target_weights(self, symbol_bars: dict[str, pd.DataFrame]) -> tuple[dict[str, float], dict]:
        """Equal-weight the selected names up to the gross exposure cap."""
        held, diagnostics = self.select(symbol_bars)
        if not held:
            return {}, diagnostics
        weight = config_xs.TARGET_GROSS_EXPOSURE / len(held)
        return {s: weight for s in held}, diagnostics
