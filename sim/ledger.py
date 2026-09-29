"""Book ledger: one sleeve per bot inside a broker account, plus a shadow twin.

Paper side = what Alpaca paper (or the dry-run broker) actually filled.
Shadow side = the same quantities re-priced at the accord's friction, plus dividends,
so the board can show the paper-optimism gap (Accord A8).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from .broker import canon
from .util import iso, utcnow, round_qty

EPS = 1e-9


def _new_sleeve(start_cash: float) -> dict:
    return {"start_cash": start_cash, "cash": start_cash, "positions": {}, "realized": 0.0,
            "high_water": start_cash, "status": "active", "benched_on": None, "trades": 0,
            "last_action": None}


def _new_shadow(start_cash: float) -> dict:
    return {"cash": start_cash, "positions": {}, "fees": 0.0, "dividends": 0.0,
            "high_water": start_cash}


class BookLedger:
    def __init__(self, data: dict):
        self.d = data

    # ---------- construction ----------
    @classmethod
    def new(cls, book_id: str, book_cfg: dict, bots_cfg: dict, mode: str) -> "BookLedger":
        sleeves, shadow = {}, {}
        for b in book_cfg["bots"]:
            sleeves[b] = _new_sleeve(bots_cfg[b]["start_cash"])
            shadow[b] = _new_shadow(bots_cfg[b]["start_cash"])
        d = {"book": book_id, "schema": 1, "created": iso(utcnow()), "mode": mode,
             "start_cash": book_cfg["start_cash"], "start_date": book_cfg["start_date"],
             "sleeves": sleeves, "shadow": shadow, "orders": {}, "trades": [], "history": [],
             "dividends_applied": [], "trueups": [], "done": {}, "meta": {b: {} for b in book_cfg["bots"]},
             "unattributed_cash": 0.0}
        if mode == "dry-run":
            d["dryrun_account"] = {"cash": book_cfg["start_cash"], "positions": {}, "orders": {}}
        return cls(d)

    @property
    def sleeves(self) -> dict:
        return self.d["sleeves"]

    @property
    def shadow(self) -> dict:
        return self.d["shadow"]

    # ---------- queries ----------
    def held(self, bot: str, symbol: str) -> float:
        p = self.sleeves[bot]["positions"].get(canon(symbol))
        return p["qty"] if p else 0.0

    def total_by_symbol(self) -> dict:
        out: dict[str, float] = {}
        for s in self.sleeves.values():
            for k, p in s["positions"].items():
                out[k] = out.get(k, 0.0) + p["qty"]
        return out

    def holders(self, key: str) -> list[str]:
        return [b for b, s in self.sleeves.items() if key in s["positions"]]

    def sleeve_equity(self, bot: str, prices: dict) -> tuple[float, float]:
        s, sh = self.sleeves[bot], self.shadow[bot]
        pv = sum(p["qty"] * prices.get(k, p.get("last_price") or p["entry_price"])
                 for k, p in s["positions"].items())
        sv = sum(p["qty"] * prices.get(k, p.get("last_price") or p["entry_price"])
                 for k, p in sh["positions"].items())
        return s["cash"] + pv, sh["cash"] + sv

    # ---------- fills ----------
    def apply_fill(self, bot: str, symbol: str, side: str, qty: float, price: float,
                   friction: float, when: str, cid: str, reason: str) -> dict:
        if qty <= EPS or price <= 0:
            raise ValueError("fill needs positive qty and price")
        key = canon(symbol)
        s, sh = self.sleeves[bot], self.shadow[bot]
        notional = qty * price
        spx = price * (1 + friction) if side == "buy" else price * (1 - friction)
        fee = qty * price * friction
        if side == "buy":
            p = s["positions"].setdefault(key, {"symbol": symbol, "qty": 0.0, "cost": 0.0,
                                                "opened": when[:10], "entry_price": price,
                                                "high_close": price, "last_price": price})
            p["qty"] = round_qty(p["qty"] + qty)
            p["cost"] += notional
            s["cash"] -= notional
            q = sh["positions"].setdefault(key, {"symbol": symbol, "qty": 0.0, "cost": 0.0,
                                                 "opened": when[:10], "entry_price": spx,
                                                 "last_price": price})
            q["qty"] = round_qty(q["qty"] + qty)
            q["cost"] += qty * spx
            sh["cash"] -= qty * spx
            realized = 0.0
        else:
            p = s["positions"].get(key)
            if not p or qty > p["qty"] + 1e-7:
                raise ValueError(f"{bot} cannot sell {qty} {symbol}: holds {p['qty'] if p else 0}")
            qty = min(qty, p["qty"])
            frac = qty / p["qty"]
            cost_out = p["cost"] * frac
            realized = notional - cost_out
            s["realized"] += realized
            s["cash"] += notional
            p["qty"] = round_qty(p["qty"] - qty)
            p["cost"] -= cost_out
            if p["qty"] <= EPS:
                s["positions"].pop(key)
            q = sh["positions"].get(key)
            if q:
                sfrac = min(1.0, qty / q["qty"]) if q["qty"] > EPS else 1.0
                q["cost"] -= q["cost"] * sfrac
                q["qty"] = round_qty(q["qty"] - qty)
                if q["qty"] <= EPS:
                    sh["positions"].pop(key)
            sh["cash"] += qty * spx
        sh["fees"] += fee
        s["trades"] += 1
        rec = {"time": when, "book": self.d["book"], "bot": bot, "side": side, "symbol": symbol,
               "qty": qty, "price": round(price, 6), "notional": round(notional, 2),
               "shadow_price": round(spx, 6), "realized": round(realized, 2),
               "client_order_id": cid, "reason": reason}
        self.d["trades"].append(rec)
        s["last_action"] = {"time": when, "text": f"{side.upper()} {symbol}: {reason}"}
        return rec

    # ---------- marks, risk, history ----------
    def mark(self, prices: dict) -> None:
        for s in list(self.sleeves.values()) + list(self.shadow.values()):
            for k, p in s["positions"].items():
                if k in prices and prices[k] > 0:
                    p["last_price"] = prices[k]

    def note_close(self, prices: dict) -> None:
        """At a close, raise each position's highest close (used by trailing stops)."""
        for s in self.sleeves.values():
            for k, p in s["positions"].items():
                if k in prices:
                    p["high_close"] = max(p.get("high_close", 0.0), prices[k])

    def update_risk(self, prices: dict, lane: str, bench_dd: float, today: str) -> list[str]:
        notes = []
        for b, s in self.sleeves.items():
            peq, seq = self.sleeve_equity(b, prices)
            s["high_water"] = max(s["high_water"], peq)
            self.shadow[b]["high_water"] = max(self.shadow[b]["high_water"], seq)
            dd_shadow = 1 - seq / self.shadow[b]["high_water"] if self.shadow[b]["high_water"] > 0 else 0
            dd_paper = 1 - peq / s["high_water"] if s["high_water"] > 0 else 0
            dd = max(dd_paper, dd_shadow)                       # bench on the worse of the two
            if lane == "arcade" and s["status"] == "active" and dd >= bench_dd:
                s["status"] = "benched"
                s["benched_on"] = today
                notes.append(f"{b} benched at {dd:.1%} drawdown (limit {bench_dd:.0%}); review required")
        return notes

    def record_history(self, day: str, prices: dict) -> None:
        entry = {"date": day, "sleeves": {}, "book": {"paper": 0.0, "shadow": 0.0}}
        for b in self.sleeves:
            peq, seq = self.sleeve_equity(b, prices)
            entry["sleeves"][b] = {"paper": round(peq, 2), "shadow": round(seq, 2),
                                   "pos": {k: p["qty"] for k, p in self.sleeves[b]["positions"].items()}}
            entry["book"]["paper"] += peq
            entry["book"]["shadow"] += seq
        entry["book"] = {k: round(v, 2) for k, v in entry["book"].items()}
        h = self.d["history"]
        if h and h[-1]["date"] == day:
            h[-1] = entry
        else:
            h.append(entry)

    # ---------- dividends (shadow only: Alpaca paper does not pay them) ----------
    def credit_dividends(self, dividends: list[dict]) -> list[str]:
        notes = []
        hist = {h["date"]: h for h in self.d["history"]}
        dates = sorted(hist)
        for dv in dividends:
            sym, ex, rate = dv.get("symbol"), dv.get("ex_date"), float(dv.get("rate") or 0)
            if not sym or not ex or rate <= 0:
                continue
            key = f"{canon(sym)}|{ex}|{rate}"
            if key in self.d["dividends_applied"] or not dates or ex > dates[-1]:
                continue                                      # done already, or ex-date not reached
            prior = [d for d in dates if d < ex]
            if prior:
                snap = hist[prior[-1]]                        # holdings at the close before ex-date
                for b, sl in snap["sleeves"].items():
                    q = sl.get("pos", {}).get(canon(sym), 0.0)
                    if q > EPS:
                        cash = q * rate
                        self.shadow[b]["cash"] += cash
                        self.shadow[b]["dividends"] += cash
                        notes.append(f"shadow dividend {sym} ex {ex}: {b} +${cash:.2f}")
            self.d["dividends_applied"].append(key)
        return notes

    # ---------- reconciliation against the broker ----------
    def reconcile(self, broker_positions: dict, broker_cash: float, rel_tol_single: float,
                  abs_tol: float = 1e-6) -> tuple[list[str], list[str]]:
        """Return (errors, notes). A symbol held by exactly one sleeve may be trued-up to the
        broker within rel_tol_single (fees taken in-kind, rounding). Anything else is an error."""
        errors, notes = [], []
        mine = self.total_by_symbol()
        for key in sorted(set(mine) | set(broker_positions)):
            lq = mine.get(key, 0.0)
            bq = broker_positions.get(key, {}).get("qty", 0.0)
            diff = bq - lq
            if abs(diff) <= abs_tol:
                continue
            holders = self.holders(key)
            if len(holders) == 1 and lq > EPS and abs(diff) / lq <= rel_tol_single:
                p = self.sleeves[holders[0]]["positions"][key]
                sp = self.shadow[holders[0]]["positions"].get(key)
                p["qty"] = round_qty(bq)
                if sp:
                    sp["qty"] = round_qty(bq)
                self.d["trueups"].append({"time": iso(utcnow()), "symbol": key, "bot": holders[0],
                                          "from": lq, "to": bq})
                notes.append(f"trued-up {key} for {holders[0]}: {lq:.9f} -> {bq:.9f}")
            else:
                errors.append(f"position mismatch {key}: ledger {lq:.9f} vs broker {bq:.9f}")
        sleeves_cash = sum(s["cash"] for s in self.sleeves.values())
        self.d["unattributed_cash"] = round(broker_cash - sleeves_cash, 2)
        return errors, notes
