"""Trading rules.

A strategy turns a symbol's bars (columns ``open high low close volume``, oldest
first) into a target exposure per bar, between 0 (flat) and 1 (full position).
The value at bar *t* uses only bars up to *t*, so the same code serves the
backtest (every bar) and the live bot (the last bar). Long only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, min_periods=n, adjust=False).mean()
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    # No losses over the window: RSI is 100, not NaN.
    return out.where(loss != 0, 100.0).where(gain.notna())


class Strategy:
    name = "base"
    warmup = 0

    def signals(self, bars: pd.DataFrame) -> pd.Series:
        raise NotImplementedError

    def target(self, bars: pd.DataFrame) -> float:
        """Target exposure on the last bar; 0 when there is not enough history."""
        if len(bars) < self.warmup:
            return 0.0
        value = self.signals(bars).iloc[-1]
        return 0.0 if pd.isna(value) else float(value)


class SmaCross(Strategy):
    """Trend following: long while the fast average is above the slow one."""

    name = "sma_cross"

    def __init__(self, fast: int = 20, slow: int = 50):
        if not 0 < fast < slow:
            raise ValueError("sma_cross: need 0 < fast < slow")
        self.fast, self.slow = fast, slow
        self.warmup = slow

    def signals(self, bars):
        close = bars["close"]
        long = sma(close, self.fast) > sma(close, self.slow)
        return long.astype(float).where(sma(close, self.slow).notna(), 0.0)


class RsiReversion(Strategy):
    """Mean reversion in an uptrend: buy oversold dips above the trend average,
    sell once the RSI recovers or the trend breaks."""

    name = "rsi_reversion"

    def __init__(self, period: int = 14, entry: float = 30, exit: float = 55, trend: int = 200):
        if not 0 < entry < exit < 100:
            raise ValueError("rsi_reversion: need 0 < entry < exit < 100")
        self.period, self.entry, self.exit, self.trend = period, entry, exit, trend
        self.warmup = max(trend, period + 1)

    def signals(self, bars):
        close = bars["close"]
        r = rsi(close, self.period).to_numpy()
        trend = sma(close, self.trend).to_numpy()
        c = close.to_numpy()
        out = np.zeros(len(c))
        holding = False
        for i in range(len(c)):
            if np.isnan(r[i]) or np.isnan(trend[i]):
                holding = False
            elif holding:
                holding = not (r[i] >= self.exit or c[i] < trend[i])
            else:
                holding = r[i] <= self.entry and c[i] > trend[i]
            out[i] = 1.0 if holding else 0.0
        return pd.Series(out, index=bars.index)


class DonchianBreakout(Strategy):
    """Breakout: buy a close above the previous ``entry``-bar high, sell a close
    below the previous ``exit``-bar low."""

    name = "donchian_breakout"

    def __init__(self, entry: int = 55, exit: int = 20):
        if not 0 < exit <= entry:
            raise ValueError("donchian_breakout: need 0 < exit <= entry")
        self.entry, self.exit = entry, exit
        self.warmup = entry + 1

    def signals(self, bars):
        upper = bars["high"].rolling(self.entry).max().shift(1).to_numpy()
        lower = bars["low"].rolling(self.exit).min().shift(1).to_numpy()
        c = bars["close"].to_numpy()
        out = np.zeros(len(c))
        holding = False
        for i in range(len(c)):
            if np.isnan(upper[i]) or np.isnan(lower[i]):
                holding = False
            elif holding:
                holding = c[i] >= lower[i]
            else:
                holding = c[i] > upper[i]
            out[i] = 1.0 if holding else 0.0
        return pd.Series(out, index=bars.index)


STRATEGIES: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (SmaCross, RsiReversion, DonchianBreakout)
}


def make_strategy(name: str, params: dict) -> Strategy:
    return STRATEGIES[name](**params)


def vol_scale(bars: pd.DataFrame, target_annual: float, lookback: int, periods_per_year: int) -> pd.Series:
    """Factor (at most 1) that brings a position's realized volatility down to the target."""
    returns = bars["close"].pct_change()
    realized = returns.rolling(lookback, min_periods=lookback).std() * np.sqrt(periods_per_year)
    scale = (target_annual / realized).clip(upper=1.0)
    return scale.fillna(0.0)
