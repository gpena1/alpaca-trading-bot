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

import pandas as pd

import config
from bot.broker import Broker
from bot.dca import (
    DCA_LEVELS,
    OPTIONS_LAYER,
    TIERS,
    WATCHLIST_CATEGORIES,
    compute_dca_status,
    traded_instruments_for_tier,
)
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

# Portfolio Architecture: Growth/Aggressive tiers are separate paper
# sub-accounts. None if their env vars aren't set -- the tier just shows as
# "not connected" rather than failing the whole dashboard.
_growth_broker = (
    Broker(config.ALPACA_GROWTH_API_KEY, config.ALPACA_GROWTH_SECRET_KEY)
    if config.ALPACA_GROWTH_API_KEY and config.ALPACA_GROWTH_SECRET_KEY
    else None
)
_aggressive_broker = (
    Broker(config.ALPACA_AGGRESSIVE_API_KEY, config.ALPACA_AGGRESSIVE_SECRET_KEY)
    if config.ALPACA_AGGRESSIVE_API_KEY and config.ALPACA_AGGRESSIVE_SECRET_KEY
    else None
)
_growth_portfolio = Portfolio(_growth_broker) if _growth_broker else None
_aggressive_portfolio = Portfolio(_aggressive_broker) if _aggressive_broker else None

TIER_BROKERS = {"conservative": _broker, "growth": _growth_broker, "aggressive": _aggressive_broker}
TIER_PORTFOLIOS = {"conservative": _portfolio, "growth": _growth_portfolio, "aggressive": _aggressive_portfolio}

_CACHE_TTL_SECONDS = 45
# One lock per tier (not a single shared lock) -- rebuilding Growth's cache
# must never block a concurrent request for Conservative or Aggressive.
_locks = {tier_key: threading.Lock() for tier_key in ("conservative", "growth", "aggressive")}
_cache = {}  # tier_key -> {"data":..., "expires_at":...}

# Kept under urllib3's default per-host connection pool size (10) so
# concurrent fetches don't churn through discarded/recreated connections.
_FETCH_POOL_WORKERS = 5

# Weekly bars for the ~24-symbol watchlist are expensive to fetch and barely
# move within a single day, so they get a much longer-lived cache than the
# rest of the (45s) dashboard payload.
_WATCHLIST_CACHE_TTL_SECONDS = 900
_watchlist_lock = threading.Lock()
_watchlist_cache = {"data": None, "expires_at": 0.0}


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
    """Runs fn(item) for every item concurrently (I/O-bound Alpaca calls,
    one per symbol) instead of sequentially -- this is what actually makes
    tab switches fast, since a Growth/Aggressive row list means 10-14
    separate weekly-bar fetches. Order is preserved; a failed item still
    yields via fn's own try/except, this just doesn't let one slow call
    block the rest."""
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

    # Observed on the Growth/Aggressive accounts: Alpaca's intraday
    # portfolio_history sometimes double-counts base_value, offsetting every
    # point by +base_value (e.g. a $1M account reports a $2M start). Detect
    # it by comparing the most recent point against the account's live,
    # authoritative equity -- if they disagree by roughly base_value,
    # subtract it back out of the whole series. Self-healing if Alpaca fixes
    # this upstream: the correction just stops triggering.
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


def _tier_account(broker):
    if broker is None:
        return {"connected": False, "equity": None, "cash": None}
    try:
        account = broker.get_account()
        return {"connected": True, "equity": float(account.equity), "cash": float(account.cash)}
    except Exception:
        logger.exception("failed to fetch Portfolio Architecture tier account")
        return {"connected": False, "equity": None, "cash": None}


def _compute_tiers():
    accounts = {
        "aggressive": _tier_account(_aggressive_broker),
        "growth": _tier_account(_growth_broker),
        "conservative": {"connected": True, "equity": _portfolio.equity(), "cash": _portfolio.cash()},
    }
    return [{**tier, "account": accounts[tier["key"]]} for tier in TIERS]


_ARMED_LABELS = {
    0: "Above Level 1",
    1: "Level 1 armed",
    2: "Level 2 armed",
    3: "Level 3 armed",
    4: "Level 4 — max add",
}
_ARMED_CLASSES = {
    0: "dca-none",
    1: "dca-armed",
    2: "dca-armed",
    3: "dca-ready",
    4: "dca-ready",
}
_DCA_BADGE_TEXT = {0: "above L1", 1: "L1 armed", 2: "L2 armed", 3: "L3 armed", 4: "L4 max"}


def _fetch_watchlist_status(inst):
    try:
        bars = _broker.get_weekly_bars(inst["symbol"], crypto=inst["crypto"])
        return compute_dca_status(bars)
    except Exception:
        logger.exception("%s: failed to fetch weekly bars for DCA status", inst["symbol"])
        return compute_dca_status(pd.DataFrame())


def _compute_watchlist():
    all_instruments = [inst for cat in WATCHLIST_CATEGORIES for inst in cat["instruments"]]
    statuses = _parallel_map(_fetch_watchlist_status, all_instruments)
    status_by_symbol = dict(zip((inst["symbol"] for inst in all_instruments), statuses))

    categories = []
    for cat in WATCHLIST_CATEGORIES:
        instruments = []
        for inst in cat["instruments"]:
            status = status_by_symbol[inst["symbol"]]
            if status.error:
                status_label, status_class = "No data", "dca-none"
            else:
                status_label = _ARMED_LABELS[status.armed_level]
                status_class = _ARMED_CLASSES[status.armed_level]
            instruments.append(
                {**inst, "status": status, "status_label": status_label, "status_class": status_class}
            )
        categories.append({**cat, "instruments": instruments})
    return categories


def get_watchlist_data():
    with _watchlist_lock:
        now = time.time()
        if _watchlist_cache["data"] is None or now >= _watchlist_cache["expires_at"]:
            _watchlist_cache["data"] = _compute_watchlist()
            _watchlist_cache["expires_at"] = now + _WATCHLIST_CACHE_TTL_SECONDS
        return _watchlist_cache["data"]


def _fetch_conservative_row(symbol):
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


def _conservative_rows():
    # Parallel across symbols: each row needs its own bars fetch + position
    # lookup, both separate Alpaca round-trips that don't depend on any
    # other symbol's result.
    return _parallel_map(_fetch_conservative_row, config.SYMBOLS)


def _fetch_dca_row(args):
    tier_key, broker, portfolio, inst = args
    symbol, crypto, label = inst["symbol"], inst["crypto"], inst["label"]
    bars = pd.DataFrame()
    try:
        bars = broker.get_weekly_bars(symbol, crypto=crypto)
        status = compute_dca_status(bars)
    except Exception:
        logger.exception("%s [%s]: failed to compute DCA row", symbol, tier_key)
        status = compute_dca_status(pd.DataFrame())

    if status.error:
        badge_class, badge_text, reason = "dca-none", "no data", "price data unavailable"
    else:
        badge_class = _ARMED_CLASSES[status.armed_level]
        badge_text = _DCA_BADGE_TEXT[status.armed_level]
        reason = (
            f"{status.pct_from_level1:+.1f}% vs Level 1 (${status.level1:.2f})"
            if status.pct_from_level1 is not None
            else "insufficient weekly history"
        )

    return {
        "symbol": symbol,
        "category": label,
        "position": portfolio.position_for(symbol),
        "badge_class": badge_class,
        "badge_text": badge_text,
        "reason": reason,
        "price": status.price,
        "sparkline": _sparkline_from_bars(bars),
    }


def _dca_rows(tier_key, broker, portfolio):
    instruments = traded_instruments_for_tier(tier_key)
    args = [(tier_key, broker, portfolio, inst) for inst in instruments]
    return _parallel_map(_fetch_dca_row, args)


_TIER_INSTRUMENTS_LABEL = {"conservative": "Strategy", "growth": "Category", "aggressive": "Category"}


def _compute_for_tier(tier_key):
    if tier_key == "conservative":
        broker, portfolio = _broker, _portfolio
        rows = _conservative_rows()
    else:
        broker = TIER_BROKERS.get(tier_key)
        portfolio = TIER_PORTFOLIOS.get(tier_key)
        if broker is None or portfolio is None:
            return {"connected": False, "tier": tier_key}
        rows = _dca_rows(tier_key, broker, portfolio)

    equity = portfolio.equity()
    chart = _compute_equity_chart(broker, equity)
    if chart and equity > 0 and abs(chart["end_equity"] - equity) / equity > 0.05:
        # Safety net: if the base_value correction above didn't fully
        # resolve the disagreement (e.g. a different Alpaca data issue),
        # don't show a chart known to be wrong -- the template's existing
        # "not enough history" empty state covers this gracefully.
        logger.warning(
            "%s: discarding equity chart, last point $%.2f still disagrees with account equity $%.2f",
            tier_key,
            chart["end_equity"],
            equity,
        )
        chart = None

    return {
        "connected": True,
        "tier": tier_key,
        "instruments_label": _TIER_INSTRUMENTS_LABEL[tier_key],
        "generated_at": _fmt_chicago(datetime.now(timezone.utc), fmt="%b %-d, %Y %-I:%M:%S %p"),
        "market_open": broker.is_market_open(),
        "equity": equity,
        "cash": portfolio.cash(),
        "exposure_pct": portfolio.exposure_pct(),
        "rows": rows,
        "chart": chart,
        "performance": _compute_transactions(broker),
    }


def _build_payload(tier_key):
    payload = _compute_for_tier(tier_key)
    payload["tiers"] = _compute_tiers()
    payload["watchlist"] = get_watchlist_data()
    payload["dca_levels"] = DCA_LEVELS
    payload["options_layer"] = OPTIONS_LAYER
    return payload


def get_dashboard_data(tier_key="conservative"):
    if tier_key not in TIER_BROKERS:
        tier_key = "conservative"
    with _locks[tier_key]:
        now = time.time()
        entry = _cache.get(tier_key)
        if entry is None or now >= entry["expires_at"]:
            entry = {"data": _build_payload(tier_key), "expires_at": now + _CACHE_TTL_SECONDS}
            _cache[tier_key] = entry
        return entry["data"]


# Background pre-warming: refreshes all three tiers' caches on a fixed
# cadence (a little faster than they expire) so a tab click almost always
# reads an already-warm cache instead of triggering the computation live.
# Only runs while someone's actually visiting the dashboard -- Render's free
# tier has very little CPU, and there's no reason to keep doing concurrent
# background fetches around the clock when nobody's looking. Idles down to
# doing nothing (just a cheap timestamp check every few seconds) after a
# few minutes of no visits, and picks back up the moment a real request
# comes in via mark_activity().
_PREWARM_INTERVAL_SECONDS = 35
_IDLE_TIMEOUT_SECONDS = 300
_last_activity_at = time.time()


def mark_activity():
    global _last_activity_at
    _last_activity_at = time.time()


def _prewarm_loop():
    while True:
        if time.time() - _last_activity_at <= _IDLE_TIMEOUT_SECONDS:
            for tier_key in TIER_BROKERS:
                try:
                    get_dashboard_data(tier_key)
                except Exception:
                    logger.exception("%s: background prewarm failed", tier_key)
            time.sleep(_PREWARM_INTERVAL_SECONDS)
        else:
            time.sleep(5)  # idle: just check back soon in case activity resumes


threading.Thread(target=_prewarm_loop, daemon=True, name="dashboard-prewarm").start()
