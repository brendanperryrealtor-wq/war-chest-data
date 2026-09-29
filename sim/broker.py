"""Broker adapters. AlpacaBroker talks to Alpaca PAPER only. DryRunBroker fills in memory.

Both return the same normalized shapes so the rest of the engine never cares which is live.
"""
from __future__ import annotations

import time as _time
from typing import Callable, Optional

import requests

from .config import PAPER_BASE
from .util import round_qty, utcnow, iso


class BrokerError(RuntimeError):
    pass


def canon(symbol: str) -> str:
    """Alpaca reports crypto positions as BTCUSD but trades BTC/USD. Compare on this key."""
    return symbol.replace("/", "").upper()


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def normalize_order(o: dict) -> dict:
    return {
        "id": o.get("id"),
        "client_order_id": o.get("client_order_id"),
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "status": o.get("status"),
        "qty": _f(o.get("qty"), None) if o.get("qty") is not None else None,
        "notional": _f(o.get("notional"), None) if o.get("notional") is not None else None,
        "filled_qty": _f(o.get("filled_qty")),
        "filled_avg_price": _f(o.get("filled_avg_price"), None) if o.get("filled_avg_price") else None,
        "submitted_at": o.get("submitted_at"),
        "filled_at": o.get("filled_at"),
    }


TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced", "stopped", "suspended"}


class AlpacaBroker:
    """Alpaca Trading API v2, paper endpoint only (Accord A9: live needs a code change)."""

    def __init__(self, key_id: str, secret: str, base: str = PAPER_BASE, timeout: int = 20):
        if not key_id or not secret:
            raise BrokerError("missing Alpaca paper keys for this book")
        if "paper-api" not in base:
            raise BrokerError("refusing non-paper endpoint")
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret})
        self.timeout = timeout

    def _req(self, method: str, path: str, **kw):
        r = self.s.request(method, self.base + path, timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise BrokerError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    def account(self) -> dict:
        a = self._req("GET", "/v2/account")
        return {"cash": _f(a.get("cash")), "equity": _f(a.get("equity")),
                "buying_power": _f(a.get("buying_power")), "status": a.get("status"),
                "trading_blocked": bool(a.get("trading_blocked")),
                "account_blocked": bool(a.get("account_blocked"))}

    def positions(self) -> dict:
        out = {}
        for p in self._req("GET", "/v2/positions"):
            out[canon(p["symbol"])] = {
                "symbol": p["symbol"], "qty": _f(p.get("qty")),
                "avg_entry_price": _f(p.get("avg_entry_price")),
                "current_price": _f(p.get("current_price")),
                "market_value": _f(p.get("market_value")),
                "asset_class": p.get("asset_class"),
            }
        return out

    def clock(self) -> dict:
        c = self._req("GET", "/v2/clock")
        return {"is_open": bool(c.get("is_open")), "timestamp": c.get("timestamp"),
                "next_open": c.get("next_open"), "next_close": c.get("next_close")}

    def asset(self, symbol: str) -> dict:
        a = self._req("GET", f"/v2/assets/{symbol}")
        return {"tradable": bool(a.get("tradable")), "fractionable": bool(a.get("fractionable")),
                "status": a.get("status"), "class": a.get("class")}

    def submit_order(self, symbol: str, side: str, client_order_id: str,
                     notional: Optional[float] = None, qty: Optional[float] = None,
                     tif: str = "day", price_hint: Optional[float] = None) -> dict:
        body = {"symbol": symbol, "side": side, "type": "market", "time_in_force": tif,
                "client_order_id": client_order_id}
        if notional is not None:
            body["notional"] = f"{notional:.2f}"
        elif qty is not None:
            body["qty"] = f"{round_qty(qty):.9f}"
        else:
            raise BrokerError("order needs notional or qty")
        try:
            return normalize_order(self._req("POST", "/v2/orders", json=body))
        except BrokerError as e:
            if "client_order_id" in str(e) and ("unique" in str(e) or "422" in str(e)):
                return self.get_order_by_client_id(client_order_id)   # already sent earlier: idempotent
            raise

    def get_order(self, order_id: str) -> dict:
        return normalize_order(self._req("GET", f"/v2/orders/{order_id}"))

    def get_order_by_client_id(self, cid: str) -> dict:
        return normalize_order(self._req("GET", "/v2/orders:by_client_order_id",
                                         params={"client_order_id": cid}))

    def wait_filled(self, order: dict, seconds: int = 30) -> dict:
        deadline = _time.time() + seconds
        while order.get("status") not in TERMINAL and _time.time() < deadline:
            _time.sleep(2)
            order = self.get_order(order["id"])
        return order


class DryRunBroker:
    """Fills every market order instantly at the decision price. State lives in the ledger file,
    so a dry run exercises the same reconcile, sleeve, shadow, and state code as paper mode."""

    def __init__(self, state: dict, price_fn: Callable[[str], Optional[float]],
                 clock_fn: Optional[Callable[[], dict]] = None,
                 asset_fn: Optional[Callable[[str], dict]] = None):
        self.state = state
        self.state.setdefault("positions", {})
        self.state.setdefault("orders", {})
        self.price_fn = price_fn
        self.clock_fn = clock_fn
        self.asset_fn = asset_fn

    def account(self) -> dict:
        eq = self.state["cash"]
        for k, p in self.state["positions"].items():
            px = self.price_fn(p["symbol"]) or p["avg"]
            eq += p["qty"] * px
        return {"cash": self.state["cash"], "equity": eq, "buying_power": self.state["cash"],
                "status": "DRY_RUN", "trading_blocked": False, "account_blocked": False}

    def positions(self) -> dict:
        out = {}
        for k, p in self.state["positions"].items():
            px = self.price_fn(p["symbol"]) or p["avg"]
            out[k] = {"symbol": p["symbol"], "qty": p["qty"], "avg_entry_price": p["avg"],
                      "current_price": px, "market_value": p["qty"] * px, "asset_class": "dry_run"}
        return out

    def clock(self) -> dict:
        if self.clock_fn:
            return self.clock_fn()
        return {"is_open": True, "timestamp": iso(utcnow()), "next_open": None, "next_close": None}

    def asset(self, symbol: str) -> dict:
        if self.asset_fn:
            return self.asset_fn(symbol)
        return {"tradable": True, "fractionable": True, "status": "active", "class": "dry_run"}

    def submit_order(self, symbol: str, side: str, client_order_id: str,
                     notional: Optional[float] = None, qty: Optional[float] = None,
                     tif: str = "day", price_hint: Optional[float] = None) -> dict:
        if client_order_id in self.state["orders"]:
            return self.state["orders"][client_order_id]
        px = price_hint or self.price_fn(symbol)
        if not px or px <= 0:
            raise BrokerError(f"dry-run: no price for {symbol}")
        key = canon(symbol)
        pos = self.state["positions"].get(key, {"symbol": symbol, "qty": 0.0, "avg": px})
        if side == "buy":
            q = round_qty((notional / px) if notional is not None else qty)
            cost = q * px
            if cost > self.state["cash"] + 1e-6:
                raise BrokerError("dry-run: insufficient cash")
            new_qty = pos["qty"] + q
            pos["avg"] = (pos["avg"] * pos["qty"] + cost) / new_qty if new_qty else px
            pos["qty"] = new_qty
            self.state["cash"] -= cost
        else:
            q = round_qty(qty if qty is not None else (notional / px))
            if q > pos["qty"] + 1e-9:
                raise BrokerError("dry-run: selling more than held")
            pos["qty"] = round_qty(pos["qty"] - q)
            self.state["cash"] += q * px
        if pos["qty"] > 0:
            self.state["positions"][key] = pos
        else:
            self.state["positions"].pop(key, None)
        now = iso(utcnow())
        order = {"id": f"dry-{client_order_id}", "client_order_id": client_order_id, "symbol": symbol,
                 "side": side, "status": "filled", "qty": q, "notional": notional,
                 "filled_qty": q, "filled_avg_price": px, "submitted_at": now, "filled_at": now}
        self.state["orders"][client_order_id] = order
        return order

    def get_order(self, order_id: str) -> dict:
        for o in self.state["orders"].values():
            if o["id"] == order_id:
                return o
        raise BrokerError(f"dry-run: unknown order {order_id}")

    def get_order_by_client_id(self, cid: str) -> dict:
        if cid not in self.state["orders"]:
            raise BrokerError(f"dry-run: unknown client order {cid}")
        return self.state["orders"][cid]

    def wait_filled(self, order: dict, seconds: int = 0) -> dict:
        return order
