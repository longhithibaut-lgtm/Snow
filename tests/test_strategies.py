import numpy as np
import pytest

from snow.strategies import STRATEGIES, DonchianBreakout, RsiReversion, SmaCross, rsi
from tests.conftest import make_bars


def random_walk(n=400, seed=0):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))


@pytest.mark.parametrize("strategy", [SmaCross(5, 20), RsiReversion(14, 35, 55, 50), DonchianBreakout(20, 10)])
def test_signals_never_use_future_bars(strategy):
    bars = make_bars(random_walk())
    full = strategy.signals(bars)
    for cut in (60, 150, 299, 399):
        prefix = strategy.signals(bars.iloc[:cut])
        assert prefix.equals(full.iloc[:cut]), f"{strategy.name} changes past signals when bars are added"


@pytest.mark.parametrize("name", list(STRATEGIES))
def test_targets_between_zero_and_one(name):
    s = STRATEGIES[name]()
    out = s.signals(make_bars(random_walk(600)))
    assert out.between(0, 1).all()


def test_sma_cross_follows_trend():
    up = make_bars(np.linspace(100, 200, 60))
    down = make_bars(np.linspace(200, 100, 60))
    assert SmaCross(5, 20).target(up) == 1.0
    assert SmaCross(5, 20).target(down) == 0.0


def test_target_is_flat_without_enough_history():
    assert SmaCross(5, 20).target(make_bars(np.linspace(100, 200, 10))) == 0.0


def test_rsi_bounds_and_extremes():
    r = rsi(make_bars(random_walk())["close"])
    assert r.dropna().between(0, 100).all()
    assert rsi(make_bars(np.linspace(1, 50, 40))["close"]).iloc[-1] == 100.0


def test_donchian_enters_on_breakout_and_exits_on_breakdown():
    closes = [100] * 25 + [110, 111, 112] + [80]
    out = DonchianBreakout(20, 10).signals(make_bars(closes))
    assert out.iloc[24] == 0 and out.iloc[25] == 1 and out.iloc[-1] == 0


def test_bad_parameters_rejected():
    with pytest.raises(ValueError):
        SmaCross(50, 20)
    with pytest.raises(ValueError):
        RsiReversion(entry=60, exit=50)
