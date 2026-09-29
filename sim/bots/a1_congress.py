"""A1 Congress copycat, v0.1 DRAFT (Arcade, paper only).

Buys common stocks that members of Congress disclose buying (official House Clerk and Senate
eFD records via a free feed). Skips filings made more than 45 days after the trade. Up to 10
positions of $200. Exits when a member discloses selling it, after 90 days, or at -15%.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")


def clean_ticker(t) -> str | None:
    if not t or not isinstance(t, str):
        return None
    t = t.strip().upper().replace("/", ".").replace("-", ".")
    return t if TICKER_RE.match(t) else None


def _d(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def amount_low(s) -> int:
    m = re.search(r"\$?([\d,]+)", str(s or ""))
    return int(m.group(1).replace(",", "")) if m else 0


def is_purchase(t: dict) -> bool:
    return "purchase" in str(t.get("type", "")).lower()


def is_sale(t: dict) -> bool:
    return "sale" in str(t.get("type", "")).lower()


def decide(ctx) -> list[dict]:
    from . import intent
    p, out = ctx.params, []
    held = ctx.sleeve["positions"]
    held_syms = {pos["symbol"] for pos in held.values()}
    exiting: set[str] = set()

    latest = ctx.data.stock_latest(sorted(held_syms)) if held_syms else {}
    for pos in held.values():
        sym, px = pos["symbol"], latest.get(pos["symbol"])
        age = (ctx.today - date.fromisoformat(pos["opened"])).days
        if px and px <= pos["entry_price"] * (1 - p["stop_loss"]):
            out.append(intent("sell", sym, px, qty=pos["qty"], reason=(
                f"Stop-loss: ${px:.2f} is {1 - px / pos['entry_price']:.0%} below the ${pos['entry_price']:.2f} entry "
                f"(limit {p['stop_loss']:.0%}).")))
            exiting.add(sym)
        elif age >= p["max_hold_days"]:
            out.append(intent("sell", sym, px or pos["entry_price"], qty=pos["qty"],
                              reason=f"Time limit: held {age} days (max {p['max_hold_days']})."))
            exiting.add(sym)

    if ctx.job != "morning":
        return out

    trades = ctx.data.congress(ctx.today - timedelta(days=p["new_disclosure_days"] + 10))

    for pos in held.values():                        # a member sold it after we bought: follow them out
        sym = pos["symbol"]
        if sym in exiting:
            continue
        opened = date.fromisoformat(pos["opened"])
        for t in trades:
            dd = _d(t.get("disclosure_date"))
            if is_sale(t) and clean_ticker(t.get("ticker")) == sym and dd and dd >= opened:
                who = t.get("member", "A member")
                out.append(intent("sell", sym, latest.get(sym) or pos["entry_price"], qty=pos["qty"],
                                  reason=f"{who} disclosed selling {sym} on {dd.isoformat()}; following them out."))
                exiting.add(sym)
                break

    seen = ctx.meta.setdefault("seen", [])
    seen_set = set(seen)
    cands = []
    for t in trades:
        if not is_purchase(t):
            continue
        tk = clean_ticker(t.get("ticker"))
        dd, td = _d(t.get("disclosure_date")), _d(t.get("transaction_date"))
        if not tk or not dd or not td:
            continue
        key = f"{t.get('member_slug') or t.get('member')}|{tk}|{td}|{dd}"
        if key in seen_set:
            continue
        seen.append(key)
        seen_set.add(key)
        lag = (dd - td).days
        if lag > p["max_filing_lag_days"] or lag < 0:
            ctx.notes.append(f"A1 skip {tk}: filed {lag} days after the trade")
            continue
        if (ctx.today - dd).days > p["new_disclosure_days"]:
            continue
        cands.append((dd, amount_low(t.get("amount_range")), tk, t))
    del seen[:-3000]

    cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
    slots = p["max_positions"] - len(held)
    chosen: list[tuple[str, dict]] = []
    for dd, amt, tk, t in cands:
        if len(chosen) >= slots:
            ctx.notes.append(f"A1 full: {tk} disclosed but no open slot")
            continue
        if tk in held_syms or any(tk == c[0] for c in chosen):
            continue
        try:
            a = ctx.asset(tk)
        except Exception as e:                        # unknown symbol at the broker
            ctx.notes.append(f"A1 skip {tk}: broker lookup failed ({str(e)[:60]})")
            continue
        if not (a.get("tradable") and a.get("fractionable")):
            ctx.notes.append(f"A1 skip {tk}: not tradable in fractions at the broker")
            continue
        chosen.append((tk, t))
    if not chosen:
        return out
    px_now = ctx.data.stock_latest([c[0] for c in chosen])
    for tk, t in chosen:
        px = px_now.get(tk)
        if not px:
            ctx.notes.append(f"A1 skip {tk}: no live price")
            continue
        who = t.get("member", "A member of Congress")
        ch = str(t.get("chamber", "")).title()
        td, dd = t.get("transaction_date"), t.get("disclosure_date")
        lag = (_d(dd) - _d(td)).days
        out.append(intent("buy", tk, px, notional=p["position_usd"], reason=(
            f"{who} ({ch}) disclosed buying {tk} on {td}, filed {dd} ({lag} days later), "
            f"size {t.get('amount_range', 'not stated')}.")))
    return out
