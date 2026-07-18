"""Read-only dashboard data, cached briefly to avoid hammering the Alpaca API.

Never calls RiskManager.evaluate() or submits orders — this module only
observes and reports the bot's state, it cannot act on it.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
from bot.broker import Broker
from bot.main import build_strategies
from bot.portfolio import Portfolio

logger = logging.getLogger("web.data")

_CHICAGO = ZoneInfo("America/Chicago")


def _fmt_chicago(dt, fmt="%b %-d, %-I:%M %p"):
    """Format a UTC-aware datetime (or epoch seconds) in Chicago local time,
    with the correct CST/CDT label for that date (zoneinfo handles the DST
    transition automatically -- no manual UTC offset math)."""
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        dt = datetime.fromtimestamp(dt, tz=timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(_CHICAGO)
    return local.strftime(fmt) + " " + local.tzname()


_broker = Broker()
_portfolio = Portfolio(_broker)
_strategies = build_strategies()

_CACHE_TTL_SECONDS = 45
_lock = threading.Lock()
_cache = {"data": None, "expires_at": 0.0}

# Kept under urllib3's default per-host connection pool size (10) so
# concurrent fetches don't churn through discarded/recreated connections.
_FETCH_POOL_WORKERS = 5

_SPARKLINE_WIDTH = 100
_SPARKLINE_HEIGHT = 28
_SPARKLINE_BARS = 30


def _sparkline_from_bars(bars):
    if bars is None or bars.empty:
        return None
    closes = bars["close"].tail(_SPARKLINE_BARS).tolist()
    if len(closes) < 2:
        return None
    min_v, max_v = min(closes), max(closes)
    span = max_v - min_v or 1.0
    n = len(closes)
    pad = 2
    plot_h = _SPARKLINE_HEIGHT - 2 * pad
    xs_ys = []
    for i, v in enumerate(closes):
        x = (i / (n - 1)) * _SPARKLINE_WIDTH
        y = pad + plot_h - ((v - min_v) / span) * plot_h
        xs_ys.append((round(x, 2), round(y, 2)))
    polyline = " ".join(f"{x},{y}" for x, y in xs_ys)
    return {
        "polyline": polyline,
        "is_up": closes[-1] >= closes[0],
        "width": _SPARKLINE_WIDTH,
        "height": _SPARKLINE_HEIGHT,
    }


def _parallel_map(fn, items):
    """Runs fn(item) for every item concurrently (I/O-bound Alpaca calls, one
    per symbol) instead of sequentially. Order is preserved; a failed item
    still yields via fn's own try/except, this just doesn't let one slow
    call block the rest."""
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(_FETCH_POOL_WORKERS, len(items))) as pool:
        return list(pool.map(fn, items))


def _signal_for(symbol):
    bars = _broker.get_bars(symbol)
    if bars.empty:
        return {"action": "hold", "reason": "no bar data available", "price": None, "sparkline": None}
    signal = _strategies[symbol].generate_signal(bars)
    return {
        "action": signal.action.value,
        "reason": signal.reason,
        "price": float(bars["close"].iloc[-1]),
        "sparkline": _sparkline_from_bars(bars),
    }


_CHART_WIDTH = 800
_CHART_HEIGHT = 220
_CHART_PAD_Y = 16


def _compute_equity_chart(broker, live_equity):
    try:
        history = broker.get_portfolio_history(period="1D", timeframe="15Min")
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

    # Alpaca's intraday portfolio_history sometimes double-counts base_value,
    # offsetting every point by +base_value (e.g. a $1M account reports a $2M
    # start). Detect it by comparing the most recent point against the
    # account's live, authoritative equity -- if they disagree by roughly
    # base_value, subtract it back out of the whole series. Self-healing if
    # Alpaca fixes this upstream: the correction just stops triggering.
    base_value = float(history.base_value) if history.base_value else 0.0
    if base_value > 0 and live_equity > 0:
        offset = points_raw[-1][1] - live_equity
        if abs(offset - base_value) / base_value < 0.05:
            points_raw = [(ts, eq - base_value) for ts, eq in points_raw]

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
                "time_label": _fmt_chicago(ts, fmt="%-I:%M %p"),
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


_PERFORMANCE_WINDOW_DAYS = 7


def _compute_transactions(broker):
    """Single source of truth for both the performance summary tiles and the
    full transaction history table -- one fetch, one chronological pairing
    walk, so the two sections can never disagree with each other."""
    empty = {
        "history": [],
        "win_rate": None,
        "closed_count": 0,
        "win_count": 0,
        "total_realized_pnl": 0.0,
        "open_count": 0,
        "window_days": _PERFORMANCE_WINDOW_DAYS,
    }
    try:
        orders = broker.get_filled_orders_since(days=_PERFORMANCE_WINDOW_DAYS)
    except Exception:
        logger.exception("failed to fetch filled orders for transaction history")
        return empty
    if not orders:
        return empty

    # No shorting in this bot: each symbol's fills alternate BUY, SELL, BUY, SELL...
    # so a simple per-symbol chronological walk pairs every round trip correctly.
    open_buys = {}  # symbol -> {qty, price}
    history = []

    for order in orders:
        side = order.side.value if hasattr(order.side, "value") else order.side
        qty = float(order.filled_qty)
        price = float(order.filled_avg_price)
        entry = {
            "time": _fmt_chicago(order.filled_at),
            "sort_key": order.filled_at,
            "symbol": order.symbol,
            "action": "Bought" if side == "buy" else "Sold",
            "side": side,
            "price": price,
            "qty": qty,
            "realized_pnl": None,
            "is_win": None,
        }

        if side == "buy":
            open_buys[order.symbol] = {"qty": qty, "price": price}
        elif side == "sell" and order.symbol in open_buys:
            buy = open_buys.pop(order.symbol)
            realized_pnl = (price - buy["price"]) * qty
            entry["realized_pnl"] = realized_pnl
            entry["is_win"] = realized_pnl >= 0

        history.append(entry)

    closed_trades = [e for e in history if e["realized_pnl"] is not None]
    closed = len(closed_trades)
    wins = sum(1 for t in closed_trades if t["is_win"])
    win_rate = (wins / closed * 100) if closed else None
    total_realized_pnl = sum(t["realized_pnl"] for t in closed_trades)

    return {
        "history": sorted(history, key=lambda e: e["sort_key"], reverse=True),
        "win_rate": win_rate,
        "closed_count": closed,
        "win_count": wins,
        "total_realized_pnl": total_realized_pnl,
        "open_count": len(open_buys),
        "window_days": _PERFORMANCE_WINDOW_DAYS,
    }


def _fetch_row(symbol):
    try:
        sig = _signal_for(symbol)
    except Exception:
        logger.exception("%s: signal computation failed", symbol)
        sig = {"action": "hold", "reason": "error fetching data", "price": None, "sparkline": None}
    position = _portfolio.position_for(symbol)
    return {
        "symbol": symbol,
        "category": _strategies[symbol].name,
        "position": position,
        "badge_class": sig["action"],
        "badge_text": sig["action"],
        "reason": sig["reason"],
        "price": sig["price"],
        "sparkline": sig["sparkline"],
    }


def _compute_rows():
    # Parallel across symbols: each row needs its own bars fetch + position
    # lookup, both separate Alpaca round-trips that don't depend on any
    # other symbol's result.
    return _parallel_map(_fetch_row, config.SYMBOLS)


def _fetch_account_block():
    return _portfolio.equity(), _portfolio.cash(), _portfolio.exposure_pct(), _broker.is_market_open()


def _build_payload():
    # rows, the account block (equity/cash/exposure/market status), and
    # transaction history are all independent of each other -- run them
    # concurrently instead of one after another. Each can take up to the
    # 15s per-call Alpaca timeout on its own; running them sequentially let
    # a single request's worst case stack up past a minute, which is long
    # enough to exceed gunicorn's worker timeout and get the whole process
    # killed mid-request -- confirmed live as an intermittent 502 Bad
    # Gateway from Render's proxy while a fresh worker spins up.
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows_future = pool.submit(_compute_rows)
        account_future = pool.submit(_fetch_account_block)
        transactions_future = pool.submit(_compute_transactions, _broker)

        rows = rows_future.result()
        performance = transactions_future.result()
        try:
            equity, cash, exposure_pct, market_open = account_future.result()
        except Exception:
            # No internal fallback for these 4 calls (unlike position_for,
            # which already degrades to None on failure) -- surface a
            # clean, friendly degraded state instead of a raw 500.
            logger.exception("failed to fetch core account data")
            return {"connected": False, "temporarily_unavailable": True}

    chart = _compute_equity_chart(_broker, equity)
    if chart and equity > 0 and abs(chart["end_equity"] - equity) / equity > 0.05:
        # Safety net: if the base_value correction above didn't fully
        # resolve the disagreement (e.g. a different Alpaca data issue),
        # don't show a chart known to be wrong -- the template's existing
        # "not enough history" empty state covers this gracefully.
        logger.warning(
            "discarding equity chart, last point $%.2f still disagrees with account equity $%.2f",
            chart["end_equity"],
            equity,
        )
        chart = None

    return {
        "connected": True,
        "generated_at": _fmt_chicago(datetime.now(timezone.utc), fmt="%b %-d, %Y %-I:%M:%S %p"),
        "market_open": market_open,
        "equity": equity,
        "cash": cash,
        "exposure_pct": exposure_pct,
        "rows": rows,
        "chart": chart,
        "performance": performance,
    }


def get_dashboard_data():
    with _lock:
        now = time.time()
        if _cache["data"] is None or now >= _cache["expires_at"]:
            _cache["data"] = _build_payload()
            _cache["expires_at"] = now + _CACHE_TTL_SECONDS
        return _cache["data"]
