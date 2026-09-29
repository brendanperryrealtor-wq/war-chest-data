"""Vault Tournament (Accord A8): our own simulator, $2,000 virtual per registered model.

Runs after the nightly data job on the repo's adjusted closes (dividends included).
S1 weights come from the FROZEN engine file itself (sha256 checked), never a re-implementation.
Friction: 7 bps per side on traded notional, as registered.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pandas as pd

from .config import ENGINE_PATH, ENGINE_SHA256_PREFIX, DATA_DIR, FRICTION_EQUITY, TOURNAMENT
from .marketdata import repo_closes

S1_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "BIL"]


class EngineIntegrityError(RuntimeError):
    pass


def load_engine(data_dir: Path = DATA_DIR, engine_path: Path = ENGINE_PATH):
    digest = hashlib.sha256(Path(engine_path).read_bytes()).hexdigest()
    if not digest.startswith(ENGINE_SHA256_PREFIX):
        raise EngineIntegrityError(f"S1 engine hash {digest[:16]} != frozen {ENGINE_SHA256_PREFIX}")
    spec = importlib.util.spec_from_file_location("s1_engine_frozen", engine_path)
    mod = importlib.util.module_from_spec(spec)
    old = sys.argv
    sys.argv = [str(engine_path), str(data_dir)]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = old
    return mod


def is_signal_day(engine, d: pd.Timestamp) -> bool:
    return engine.first_trading_monday(d.year, d.month) == d


def s1_weights(engine, d: pd.Timestamp, m: dict) -> dict:
    w = engine.target_weights(d, m["sma_n"], m["top_k"], m["mom_m"])
    w = {k: float(v) for k, v in w.items() if v > 0}
    cash = max(0.0, 1.0 - sum(w.values()))
    if cash > 1e-9:
        w["BIL"] = w.get("BIL", 0.0) + cash
    return w


def _rebalance(model: dict, target_w: dict, day: str, reason: str) -> None:
    """Trade to target weights at the day's close; 7 bps per side on every dollar traded.
    Buying from idle cash is one side; swapping ETF for ETF (BIL included) is two."""
    cur = model["holdings"]
    eq = sum(cur.values()) + model["cash"]
    if cur:
        traded = sum(abs(target_w.get(k, 0.0) * eq - cur.get(k, 0.0)) for k in set(target_w) | set(cur))
    else:
        traded = eq * sum(target_w.values())
    cost = traded * FRICTION_EQUITY
    eq_after = eq - cost
    model["holdings"] = {k: w * eq_after for k, w in target_w.items() if w > 0}
    model["cash"] = 0.0
    model["costs"] += cost
    model["rebalances"].append({"date": day, "weights": {k: round(v, 4) for k, v in target_w.items()},
                                "traded": round(traded, 2), "cost": round(cost, 2), "reason": reason})


def update(state: dict | None, px: pd.DataFrame | None = None, engine=None,
           cfg: dict = TOURNAMENT) -> dict:
    px = px if px is not None else repo_closes(S1_UNIVERSE)
    engine = engine or load_engine()
    if not state:
        state = {"start_date": cfg["start_date"], "start_cash": cfg["start_cash"], "models": {}}
    for mid, m in cfg["models"].items():
        state["models"].setdefault(mid, {"name": m["name"], "version": m["version"], "cash": cfg["start_cash"],
                                         "holdings": {}, "costs": 0.0, "last_date": None, "history": [],
                                         "rebalances": [], "started": None})
    days = px.index[px.index >= pd.Timestamp(state["start_date"])]
    for mid, m in cfg["models"].items():
        st = state["models"][mid]
        last = pd.Timestamp(st["last_date"]) if st["last_date"] else None
        for d in days:
            if last is not None and d <= last:
                continue
            ds = d.date().isoformat()
            if st["started"] is None:
                if m["kind"] == "buy_hold":
                    _rebalance(st, {m["symbol"]: 1.0}, ds, "Opening allocation: 100% SPY, held forever.")
                else:
                    if is_signal_day(engine, d):
                        _rebalance(st, s1_weights(engine, d, m), ds, "Opening allocation on an S1 signal day.")
                    else:
                        _rebalance(st, {"BIL": 1.0}, ds, "Waiting for the first S1 signal day; parked in T-bills.")
                st["started"] = ds
            else:
                prev = px.index[px.index.get_loc(d) - 1]
                for k in list(st["holdings"]):
                    r = px.at[d, k] / px.at[prev, k]
                    if not pd.notna(r):
                        raise ValueError(f"missing price for {k} on {ds}")
                    st["holdings"][k] *= float(r)
                if m["kind"] == "s1" and is_signal_day(engine, d):
                    _rebalance(st, s1_weights(engine, d, m), ds, "Monthly S1 signal day (first trading Monday).")
            eq = sum(st["holdings"].values()) + st["cash"]
            st["history"].append({"date": ds, "equity": round(eq, 2)})
            st["last_date"] = ds
            last = d
    for mid, st in state["models"].items():
        eq = sum(st["holdings"].values()) + st["cash"]
        st["equity"] = round(eq, 2)
        st["return_pct"] = round((eq / state["start_cash"] - 1) * 100, 3)
        tot = eq if eq > 0 else 1.0
        st["weights"] = {k: round(v / tot, 4) for k, v in st["holdings"].items()}
    if "B0" in state["models"]:
        b0r = state["models"]["B0"].get("return_pct", 0.0)
        for st in state["models"].values():
            st["vs_b0_pct"] = round(st.get("return_pct", 0.0) - b0r, 3)
    return state
