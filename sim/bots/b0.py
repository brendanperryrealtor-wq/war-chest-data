"""B0-1.0 Control: 100% broad US equity ETF, buy and hold. Cannot be fired, only beaten."""
from __future__ import annotations


def decide(ctx) -> list[dict]:
    from . import intent
    sym = ctx.params["symbol"]
    if ctx.job != "preclose":
        return []
    if any(p["symbol"] == sym for p in ctx.sleeve["positions"].values()):
        return []                                   # already invested; B0 never sells
    px = ctx.data.stock_latest([sym]).get(sym)
    if not px:
        ctx.notes.append(f"B0: no live price for {sym}; no trade (fail closed)")
        return []
    return [intent("buy", sym, px, notional=ctx.sleeve["cash"],
                   reason=("Opening position. B0 puts the whole book into the broad market (SPY) "
                           "and holds it. It is the benchmark every other bot is scored against."))]
