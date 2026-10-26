import dataclasses

from snow.config import RiskConfig
from snow.risk import SymbolView, plan_orders, round_qty


def by_symbol(plan):
    return {o.symbol: o for o in plan.orders}


def test_entry_sized_to_position_cap(bot):
    plan = plan_orders(bot, equity=10_000, cash=10_000,
                       views=[SymbolView("AAA", 100, 1.0), SymbolView("BBB", 50, 0.0)])
    (order,) = plan.orders
    assert order.side == "buy" and order.symbol == "AAA"
    assert order.value == 5_000  # 1 / 2 symbols of the budget


def test_exit_sells_everything_even_below_threshold(bot):
    bot = dataclasses.replace(bot, risk=RiskConfig(rebalance_threshold_pct=0.5, min_order_value=1_000))
    plan = plan_orders(bot, equity=10_000, cash=9_990, views=[SymbolView("AAA", 10, 0.0, qty=1)])
    (order,) = plan.orders
    assert (order.side, order.qty) == ("sell", 1)


def test_small_drift_is_not_traded(bot):
    plan = plan_orders(bot, equity=10_000, cash=5_050, views=[SymbolView("AAA", 99, 1.0, qty=50)])
    assert plan.orders == []


def test_daily_loss_limit_blocks_buys_not_sells(bot):
    plan = plan_orders(bot, equity=10_000, cash=5_000, daily_loss_hit=True,
                       views=[SymbolView("AAA", 100, 1.0), SymbolView("BBB", 100, 0.0, qty=50)])
    assert [o.side for o in plan.orders] == ["sell"]
    assert any("daily loss" in n for n in plan.notes)


def test_llm_veto_only_on_new_entries(bot):
    asked = []

    def veto(symbol):
        asked.append(symbol)
        return False, "rating Sell"

    plan = plan_orders(bot, equity=10_000, cash=7_500, approve=veto,
                       views=[SymbolView("AAA", 100, 1.0), SymbolView("BBB", 100, 1.0, qty=25)])
    assert asked == ["AAA"]
    assert [(o.symbol, o.side) for o in plan.orders] == [("BBB", "buy")]


def test_buys_scaled_to_cash(bot):
    plan = plan_orders(bot, equity=10_000, cash=2_000,
                       views=[SymbolView("AAA", 100, 1.0), SymbolView("BBB", 100, 1.0)])
    assert sum(o.value for o in plan.orders) <= 2_000 + 1e-6
    assert any("to fit the cash" in n for n in plan.notes)


def test_budget_is_bot_share_of_account(bot):
    bot = dataclasses.replace(bot, capital_pct=0.5)
    plan = plan_orders(bot, equity=10_000, cash=10_000,
                       views=[SymbolView("AAA", 100, 1.0), SymbolView("BBB", 100, 1.0)])
    assert sum(o.value for o in plan.orders) == 5_000


def test_open_order_blocks_symbol(bot):
    plan = plan_orders(bot, equity=10_000, cash=10_000, views=[SymbolView("AAA", 100, 1.0, has_open_order=True)])
    assert plan.orders == []


def test_gross_exposure_cap(bot):
    bot = dataclasses.replace(bot, risk=dataclasses.replace(bot.risk, max_position_pct=1.0, max_gross_exposure=0.6))
    plan = plan_orders(bot, equity=10_000, cash=10_000,
                       views=[SymbolView("AAA", 100, 1.0), SymbolView("BBB", 100, 1.0)])
    assert abs(sum(o.value for o in plan.orders) - 6_000) < 1e-6


def test_round_qty():
    assert round_qty("BTC/USD", 0.1234567891) == 0.123456
    assert round_qty("AAPL", 3.99999, fractional_shares=False) == 3
    assert round_qty("AAPL", 3.123456) == 3.1234
