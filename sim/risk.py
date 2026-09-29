"""Pre-trade checks. Every intent passes here before it can reach a broker.

A rejected intent is logged with its reason and never retried within the same run.
"""
from __future__ import annotations

from .config import CASH_BUFFER, MIN_ORDER_USD, VAULT_LADDER, KILL_SWITCH
from .util import floor_cents


def ladder_multiplier(drawdown: float) -> float | None:
    """Accord Article 8 drawdown ladder. None means suspended for audit (beyond 12%)."""
    for limit, mult in VAULT_LADDER:
        if drawdown < limit:
            return mult
    return None


def kill_switch_on() -> bool:
    return KILL_SWITCH.exists()


def check_intent(intent: dict, sleeve: dict, bot_cfg: dict, asset_class: str,
                 market_open: bool, reserved_cash: float = 0.0) -> tuple[bool, str, dict]:
    """Return (ok, why, adjusted_intent). Buys are sized in dollars, sells in quantity."""
    it = dict(intent)
    side, sym = it.get("side"), it.get("symbol")
    params = bot_cfg.get("params", {})
    if side not in ("buy", "sell") or not sym:
        return False, "malformed intent", it
    if kill_switch_on():
        return False, "kill switch is on", it
    if asset_class == "us_equity" and not market_open:
        return False, "stock market is closed", it
    positions = sleeve["positions"]
    key = sym.replace("/", "").upper()
    if side == "buy":
        if sleeve["status"] != "active":
            return False, f"bot is {sleeve['status']}; no new entries", it
        if key in positions:
            return False, "already held (v0.1 specs never add to a position)", it
        max_pos = params.get("max_positions")
        if max_pos is not None and len(positions) >= max_pos:
            return False, f"at max positions ({max_pos})", it
        spendable = floor_cents(max(0.0, sleeve["cash"] - reserved_cash) * CASH_BUFFER)
        notional = floor_cents(min(float(it.get("notional") or 0.0), spendable))
        if notional < MIN_ORDER_USD:
            return False, f"not enough sleeve cash (${spendable:.2f} spendable)", it
        it["notional"], it["qty"] = notional, None
        return True, "ok", it
    held = positions.get(key, {}).get("qty", 0.0)
    if held <= 0:
        return False, "nothing to sell", it
    qty = float(it.get("qty") or held)
    if qty > held + 1e-9:
        return False, f"sell {qty} exceeds held {held}", it
    it["qty"], it["notional"] = min(qty, held), None
    return True, "ok", it
