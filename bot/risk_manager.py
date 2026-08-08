"""Turns a strategy's target position state into risk-checked orders.

Responsibilities:
  - reconcile the current signed position against the strategy's target
  - position sizing via fixed-fractional allocation per symbol
  - per-symbol and total-portfolio exposure caps, using ABSOLUTE value so
    a long and a short don't net out to "no exposure"
  - stop-loss / take-profit exits, sign-aware for shorts
  - protective stop prices for native GTC stop orders

Position flips (long -> short or short -> long) are emitted as TWO
decisions: close, then open. Alpaca rejects a single order that crosses
through zero on equities, so this has to be explicit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alpaca.trading.enums import OrderSide

import config
from bot.portfolio import Portfolio
from bot.strategies.base import Signal, TargetPosition

logger = logging.getLogger(__name__)


@dataclass
class OrderDecision:
    symbol: str
    side: OrderSide
    qty: float
    reason: str
    # Price for a protective stop order to submit alongside an entry.
    # None for closing orders, which need no protection.
    stop_price: float | None = None
    is_entry: bool = False


def _state_of(qty: float) -> TargetPosition:
    if qty > 0:
        return TargetPosition.LONG
    if qty < 0:
        return TargetPosition.SHORT
    return TargetPosition.FLAT


class RiskManager:
    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio

    def evaluate(
        self,
        symbol: str,
        signal: Signal,
        current_price: float,
        strategy_name: str,
    ) -> list[OrderDecision]:
        position = self.portfolio.position_for(symbol)
        current_qty = position.qty if position else 0.0
        current_state = _state_of(current_qty)

        exit_decision = self._check_stop_take(symbol, position, current_price, strategy_name)
        if exit_decision:
            return [exit_decision]

        target = signal.target
        if target == TargetPosition.HOLD:
            return []
        if target == TargetPosition.SHORT and not self._can_short(symbol):
            logger.debug("%s: SHORT target degraded to FLAT (not shortable)", symbol)
            target = TargetPosition.FLAT
        if target == current_state:
            return []

        decisions: list[OrderDecision] = []

        if current_state != TargetPosition.FLAT:
            decisions.append(
                OrderDecision(
                    symbol=symbol,
                    side=OrderSide.SELL if current_qty > 0 else OrderSide.BUY,
                    qty=abs(current_qty),
                    reason=f"closing {current_state.value} to reach target {target.value}",
                )
            )

        if target in (TargetPosition.LONG, TargetPosition.SHORT):
            entry = self._build_entry(symbol, target, signal, current_price, closing_qty=abs(current_qty))
            if entry:
                decisions.append(entry)

        return decisions

    def _can_short(self, symbol: str) -> bool:
        return config.ALLOW_SHORTING and symbol in config.SHORTABLE_SYMBOLS

    def _check_stop_take(
        self, symbol: str, position, current_price: float, strategy_name: str
    ) -> OrderDecision | None:
        """Backstop only. Real protection is the native GTC stop submitted at
        entry -- this catches the gap between daily runs for crypto and any
        stop that failed to register."""
        if position is None or position.qty == 0:
            return None

        entry = position.avg_entry_price
        if entry <= 0:
            return None

        is_long = position.qty > 0
        # For a short, a rising price is the loss -- flip the sign.
        change_pct = (current_price - entry) / entry
        if not is_long:
            change_pct = -change_pct

        closing_side = OrderSide.SELL if is_long else OrderSide.BUY

        if change_pct <= -config.STOP_LOSS_PCT:
            return OrderDecision(
                symbol,
                closing_side,
                abs(position.qty),
                f"stop-loss: {change_pct:.1%} <= -{config.STOP_LOSS_PCT:.0%}",
            )

        take_profit = config.TAKE_PROFIT_PCT_BY_STRATEGY.get(strategy_name)
        if take_profit is not None and change_pct >= take_profit:
            return OrderDecision(
                symbol,
                closing_side,
                abs(position.qty),
                f"take-profit: {change_pct:.1%} >= {take_profit:.0%}",
            )
        return None

    def _build_entry(
        self,
        symbol: str,
        target: TargetPosition,
        signal: Signal,
        current_price: float,
        closing_qty: float,
    ) -> OrderDecision | None:
        equity = self.portfolio.equity()
        if equity <= 0 or current_price <= 0:
            return None

        # Exposure freed by the close we're about to submit counts as room.
        exposure_now = self.portfolio.exposure_pct()
        freed_pct = (closing_qty * current_price) / equity if closing_qty else 0.0
        room_left_pct = config.MAX_TOTAL_EXPOSURE_PCT - (exposure_now - freed_pct)
        if room_left_pct <= 0:
            logger.info("%s: entry skipped, total exposure cap reached", symbol)
            return None

        allocation_pct = min(config.MAX_ALLOCATION_PCT_PER_SYMBOL, room_left_pct)
        qty = round((equity * allocation_pct) / current_price, 6)
        if qty <= 0:
            return None

        if target == TargetPosition.LONG:
            side = OrderSide.BUY
            stop_price = round(current_price * (1 - config.STOP_LOSS_PCT), 2)
        else:
            side = OrderSide.SELL
            stop_price = round(current_price * (1 + config.STOP_LOSS_PCT), 2)

        return OrderDecision(
            symbol=symbol,
            side=side,
            qty=qty,
            reason=signal.reason,
            stop_price=stop_price,
            is_entry=True,
        )
