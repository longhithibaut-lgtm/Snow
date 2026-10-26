import numpy as np
import pandas as pd

from snow.backtest import compute_metrics, run_backtest
from snow.config import BacktestConfig
from tests.conftest import make_bars

NO_COSTS = BacktestConfig(initial_cash=10_000, fee_pct_stock=0, fee_pct_crypto=0, slippage_pct=0)


def test_order_fills_at_next_open(bot):
    closes = [100, 100, 100, 100, 100, 101, 102, 103, 104, 105]
    bars = make_bars(closes)
    bars["open"] = bars["close"] + 0.5  # opens distinct from the previous close
    flat = make_bars([50] * 10)
    result = run_backtest(bot, {"AAA": bars, "BBB": flat}, NO_COSTS)
    t, fill = result.fills[0]
    # sma(3) > sma(5) first on bar 5 (close 101); the fill is the open of bar 6.
    assert t == bars.index[6]
    assert fill.symbol == "AAA" and fill.price == bars["open"].iloc[6]


def test_flat_strategy_keeps_cash(bot):
    down = make_bars(np.linspace(200, 100, 40))
    result = run_backtest(bot, {"AAA": down, "BBB": down}, NO_COSTS)
    assert result.fills == []
    assert result.equity.iloc[-1] == 10_000
    assert result.benchmark.iloc[-1] < 10_000


def test_costs_reduce_returns(bot):
    up = make_bars(np.linspace(100, 200, 60))
    costly = BacktestConfig(initial_cash=10_000, fee_pct_stock=0.01, slippage_pct=0.01)
    free = run_backtest(bot, {"AAA": up, "BBB": up}, NO_COSTS).equity.iloc[-1]
    paid = run_backtest(bot, {"AAA": up, "BBB": up}, costly).equity.iloc[-1]
    assert paid < free


def test_mixed_calendars(bot):
    crypto = make_bars(np.linspace(100, 200, 70))
    stock = make_bars(np.linspace(100, 150, 50), freq="B")
    bot = bot.__class__(**{**bot.__dict__, "symbols": ["BTC/USD", "AAA"]})
    result = run_backtest(bot, {"BTC/USD": crypto, "AAA": stock}, NO_COSTS)
    assert result.ppy == 365
    assert result.equity.index.is_monotonic_increasing
    assert {f.symbol for _, f in result.fills} == {"BTC/USD", "AAA"}
    # A stock order only fills on a business day.
    assert all(t.day_of_week < 5 for t, f in result.fills if f.symbol == "AAA")


def test_window_and_split(bot):
    up = make_bars(np.linspace(100, 300, 200))
    result = run_backtest(bot, {"AAA": up, "BBB": up}, NO_COSTS, start="2024-02-01", end="2024-06-01")
    assert result.equity.index[0] == pd.Timestamp("2024-02-01")
    m = result.metrics(split="2024-04-01")
    assert set(m) == {"total", "in-sample", "out-of-sample"}


def test_metrics():
    eq = pd.Series([100, 110, 99, 120], index=pd.date_range("2024-01-01", periods=4))
    m = compute_metrics(eq, 365)
    assert round(m.total_return, 4) == 0.2
    assert round(m.max_drawdown, 4) == -0.1
