"""Common interface every strategy implements.

Strategies now emit a TARGET POSITION STATE rather than a BUY/SELL event.

Why: the old event model only fired on the exact bar where a condition
flipped (e.g. the bar an SMA crossover happened). If that signal was
missed -- market closed, exposure cap full, position stopped out an hour
earlier, process restarted -- it never came back, and the bot sat flat
through the entire move it had correctly identified. A target state is
re-derivable from the bars on every cycle, so the system can always
recover to the position it should be holding.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class TargetPosition(Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"
    HOLD = "hold"  # no opinion this bar -- keep whatever is currently held


@dataclass
class Signal:
    target: TargetPosition
    reason: str


class Strategy(ABC):
    """A strategy turns a bar history into a target position state.

    `bars` is a DataFrame indexed by timestamp with 'open', 'high', 'low',
    'close', 'volume' columns, oldest first. With daily bars, one row is
    one trading day.

    `allow_short` is set by the caller from config.SHORTABLE_SYMBOLS. A
    strategy must never emit SHORT when it is False -- resolve to FLAT
    instead, so a long-only instrument (crypto) exits rather than
    submitting an order Alpaca will reject.
    """

    name: str = "base"

    def __init__(self, allow_short: bool = False):
        self.allow_short = allow_short

    def _short_or_flat(self, reason: str) -> Signal:
        if self.allow_short:
            return Signal(TargetPosition.SHORT, reason)
        return Signal(TargetPosition.FLAT, f"{reason} (shorting disabled -- flat)")

    @abstractmethod
    def generate_signal(self, bars: pd.DataFrame) -> Signal:
        raise NotImplementedError
