from datetime import date, datetime, timezone, timedelta

import numpy as np
import pandas as pd
import pytest

from sim.bots import Ctx, REGISTRY
from sim.config import BOOKS, BOTS
from sim.indicators import rsi_wilder, ema_series
from sim.ledger import BookLedger
from tests.conftest import FakeHub, bars

TODAY = date(2026, 10, 7)
NOW = datetime(2026, 10, 7, 19, 15, tzinfo=timezone.utc)


def ctx_for(bot, hub, job="preclose", ledger=None, asset=None, today=TODAY, now=NOW):
    L = ledger or BookLedger.new(BOTS[bot]["book"], BOOKS[BOTS[bot]["book"]], BOTS, "dry-run")
    return Ctx(bot=bot, job=job, now_utc=now, today=today, sleeve=L.sleeves[bot],
               meta=L.d["meta"][bot], params=BOTS[bot]["params"], data=hub,
               asset=asset or (lambda s: {"tradable": True, "fractionable": True})), L


def test_rsi_matches_hand_calculation():
    s = pd.Series([10.0, 11.0, 10.0, 12.0, 11.0])
    r = rsi_wilder(s, 2)
    # changes +1,-1,+2,-1 ; seed avg gain .5 loss .5 ; then g=(.5+2)/2=1.25 l=.25 ; then g=.625 l=.625
    assert r.iloc[2] == pytest.approx(50.0)
    assert r.iloc[3] == pytest.approx(100 - 100 / (1 + 1.25 / 0.25))
    assert r.iloc[4] == pytest.approx(50.0)


def test_b0_buys_once_at_preclose():
    hub = FakeHub(stock_latest={"SPY": 700.0})
    c, L = ctx_for("B0", hub)
    out = REGISTRY["B0"](c)
    assert len(out) == 1 and out[0]["side"] == "buy" and out[0]["notional"] == 2000.0
    L.apply_fill("B0", "SPY", "buy", 2.84, 700.0, 0.0007, "t", "c", "x")
    assert REGISTRY["B0"](c) == []
    c2, _ = ctx_for("B0", hub, job="morning", ledger=L)
    assert REGISTRY["B0"](c2) == []


def test_dip_buyer_enters_on_dip_in_uptrend_and_exits_on_bounce():
    closes = list(np.linspace(400, 500, 260)) + [490.0, 480.0]      # uptrend, then two down days
    hub = FakeHub(stock_bars={"SPY": bars(closes, TODAY - timedelta(days=1))},
                  stock_latest={"SPY": 470.0})
    c, L = ctx_for("A3", hub)
    out = REGISTRY["A3"](c)
    assert [o["symbol"] for o in out if o["side"] == "buy"] == ["SPY"]
    assert "RSI" in out[0]["reason"]
    L.apply_fill("A3", "SPY", "buy", 1.0, 470.0, 0.0007, "2026-10-07T19:15:00Z", "c", "x")
    nxt = TODAY + timedelta(days=1)
    hub2 = FakeHub(stock_bars={"SPY": bars(closes + [470.0], nxt - timedelta(days=1))},
                   stock_latest={"SPY": 495.0})
    c2, _ = ctx_for("A3", hub2, ledger=L, today=nxt)
    out2 = REGISTRY["A3"](c2)
    assert out2 and out2[0]["side"] == "sell" and "Bounce" in out2[0]["reason"]


def test_dip_buyer_ignores_dip_in_downtrend():
    closes = list(np.linspace(500, 400, 262))
    hub = FakeHub(stock_bars={"QQQ": bars(closes, TODAY - timedelta(days=1))}, stock_latest={"QQQ": 390.0})
    c, _ = ctx_for("A3", hub)
    assert [o for o in REGISTRY["A3"](c) if o["symbol"] == "QQQ"] == []


def test_crypto_trend_cross_up_buys_and_cross_down_sells():
    up = list(np.linspace(50_000, 70_000, 120))
    now = datetime(2026, 10, 7, 15, 23, tzinfo=timezone.utc)
    hub = FakeHub(crypto_bars={"BTC/USD": bars(up, date(2026, 10, 6))}, crypto_latest={"BTC/USD": 70_500.0})
    c, L = ctx_for("A4", hub, job="crypto", now=now)
    out = REGISTRY["A4"](c)
    assert out and out[0]["side"] == "buy" and out[0]["symbol"] == "BTC/USD"
    L.apply_fill("A4", "BTC/USD", "buy", 0.0142, 70_500.0, 0.0025, "t", "c", "x")
    down = up + list(np.linspace(69_000, 40_000, 60))
    hub2 = FakeHub(crypto_bars={"BTC/USD": bars(down, date(2026, 10, 6))}, crypto_latest={"BTC/USD": 40_000.0})
    c2, _ = ctx_for("A4", hub2, job="crypto", ledger=L, now=now)
    out2 = REGISTRY["A4"](c2)
    assert out2 and out2[0]["side"] == "sell" and out2[0]["qty"] == pytest.approx(0.0142)


def test_crypto_stale_bars_fail_closed():
    up = list(np.linspace(50_000, 70_000, 120))
    now = datetime(2026, 10, 12, 15, 23, tzinfo=timezone.utc)
    hub = FakeHub(crypto_bars={"BTC/USD": bars(up, date(2026, 10, 6))}, crypto_latest={"BTC/USD": 70_500.0})
    c, _ = ctx_for("A4", hub, job="crypto", now=now, today=date(2026, 10, 12))
    assert REGISTRY["A4"](c) == [] and any("stale" in n for n in c.notes)


def test_momentum_breakout_entry_and_trailing_stop():
    n = 300
    flat = [100.0] * (n - 1)
    hot = flat + [120.0]
    vols = [1_000_000] * (n - 1) + [3_000_000]
    cold = flat + [101.0]
    end = TODAY - timedelta(days=1)
    hub = FakeHub(stock_bars={"HOT": bars(hot, end, vols), "COLD": bars(cold, end)},
                  stock_latest={"HOT": 121.0, "COLD": 101.0}, sp500=["HOT", "COLD"])
    c, L = ctx_for("A2", hub)
    out = REGISTRY["A2"](c)
    assert [o["symbol"] for o in out] == ["HOT"] and "3.0x" in out[0]["reason"]
    L.apply_fill("A2", "HOT", "buy", 2.0, 121.0, 0.0007, "2026-10-07T19:15:00Z", "c", "x")
    later = TODAY + timedelta(days=7)
    path = hot + [130.0, 135.0, 128.0, 122.0]
    hub2 = FakeHub(stock_bars={"HOT": bars(path, later - timedelta(days=1), vols + [1e6] * 4)},
                   stock_latest={"HOT": 120.0}, sp500=["HOT"])
    c2, _ = ctx_for("A2", hub2, ledger=L, today=later)
    out2 = REGISTRY["A2"](c2)
    assert out2 and out2[0]["side"] == "sell" and "Trailing stop" in out2[0]["reason"]


def test_congress_copycat_filters_buys_and_follows_sales():
    trades = [
        {"member": "Hon. A", "member_slug": "a", "chamber": "house", "ticker": "NVDA", "type": "purchase",
         "amount_range": "$15,001 - $50,000", "transaction_date": "2026-09-20", "disclosure_date": "2026-10-05"},
        {"member": "Hon. B", "member_slug": "b", "chamber": "senate", "ticker": "LATE", "type": "purchase",
         "amount_range": "$1,001 - $15,000", "transaction_date": "2026-07-01", "disclosure_date": "2026-10-05"},
        {"member": "Hon. C", "member_slug": "c", "chamber": "house", "ticker": "OLD", "type": "purchase",
         "amount_range": "$1,001 - $15,000", "transaction_date": "2026-08-01", "disclosure_date": "2026-08-20"},
        {"member": "Hon. D", "member_slug": "d", "chamber": "house", "ticker": "IBIT", "type": "purchase",
         "amount_range": "$1,001 - $15,000", "transaction_date": "2026-10-01", "disclosure_date": "2026-10-06"},
    ]
    assets = {"NVDA": {"tradable": True, "fractionable": True}, "IBIT": {"tradable": True, "fractionable": False}}
    hub = FakeHub(stock_latest={"NVDA": 180.0}, congress=trades)
    c, L = ctx_for("A1", hub, job="morning", asset=lambda s: assets.get(s, {"tradable": False}))
    out = REGISTRY["A1"](c)
    assert [o["symbol"] for o in out] == ["NVDA"]
    assert "Hon. A" in out[0]["reason"] and "15 days later" in out[0]["reason"]
    assert any("LATE" in n for n in c.notes) and any("IBIT" in n for n in c.notes)
    assert REGISTRY["A1"](c) == []                     # already seen: never re-bought from the same filing
    L.apply_fill("A1", "NVDA", "buy", 1.1, 180.0, 0.0007, "2026-10-07T13:50:00Z", "c", "x")
    sale = trades + [{"member": "Hon. A", "member_slug": "a", "chamber": "house", "ticker": "NVDA",
                      "type": "sale (full)", "transaction_date": "2026-10-08", "disclosure_date": "2026-10-09"}]
    hub2 = FakeHub(stock_latest={"NVDA": 185.0}, congress=sale)
    c2, _ = ctx_for("A1", hub2, job="morning", ledger=L, today=date(2026, 10, 9))
    out2 = REGISTRY["A1"](c2)
    assert out2 and out2[0]["side"] == "sell" and "selling" in out2[0]["reason"]
    hub3 = FakeHub(stock_latest={"NVDA": 150.0}, congress=[])
    c3, _ = ctx_for("A1", hub3, job="preclose", ledger=L, today=date(2026, 10, 9))
    out3 = REGISTRY["A1"](c3)
    assert out3 and "Stop-loss" in out3[0]["reason"]
