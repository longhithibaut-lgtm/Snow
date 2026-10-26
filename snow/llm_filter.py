"""LLM veto on new entries, through TradingAgents.

The rules decide; the LLM only gets to say no to opening a position. It is asked
once per symbol and day (the answer is cached in the bot's state), never about
exits, and never in backtests: an LLM asked about a past date may already know
what happened next, which would flatter the results.

TradingAgents is optional: ``pip install -e ".[llm]"``.
"""

from __future__ import annotations

import logging
from datetime import date

from snow.config import LLMFilterConfig
from snow.state import BotState
from snow.symbols import is_crypto, to_yahoo

log = logging.getLogger(__name__)


class TradingAgentsFilter:
    def __init__(self, cfg: LLMFilterConfig, state: BotState):
        self.cfg = cfg
        self.state = state
        self._graph = None

    def _graph_instance(self):
        if self._graph is None:
            try:
                from tradingagents.default_config import DEFAULT_CONFIG
                from tradingagents.graph.trading_graph import TradingAgentsGraph
            except ImportError as exc:
                raise RuntimeError(
                    "the LLM filter needs TradingAgents: pip install -e \".[llm]\"") from exc
            config = {**DEFAULT_CONFIG, **self.cfg.tradingagents_config}
            self._graph = TradingAgentsGraph(config=config)
        return self._graph

    def rating(self, symbol: str, day: str) -> str:
        cached = self.state.llm_cache.get(f"{symbol}|{day}")
        if cached:
            return cached
        _, rating = self._graph_instance().propagate(
            to_yahoo(symbol), day, asset_type="crypto" if is_crypto(symbol) else "stock")
        self.state.llm_cache[f"{symbol}|{day}"] = rating
        self.state.save()
        return rating

    def approve(self, symbol: str) -> tuple[bool, str]:
        day = date.today().isoformat()
        try:
            rating = self.rating(symbol, day)
        except Exception as exc:  # noqa: BLE001 - any failure falls back to on_error
            log.warning("LLM filter failed for %s: %s", symbol, exc)
            if self.cfg.on_error == "allow":
                return True, f"LLM error, allowed: {exc}"
            return False, f"LLM error, blocked: {exc}"
        if rating in self.cfg.veto_ratings or rating == "REVIEW":
            return False, f"rating {rating}"
        return True, f"rating {rating}"
