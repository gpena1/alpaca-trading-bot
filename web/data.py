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


_CHART_WIDTH = 800
_CHART_HEIGHT = 220
_CHART_PAD_Y = 16


def _compute_equity_chart():
    try:
        history = _broker.get_portfolio_history(period="1D", timeframe="15Min")
    except Exception:
        logger.exception("failed to fetch portfolio history")
        return None

    points_raw = [
        (ts, eq)
        for ts, eq in zip(history.timestamp or [], history.equity or [])
        if eq is not None
    ]
    if len(points_raw) < 2:
        return None

    values = [eq for _, eq in points_raw]
    min_v, max_v = min(values), max(values)
    span = max_v - min_v or 1.0  # avoid div-by-zero on a perfectly flat line
    n = len(points_raw)
    plot_h = _CHART_HEIGHT - 2 * _CHART_PAD_Y

    points = []
    for i, (ts, eq) in enumerate(points_raw):
        x = (i / (n - 1)) * _CHART_WIDTH
        y = _CHART_PAD_Y + plot_h - ((eq - min_v) / span) * plot_h
        points.append(
            {
                "x": round(x, 2),
                "y": round(y, 2),
                "equity": eq,
                "time_label": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%-I:%M %p UTC"),
            }
        )

    polyline = " ".join(f"{p['x']},{p['y']}" for p in points)
    area_path = (
        f"M {points[0]['x']},{_CHART_HEIGHT} "
        + " ".join(f"L {p['x']},{p['y']}" for p in points)
        + f" L {points[-1]['x']},{_CHART_HEIGHT} Z"
    )

    start_equity = points_raw[0][1]
    end_equity = points_raw[-1][1]
    delta = end_equity - start_equity
    delta_pct = (delta / start_equity * 100) if start_equity else 0.0

    return {
        "points": points,
        "polyline": polyline,
        "area_path": area_path,
        "last_point": points[-1],
        "start_equity": start_equity,
        "end_equity": end_equity,
        "delta": delta,
        "delta_pct": delta_pct,
        "is_up": delta >= 0,
        "width": _CHART_WIDTH,
        "height": _CHART_HEIGHT,
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
        "chart": _compute_equity_chart(),
    }


def get_dashboard_data():
    with _lock:
        now = time.time()
        if _cache["data"] is None or now >= _cache["expires_at"]:
            _cache["data"] = _compute()
            _cache["expires_at"] = now + _CACHE_TTL_SECONDS
        return _cache["data"]
