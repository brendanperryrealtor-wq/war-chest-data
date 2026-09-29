"""End to end in dry-run: the same runner, ledgers, reconciliation, and state files the
GitHub workflow uses, with a fake market and the in-memory broker."""
from datetime import date, datetime, timezone

import numpy as np
import pytest

from sim.run import Runner, Deps
from sim.util import read_json
from tests.conftest import FakeHub, bars


class FakeDeps(Deps):
    def __init__(self, hub, market_open=True):
        super().__init__(env={})
        self._hub, self._open = hub, market_open

    def hub(self, now_utc, today):
        return self._hub

    def clock_fn(self, book_id, now_utc):
        return lambda: {"is_open": self._open, "timestamp": None, "next_open": None, "next_close": None}

    def asset_fn(self, book_id):
        return lambda s: {"tradable": True, "fractionable": True, "status": "active", "class": "us_equity"}


def market(day_end: date):
    flat = [100.0] * 299
    etf = {s: bars(list(np.linspace(400, 450, 300)), day_end) for s in ("SPY", "QQQ", "IWM", "DIA")}
    etf["HOT"] = bars(flat + [120.0], day_end, [1e6] * 299 + [2.5e6])
    latest = {"SPY": 451.0, "QQQ": 451.0, "IWM": 451.0, "DIA": 451.0, "HOT": 121.0}
    return FakeHub(stock_bars=etf, stock_latest=latest, sp500=["HOT"], congress=[],
                   crypto_bars={"BTC/USD": bars(list(np.linspace(50_000, 70_000, 120)), date(2026, 10, 2)),
                                "ETH/USD": bars(list(np.linspace(4_000, 3_000, 120)), date(2026, 10, 2))},
                   crypto_latest={"BTC/USD": 70_100.0, "ETH/USD": 2_990.0})


def test_full_day_dry_run(tmp_path):
    hub = market(date(2026, 10, 2))
    # Saturday Oct 3: crypto goes first
    r = Runner("crypto", datetime(2026, 10, 3, 15, 23, tzinfo=timezone.utc), FakeDeps(hub),
               state_dir=tmp_path).execute()
    assert r["status"] == "ok", r
    trades = read_json(tmp_path / "trades.json")["trades"]
    assert [(t["bot"], t["side"], t["symbol"]) for t in trades] == [("A4", "buy", "BTC/USD")]
    assert "Uptrend" in trades[0]["reason"]

    # Monday Oct 5, pre-close: B0 buys SPY, A2 buys the breakout, A3 sees no dip
    now = datetime(2026, 10, 5, 19, 13, tzinfo=timezone.utc)
    r = Runner("preclose", now, FakeDeps(hub), state_dir=tmp_path).execute()
    assert r["status"] == "ok", r
    t = read_json(tmp_path / "trades.json")["trades"]
    got = sorted((x["bot"], x["side"], x["symbol"]) for x in t)
    assert got == [("A2", "buy", "HOT"), ("A4", "buy", "BTC/USD"), ("B0", "buy", "SPY")]
    b0 = next(x for x in t if x["bot"] == "B0")
    assert b0["notional"] == pytest.approx(1990.0, abs=0.01)
    assert b0["shadow_price"] > b0["price"]                        # friction on the shadow side

    # same window again: idempotent
    r = Runner("preclose", now, FakeDeps(hub), state_dir=tmp_path).execute()
    assert len(read_json(tmp_path / "trades.json")["trades"]) == 3
    assert any("already ran" in n for n in r["notes"])

    # outside the window: skipped, nothing traded
    r = Runner("preclose", datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc), FakeDeps(hub),
               state_dir=tmp_path).execute()
    assert r["status"] == "skipped"

    # evening: EOD history + Tournament; board files complete
    r = Runner("eod", datetime(2026, 10, 6, 2, 40, tzinfo=timezone.utc), FakeDeps(hub),
               state_dir=tmp_path).execute()
    assert r["status"] == "ok", r
    acc = read_json(tmp_path / "accounts.json")["books"]
    assert acc["war_chest"]["paper_equity"] == pytest.approx(2000.0, abs=0.5)
    assert acc["arcade_stocks"]["positions"][0]["symbol"] == "HOT"
    bots = {b["id"]: b for b in read_json(tmp_path / "bots.json")["bots"]}
    assert bots["A2"]["trades"] == 1 and bots["A3"]["trades"] == 0 and bots["B0"]["status"] == "active"
    assert {m["id"] for m in read_json(tmp_path / "bots.json")["tournament"]} == {"B0", "S1"}
    hist = read_json(tmp_path / "equity_history.json")
    assert hist["books"]["war_chest"][-1]["date"] == "2026-10-05"
    health = read_json(tmp_path / "health.json")
    assert set(health["last_runs"]) == {"crypto", "preclose", "eod"} and not health["open_errors"]


def test_market_closed_means_no_stock_trades(tmp_path):
    hub = market(date(2026, 10, 2))
    r = Runner("preclose", datetime(2026, 10, 5, 19, 13, tzinfo=timezone.utc), FakeDeps(hub, market_open=False),
               state_dir=tmp_path).execute()
    assert r["status"] == "ok"
    assert read_json(tmp_path / "trades.json")["trades"] == []
    assert any("market closed" in n for n in r["notes"])


def test_books_wait_for_their_start_date(tmp_path):
    hub = market(date(2026, 10, 2))
    r = Runner("preclose", datetime(2026, 10, 1, 19, 13, tzinfo=timezone.utc), FakeDeps(hub),
               state_dir=tmp_path).execute()
    assert any("starts 2026-10-05" in n for n in r["notes"])
    assert read_json(tmp_path / "accounts.json")["books"]["war_chest"]["status"] == "not started"


def test_every_cron_in_the_workflow_maps_to_a_job():
    import re
    from pathlib import Path
    from sim.run import SCHEDULE_TO_JOB
    yml = (Path(__file__).resolve().parent.parent / ".github/workflows/sim.yml").read_text()
    crons = re.findall(r'cron:\s*"([^"]+)"', yml)
    assert crons and all(c in SCHEDULE_TO_JOB for c in crons), crons
