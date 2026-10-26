"""Price bars, as DataFrames with columns ``open high low close volume``.

Live runs read Alpaca's data (the same source as the broker); backtests read
Yahoo Finance, which has longer free history. Both return bars oldest first.
"""

from __future__ import annotations

import os
import warnings
from datetime import datetime, timedelta, timezone

import pandas as pd

from snow.symbols import is_crypto, to_yahoo

COLUMNS = ["open", "high", "low", "close", "volume"]


def periods_per_year(symbol: str, timeframe: str) -> int:
    crypto = is_crypto(symbol)
    if timeframe == "1Day":
        return 365 if crypto else 252
    return 24 * 365 if crypto else 252 * 7


def bar_length(timeframe: str) -> timedelta:
    return timedelta(days=1) if timeframe == "1Day" else timedelta(hours=1)


def _lookback_start(symbol: str, timeframe: str, bars: int, now: datetime) -> datetime:
    """A start date far enough back to cover ``bars`` bars, weekends and holidays included."""
    if timeframe == "1Day":
        days = bars if is_crypto(symbol) else bars * 1.5 + 10
    else:
        days = bars / 24 + 2 if is_crypto(symbol) else bars / 7 * 1.5 + 5
    return now - timedelta(days=days)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=str.lower)[COLUMNS].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=["close"])


class AlpacaData:
    def __init__(self, api_key: str | None = None, secret_key: str | None = None):
        from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient

        api_key = api_key or os.environ.get("ALPACA_API_KEY")
        secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY")
        # Crypto bars are public; stock bars need keys.
        self.stocks = StockHistoricalDataClient(api_key, secret_key) if api_key and secret_key else None
        self.crypto = CryptoHistoricalDataClient(api_key, secret_key)

    def get_bars(self, symbol: str, timeframe: str, lookback: int) -> pd.DataFrame:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        tf = TimeFrame.Day if timeframe == "1Day" else TimeFrame.Hour
        now = datetime.now(timezone.utc)
        start = _lookback_start(symbol, timeframe, lookback, now)
        if is_crypto(symbol):
            raw = self.crypto.get_crypto_bars(
                CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start)).df
        else:
            if self.stocks is None:
                raise RuntimeError("stock bars need ALPACA_API_KEY and ALPACA_SECRET_KEY")
            # IEX is the feed free accounts may query for recent bars.
            feed = DataFeed(os.environ.get("ALPACA_DATA_FEED", "iex"))
            raw = self.stocks.get_stock_bars(
                StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start,
                                 adjustment=Adjustment.ALL, feed=feed)).df
        if raw.empty:
            return pd.DataFrame(columns=COLUMNS)
        if isinstance(raw.index, pd.MultiIndex):
            raw = raw.xs(symbol, level="symbol")
        return _clean(raw).tail(lookback)


class YahooData:
    def get_history(self, symbol: str, timeframe: str, start: str, end: str | None = None) -> pd.DataFrame:
        import yfinance as yf

        interval = "1d" if timeframe == "1Day" else "1h"
        # yfinance trips pandas deprecation warnings internally; they are not ours to fix.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            warnings.simplefilter("ignore", FutureWarning)
            raw = yf.Ticker(to_yahoo(symbol)).history(
                start=start, end=end, interval=interval, auto_adjust=True)
        if raw.empty:
            raise ValueError(f"Yahoo Finance returned no data for {symbol} ({start} to {end or 'today'})")
        df = _clean(raw)
        if timeframe == "1Day":
            # Daily bars keyed by calendar date, so stocks and crypto line up.
            df.index = pd.DatetimeIndex(df.index.tz_localize(None).normalize())
        else:
            df.index = df.index.tz_convert("UTC").tz_localize(None)
        return df[~df.index.duplicated(keep="last")]
