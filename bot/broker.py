"""Thin wrapper around the Alpaca SDK for market data and order execution.

Handles the equity/crypto split: equities use the stock data API and are
gated by market hours; crypto uses the crypto data API and trades 24/7.
"""

import logging

import pandas as pd
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

import config

logger = logging.getLogger(__name__)


def is_crypto(symbol: str) -> bool:
    return symbol in config.CRYPTO_SYMBOLS


class Broker:
    def __init__(self):
        self.trading_client = TradingClient(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            paper=config.IS_PAPER,
        )
        self.stock_data_client = StockHistoricalDataClient(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY
        )
        self.crypto_data_client = CryptoHistoricalDataClient(
            config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY
        )

    def is_market_open(self) -> bool:
        clock = self.trading_client.get_clock()
        return clock.is_open

    def get_bars(self, symbol: str, limit: int = config.LOOKBACK_BARS) -> pd.DataFrame:
        timeframe = TimeFrame(config.BAR_TIMEFRAME_MINUTES, TimeFrame.Minute.unit)

        if is_crypto(symbol):
            request = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                limit=limit,
            )
            bar_set = self.crypto_data_client.get_crypto_bars(request)
        else:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                limit=limit,
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

    def submit_market_order(self, symbol: str, qty: float, side: OrderSide):
        # Crypto supports GTC; equities must use DAY orders that respect market hours.
        time_in_force = TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY
        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=time_in_force,
        )
        logger.info("Submitting %s order: %s qty=%s", side.value, symbol, qty)
        return self.trading_client.submit_order(order_request)
