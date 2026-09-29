"""A4 Crypto trend, v0.1 DRAFT (Arcade, paper only).

BTC/USD and ETH/USD. Signal on completed daily bars: hold while the 20-day EMA is above the
50-day EMA, flat otherwise. $1,000 per coin. Checked hourly, so it acts within an hour of a
crossover. Spot only: no margin, no shorting, no leverage.
"""
from __future__ import annotations

from datetime import timedelta

from ..indicators import ema_series


def decide(ctx) -> list[dict]:
    from . import intent
    p, out = ctx.params, []
    syms = p["symbols"]
    bars = ctx.data.crypto_daily(syms, lookback_days=160)
    latest = ctx.data.crypto_latest(syms)
    held = {pos["symbol"].replace("/", ""): pos for pos in ctx.sleeve["positions"].values()}
    for sym in syms:
        df, px = bars.get(sym), latest.get(sym)
        if df is None or px is None or len(df) < p["slow"] + 5:
            ctx.notes.append(f"A4 {sym}: missing price or history (fail closed)")
            continue
        if df.index[-1] < ctx.now_utc.date() - timedelta(days=2):
            ctx.notes.append(f"A4 {sym}: daily bars stale (last {df.index[-1]}); no trade")
            continue
        fast = float(ema_series(df["close"], p["fast"]).iloc[-1])
        slow = float(ema_series(df["close"], p["slow"]).iloc[-1])
        key = sym.replace("/", "")
        if fast > slow and key not in held:
            out.append(intent("buy", sym, px, notional=p["position_usd"], reason=(
                f"Uptrend: the 20-day average (${fast:,.0f}) is above the 50-day (${slow:,.0f}).")))
        elif fast <= slow and key in held:
            out.append(intent("sell", sym, px, qty=held[key]["qty"], reason=(
                f"Trend turned down: the 20-day average (${fast:,.0f}) fell below the 50-day (${slow:,.0f}).")))
    return out
