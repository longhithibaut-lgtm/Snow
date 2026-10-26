import pytest

from snow.config import load_config, parse_config


def raw(**bot_overrides):
    bot = {"name": "a", "symbols": ["SPY"], "strategy": {"name": "sma_cross"}}
    bot.update(bot_overrides)
    return {"bots": [bot]}


def test_example_config_parses():
    cfg = load_config("config.example.yaml")
    assert [b.name for b in cfg.bots] == ["crypto-trend", "us-dips"]
    assert cfg.bot("crypto-trend").is_crypto_only


def test_symbol_owned_by_two_bots_rejected():
    r = raw()
    r["bots"].append({"name": "b", "symbols": ["SPY"], "strategy": {"name": "sma_cross"}, "capital_pct": 0.1})
    r["bots"][0]["capital_pct"] = 0.5
    with pytest.raises(ValueError, match="belongs to both"):
        parse_config(r)


def test_capital_over_one_rejected():
    r = raw(capital_pct=0.8)
    r["bots"].append({"name": "b", "symbols": ["QQQ"], "strategy": {"name": "sma_cross"}, "capital_pct": 0.3})
    with pytest.raises(ValueError, match="add up"):
        parse_config(r)


@pytest.mark.parametrize("bad", [
    {"strategy": {"name": "magic"}},
    {"strategy": {"name": "sma_cross", "params": {"fast": 50, "slow": 10}}},
    {"risk": {"max_gross_exposure": 2}},
    {"risk": {"stop_loss": 0.1}},
    {"timeframe": "5Min"},
])
def test_invalid_bot_rejected(bad):
    with pytest.raises(ValueError):
        parse_config(raw(**bad))
