"""Backtest a bot on historical bars.

The bot is replayed bar by bar with the live code path: targets from the same
strategy, orders from the same ``plan_orders`` and its risk limits. An order
decided on the close of bar *t* fills at the open of the next bar of that
symbol, with fees and slippage. The LLM filter is not used: a model asked about
a past date may already know what came next.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from snow.brokers import Fill, SimBroker
from snow.config import BacktestConfig, BotConfig
from snow.data import periods_per_year
from snow.engine import target_series
from snow.risk import SymbolView, plan_orders
from snow.strategies import make_strategy
from snow.symbols import is_crypto


@dataclass
class Metrics:
    start: str
    end: str
    total_return: float
    cagr: float
    volatility: float
    sharpe: float
    max_drawdown: float

    def row(self) -> dict:
        return {
            "période": f"{self.start} → {self.end}",
            "rendement": f"{self.total_return:+.1%}",
            "CAGR": f"{self.cagr:+.1%}",
            "volatilité": f"{self.volatility:.1%}",
            "Sharpe": f"{self.sharpe:.2f}",
            "max drawdown": f"{self.max_drawdown:.1%}",
        }


def compute_metrics(equity: pd.Series, ppy: int) -> Metrics:
    equity = equity.dropna()
    returns = equity.pct_change().dropna()
    days = max((equity.index[-1] - equity.index[0]).days, 1)
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (1 + total) ** (365.25 / days) - 1 if total > -1 else -1.0
    std = returns.std()
    sharpe = returns.mean() / std * math.sqrt(ppy) if std and std > 0 else 0.0
    drawdown = (equity / equity.cummax() - 1).min()
    return Metrics(
        start=str(equity.index[0].date()), end=str(equity.index[-1].date()),
        total_return=float(total), cagr=float(cagr),
        volatility=float(std * math.sqrt(ppy)) if std == std else 0.0,
        sharpe=float(sharpe), max_drawdown=float(drawdown),
    )


@dataclass
class BacktestResult:
    bot: str
    equity: pd.Series
    benchmark: pd.Series
    fills: list[tuple[pd.Timestamp, Fill]] = field(default_factory=list)
    exposure: pd.Series | None = None
    ppy: int = 252

    @property
    def fees(self) -> float:
        return sum(f.fee for _, f in self.fills)

    def metrics(self, split: str | None = None) -> dict[str, tuple[Metrics, Metrics]]:
        """Strategy and buy-and-hold metrics over the whole run, and before/after ``split``."""
        out = {"total": (compute_metrics(self.equity, self.ppy), compute_metrics(self.benchmark, self.ppy))}
        if split:
            cut = pd.Timestamp(split)
            for label, mask in (("in-sample", self.equity.index < cut), ("out-of-sample", self.equity.index >= cut)):
                if mask.sum() > 2:
                    out[label] = (compute_metrics(self.equity[mask], self.ppy),
                                  compute_metrics(self.benchmark[mask], self.ppy))
        return out

    def fills_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{"date": t, **dataclasses.asdict(f)} for t, f in self.fills])


def run_backtest(bot: BotConfig, histories: dict[str, pd.DataFrame], settings: BacktestConfig,
                 start: str | None = None, end: str | None = None) -> BacktestResult:
    """Replay ``bot`` on ``histories`` (bars per symbol, which may start before ``start`` for warm-up)."""
    # The bot gets the whole simulated account.
    bot = dataclasses.replace(bot, capital_pct=1.0)
    strategy = make_strategy(bot.strategy.name, bot.strategy.params)
    targets = {s: target_series(bot, strategy, h, s) for s, h in histories.items()}

    index = sorted(set().union(*(h.index for h in histories.values())))
    index = pd.DatetimeIndex(index)
    if start:
        index = index[index >= pd.Timestamp(start)]
    if end:
        index = index[index <= pd.Timestamp(end)]
    if len(index) < 2:
        raise ValueError("not enough bars in the backtest window")

    broker = SimBroker(cash=settings.initial_cash, fee_pct_stock=settings.fee_pct_stock,
                       fee_pct_crypto=settings.fee_pct_crypto, slippage_pct=settings.slippage_pct)
    equity, exposure, fills = {}, {}, []
    last_target: dict[str, float] = {}
    prev_equity = settings.initial_cash

    for t in index:
        today = {s: h.loc[t] for s, h in histories.items() if t in h.index}
        for f in broker.fill_pending({s: float(b["open"]) for s, b in today.items()}):
            fills.append((t, f))
        broker.mark({s: float(b["close"]) for s, b in today.items()})
        eq = broker.equity()
        equity[t] = eq
        exposure[t] = 1 - broker.cash / eq if eq > 0 else 0.0

        for s in today:
            last_target[s] = float(targets[s].loc[t])
        views = [SymbolView(s, price=broker.marks[s], target=last_target[s],
                            qty=broker.holdings.get(s, 0.0), has_open_order=s in broker.open_order_symbols())
                 for s in last_target]
        daily_loss_hit = eq < prev_equity * (1 - bot.risk.daily_loss_limit_pct)
        plan = plan_orders(bot, equity=eq, cash=broker.cash, views=views, daily_loss_hit=daily_loss_hit)
        for order in plan.orders:
            broker.submit(order)
        prev_equity = eq

    equity = pd.Series(equity, name="strategy")
    any_crypto = any(is_crypto(s) for s in histories)
    ppy = periods_per_year("X/USD" if any_crypto else "X", bot.timeframe)
    return BacktestResult(bot=bot.name, equity=equity, benchmark=buy_and_hold(histories, equity.index, settings),
                          fills=fills, exposure=pd.Series(exposure), ppy=ppy)


def buy_and_hold(histories: dict[str, pd.DataFrame], index: pd.DatetimeIndex,
                 settings: BacktestConfig) -> pd.Series:
    """Equal weights bought at each symbol's first open in the window and held, fees included."""
    legs = []
    for s, h in histories.items():
        h = h[h.index >= index[0]]
        if h.empty:
            continue
        fee = settings.fee_pct_crypto if is_crypto(s) else settings.fee_pct_stock
        entry = h["open"].iloc[0] * (1 + settings.slippage_pct) * (1 + fee)
        legs.append((h["close"] / entry).reindex(index).ffill().fillna(1.0))
    return pd.Series(np.mean(legs, axis=0) * settings.initial_cash, index=index, name="buy_and_hold")
