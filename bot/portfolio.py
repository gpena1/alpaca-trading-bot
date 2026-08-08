"""Read-only view of account state: equity, cash, and per-symbol exposure.

Position quantities are SIGNED -- Alpaca reports a short as a negative qty
and a negative market_value. Exposure therefore has to be computed on
absolute values, or a $10k long and a $10k short net out to "zero
exposure" and the caps stop protecting anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bot.broker import Broker

logger = logging.getLogger(__name__)


@dataclass
class PositionInfo:
    symbol: str
    qty: float  # signed: positive = long, negative = short
    market_value: float  # signed
    avg_entry_price: float
    unrealized_pl: float

    @property
    def is_short(self) -> bool:
        return self.qty < 0


class Portfolio:
    def __init__(self, broker: Broker):
        self.broker = broker

    def equity(self) -> float:
        return float(self.broker.get_account().equity)

    def cash(self) -> float:
        return float(self.broker.get_account().cash)

    def position_for(self, symbol: str) -> PositionInfo | None:
        pos = self.broker.get_position(symbol)
        if pos is None:
            return None
        return PositionInfo(
            symbol=symbol,
            qty=float(pos.qty),
            market_value=float(pos.market_value),
            avg_entry_price=float(pos.avg_entry_price),
            unrealized_pl=float(pos.unrealized_pl),
        )

    def total_exposure(self) -> float:
        return sum(abs(float(p.market_value)) for p in self.broker.get_positions())

    def exposure_pct(self) -> float:
        equity = self.equity()
        if equity <= 0:
            return 0.0
        return self.total_exposure() / equity

    def log_summary(self):
        equity = self.equity()
        cash = self.cash()
        positions = self.broker.get_positions()
        logger.info(
            "Account equity=$%.2f cash=$%.2f gross_exposure=%.1f%% open_positions=%d",
            equity,
            cash,
            self.exposure_pct() * 100,
            len(positions),
        )
        for p in positions:
            qty = float(p.qty)
            logger.info(
                "  %s %s qty=%s mv=$%.2f unrealized_pl=$%.2f",
                p.symbol,
                "SHORT" if qty < 0 else "LONG",
                qty,
                float(p.market_value),
                float(p.unrealized_pl),
            )
