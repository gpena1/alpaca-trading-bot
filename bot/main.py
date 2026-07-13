"""Entry point: polls SPY, QQQ, BTC/USD, GLD, and USO on a fixed interval,
runs each symbol's assigned strategy, and routes any signal through the
risk manager before placing an order.
"""

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
    strategies = {}
    for symbol, strategy_key in config.STRATEGY_MAP.items():
        strategy_cls = STRATEGY_REGISTRY[strategy_key]
        strategies[symbol] = strategy_cls()
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
                "%s [%s]: %s (%s) @ %.2f",
                symbol,
                strategy.name,
                signal.action.value.upper(),
                signal.reason,
                current_price,
            )

            decision = risk_manager.evaluate(symbol, signal, current_price)
            if decision is None:
                continue

            broker.submit_market_order(decision.symbol, decision.qty, decision.side)
            logger.info(
                "%s: order submitted %s qty=%s reason=%s",
                symbol,
                decision.side.value,
                decision.qty,
                decision.reason,
            )
        except Exception:
            logger.exception("%s: error during cycle, skipping this symbol", symbol)


def main():
    logger.info(
        "Starting trading bot | base_url=%s paper=%s symbols=%s",
        config.ALPACA_BASE_URL,
        config.IS_PAPER,
        config.SYMBOLS,
    )

    broker = Broker()
    portfolio = Portfolio(broker)
    risk_manager = RiskManager(portfolio)
    strategies = build_strategies()

    while True:
        run_cycle(broker, portfolio, risk_manager, strategies)
        logger.info("Cycle complete, sleeping %ss", config.POLL_INTERVAL_SECONDS)
        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
