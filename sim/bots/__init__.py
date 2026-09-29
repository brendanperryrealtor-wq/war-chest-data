"""Bot registry. Each bot module exposes decide(ctx) -> list of intents.

An intent is a dict: side, symbol, notional (buys) or qty (sells), price (decision price),
and reason (plain English, shown on the board as "why did the bot do that?").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from . import b0, a1_congress, a2_momentum, a3_dip, a4_crypto

REGISTRY: dict[str, Callable] = {
    "B0": b0.decide,
    "A1": a1_congress.decide,
    "A2": a2_momentum.decide,
    "A3": a3_dip.decide,
    "A4": a4_crypto.decide,
}


@dataclass
class Ctx:
    bot: str
    job: str
    now_utc: datetime
    today: date
    sleeve: dict
    meta: dict
    params: dict
    data: Any                      # DataHub
    asset: Callable[[str], dict]   # broker asset lookup
    notes: list = field(default_factory=list)

    def held_symbols(self) -> list[str]:
        return [p["symbol"] for p in self.sleeve["positions"].values()]


def intent(side: str, symbol: str, price: float, reason: str, notional: float | None = None,
           qty: float | None = None) -> dict:
    return {"side": side, "symbol": symbol, "price": price, "reason": reason,
            "notional": notional, "qty": qty}
