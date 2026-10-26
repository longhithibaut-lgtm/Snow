from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from snow.brokers import AlpacaBroker, LiveTradingNotConfirmed, SimBroker
from snow.config import Config
from snow.engine import closed_bars, run_bot
from tests.conftest import make_bars

NOW = datetime(2024, 3, 1, 21, 0, tzinfo=timezone.utc)


class FakeData:
    def __init__(self, bars):
        self.bars = bars

    def get_bars(self, symbol, timeframe, lookback):
        return self.bars[symbol]


@pytest.fixture
def cfg(bot, tmp_path):
    return Config(bots=[bot], state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))


def uptrend():
    return make_bars(np.linspace(100, 150, 40), start="2024-01-01")


def test_run_places_entries(cfg, bot):
    broker = SimBroker(cash=10_000)
    report = run_bot(cfg, bot, broker, FakeData({"AAA": uptrend(), "BBB": uptrend()}), now=NOW)
    assert {o.symbol for o in report.orders} == {"AAA", "BBB"}
    assert broker.open_order_symbols() == {"AAA", "BBB"}


def test_second_run_does_not_duplicate_open_orders(cfg, bot):
    broker = SimBroker(cash=10_000)
    data = FakeData({"AAA": uptrend(), "BBB": uptrend()})
    run_bot(cfg, bot, broker, data, now=NOW)
    report = run_bot(cfg, bot, broker, data, now=NOW)
    assert report.orders == []
    assert len(broker.pending) == 2


def test_dry_run_sends_nothing_and_journals(cfg, bot, tmp_path):
    broker = SimBroker(cash=10_000)
    report = run_bot(cfg, bot, broker, FakeData({"AAA": uptrend(), "BBB": uptrend()}), dry_run=True, now=NOW)
    assert report.orders and broker.pending == []
    journal = pd.read_csv(tmp_path / "logs" / "trades.csv")
    assert set(journal["status"]) == {"not sent"}


def test_kill_switch(cfg, bot, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "STOP").write_text("x")
    broker = SimBroker(cash=10_000)
    report = run_bot(cfg, bot, broker, FakeData({"AAA": uptrend(), "BBB": uptrend()}), now=NOW)
    assert report.skipped and broker.pending == []


def test_llm_filter_vetoes_entries(cfg, bot):
    bot.llm_filter.enabled = True
    broker = SimBroker(cash=10_000)
    report = run_bot(cfg, bot, broker, FakeData({"AAA": uptrend(), "BBB": uptrend()}), now=NOW,
                     approver_factory=lambda b, s: (lambda sym: (sym == "BBB", "test")))
    assert [o.symbol for o in report.orders] == ["BBB"]


def test_data_failure_leaves_symbol_alone(cfg, bot):
    class Broken(FakeData):
        def get_bars(self, symbol, timeframe, lookback):
            if symbol == "AAA":
                raise ConnectionError("down")
            return super().get_bars(symbol, timeframe, lookback)

    broker = SimBroker(cash=5_000, holdings={"AAA": 50})
    broker.mark({"AAA": 100})
    report = run_bot(cfg, bot, broker, Broken({"BBB": uptrend()}), now=NOW)
    assert all(o.symbol != "AAA" for o in report.orders)
    assert any("AAA" in e for e in report.errors)


def test_closed_bars_drops_forming_bar():
    bars = make_bars([1, 2, 3], start="2024-02-28")  # last bar starts 2024-03-01 00:00
    assert len(closed_bars(bars, "BTC/USD", "1Day", True, NOW)) == 2
    assert len(closed_bars(bars, "BTC/USD", "1Day", True, datetime(2024, 3, 2, 0, 1, tzinfo=timezone.utc))) == 3
    # After the US close, today's stock bar is final.
    assert len(closed_bars(bars, "SPY", "1Day", False, NOW)) == 3
    assert len(closed_bars(bars, "SPY", "1Day", True, NOW)) == 2


def test_live_needs_confirmation(monkeypatch):
    monkeypatch.delenv("SNOW_LIVE_CONFIRM", raising=False)
    with pytest.raises(LiveTradingNotConfirmed):
        AlpacaBroker(["SPY"], live=True)
