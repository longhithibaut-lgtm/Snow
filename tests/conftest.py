import numpy as np
import pandas as pd
import pytest

from snow.config import BotConfig, RiskConfig, StrategyConfig


def make_bars(closes, start="2024-01-01", freq="D"):
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range(start, periods=len(closes), freq=freq)
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) * 1.01,
                         "low": np.minimum(opens, closes) * 0.99, "close": closes,
                         "volume": 1000.0}, index=idx)


@pytest.fixture
def bars_factory():
    return make_bars


@pytest.fixture
def bot():
    return BotConfig(name="t", symbols=["AAA", "BBB"], strategy=StrategyConfig("sma_cross", {"fast": 3, "slow": 5}),
                     risk=RiskConfig(rebalance_threshold_pct=0.01, min_order_value=1, cash_buffer_pct=0.0))
