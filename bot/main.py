"""Entry point: evaluates each symbol's target position once per day.

The supported deployment is a scheduled `python3 -m bot.main --once`
after the close -- with daily bars there is nothing to gain from a
polling loop, and one short-lived job is cheaper and more reliable than
an always-on process.
"""

import argparse
import logging
import time

import config
from bot.broker import Broker, is_crypto
from bot.portfolio import Portfolio
from bot.risk_manager import RiskManager
from bot.strategies import STRATEGY_REGISTRY

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot.main")


def build_strategies():
    """Each strategy is told whether its symbol can be shorted. Alpaca does
    not support shorting crypto, so BTC/USD gets allow_short=False and its
    SHORT targets resolve to FLAT."""
    strategies = {}
    for symbol, strategy_key in config.STRATEGY_MAP.items():
        strategy_cls = STRATEGY_REGISTRY[strategy_key]
        allow_short = config.ALLOW_SHORTING and symbol in config.SHORTABLE_SYMBOLS
        strategies[symbol] = strategy_cls(allow_short=allow_short)
    return strategies


def run_cycle(broker: Broker, portfolio: Portfolio, risk_manager: RiskManager, strategies: dict):
    market_open = broker.is_market_open()
    portfolio.log_summary()

    for symbol in config.SYMBOLS:
        if not is_crypto(symbol) and not market_open:
            logger.debug("%s: equity market closed, skipping", symbol)
            continue

        try:
            bars = broker.get_bars(symbol)
            if bars.empty:
                logger.warning("%s: no bar data returned, skipping", symbol)
                continue

            strategy = strategies[symbol]
            signal = strategy.generate_signal(bars)
            current_price = float(bars["close"].iloc[-1])

            logger.info(
                "%s [%s]: target=%s (%s) @ %.2f",
                symbol,
                strategy.name,
                signal.target.value.upper(),
                signal.reason,
                current_price,
            )

            decisions = risk_manager.evaluate(symbol, signal, current_price, strategy.name)
            if not decisions:
                continue

            # Any resting protective stop belongs to the position we're
            # about to change. Cancel first, or it survives its position.
            broker.cancel_open_orders_for(symbol)

            for decision in decisions:
                broker.submit_market_order(decision.symbol, decision.qty, decision.side)
                logger.info(
                    "%s: %s qty=%s reason=%s",
                    symbol,
                    decision.side.value,
                    decision.qty,
                    decision.reason,
                )
                if decision.is_entry and decision.stop_price and config.USE_NATIVE_STOP_ORDERS:
                    from alpaca.trading.enums import OrderSide

                    protective_side = (
                        OrderSide.SELL if decision.side == OrderSide.BUY else OrderSide.BUY
                    )
                    broker.submit_stop_order(
                        symbol, decision.qty, protective_side, decision.stop_price
                    )
        except Exception:
            logger.exception("%s: error during cycle, skipping this symbol", symbol)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle and exit. This is the intended mode -- "
        "schedule it once per day after the close.",
    )
    args = parser.parse_args()

    logger.info(
        "Starting trading bot | base_url=%s paper=%s timeframe=%s shorting=%s mode=%s",
        config.ALPACA_BASE_URL,
        config.IS_PAPER,
        config.BAR_TIMEFRAME,
        config.ALLOW_SHORTING,
        "once" if args.once else "loop",
    )

    broker = Broker()
    portfolio = Portfolio(broker)
    risk_manager = RiskManager(portfolio)
    strategies = build_strategies()

    if args.once:
        run_cycle(broker, portfolio, risk_manager, strategies)
        return

    while True:
        run_cycle(broker, portfolio, risk_manager, strategies)
        logger.info("Cycle complete, sleeping %ss", config.POLL_INTERVAL_SECONDS)
        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
