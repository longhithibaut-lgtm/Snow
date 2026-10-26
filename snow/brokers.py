"""Brokers: Alpaca (paper or live) and a local simulator for backtests and dry runs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from snow.risk import OrderIntent
from snow.symbols import is_crypto, position_key


class LiveTradingNotConfirmed(RuntimeError):
    pass


@dataclass
class Account:
    equity: float
    cash: float
    # Equity at the previous close: the reference for the daily loss limit.
    last_equity: float


@dataclass
class Fill:
    symbol: str
    side: str
    qty: float
    price: float
    fee: float


class Broker:
    def account(self) -> Account: ...
    def positions(self) -> dict[str, float]:
        """Held quantity per symbol, keyed as written in the config (``BTC/USD``)."""
    def open_order_symbols(self) -> set[str]: ...
    def submit(self, order: OrderIntent, client_order_id: str | None = None) -> str: ...
    def is_market_open(self) -> bool: ...


class AlpacaBroker(Broker):
    """Alpaca's trading API. Paper unless ``live=True`` *and* SNOW_LIVE_CONFIRM=yes."""

    def __init__(self, symbols: list[str], live: bool = False):
        from alpaca.trading.client import TradingClient

        if live and os.environ.get("SNOW_LIVE_CONFIRM") != "yes":
            raise LiveTradingNotConfirmed(
                "live trading needs both --live and the environment variable SNOW_LIVE_CONFIRM=yes")
        prefix = "ALPACA_LIVE_" if live else "ALPACA_"
        key, secret = os.environ.get(prefix + "API_KEY"), os.environ.get(prefix + "SECRET_KEY")
        if not key or not secret:
            raise RuntimeError(f"set {prefix}API_KEY and {prefix}SECRET_KEY (see .env.example)")
        self.live = live
        self.client = TradingClient(key, secret, paper=not live)
        # Alpaca names crypto positions BTCUSD; map them back to the config's BTC/USD.
        self._names = {position_key(s): s for s in symbols}

    def _name(self, raw: str) -> str:
        return self._names.get(raw, raw)

    def account(self) -> Account:
        a = self.client.get_account()
        return Account(equity=float(a.equity), cash=float(a.cash), last_equity=float(a.last_equity))

    def positions(self) -> dict[str, float]:
        return {self._name(p.symbol): float(p.qty) for p in self.client.get_all_positions()}

    def open_order_symbols(self) -> set[str]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        return {self._name(o.symbol) for o in orders}

    def submit(self, order: OrderIntent, client_order_id: str | None = None) -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        req = MarketOrderRequest(
            symbol=order.symbol,
            qty=order.qty,
            side=OrderSide.BUY if order.side == "buy" else OrderSide.SELL,
            # Crypto trades around the clock; fractional stock orders must be DAY.
            time_in_force=TimeInForce.GTC if is_crypto(order.symbol) else TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        return str(self.client.submit_order(req).id)

    def is_market_open(self) -> bool:
        return bool(self.client.get_clock().is_open)

    def close_all(self, symbols: list[str]) -> list[str]:
        """Close the positions in these symbols at market. Returns the symbols closed."""
        closed = []
        for sym, qty in self.positions().items():
            if sym in symbols and qty != 0:
                self.client.close_position(position_key(sym))
                closed.append(sym)
        return closed


@dataclass
class SimBroker(Broker):
    """Fills market orders at the next bar's open, with fees and slippage."""

    cash: float
    fee_pct_stock: float = 0.0
    fee_pct_crypto: float = 0.0025
    slippage_pct: float = 0.0005
    holdings: dict[str, float] = field(default_factory=dict)
    pending: list[OrderIntent] = field(default_factory=list)
    marks: dict[str, float] = field(default_factory=dict)
    last_equity: float | None = None
    market_open: bool = True

    def mark(self, prices: dict[str, float]) -> None:
        self.marks.update({s: p for s, p in prices.items() if p == p})  # skips NaN

    def equity(self) -> float:
        return self.cash + sum(q * self.marks.get(s, 0.0) for s, q in self.holdings.items())

    def account(self) -> Account:
        eq = self.equity()
        return Account(equity=eq, cash=self.cash,
                       last_equity=self.last_equity if self.last_equity is not None else eq)

    def positions(self) -> dict[str, float]:
        return {s: q for s, q in self.holdings.items() if q > 0}

    def open_order_symbols(self) -> set[str]:
        return {o.symbol for o in self.pending}

    def submit(self, order: OrderIntent, client_order_id: str | None = None) -> str:
        self.pending.append(order)
        return f"sim-{len(self.pending)}"

    def is_market_open(self) -> bool:
        return self.market_open

    def fill_pending(self, open_prices: dict[str, float]) -> list[Fill]:
        """Fill the orders whose symbol has a price in ``open_prices``; the rest wait."""
        fills, waiting = [], []
        # Sells first, so their proceeds pay for the buys.
        for o in sorted(self.pending, key=lambda o: o.side != "sell"):
            px = open_prices.get(o.symbol)
            if px is None or px != px or px <= 0:
                waiting.append(o)
                continue
            fee_pct = self.fee_pct_crypto if is_crypto(o.symbol) else self.fee_pct_stock
            held = self.holdings.get(o.symbol, 0.0)
            if o.side == "sell":
                qty = min(o.qty, held)
                price = px * (1 - self.slippage_pct)
                fee = qty * price * fee_pct
                self.cash += qty * price - fee
                self.holdings[o.symbol] = held - qty
            else:
                price = px * (1 + self.slippage_pct)
                # A gap up can make the order cost more than the cash on hand.
                qty = min(o.qty, self.cash / (price * (1 + fee_pct)))
                fee = qty * price * fee_pct
                self.cash -= qty * price + fee
                self.holdings[o.symbol] = held + qty
            if qty > 0:
                fills.append(Fill(o.symbol, o.side, qty, price, fee))
            self.marks[o.symbol] = px
        self.pending = waiting
        return fills
