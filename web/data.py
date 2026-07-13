"""Read-only dashboard data, cached briefly to avoid hammering the Alpaca API.

Never calls RiskManager.evaluate() or submits orders — this module only
observes and reports the bot's state, it cannot act on it.
"""

import logging
import threading
import time
from datetime import datetime, timezone

import config
from bot.broker import Broker
from bot.main import build_strategies
from bot.portfolio import Portfolio

logger = logging.getLogger("web.data")

_broker = Broker()
_portfolio = Portfolio(_broker)
_strategies = build_strategies()

_CACHE_TTL_SECONDS = 45
_lock = threading.Lock()
_cache = {"data": None, "expires_at": 0.0}


def _signal_for(symbol):
    bars = _broker.get_bars(symbol)
    if bars.empty:
        return {"action": "hold", "reason": "no bar data available", "price": None}
    signal = _strategies[symbol].generate_signal(bars)
    return {
        "action": signal.action.value,
        "reason": signal.reason,
        "price": float(bars["close"].iloc[-1]),
    }


def _compute():
    rows = []
    for symbol in config.SYMBOLS:
        try:
            sig = _signal_for(symbol)
        except Exception:
            logger.exception("%s: signal computation failed", symbol)
            sig = {"action": "hold", "reason": "error fetching data", "price": None}
        rows.append(
            {
                "symbol": symbol,
                "strategy": _strategies[symbol].name,
                "signal": sig,
                "position": _portfolio.position_for(symbol),
            }
        )

    try:
        orders = list(_broker.get_recent_orders(limit=20))
    except Exception:
        logger.exception("failed to fetch recent orders")
        orders = []

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "market_open": _broker.is_market_open(),
        "equity": _portfolio.equity(),
        "cash": _portfolio.cash(),
        "exposure_pct": _portfolio.exposure_pct(),
        "rows": rows,
        "orders": orders,
    }


def get_dashboard_data():
    with _lock:
        now = time.time()
        if _cache["data"] is None or now >= _cache["expires_at"]:
            _cache["data"] = _compute()
            _cache["expires_at"] = now + _CACHE_TTL_SECONDS
        return _cache["data"]
