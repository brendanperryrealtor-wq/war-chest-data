"""A3 Dip buyer, v0.1 DRAFT (Arcade, paper only).

SPY, QQQ, IWM, DIA. Decision price = the latest trade at the pre-close run (about 45 minutes
before the close), used as today's provisional close. Buy $500 when that price is above its
200-day average and the 2-day RSI is below 10. Exit when price closes above its 5-day average,
or after 10 trading days.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from ..indicators import rsi_wilder


def decide(ctx) -> list[dict]:
    from . import intent
    p, out = ctx.params, []
    syms = p["symbols"]
    bars = ctx.data.stock_daily(syms, lookback_days=420)
    latest = ctx.data.stock_latest(syms)
    held = {pos["symbol"]: pos for pos in ctx.sleeve["positions"].values()}
    open_slots = p["max_positions"] - len(held)
    for sym in syms:
        df, px = bars.get(sym), latest.get(sym)
        if df is None or px is None or len(df) < p["trend_sma"] + 5:
            ctx.notes.append(f"A3 {sym}: missing price or history (fail closed)")
            continue
        closes = pd.concat([df["close"], pd.Series([px], index=[ctx.today])])
        closes = closes[~closes.index.duplicated(keep="last")]
        sma_long = float(closes.iloc[-p["trend_sma"]:].mean())
        sma_exit = float(closes.iloc[-p["exit_sma"]:].mean())
        rsi = float(rsi_wilder(closes, p["rsi_period"]).iloc[-1])
        if sym in held:
            pos = held[sym]
            opened = date.fromisoformat(pos["opened"])
            days = int((df.index > opened).sum()) + (1 if ctx.today > opened else 0)
            if px > sma_exit and ctx.today > opened:
                out.append(intent("sell", sym, px, qty=pos["qty"], reason=(
                    f"Bounce: ${px:.2f} is back above its 5-day average (${sma_exit:.2f}); taking the rebound.")))
            elif days >= p["max_hold_days"]:
                out.append(intent("sell", sym, px, qty=pos["qty"],
                                  reason=f"Time exit: {days} trading days without a bounce (max {p['max_hold_days']})."))
        elif open_slots > 0 and px > sma_long and rsi < p["rsi_entry"]:
            out.append(intent("buy", sym, px, notional=p["position_usd"], reason=(
                f"Sharp dip in an uptrend: 2-day RSI {rsi:.1f} (below {p['rsi_entry']:.0f}) while "
                f"${px:.2f} is above its 200-day average (${sma_long:.2f}).")))
            open_slots -= 1
    return out
