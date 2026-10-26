"""From target exposures to orders, under the bot's risk limits.

``plan_orders`` is pure: the live bot and the backtest both call it with the
account as they see it, so a backtest exercises the same sizing and limits as
the live bot.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from snow.config import BotConfig
from snow.symbols import is_crypto

# approve(symbol) -> (approved, reason). Asked only for new entries.
Approver = Callable[[str], tuple[bool, str]]


@dataclass
class SymbolView:
    symbol: str
    price: float
    target: float          # 0..1 from the strategy, after volatility scaling
    qty: float = 0.0       # currently held
    has_open_order: bool = False

    @property
    def value(self) -> float:
        return self.qty * self.price


@dataclass
class OrderIntent:
    symbol: str
    side: str              # "buy" or "sell"
    qty: float
    price: float           # reference price used for sizing
    reason: str

    @property
    def value(self) -> float:
        return self.qty * self.price


@dataclass
class Plan:
    orders: list[OrderIntent]
    notes: list[str]


def round_qty(symbol: str, qty: float, fractional_shares: bool = True) -> float:
    """Round a quantity down to what the broker accepts."""
    if is_crypto(symbol):
        decimals = 6
    elif fractional_shares:
        decimals = 4
    else:
        decimals = 0
    factor = 10 ** decimals
    return math.floor(qty * factor + 1e-9) / factor


def plan_orders(
    bot: BotConfig,
    equity: float,
    cash: float,
    views: list[SymbolView],
    daily_loss_hit: bool = False,
    approve: Approver | None = None,
) -> Plan:
    risk = bot.risk
    budget = equity * bot.capital_pct
    cap = bot.position_cap_pct
    notes: list[str] = []

    desired = {v.symbol: max(0.0, min(1.0, v.target)) * cap * budget for v in views}
    gross = sum(desired.values())
    if gross > risk.max_gross_exposure * budget > 0:
        scale = risk.max_gross_exposure * budget / gross
        desired = {s: d * scale for s, d in desired.items()}
        notes.append(f"gross exposure capped: targets scaled by {scale:.2f}")

    threshold = max(risk.rebalance_threshold_pct * budget, risk.min_order_value)
    sells: list[OrderIntent] = []
    buys: list[OrderIntent] = []

    for v in views:
        want = desired[v.symbol]
        delta = want - v.value
        if v.price <= 0:
            notes.append(f"{v.symbol}: no price, skipped")
            continue
        if v.has_open_order:
            if abs(delta) >= threshold or (want == 0 and v.qty > 0):
                notes.append(f"{v.symbol}: an order is already open, waiting for it")
            continue

        if want == 0 and v.qty > 0:
            # A full exit is never held back by thresholds, limits or the LLM.
            sells.append(OrderIntent(v.symbol, "sell", v.qty, v.price, "exit: target is flat"))
            continue
        if abs(delta) < threshold:
            continue

        if delta < 0:
            qty = min(v.qty, round_qty(v.symbol, -delta / v.price, bot.fractional_shares))
            if qty > 0:
                sells.append(OrderIntent(v.symbol, "sell", qty, v.price, "trim to target"))
            continue

        if daily_loss_hit:
            notes.append(f"{v.symbol}: buy blocked, daily loss limit reached")
            continue
        entry = v.qty == 0
        if entry and approve is not None:
            ok, why = approve(v.symbol)
            if not ok:
                notes.append(f"{v.symbol}: entry vetoed by LLM filter ({why})")
                continue
        buys.append(OrderIntent(v.symbol, "buy", delta / v.price, v.price,
                                "entry" if entry else "add to target"))

    # Buys are paid from the cash on hand now: sell proceeds may not have
    # settled (stocks queued for the open), so they count from the next run.
    invested = sum(v.value for v in views)
    spendable = min(cash - risk.cash_buffer_pct * budget,
                    budget * (1 - risk.cash_buffer_pct) - invested)
    wanted = sum(b.value for b in buys)
    if buys and wanted > spendable:
        scale = max(0.0, spendable) / wanted
        notes.append(f"buys scaled by {scale:.2f} to fit the cash available (buffer kept)")
        for b in buys:
            b.qty *= scale

    final_buys = []
    for b in buys:
        b.qty = round_qty(b.symbol, b.qty, bot.fractional_shares)
        if b.qty > 0 and b.value >= risk.min_order_value:
            final_buys.append(b)
    return Plan(orders=sells + final_buys, notes=notes)
