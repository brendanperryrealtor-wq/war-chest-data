"""War Chest paper sim runner. Invoked by GitHub Actions (see .github/workflows/sim.yml).

    python -m sim.run --job preclose          # one job
    python -m sim.run --auto                  # pick the job from the GitHub event
    python -m sim.run --job snapshot --now 2026-10-05T19:15:00Z --force   # testing

Jobs: morning (A1), preclose (B0, A1 exits, A2, A3), crypto (A4, hourly), snapshot
(marks only), eod (after the nightly data job: history, shadow dividends, Tournament),
check (verify every key, account, and data feed; no trading), rehearsal (every bot decides on
live data with simulated fills in state/sim/rehearsal/, ignoring windows and start dates).
Every failure is contained to its book and written to state/sim/health.json (fail closed).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import state as state_mod
from . import tournament as tour_mod
from .bots import REGISTRY, Ctx
from .broker import AlpacaBroker, DryRunBroker, BrokerError, TERMINAL, canon
from .config import (BOOKS, BOTS, STATE_DIR, MODE_FILE, WINDOWS_PT, ARCADE_BENCH_DD,
                     FRICTION_EQUITY, FRICTION_CRYPTO)
from .datahub import DataHub
from .ledger import BookLedger
from .marketdata import AlpacaData
from .risk import check_intent, kill_switch_on
from .util import iso, utcnow, market_date, in_window, read_json, write_json, safe_id

JOB_BOOKS = {
    "morning": ["arcade_stocks"],
    "preclose": ["war_chest", "arcade_stocks"],
    "crypto": ["arcade_crypto"],
    "snapshot": list(BOOKS),
    "eod": list(BOOKS),
}
TRADING_JOBS = {"morning", "preclose", "crypto"}
SCHEDULE_TO_JOB = {                      # must match the cron strings in sim.yml exactly
    "47 13,14 * * 1-5": "morning",
    "13 19,20 * * 1-5": "preclose",
    "23 * * * *": "crypto",
    "37 13-21 * * 1-5": "snapshot",
}


class SetupError(RuntimeError):
    pass


def resolve_job(args) -> str:
    if not args.auto:
        return args.job
    ev = os.environ.get("EVENT", "")
    if ev == "workflow_run":
        return "eod"
    if ev == "workflow_dispatch":
        return os.environ.get("JOB_INPUT") or "snapshot"
    return SCHEDULE_TO_JOB.get(os.environ.get("SCHEDULE", ""), "snapshot")


def load_modes() -> dict:
    m = read_json(MODE_FILE, {}) or {}
    return {b: (m.get(b) if m.get(b) in ("dry-run", "paper") else "dry-run") for b in BOOKS}


class Deps:
    """Everything that touches the outside world, swappable in tests."""

    def __init__(self, env: dict | None = None):
        self.env = env if env is not None else dict(os.environ)

    def keys(self, book_id: str) -> tuple[str | None, str | None]:
        e = BOOKS[book_id]["env"]
        return self.env.get(f"ALPACA_{e}_KEY_ID"), self.env.get(f"ALPACA_{e}_SECRET")

    def any_keys(self) -> tuple[str | None, str | None]:
        for b in BOOKS:
            k, s = self.keys(b)
            if k and s:
                return k, s
        return None, None

    def alpaca_data(self):
        k, s = self.any_keys()
        return AlpacaData(k, s) if k and s else None

    def hub(self, now_utc: datetime, today: date) -> DataHub:
        return DataHub(self.alpaca_data(), now_utc, today,
                       congress_key=self.env.get("CONGRESS_API_KEY"))

    def paper_broker(self, book_id: str):
        k, s = self.keys(book_id)
        return AlpacaBroker(k, s)

    def clock_fn(self, book_id: str, now_utc: datetime):
        k, s = self.keys(book_id)
        if k and s:
            return AlpacaBroker(k, s).clock
        def heuristic():
            from .util import PT
            t = now_utc.astimezone(PT)
            open_ = t.weekday() < 5 and (6, 30) <= (t.hour, t.minute) < (13, 0)
            return {"is_open": open_, "timestamp": iso(now_utc), "next_open": None, "next_close": None}
        return heuristic

    def asset_fn(self, book_id: str):
        k, s = self.keys(book_id)
        if k and s:
            return AlpacaBroker(k, s).asset
        return None


class Runner:
    def __init__(self, job: str, now_utc: datetime | None = None, deps: Deps | None = None,
                 force: bool = False, state_dir: Path = STATE_DIR, modes: dict | None = None,
                 assume_open: bool = False):
        self.job = job
        self.now = now_utc or utcnow()
        self.today = market_date(self.now)
        self.deps = deps or Deps()
        self.force = force
        self.state_dir = Path(state_dir)
        self.modes = modes or load_modes()
        self.assume_open = assume_open      # rehearsal only: pretend the market is open
        self.hub = self.deps.hub(self.now, self.today)
        self.run = {"job": job, "started": iso(self.now), "finished": None, "status": "ok",
                    "notes": [], "errors": [], "book_errors": {}}
        self.ledgers: dict[str, BookLedger] = {}
        self.prices: dict[str, dict] = {}
        self.changed = False                # anything worth a commit beyond fresh marks

    # ---------- helpers ----------
    def ledger_path(self, book_id: str) -> Path:
        return self.state_dir / f"ledger_{book_id}.json"

    def note(self, msg: str):
        self.run["notes"].append(msg)

    def friction(self, book_id: str) -> float:
        return FRICTION_CRYPTO if BOOKS[book_id]["asset_class"] == "crypto" else FRICTION_EQUITY

    def latest_for(self, book_id: str, symbols) -> dict:
        syms = sorted(set(symbols))
        if not syms:
            return {}
        if BOOKS[book_id]["asset_class"] == "crypto":
            return self.hub.crypto_latest(syms)
        return self.hub.stock_latest(syms)

    def make_broker(self, book_id: str, mode: str, L: BookLedger | None):
        if mode == "paper":
            return self.deps.paper_broker(book_id)
        acct = L.d["dryrun_account"] if L is not None else {"cash": BOOKS[book_id]["start_cash"],
                                                           "positions": {}, "orders": {}}

        def price_fn(symbol):
            return self.latest_for(book_id, [symbol]).get(symbol)
        clock = ((lambda: {"is_open": True, "timestamp": iso(self.now), "next_open": None,
                           "next_close": None}) if self.assume_open else self.deps.clock_fn(book_id, self.now))
        return DryRunBroker(acct, price_fn, clock_fn=clock, asset_fn=self.deps.asset_fn(book_id))

    # ---------- main ----------
    def execute(self) -> dict:
        kill = kill_switch_on()
        if self.job in WINDOWS_PT and not self.force and not in_window(self.now, WINDOWS_PT[self.job]):
            self.run["status"] = "skipped"
            self.note(f"{self.job}: outside its Pacific-time window {WINDOWS_PT[self.job]}; nothing done")
        else:
            for book_id in JOB_BOOKS[self.job]:
                try:
                    self.run_book(book_id)
                    self.run["book_errors"][book_id] = []
                except Exception as e:                     # contain the failure to this book
                    msg = f"{book_id}: {type(e).__name__}: {e}"
                    self.run["errors"].append(msg)
                    self.run["book_errors"][book_id] = [msg]
                    self.run["traceback"] = traceback.format_exc()[-2000:]
            if self.job == "eod":
                self.update_tournament()
        # load untouched ledgers so board state always covers every book
        for book_id in BOOKS:
            if book_id not in self.ledgers:
                d = read_json(self.ledger_path(book_id))
                if d:
                    self.ledgers[book_id] = BookLedger(d)
                    self.prices.setdefault(book_id, {k: p.get("last_price") for s in d["sleeves"].values()
                                                     for k, p in s["positions"].items()})
        tour = read_json(self.state_dir / "tournament.json")
        state_mod.build(self.ledgers, self.modes, tour, iso(utcnow()), self.prices, self.state_dir)
        if self.run["errors"] and self.run["status"] == "ok":
            self.run["status"] = "error"
        self.run["finished"] = iso(utcnow())
        state_mod.record_run(self.run, self.modes, kill, self.state_dir)
        return self.run

    def run_book(self, book_id: str):
        cfg = BOOKS[book_id]
        mode = self.modes[book_id]
        if self.today < date.fromisoformat(cfg["start_date"]) and not self.force:
            self.note(f"{cfg['label']}: starts {cfg['start_date']}; waiting")
            return
        path = self.ledger_path(book_id)
        data = read_json(path)
        L = BookLedger(data) if data else None
        if L is not None and L.d.get("mode") != mode:          # dry-run -> paper switch: archive, start clean
            archive = self.state_dir / f"ledger_{book_id}_{L.d.get('mode')}_{self.today.isoformat()}.json"
            write_json(archive, L.d)
            self.note(f"{cfg['label']}: mode changed {L.d.get('mode')} -> {mode}; old ledger archived")
            L = None
        if L is None:
            L = BookLedger.new(book_id, cfg, BOTS, mode)
            broker = self.make_broker(book_id, mode, L)
            if mode == "paper":
                acct, pos = broker.account(), broker.positions()
                if cfg.get("capital_cap"):
                    if pos or acct["cash"] < cfg["start_cash"]:
                        raise SetupError(
                            f"{cfg['label']}: needs at least ${cfg['start_cash']:,.0f} cash and no positions before "
                            f"the first run (found cash ${acct['cash']:,.2f}, {len(pos)} positions)")
                    L.d["reserve_cash"] = round(acct["cash"] - cfg["start_cash"], 2)
                    self.note(f"{cfg['label']}: manages ${cfg['start_cash']:,.0f}; "
                              f"${L.d['reserve_cash']:,.2f} of the paper account is an untouched reserve")
                elif pos or abs(acct["equity"] - cfg["start_cash"]) > 0.01 * cfg["start_cash"]:
                    raise SetupError(
                        f"{cfg['label']}: reset this Alpaca paper account to ${cfg['start_cash']:,.0f} with no "
                        f"positions before the first run (found equity ${acct['equity']:,.2f}, {len(pos)} positions)")
            self.note(f"{cfg['label']}: new {mode} ledger opened with ${cfg['start_cash']:,.2f}")
            self.changed = True
        else:
            broker = self.make_broker(book_id, mode, L)
        self.ledgers[book_id] = L
        try:
            self._run_book_steps(book_id, cfg, L, broker)
        finally:
            write_json(path, L.d)          # never lose the record of an order that reached the broker

    def _run_book_steps(self, book_id: str, cfg: dict, L: BookLedger, broker):
        fr = self.friction(book_id)

        # 1. bring earlier orders up to date
        for cid, rec in L.d["orders"].items():
            if not rec.get("id"):
                continue
            if rec.get("status") in TERMINAL and rec.get("applied_qty", 0) >= rec.get("filled_qty", 0) - 1e-12:
                continue
            try:
                self.apply_order(L, cid, broker.get_order(rec["id"]), fr)
            except BrokerError as e:
                self.note(f"{book_id}: could not refresh order {cid}: {e}")

        # 2. reconcile with the broker (fail closed on anything unexplained)
        acct, bpos = broker.account(), broker.positions()
        if acct.get("trading_blocked") or acct.get("account_blocked"):
            raise SetupError(f"{cfg['label']}: broker reports the account is blocked")
        tol = 0.005 if cfg["asset_class"] == "crypto" else 1e-4
        errors, notes = L.reconcile(bpos, acct["cash"] - L.d.get("reserve_cash", 0.0), rel_tol_single=tol)
        for n in notes:
            self.note(f"{book_id}: {n}")
        if errors:
            raise SetupError("; ".join(errors))
        unattributed = L.d.get("unattributed_cash", 0.0)
        eq_book = (acct["equity"] - L.d.get("reserve_cash", 0.0)) or cfg["start_cash"]
        if abs(unattributed) > max(1.0, 0.01 * eq_book):
            raise SetupError(f"{cfg['label']}: ${unattributed:,.2f} of cash is unexplained by the sleeves")

        # 3. marks and risk
        prices = {k: p["current_price"] for k, p in bpos.items() if p.get("current_price")}
        L.mark(prices)
        self.prices[book_id] = prices
        for n in L.update_risk(prices, cfg["lane"], ARCADE_BENCH_DD, self.today.isoformat()):
            self.note(f"{book_id}: {n}")

        # 4. bots
        if self.job in TRADING_JOBS:
            self.run_bots(book_id, L, broker)
            acct, bpos = broker.account(), broker.positions()
            prices = {k: p["current_price"] for k, p in bpos.items() if p.get("current_price")}
            L.mark(prices)
            self.prices[book_id] = prices

        # 5. end of day: history, highest closes, shadow dividends
        if self.job == "eod":
            L.note_close(prices)
            L.record_history(self.today.isoformat(), prices)
            if cfg["asset_class"] == "us_equity":
                held = sorted({p["symbol"] for s in L.sleeves.values() for p in s["positions"].values()} |
                              {k for h in L.d["history"][-10:] for sl in h["sleeves"].values() for k in sl["pos"]})
                try:
                    divs = self.hub.cash_dividends(held, self.today - timedelta(days=14), self.today)
                    for n in L.credit_dividends(divs):
                        self.note(f"{book_id}: {n}")
                except Exception as e:
                    self.note(f"{book_id}: dividend check failed, will retry next EOD ({e})")

    def run_bots(self, book_id: str, L: BookLedger, broker):
        cfg = BOOKS[book_id]
        if kill_switch_on():
            self.note(f"{book_id}: kill switch on; no bot ran")
            return
        clock = broker.clock()
        market_open = bool(clock.get("is_open"))
        if cfg["asset_class"] == "us_equity" and not market_open:
            self.note(f"{cfg['label']}: market closed at run time (holiday or late start); no trades")
            return
        fr = self.friction(book_id)
        for bot in cfg["bots"]:
            bcfg = BOTS[bot]
            if self.job not in bcfg["jobs"]:
                continue
            stamp = self.now.strftime("%Y%m%d%H") if self.job == "crypto" else self.today.strftime("%Y%m%d")
            done_key = f"{bot}:{self.job}:{stamp}"
            if done_key in L.d["done"] and not self.force:
                self.note(f"{bot}: already ran {self.job} for {stamp}")
                continue
            ctx = Ctx(bot=bot, job=self.job, now_utc=self.now, today=self.today, sleeve=L.sleeves[bot],
                      meta=L.d["meta"].setdefault(bot, {}), params=bcfg["params"], data=self.hub,
                      asset=broker.asset)
            try:
                intents = REGISTRY[bot](ctx)
            except Exception as e:
                self.note(f"{bot}: decision failed, no trades ({type(e).__name__}: {str(e)[:200]})")
                intents = []
            for n in ctx.notes:
                self.note(n)
            intents.sort(key=lambda it: 0 if it["side"] == "sell" else 1)    # free cash before buying
            rejections = L.d.setdefault("rejections", [])
            for it in intents:
                reserved = sum(float(o.get("notional_sent") or 0.0) for o in L.d["orders"].values()
                               if o.get("bot") == bot and o.get("side") == "buy"
                               and o.get("status") not in TERMINAL and o.get("status") != "error"
                               and not o.get("applied_qty"))
                ok, why, it2 = check_intent(it, L.sleeves[bot], bcfg, cfg["asset_class"], market_open,
                                            reserved_cash=reserved)
                if not ok:
                    rejections.append({"time": iso(self.now), "bot": bot, "side": it["side"],
                                       "symbol": it["symbol"], "why": why, "reason": it["reason"]})
                    continue
                cid = safe_id(f"{bot}-{stamp}-{self.job}-{it2['side']}-{canon(it2['symbol'])}")
                L.d["orders"][cid] = {"id": None, "bot": bot, "symbol": it2["symbol"], "side": it2["side"],
                                      "status": "submitting", "reason": it2["reason"], "applied_qty": 0.0,
                                      "filled_qty": 0.0, "submitted": iso(self.now),
                                      "decision_price": it2.get("price"),
                                      "notional_sent": it2.get("notional"), "qty_sent": it2.get("qty")}
                try:
                    order = broker.submit_order(it2["symbol"], it2["side"], cid, notional=it2.get("notional"),
                                                qty=it2.get("qty"),
                                                tif="gtc" if cfg["asset_class"] == "crypto" else "day",
                                                price_hint=it2.get("price"))
                    L.d["orders"][cid]["id"] = order["id"]
                    order = broker.wait_filled(order, 30)
                    self.apply_order(L, cid, order, fr)
                except BrokerError as e:
                    self.changed = True
                    L.d["orders"][cid]["status"] = "error"
                    L.d["orders"][cid]["error"] = str(e)[:300]
                    self.note(f"{bot}: order {cid} failed: {str(e)[:200]}")
            del rejections[:-500]
            L.d["done"][done_key] = iso(self.now)

    def apply_order(self, L: BookLedger, cid: str, order: dict, friction: float):
        rec = L.d["orders"][cid]
        rec.update(status=order.get("status"), filled_qty=order.get("filled_qty") or 0.0,
                   filled_avg_price=order.get("filled_avg_price"), id=order.get("id") or rec.get("id"))
        delta = (order.get("filled_qty") or 0.0) - rec.get("applied_qty", 0.0)
        px = order.get("filled_avg_price")
        if delta > 1e-12 and px:
            when = order.get("filled_at") or iso(utcnow())
            L.apply_fill(rec["bot"], rec["symbol"], rec["side"], delta, float(px), friction, when, cid, rec["reason"])
            rec["applied_qty"] = rec.get("applied_qty", 0.0) + delta
            self.changed = True

    def update_tournament(self):
        path = self.state_dir / "tournament.json"
        try:
            st = tour_mod.update(read_json(path))
            write_json(path, st)
            self.note("tournament updated through " + ", ".join(
                f"{m} {s.get('last_date')}" for m, s in st["models"].items()))
        except Exception as e:
            self.run["errors"].append(f"tournament: {type(e).__name__}: {e}")


def run_check(now_utc: datetime | None = None, deps: Deps | None = None,
              state_dir: Path = STATE_DIR) -> dict:
    """Verify each book's keys and paper account, plus every data feed. Never trades."""
    deps = deps or Deps()
    now = now_utc or utcnow()
    run = {"job": "check", "started": iso(now), "finished": None, "status": "ok",
           "notes": [], "errors": [], "book_errors": {}}
    modes = load_modes()
    for book_id, cfg in BOOKS.items():
        k, s = deps.keys(book_id)
        if not (k and s):
            run["errors"].append(f"{cfg['label']}: no keys in repo secrets")
            continue
        try:
            b = deps.paper_broker(book_id)
            a, pos, clk = b.account(), b.positions(), b.clock()
            want = cfg["start_cash"]
            ok = (a["cash"] >= want) if cfg.get("capital_cap") else abs(a["equity"] - want) <= 0.01 * want
            run["notes"].append(
                f"{cfg['label']}: key OK, paper cash ${a['cash']:,.2f}, equity ${a['equity']:,.2f}, "
                f"{len(pos)} positions, market {'open' if clk['is_open'] else 'closed'}; "
                f"{'ready' if ok and not pos else 'NOT READY'} for a ${want:,.0f} "
                f"{'capped ' if cfg.get('capital_cap') else ''}book (mode {modes[book_id]})")
            if not ok or pos:
                run["errors"].append(f"{cfg['label']}: account does not match its ${want:,.0f} setup")
        except Exception as e:
            run["errors"].append(f"{cfg['label']}: {type(e).__name__}: {str(e)[:200]}")
    hub = deps.hub(now, market_date(now))
    for label, fn in (("stock quotes (IEX)", lambda: hub.stock_latest(["SPY", "QQQ"])),
                      ("crypto quotes", lambda: hub.crypto_latest(["BTC/USD", "ETH/USD"])),
                      ("congress feed", lambda: {"trades": len(hub.congress(market_date(now) - timedelta(days=7)))}),
                      ("S&P 500 list", lambda: {"members": len(hub.sp500())})):
        try:
            run["notes"].append(f"{label}: OK {fn()}")
        except Exception as e:
            run["errors"].append(f"{label}: {type(e).__name__}: {str(e)[:200]}")
    run["status"] = "error" if run["errors"] else "ok"
    run["finished"] = iso(utcnow())
    state_mod.record_run(run, modes, kill_switch_on(), state_dir)
    return run


def run_rehearsal(now_utc: datetime | None = None, deps: Deps | None = None,
                  state_dir: Path = STATE_DIR / "rehearsal") -> dict:
    """What would every bot do right now? Live data, simulated fills, separate state folder."""
    state_dir = Path(state_dir)
    shutil.rmtree(state_dir, ignore_errors=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    now = now_utc or utcnow()
    modes = {b: "dry-run" for b in BOOKS}
    merged = {"job": "rehearsal", "started": iso(now), "finished": None, "status": "ok",
              "notes": [], "errors": [], "book_errors": {}}
    for job in ("morning", "preclose", "crypto"):
        r = Runner(job, now, deps, force=True, state_dir=state_dir, modes=modes, assume_open=True).execute()
        merged["notes"] += [f"[{job}] {n}" for n in r["notes"]]
        merged["errors"] += [f"[{job}] {e}" for e in r["errors"]]
    merged["status"] = "error" if merged["errors"] else "ok"
    merged["finished"] = iso(utcnow())
    return merged


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", choices=list(JOB_BOOKS) + ["check", "rehearsal"])
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--now")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    if not args.job and not args.auto:
        ap.error("--job or --auto required")
    job = resolve_job(args)
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
    runner = None
    if job == "check":
        run = run_check(now)
    elif job == "rehearsal":
        run = run_rehearsal(now)
    else:
        runner = Runner(job, now_utc=now, force=args.force)
        run = runner.execute()
    print(f"[{run['status']}] {job} started {run['started']} finished {run['finished']}")
    for n in run["notes"]:
        print("  -", n)
    for e in run["errors"]:
        print("  ERROR", e)
    commit = run["status"] != "skipped" and (job != "crypto" or (runner and runner.changed) or bool(run["errors"]))
    with Path(os.environ.get("GITHUB_ENV", "/dev/null")).open("a") as f:
        f.write(f"SIM_JOB={job}\nSIM_COMMIT={'yes' if commit else 'no'}\n")
    return 0          # errors are recorded in health.json; the workflow still commits state


if __name__ == "__main__":
    sys.exit(main())
