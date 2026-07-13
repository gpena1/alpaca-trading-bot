"""Read-only view of account state: equity, cash, and per-symbol exposure."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bot.broker import Broker

logger = logging.getLogger(__name__)


@dataclass
class PositionInfo:
    symbol: str
    qty: float
    market_value: float
    avg_entry_price: float
    unrealized_pl: float


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
        return sum(float(p.market_value) for p in self.broker.get_positions())

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
            "Account equity=$%.2f cash=$%.2f exposure=%.1f%% open_positions=%d",
            equity,
            cash,
            self.exposure_pct() * 100,
            len(positions),
        )
        for p in positions:
            logger.info(
                "  %s qty=%s mv=$%.2f unrealized_pl=$%.2f",
                p.symbol,
                p.qty,
                float(p.market_value),
                float(p.unrealized_pl),
            )
