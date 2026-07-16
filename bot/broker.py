"""Thin wrapper around the Alpaca SDK for market data and order execution.

Handles the equity/crypto split: equities use the stock data API and are
gated by market hours; crypto uses the crypto data API and trades 24/7.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from alpaca.common.enums import Sort
from alpaca.data.enums import DataFeed
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest, MarketOrderRequest
from requests.adapters import HTTPAdapter

import config

logger = logging.getLogger(__name__)

# alpaca-py's internal request path (RESTClient._one_request) calls
# self._session.request(...) without ever passing a timeout, so a hung or
# very slow Alpaca response (seen in practice tonight: 504s, multi-minute
# stalls) blocks that call forever with no way for calling code to recover.
# With the dashboard now fetching many symbols concurrently per page load,
# a single stuck call can hang the whole page indefinitely -- this is a
# real, confirmed cause of the "white screen that never loads" symptom.
# Enforced here, once, at the requests.Session level so it applies to every
# Alpaca call (trading, stock data, crypto data) without touching each of
# the many call sites throughout the codebase.
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


class Broker:
    def __init__(self, api_key: str | None = None, secret_key: str | None = None):
        # Defaults to the primary account; callers pass explicit credentials
        # to point at one of the other Portfolio Architecture paper accounts.
        api_key = api_key or config.ALPACA_API_KEY
        secret_key = secret_key or config.ALPACA_SECRET_KEY
        self.trading_client = TradingClient(
            api_key,
            secret_key,
            paper=config.IS_PAPER,
        )
        self.stock_data_client = StockHistoricalDataClient(api_key, secret_key)
        self.crypto_data_client = CryptoHistoricalDataClient(api_key, secret_key)
        for client in (self.trading_client, self.stock_data_client, self.crypto_data_client):
            _enforce_timeout(client._session)

    def is_market_open(self) -> bool:
        clock = self.trading_client.get_clock()
        return clock.is_open

    def get_bars(self, symbol: str, limit: int = config.LOOKBACK_BARS) -> pd.DataFrame:
        timeframe = TimeFrame(config.BAR_TIMEFRAME_MINUTES, TimeFrame.Minute.unit)
        # Without an explicit start, Alpaca defaults to "since UTC midnight
        # today", which starves the lookback window (especially for equities,
        # which only trade ~6.5h/day). 10 calendar days comfortably covers
        # LOOKBACK_BARS for both crypto (trades 24/7) and equities (accounting
        # for weekends/holidays).
        start = datetime.now(timezone.utc) - timedelta(days=10)

        if is_crypto(symbol):
            request = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                start=start,
                limit=limit,
            )
            bar_set = self.crypto_data_client.get_crypto_bars(request)
        else:
            # feed=IEX is required on the free data plan -- omitting it lets
            # the SDK default toward SIP, which this account isn't entitled
            # to and which silently degrades results instead of erroring.
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                start=start,
                limit=limit,
                feed=DataFeed.IEX,
            )
            bar_set = self.stock_data_client.get_stock_bars(request)

        df = bar_set.df
        if df.empty:
            return df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return df.sort_index()

    def get_weekly_bars(self, symbol: str, crypto: bool, limit: int = 104) -> pd.DataFrame:
        # 104 weeks (2 years) comfortably covers the 20-week lookback the
        # DCA framework's moving average needs, even after weekends/holidays
        # thin out equity bars.
        start = datetime.now(timezone.utc) - timedelta(weeks=130)
        timeframe = TimeFrame(1, TimeFrame.Week.unit)

        if crypto:
            request = CryptoBarsRequest(
                symbol_or_symbols=symbol, timeframe=timeframe, start=start, limit=limit
            )
            bar_set = self.crypto_data_client.get_crypto_bars(request)
        else:
            request = StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=timeframe, start=start, limit=limit, feed=DataFeed.IEX
            )
            bar_set = self.stock_data_client.get_stock_bars(request)

        df = bar_set.df
        if df.empty:
            return df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return df.sort_index()

    def get_bars_range(
        self, symbol: str, crypto: bool, timeframe: TimeFrame, start: datetime, end: datetime | None = None
    ) -> pd.DataFrame:
        """Arbitrary date-range historical fetch, for the backtester only --
        live trading paths use get_bars/get_weekly_bars, which stay on their
        fixed rolling windows. Alpaca paginates internally for a range this
        wide, so no `limit` is passed (defaults high enough to get it all)."""
        if crypto:
            request = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start, end=end)
            bar_set = self.crypto_data_client.get_crypto_bars(request)
        else:
            request = StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=timeframe, start=start, end=end, feed=DataFeed.IEX
            )
            bar_set = self.stock_data_client.get_stock_bars(request)

        df = bar_set.df
        if df.empty:
            return df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return df.sort_index()

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

    def get_filled_orders_since(self, days: int = 7):
        after = datetime.now(timezone.utc) - timedelta(days=days)
        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, after=after, direction=Sort.ASC, limit=500
        )
        orders = self.trading_client.get_orders(request)
        return [o for o in orders if o.filled_at is not None]

    def get_portfolio_history(self, period: str = "1D", timeframe: str = "15Min"):
        request = GetPortfolioHistoryRequest(
            period=period, timeframe=timeframe, extended_hours=True
        )
        return self.trading_client.get_portfolio_history(request)

    def submit_market_order(self, symbol: str, qty: float, side: OrderSide, crypto: bool | None = None):
        # Crypto supports GTC; equities must use DAY orders that respect market hours.
        # `crypto` lets callers outside config.CRYPTO_SYMBOLS's fixed set (e.g. the
        # Portfolio Architecture tiers' own crypto symbols) state it explicitly;
        # defaults to the old symbol-lookup behavior when not given.
        if crypto is None:
            crypto = is_crypto(symbol)
        time_in_force = TimeInForce.GTC if crypto else TimeInForce.DAY
        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=time_in_force,
        )
        logger.info("Submitting %s order: %s qty=%s", side.value, symbol, qty)
        return self.trading_client.submit_order(order_request)
