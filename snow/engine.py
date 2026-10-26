"""The live bot: one pass reads the account and the bars, and places the orders
that bring each symbol to its target."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from snow.brokers import Broker
from snow.config import BotConfig, Config
from snow.data import bar_length, periods_per_year
from snow.notify import notify
from snow.risk import OrderIntent, SymbolView, plan_orders
from snow.state import BotState, journal
from snow.strategies import Strategy, make_strategy, vol_scale
from snow.symbols import is_crypto

log = logging.getLogger(__name__)

KILL_SWITCH = "STOP"


def target_series(bot: BotConfig, strategy: Strategy, bars: pd.DataFrame, symbol: str) -> pd.Series:
    """Target exposure per bar: the strategy's signal, scaled down for volatility if configured."""
    targets = strategy.signals(bars)
    if bot.risk.vol_target_annual:
        targets = targets * vol_scale(bars, bot.risk.vol_target_annual, bot.risk.vol_lookback,
                                      periods_per_year(symbol, bot.timeframe))
    return targets.fillna(0.0)


def closed_bars(bars: pd.DataFrame, symbol: str, timeframe: str, market_open: bool,
                now: datetime) -> pd.DataFrame:
    """Drop the bar still forming, so a signal never flips on a partial bar."""
    if bars.empty:
        return bars
    if not is_crypto(symbol) and not market_open:
        return bars  # session over: the last bar is final
    start = bars.index[-1]
    start = start.tz_convert("UTC") if start.tzinfo else start.tz_localize("UTC")
    if start + bar_length(timeframe) > now:
        return bars.iloc[:-1]
    return bars


@dataclass
class RunReport:
    bot: str
    orders: list[OrderIntent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: bool = False

    def summary(self, mode: str) -> str:
        lines = [f"[snow:{self.bot}] ({mode})"]
        lines += [f"  {o.side.upper()} {o.qty:g} {o.symbol} (~{o.value:,.2f} $) - {o.reason}" for o in self.orders]
        lines += [f"  note: {n}" for n in self.notes]
        lines += [f"  ERROR: {e}" for e in self.errors]
        if len(lines) == 1:
            lines.append("  nothing to do")
        return "\n".join(lines)


def run_bot(cfg: Config, bot: BotConfig, broker: Broker, data, mode: str = "paper",
            dry_run: bool = False, approver_factory=None, now: datetime | None = None) -> RunReport:
    """One pass of ``bot``. ``approver_factory(bot, state)`` builds the LLM filter (None = from config)."""
    now = now or datetime.now(timezone.utc)
    report = RunReport(bot=bot.name)

    if (Path(cfg.state_dir) / KILL_SWITCH).exists():
        report.skipped = True
        report.notes.append(f"kill switch: {Path(cfg.state_dir) / KILL_SWITCH} exists, no trading")
        return report

    state = BotState.load(cfg.state_dir, bot.name)
    strategy = make_strategy(bot.strategy.name, bot.strategy.params)
    acct = broker.account()
    positions = broker.positions()
    open_syms = broker.open_order_symbols()
    market_open = broker.is_market_open() if not bot.is_crypto_only else True
    budget = acct.equity * bot.capital_pct

    views = []
    for sym in bot.symbols:
        qty = positions.get(sym, 0.0)
        try:
            bars = closed_bars(data.get_bars(sym, bot.timeframe, bot.lookback_bars),
                               sym, bot.timeframe, market_open, now)
        except Exception as exc:  # noqa: BLE001 - one symbol's data must not stop the others
            bars = pd.DataFrame()
            report.errors.append(f"{sym}: no data ({exc})")
        if len(bars) < strategy.warmup:
            # Not enough history: leave the symbol alone. A data gap is not a reason to sell.
            report.notes.append(f"{sym}: {len(bars)} bars, {strategy.warmup} needed - symbol left as is")
            continue
        price = float(bars["close"].iloc[-1])
        target = float(target_series(bot, strategy, bars, sym).iloc[-1])
        views.append(SymbolView(sym, price=price, target=target, qty=qty, has_open_order=sym in open_syms))
        log.info("%s %s: close=%.4f target=%.2f held=%g", bot.name, sym, price, target, qty)

    daily_loss_hit = acct.equity < acct.last_equity * (1 - bot.risk.daily_loss_limit_pct)
    if daily_loss_hit:
        report.notes.append(f"daily loss limit hit: equity {acct.equity:,.2f} vs {acct.last_equity:,.2f} "
                            f"at the previous close; no new buys today")

    approve = None
    if bot.llm_filter.enabled:
        if approver_factory is None:
            from snow.llm_filter import TradingAgentsFilter

            def approver_factory(b, s):
                return TradingAgentsFilter(b.llm_filter, s).approve
        approve = approver_factory(bot, state)

    plan = plan_orders(bot, equity=acct.equity, cash=acct.cash, views=views,
                       daily_loss_hit=daily_loss_hit, approve=approve)
    report.notes += plan.notes
    log.info("%s: budget %.2f, %d order(s)", bot.name, budget, len(plan.orders))

    stamp = now.strftime("%Y%m%d%H%M%S")
    for order in plan.orders:
        row = {"bot": bot.name, "mode": "dry-run" if dry_run else mode, "symbol": order.symbol,
               "side": order.side, "qty": order.qty, "ref_price": order.price,
               "value": round(order.value, 2), "reason": order.reason}
        if dry_run:
            journal(cfg.log_dir, {**row, "status": "not sent"})
            report.orders.append(order)
            continue
        try:
            oid = broker.submit(order, client_order_id=f"snow-{bot.name}-{order.symbol.replace('/', '')}-{stamp}")
            journal(cfg.log_dir, {**row, "order_id": oid, "status": "submitted"})
            report.orders.append(order)
        except Exception as exc:  # noqa: BLE001
            journal(cfg.log_dir, {**row, "status": f"error: {exc}"})
            report.errors.append(f"{order.side} {order.symbol}: {exc}")

    state.last_run = now.isoformat(timespec="seconds")
    state.save()
    if report.orders or report.errors or daily_loss_hit:
        notify(report.summary("dry-run" if dry_run else mode))
    return report
