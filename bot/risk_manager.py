"""Turns a strategy Signal into a concrete, risk-checked order decision.

Responsibilities:
  - position sizing via fixed-fractional allocation per symbol
  - per-symbol and total-portfolio exposure caps
  - stop-loss / take-profit exits that override the strategy signal
  - no shorting: SELL only closes an existing long, never opens a short
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alpaca.trading.enums import OrderSide

import config
from bot.portfolio import Portfolio
from bot.strategies.base import Signal, SignalAction

logger = logging.getLogger(__name__)


@dataclass
class OrderDecision:
    symbol: str
    side: OrderSide
    qty: float
    reason: str


class RiskManager:
    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio

    def evaluate(self, symbol: str, signal: Signal, current_price: float) -> OrderDecision | None:
        position = self.portfolio.position_for(symbol)

        stop_take_decision = self._check_stop_take(symbol, position, current_price)
        if stop_take_decision:
            return stop_take_decision

        if signal.action == SignalAction.BUY:
            return self._evaluate_buy(symbol, signal, position, current_price)
        if signal.action == SignalAction.SELL:
            return self._evaluate_sell(symbol, signal, position)
        return None

    def _check_stop_take(self, symbol, position, current_price: float) -> OrderDecision | None:
        if position is None or position.qty <= 0:
            return None

        entry = position.avg_entry_price
        if entry <= 0:
            return None

        change_pct = (current_price - entry) / entry

        if change_pct <= -config.STOP_LOSS_PCT:
            return OrderDecision(
                symbol,
                OrderSide.SELL,
                position.qty,
                f"stop-loss triggered: {change_pct:.1%} <= -{config.STOP_LOSS_PCT:.0%}",
            )
        if change_pct >= config.TAKE_PROFIT_PCT:
            return OrderDecision(
                symbol,
                OrderSide.SELL,
                position.qty,
                f"take-profit triggered: {change_pct:.1%} >= {config.TAKE_PROFIT_PCT:.0%}",
            )
        return None

    def _evaluate_buy(self, symbol, signal: Signal, position, current_price: float) -> OrderDecision | None:
        if position is not None and position.qty > 0:
            logger.debug("%s: BUY signal ignored, already holding a position", symbol)
            return None

        equity = self.portfolio.equity()
        if equity <= 0:
            return None

        room_left_pct = config.MAX_TOTAL_EXPOSURE_PCT - self.portfolio.exposure_pct()
        if room_left_pct <= 0:
            logger.info("%s: BUY skipped, total exposure cap reached", symbol)
            return None

        allocation_pct = min(config.MAX_ALLOCATION_PCT_PER_SYMBOL, room_left_pct)
        target_dollars = equity * allocation_pct
        if target_dollars <= 0 or current_price <= 0:
            return None

        qty = round(target_dollars / current_price, 6)
        if qty <= 0:
            return None

        return OrderDecision(symbol, OrderSide.BUY, qty, signal.reason)

    def _evaluate_sell(self, symbol, signal: Signal, position) -> OrderDecision | None:
        if position is None or position.qty <= 0:
            logger.debug("%s: SELL signal ignored, no open position to close", symbol)
            return None
        return OrderDecision(symbol, OrderSide.SELL, position.qty, signal.reason)
