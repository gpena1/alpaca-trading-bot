"""Common interface every strategy implements."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class SignalAction(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class Signal:
    action: SignalAction
    reason: str


class Strategy(ABC):
    """A strategy turns a bar history into a BUY/SELL/HOLD signal.

    `bars` is a DataFrame indexed by timestamp with at least an 'open',
    'high', 'low', 'close', 'volume' columns, oldest first.
    """

    name: str = "base"

    @abstractmethod
    def generate_signal(self, bars: pd.DataFrame) -> Signal:
        raise NotImplementedError
