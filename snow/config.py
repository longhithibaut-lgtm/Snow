"""Bot configuration, loaded from a YAML file.

Each bot owns a list of symbols and a share of the account (``capital_pct``).
Several bots can share one Alpaca account as long as no symbol belongs to two
bots: a position is attributed to the bot that owns its symbol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from snow.symbols import is_crypto

TIMEFRAMES = ("1Day", "1Hour")


@dataclass
class RiskConfig:
    # Largest share of the bot's budget held in one symbol. None = 1 / number of symbols.
    max_position_pct: float | None = None
    # Sum of all positions as a share of the budget. 1.0 = no leverage.
    max_gross_exposure: float = 1.0
    # No new buys once the account is down this much since the previous close.
    daily_loss_limit_pct: float = 0.03
    # Only trade when the gap to the target is larger than this share of the budget.
    rebalance_threshold_pct: float = 0.02
    min_order_value: float = 10.0
    # Annualized volatility target per position; None disables volatility scaling.
    vol_target_annual: float | None = None
    vol_lookback: int = 20
    # Cash kept aside for fees and price moves between signal and fill.
    cash_buffer_pct: float = 0.02


@dataclass
class LLMFilterConfig:
    enabled: bool = False
    # Ratings that block a new entry. Exits are never blocked.
    veto_ratings: list[str] = field(default_factory=lambda: ["Sell", "Underweight"])
    # What to do when the LLM call fails: "block" skips the entry, "allow" lets it through.
    on_error: str = "block"
    # Passed to TradingAgents' config (llm_provider, deep_think_llm, quick_think_llm...).
    tradingagents_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyConfig:
    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class BotConfig:
    name: str
    symbols: list[str]
    strategy: StrategyConfig
    timeframe: str = "1Day"
    capital_pct: float = 1.0
    # Bars of history fetched for the strategy.
    lookback_bars: int = 300
    # Buy fractions of a share (Alpaca supports it for most US stocks).
    fractional_shares: bool = True
    risk: RiskConfig = field(default_factory=RiskConfig)
    llm_filter: LLMFilterConfig = field(default_factory=LLMFilterConfig)

    @property
    def position_cap_pct(self) -> float:
        if self.risk.max_position_pct is not None:
            return self.risk.max_position_pct
        return 1.0 / len(self.symbols)

    @property
    def is_crypto_only(self) -> bool:
        return all(is_crypto(s) for s in self.symbols)


@dataclass
class BacktestConfig:
    initial_cash: float = 10_000.0
    # Per-side costs as a fraction of the traded value.
    fee_pct_stock: float = 0.0
    fee_pct_crypto: float = 0.0025
    slippage_pct: float = 0.0005


@dataclass
class Config:
    bots: list[BotConfig]
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    state_dir: str = "state"
    log_dir: str = "logs"

    def bot(self, name: str) -> BotConfig:
        for b in self.bots:
            if b.name == name:
                return b
        raise KeyError(f"no bot named {name!r} (known: {', '.join(b.name for b in self.bots)})")


def _build(cls, raw: dict | None, where: str):
    raw = raw or {}
    known = set(cls.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)} (allowed: {sorted(known)})")
    return cls(**raw)


def parse_config(raw: dict) -> Config:
    if not raw or not raw.get("bots"):
        raise ValueError("config: at least one bot is required under 'bots'")

    bots = []
    for i, b in enumerate(raw["bots"]):
        b = dict(b)
        where = f"bots[{i}] ({b.get('name', '?')})"
        b["strategy"] = _build(StrategyConfig, b.get("strategy"), f"{where}.strategy")
        b["risk"] = _build(RiskConfig, b.get("risk"), f"{where}.risk")
        b["llm_filter"] = _build(LLMFilterConfig, b.get("llm_filter"), f"{where}.llm_filter")
        bots.append(_build(BotConfig, b, where))

    cfg = Config(
        bots=bots,
        backtest=_build(BacktestConfig, raw.get("backtest"), "backtest"),
        state_dir=raw.get("state_dir", "state"),
        log_dir=raw.get("log_dir", "logs"),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    from snow.strategies import STRATEGIES

    names, owners = set(), {}
    total_capital = 0.0
    for bot in cfg.bots:
        if bot.name in names:
            raise ValueError(f"duplicate bot name {bot.name!r}")
        names.add(bot.name)
        if not bot.symbols:
            raise ValueError(f"bot {bot.name!r}: no symbols")
        for s in bot.symbols:
            if s in owners:
                raise ValueError(f"symbol {s!r} belongs to both {owners[s]!r} and {bot.name!r}")
            owners[s] = bot.name
        if bot.timeframe not in TIMEFRAMES:
            raise ValueError(f"bot {bot.name!r}: timeframe must be one of {TIMEFRAMES}")
        if bot.strategy.name not in STRATEGIES:
            raise ValueError(f"bot {bot.name!r}: unknown strategy {bot.strategy.name!r} "
                             f"(available: {', '.join(STRATEGIES)})")
        if not 0 < bot.capital_pct <= 1:
            raise ValueError(f"bot {bot.name!r}: capital_pct must be in (0, 1]")
        if not 0 < bot.position_cap_pct <= 1:
            raise ValueError(f"bot {bot.name!r}: max_position_pct must be in (0, 1]")
        if not 0 < bot.risk.max_gross_exposure <= 1:
            raise ValueError(f"bot {bot.name!r}: max_gross_exposure must be in (0, 1] (no leverage)")
        if bot.llm_filter.on_error not in ("block", "allow"):
            raise ValueError(f"bot {bot.name!r}: llm_filter.on_error must be 'block' or 'allow'")
        # Fails early on bad strategy parameters.
        STRATEGIES[bot.strategy.name](**bot.strategy.params)
        total_capital += bot.capital_pct
    if total_capital > 1.0 + 1e-9:
        raise ValueError(f"the bots' capital_pct add up to {total_capital:.2f}, more than the account")


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f))
