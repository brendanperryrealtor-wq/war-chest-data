"""A2 Momentum swing, v0.1 DRAFT (Arcade, paper only).

Universe: S&P 500 members. Signal on the last COMPLETED session: close at its 252-day high,
volume above 1.5x its prior 50-session average, close above its 50-day average. Up to 8
positions of $250, strongest volume surge first. Exit before the close on a 10% trailing stop
from the highest close since entry, or a price below the 50-day average.
"""
from __future__ import annotations

from datetime import date, timedelta


def decide(ctx) -> list[dict]:
    from . import intent
    p, out = ctx.params, []
    held = ctx.sleeve["positions"]
    held_syms = sorted({pos["symbol"] for pos in held.values()})
    universe = ctx.data.sp500()
    bars = ctx.data.stock_daily(sorted(set(universe) | set(held_syms)), lookback_days=420)

    stale_cut = ctx.today - timedelta(days=5)
    latest = ctx.data.stock_latest(held_syms) if held_syms else {}
    for pos in held.values():
        sym, px, df = pos["symbol"], latest.get(pos["symbol"]), bars.get(pos["symbol"])
        if px is None or df is None or len(df) < p["sma"]:
            ctx.notes.append(f"A2 hold {sym}: missing price or history, no exit check (fail closed)")
            continue
        closes = df["close"]
        since = closes[closes.index >= date.fromisoformat(pos["opened"])]
        peak = max([pos["entry_price"], px, *since.tolist()])
        sma = float(closes.iloc[-p["sma"]:].mean())
        if px <= peak * (1 - p["trail"]):
            out.append(intent("sell", sym, px, qty=pos["qty"], reason=(
                f"Trailing stop: ${px:.2f} is {1 - px / peak:.0%} below its ${peak:.2f} high since entry "
                f"(limit {p['trail']:.0%}).")))
        elif px < sma:
            out.append(intent("sell", sym, px, qty=pos["qty"],
                              reason=f"Trend break: ${px:.2f} fell below its 50-day average of ${sma:.2f}."))

    slots = p["max_positions"] - len(held)
    if slots <= 0:
        return out
    cands = []
    for sym in universe:
        if sym in held_syms:
            continue
        df = bars.get(sym)
        if df is None or len(df) < p["lookback_high"] + 1 or df.index[-1] < stale_cut:
            continue
        c, v = df["close"], df["volume"]
        last_c, last_v = float(c.iloc[-1]), float(v.iloc[-1])
        if last_c < float(c.iloc[-p["lookback_high"]:].max()) - 1e-9:
            continue
        vavg = float(v.iloc[-(p["vol_avg"] + 1):-1].mean())
        if vavg <= 0 or last_v < p["vol_mult"] * vavg:
            continue
        sma = float(c.iloc[-p["sma"]:].mean())
        if last_c <= sma:
            continue
        cands.append((last_v / vavg, sym, last_c, df.index[-1]))
    cands.sort(reverse=True)
    picks = []
    for ratio, sym, last_c, d in cands:
        if len(picks) >= slots:
            break
        try:
            a = ctx.asset(sym)
        except Exception:
            continue
        if a.get("tradable") and a.get("fractionable"):
            picks.append((ratio, sym, last_c, d))
    if not picks:
        return out
    now_px = ctx.data.stock_latest([s for _, s, _, _ in picks])
    for ratio, sym, last_c, d in picks:
        px = now_px.get(sym)
        if not px:
            ctx.notes.append(f"A2 skip {sym}: no live price")
            continue
        out.append(intent("buy", sym, px, notional=p["position_usd"], reason=(
            f"Breakout: closed at a 52-week high (${last_c:.2f}) on {d.isoformat()} with {ratio:.1f}x its "
            f"normal volume, above its 50-day average.")))
    return out
