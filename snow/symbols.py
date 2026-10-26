"""Symbol conventions.

Symbols are written the Alpaca way: ``AAPL`` for a stock, ``BTC/USD`` for a
crypto pair. Yahoo Finance and TradingAgents write crypto as ``BTC-USD``.
"""


def is_crypto(symbol: str) -> bool:
    return "/" in symbol


def to_yahoo(symbol: str) -> str:
    return symbol.replace("/", "-")


def position_key(symbol: str) -> str:
    """Alpaca reports crypto positions without the slash (``BTCUSD``)."""
    return symbol.replace("/", "")
