"""Board-facing state files (state/sim/*.json). Written after every run, success or not.

Contracts are listed in claude/paper-sim-blueprint.md Section 6. Numbers only come from
ledgers and the tournament; nothing here invents a value (fail visible, not fail pretty).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from .config import BOOKS, BOTS, STATE_DIR, TOURNAMENT, DATA_DIR
from .util import read_json, write_json, iso, utcnow

TRADES_KEEP = 400
RECENT_RUNS_KEEP = 80


def _spy_return_since(start: str, data_dir: Path = DATA_DIR) -> tuple[float | None, str | None]:
    """SPY total return from the last close before `start` through the latest repo close."""
    try:
        s = pd.read_csv(Path(data_dir) / "SPY.csv", parse_dates=["date"]).set_index("date")["adj_close"]
    except Exception:
        return None, None
    before = s[s.index < pd.Timestamp(start)]
    if before.empty or s.index[-1] < pd.Timestamp(start):
        return None, None
    return float(s.iloc[-1] / before.iloc[-1] - 1), s.index[-1].date().isoformat()


def build(ledgers: dict, modes: dict, tournament: dict | None, now_iso: str, prices_by_book: dict,
          state_dir: Path = STATE_DIR, data_dir: Path = DATA_DIR) -> dict:
    accounts, bots, trades, hist_books, hist_bots = {}, [], [], {}, {}
    for book_id, cfg in BOOKS.items():
        L = ledgers.get(book_id)
        entry = {"label": cfg["label"], "lane": cfg["lane"], "mode": modes.get(book_id, "dry-run"),
                 "start_cash": cfg["start_cash"], "start_date": cfg["start_date"], "status": "not started",
                 "paper_equity": None, "shadow_equity": None, "cash": None, "positions": [],
                 "since_start_pct": None, "shadow_since_start_pct": None, "unattributed_cash": None}
        if L is not None:
            prices = prices_by_book.get(book_id, {})
            peq = seq = cash = 0.0
            for b in cfg["bots"]:
                pe, se = L.sleeve_equity(b, prices)
                peq, seq, cash = peq + pe, seq + se, cash + L.sleeves[b]["cash"]
                for k, p in L.sleeves[b]["positions"].items():
                    px = prices.get(k, p.get("last_price"))
                    entry["positions"].append({"symbol": p["symbol"], "bot": b, "qty": p["qty"],
                                               "price": px, "value": round(p["qty"] * (px or 0), 2),
                                               "cost": round(p["cost"], 2), "opened": p["opened"]})
            entry.update(status="running", paper_equity=round(peq, 2), shadow_equity=round(seq, 2),
                         cash=round(cash, 2), unattributed_cash=L.d.get("unattributed_cash"),
                         since_start_pct=round((peq / cfg["start_cash"] - 1) * 100, 3),
                         shadow_since_start_pct=round((seq / cfg["start_cash"] - 1) * 100, 3))
            trades.extend(L.d["trades"][-TRADES_KEEP:])
            hist_books[book_id] = [{"date": h["date"], **h["book"]} for h in L.d["history"]]
            for b in cfg["bots"]:
                hist_bots[b] = [{"date": h["date"], "paper": h["sleeves"][b]["paper"],
                                 "shadow": h["sleeves"][b]["shadow"]} for h in L.d["history"] if b in h["sleeves"]]
        accounts[book_id] = entry
        for b in cfg["bots"]:
            bc = BOTS[b]
            row = {"id": b, "name": bc["name"], "version": bc["version"], "lane": bc["lane"], "book": book_id,
                   "blurb": bc["blurb"], "start_cash": bc["start_cash"], "start_date": cfg["start_date"],
                   "status": "pending", "paper_equity": None, "shadow_equity": None, "return_pct": None,
                   "shadow_return_pct": None, "vs_b0_pct": None, "drawdown_pct": None, "trades": 0,
                   "positions": [], "last_action": None}
            if L is not None:
                s = L.sleeves[b]
                pe, se = L.sleeve_equity(b, prices_by_book.get(book_id, {}))
                r = pe / bc["start_cash"] - 1
                spy, spy_asof = _spy_return_since(cfg["start_date"], data_dir)
                row.update(status=s["status"], paper_equity=round(pe, 2), shadow_equity=round(se, 2),
                           return_pct=round(r * 100, 3), shadow_return_pct=round((se / bc["start_cash"] - 1) * 100, 3),
                           vs_b0_pct=None if spy is None else round((r - spy) * 100, 3), spy_asof=spy_asof,
                           drawdown_pct=round((1 - pe / s["high_water"]) * 100, 3) if s["high_water"] else None,
                           trades=s["trades"], last_action=s["last_action"],
                           positions=[p["symbol"] for p in s["positions"].values()])
            bots.append(row)
    trades.sort(key=lambda t: t["time"], reverse=True)
    tour_rows = []
    if tournament:
        for mid, st in tournament.get("models", {}).items():
            tour_rows.append({"id": mid, "name": st["name"], "version": st["version"], "lane": "vault",
                              "book": "tournament", "equity": st.get("equity"), "return_pct": st.get("return_pct"),
                              "vs_b0_pct": st.get("vs_b0_pct"), "weights": st.get("weights"),
                              "started": st.get("started"), "last_date": st.get("last_date"),
                              "last_rebalance": st["rebalances"][-1] if st.get("rebalances") else None})
    out = {
        "accounts": {"as_of": now_iso, "books": accounts},
        "bots": {"as_of": now_iso, "bots": bots, "tournament": tour_rows,
                 "note": "vs_b0_pct compares each bot's return with SPY's total return since the close before the bot started (SPY through the last nightly close)."},
        "trades": {"as_of": now_iso, "trades": trades[:TRADES_KEEP]},
        "equity_history": {"as_of": now_iso, "books": hist_books, "bots": hist_bots,
                           "tournament": {m: st["history"] for m, st in (tournament or {}).get("models", {}).items()}},
    }
    for name, obj in out.items():
        write_json(Path(state_dir) / f"{name}.json", obj)
    return out


def record_run(run: dict, modes: dict, kill: bool, state_dir: Path = STATE_DIR) -> dict:
    path = Path(state_dir) / "health.json"
    h = read_json(path, {}) or {}
    h.setdefault("last_runs", {})[run["job"]] = run
    rec = h.setdefault("recent", [])
    rec.append({k: run[k] for k in ("job", "started", "finished", "status")} | {"errors": len(run["errors"])})
    del rec[:-RECENT_RUNS_KEEP]
    h.update(as_of=run["finished"], modes=modes, kill_switch=kill,
             open_errors={b: e for b, e in (h.get("open_errors") or {}).items()})
    for book, errs in run.get("book_errors", {}).items():
        if errs:
            h["open_errors"][book] = {"since": run["finished"], "errors": errs}
        else:
            h["open_errors"].pop(book, None)
    write_json(path, h)
    return h
