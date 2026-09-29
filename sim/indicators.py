"""Deterministic indicator math. No LLM arithmetic touches an order (Accord Article 1)."""
from __future__ import annotations

import pandas as pd


def sma(s: pd.Series, n: int) -> float:
    if len(s) < n:
        raise ValueError(f"need {n} values, have {len(s)}")
    return float(s.iloc[-n:].mean())


def ema_series(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def rsi_wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder's RSI. Seeded with a simple average of the first n changes."""
    d = s.diff().dropna()
    gain, loss = d.clip(lower=0.0), (-d).clip(lower=0.0)
    g0, l0 = gain.iloc[:n].mean(), loss.iloc[:n].mean()
    out = pd.Series(index=s.index, dtype=float)
    if len(d) < n:
        return out
    avg_g, avg_l = g0, l0
    idx = d.index
    for i in range(n, len(d) + 1):
        if i > n:
            avg_g = (avg_g * (n - 1) + gain.iloc[i - 1]) / n
            avg_l = (avg_l * (n - 1) + loss.iloc[i - 1]) / n
        if avg_l == 0:
            val = 100.0 if avg_g > 0 else 50.0
        else:
            rs = avg_g / avg_l
            val = 100.0 - 100.0 / (1.0 + rs)
        out.loc[idx[i - 1]] = val
    return out
