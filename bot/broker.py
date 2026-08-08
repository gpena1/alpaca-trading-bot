"""Thin wrapper around the Alpaca SDK for market data and order execution.

Handles the equity/crypto split: equities use the stock data API and are
gated by market hours; crypto uses the crypto data API and trades 24/7.

Now fetches DAILY bars and can submit native GTC stop orders, so
protective stops are live on Alpaca's side between daily runs instead of
only being checked when this process happens to be awake.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    GetPortfolioHistoryRequest,
    MarketOrderRequest,
    StopOrderRequest,
)
from requests.adapters import HTTPAdapter

import config

logger = logging.getLogger(__name__)

# alpaca-py's internal request path (RESTClient._one_request) calls
# self._session.request(...) without ever passing a timeout, so a hung or
# very slow Alpaca response blocks that call forever with no way for
# calling code to recover. Enforced here, once, at the requests.Session
# level so it applies to every Alpaca call.
_ALPACA_REQUEST_TIMEOUT_SECONDS = 15


class _TimeoutHTTPAdapter(HTTPAdapter):
    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = _ALPACA_REQUEST_TIMEOUT_SECONDS
        return super().send(request, **kwargs)


def _enforce_timeout(session: requests.Session) -> None:
    session.mount("https://", _TimeoutHTTPAdapter())
    session.mount("http://", _TimeoutHTTPAdapter())


def is_crypto(symbol: str) -> bool:
    return symbol in config.CRYPTO_SYMBOLS


def daily_timeframe() -> TimeFrame:
    return TimeFrame.Day


class Broker:
    def __init__(self):
        self.trading_client = TradingClient(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            paper=config.IS_PAPER,
        )
        self.stock_data_client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
        self.crypto_data_client = CryptoHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
        for client in (self.trading_client, self.stock_data_client, self.crypto_data_client):
            _enforce_timeout(client._session)

    def is_market_open(self) -> bool:
        clock = self.trading_client.get_clock()
        return clock.is_open

    def get_bars(self, symbol: str, limit: int = config.LOOKBACK_BARS) -> pd.DataFrame:
        # Daily bars: request enough CALENDAR days to yield `limit` TRADING
        # days. Equities trade ~252 of 365 days, so 1.6x plus a buffer is
        # comfortable; crypto trades every day and will simply get more
        # than it needs, then be truncated by `limit`.
        calendar_days = int(limit * 1.6) + 30
        start = datetime.now(timezone.utc) - timedelta(days=calendar_days)
        return self.get_bars_range(symbol, is_crypto(symbol), daily_timeframe(), start, None, limit)

    def get_bars_range(
        self,
        symbol: str,
        crypto: bool,
        timeframe: TimeFrame,
        start: datetime,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        if crypto:
            request = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start, end=end)
            bar_set = self.crypto_data_client.get_crypto_bars(request)
        else:
            # feed=IEX is required on the free data plan -- omitting it lets
            # the SDK default toward SIP, which this account isn't entitled
            # to and which silently degrades results instead of erroring.
            # adjustment=ALL gives split- AND dividend-adjusted closes. Without
            # it, a buy-and-hold benchmark is understated by its entire dividend
            # yield, which is the same order of magnitude as any edge being
            # measured against it.
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                feed=DataFeed.IEX,
                adjustment=Adjustment.ALL,
            )
            bar_set = self.stock_data_client.get_stock_bars(request)

        df = bar_set.df
        if df.empty:
            return df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df = df.sort_index()
        if limit is not None and len(df) > limit:
            df = df.iloc[-limit:]
        return df

    def get_account(self):
        return self.trading_client.get_account()

    def get_positions(self):
        return self.trading_client.get_all_positions()

    def get_position(self, symbol: str):
        try:
            return self.trading_client.get_open_position(symbol.replace("/", ""))
        except Exception:
            return None

    def get_recent_orders(self, limit: int = 20):
        request = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit, direction=Sort.DESC)
        return self.trading_client.get_orders(request)

    def get_open_orders(self, symbol: str | None = None):
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500, direction=Sort.DESC)
        orders = self.trading_client.get_orders(request)
        if symbol is None:
            return orders
        key = symbol.replace("/", "")
        return [o for o in orders if o.symbol.replace("/", "") == key]

    def cancel_open_orders_for(self, symbol: str) -> int:
        """Cancel any resting orders (i.e. stale protective stops) for a
        symbol. Must run before reversing or closing a position, or the old
        stop survives the position it was protecting and can open an
        unintended new one when it triggers."""
        cancelled = 0
        for order in self.get_open_orders(symbol):
            try:
                self.trading_client.cancel_order_by_id(order.id)
                cancelled += 1
            except Exception:
                logger.exception("%s: failed to cancel order %s", symbol, order.id)
        if cancelled:
            logger.info("%s: cancelled %d resting order(s)", symbol, cancelled)
        return cancelled

    def get_filled_orders_since(self, days: int = 7):
        after = datetime.now(timezone.utc) - timedelta(days=days)
        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, after=after, direction=Sort.ASC, limit=500
        )
        orders = self.trading_client.get_orders(request)
        return [o for o in orders if o.filled_at is not None]

    def get_portfolio_history(self, period: str = "1M", timeframe: str = "1D"):
        request = GetPortfolioHistoryRequest(period=period, timeframe=timeframe, extended_hours=True)
        return self.trading_client.get_portfolio_history(request)

    def submit_market_order(self, symbol: str, qty: float, side: OrderSide):
        time_in_force = TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY
        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=time_in_force,
        )
        logger.info("Submitting %s market order: %s qty=%s", side.value, symbol, qty)
        return self.trading_client.submit_order(order_request)

    def submit_stop_order(self, symbol: str, qty: float, side: OrderSide, stop_price: float):
        """Protective stop that rests on Alpaca between daily runs.

        Not supported for crypto on Alpaca -- those fall back to the
        in-process stop check in RiskManager, which only fires when the bot
        runs. That is a real coverage gap for BTC/USD and is why the crypto
        sleeve carries a wider effective risk than the equity sleeves.
        """
        if is_crypto(symbol):
            logger.info("%s: native stop orders unsupported for crypto, relying on in-process check", symbol)
            return None
        order_request = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            stop_price=stop_price,
            time_in_force=TimeInForce.GTC,
        )
        logger.info("Submitting protective stop: %s %s qty=%s @ %.2f", symbol, side.value, qty, stop_price)
        return self.trading_client.submit_order(order_request)
