import pytest

from sim import risk
from sim.config import BOOKS, BOTS
from sim.ledger import BookLedger


def new_arcade():
    return BookLedger.new("arcade_stocks", BOOKS["arcade_stocks"], BOTS, "dry-run")


def test_buy_sell_paper_and_shadow_math():
    L = new_arcade()
    L.apply_fill("A3", "SPY", "buy", 1.0, 500.0, 0.0007, "2026-10-05T19:15:00Z", "c1", "dip")
    s, sh = L.sleeves["A3"], L.shadow["A3"]
    assert s["cash"] == pytest.approx(1500.0)
    assert sh["cash"] == pytest.approx(2000 - 500 * 1.0007)
    assert s["positions"]["SPY"]["qty"] == 1.0
    L.apply_fill("A3", "SPY", "sell", 0.4, 520.0, 0.0007, "2026-10-06T19:15:00Z", "c2", "bounce")
    assert s["cash"] == pytest.approx(1500 + 0.4 * 520)
    assert s["realized"] == pytest.approx(0.4 * 520 - 0.4 * 500)
    assert s["positions"]["SPY"]["qty"] == pytest.approx(0.6)
    assert sh["cash"] == pytest.approx(2000 - 500 * 1.0007 + 0.4 * 520 * 0.9993)
    peq, seq = L.sleeve_equity("A3", {"SPY": 520.0})
    assert peq == pytest.approx(1500 + 0.4 * 520 + 0.6 * 520)
    assert seq < peq                                    # friction makes shadow worse
    with pytest.raises(ValueError):
        L.apply_fill("A3", "SPY", "sell", 1.0, 520.0, 0.0007, "t", "c3", "too much")


def test_reconcile_trueup_and_mismatch():
    L = new_arcade()
    L.apply_fill("A1", "NVDA", "buy", 1.0, 200.0, 0.0007, "t", "c1", "x")
    errs, notes = L.reconcile({"NVDA": {"qty": 1.0}}, broker_cash=5800.0, rel_tol_single=1e-4)
    assert errs == [] and L.d["unattributed_cash"] == pytest.approx(0.0)
    errs, notes = L.reconcile({"NVDA": {"qty": 0.99995}}, broker_cash=5800.0, rel_tol_single=1e-4)
    assert errs == [] and notes and L.held("A1", "NVDA") == pytest.approx(0.99995)
    errs, _ = L.reconcile({"NVDA": {"qty": 0.9}}, broker_cash=5800.0, rel_tol_single=1e-4)
    assert errs                                          # 10% gap is never trued-up
    errs, _ = L.reconcile({"NVDA": {"qty": 0.99995}, "AAPL": {"qty": 1.0}}, 5800.0, 1e-4)
    assert any("AAPL" in e for e in errs)                # a position no sleeve owns


def test_dividends_credit_shadow_once_after_ex_date():
    L = BookLedger.new("war_chest", BOOKS["war_chest"], BOTS, "dry-run")
    L.apply_fill("B0", "SPY", "buy", 2.0, 700.0, 0.0007, "2026-10-05T19:00:00Z", "c", "b0")
    L.record_history("2026-10-05", {"SPY": 700.0})
    L.record_history("2026-10-06", {"SPY": 701.0})
    before = L.shadow["B0"]["cash"]
    future = [{"symbol": "SPY", "ex_date": "2026-12-19", "rate": 1.9}]
    assert L.credit_dividends(future) == [] and L.shadow["B0"]["cash"] == before
    div = [{"symbol": "SPY", "ex_date": "2026-10-06", "rate": 1.5}]
    L.credit_dividends(div)
    assert L.shadow["B0"]["cash"] == pytest.approx(before + 3.0)
    L.credit_dividends(div)                               # idempotent
    assert L.shadow["B0"]["cash"] == pytest.approx(before + 3.0)
    assert L.sleeves["B0"]["cash"] == pytest.approx(2000 - 1400)   # paper never gets dividends


def test_arcade_bench_at_25_percent():
    L = new_arcade()
    L.apply_fill("A2", "XYZ", "buy", 10.0, 100.0, 0.0007, "t", "c", "x")
    notes = L.update_risk({"XYZ": 100.0}, "arcade", 0.25, "2026-10-05")
    assert L.sleeves["A2"]["status"] == "active" and not notes
    notes = L.update_risk({"XYZ": 49.0}, "arcade", 0.25, "2026-10-06")   # 1000 + 490 vs 2000 hw
    assert L.sleeves["A2"]["status"] == "benched" and notes


def test_ladder():
    assert risk.ladder_multiplier(0.02) == 1.0
    assert risk.ladder_multiplier(0.05) == 0.75
    assert risk.ladder_multiplier(0.08) == 0.5
    assert risk.ladder_multiplier(0.11) == 0.0
    assert risk.ladder_multiplier(0.13) is None


def test_check_intent_rules(tmp_path, monkeypatch):
    L = new_arcade()
    sl = L.sleeves["A3"]
    ok, why, it = risk.check_intent({"side": "buy", "symbol": "SPY", "notional": 5000, "reason": "r"},
                                    sl, BOTS["A3"], "us_equity", True)
    assert ok and it["notional"] == pytest.approx(1990.0)      # capped to cash x 0.995
    ok, why, _ = risk.check_intent({"side": "buy", "symbol": "SPY", "notional": 100, "reason": "r"},
                                   sl, BOTS["A3"], "us_equity", False)
    assert not ok and "closed" in why
    L.apply_fill("A3", "SPY", "buy", 1.0, 500.0, 0.0007, "t", "c", "x")
    ok, why, _ = risk.check_intent({"side": "buy", "symbol": "SPY", "notional": 100, "reason": "r"},
                                   sl, BOTS["A3"], "us_equity", True)
    assert not ok and "already held" in why
    ok, why, it = risk.check_intent({"side": "sell", "symbol": "SPY", "qty": 5, "reason": "r"},
                                    sl, BOTS["A3"], "us_equity", True)
    assert not ok
    ok, why, it = risk.check_intent({"side": "sell", "symbol": "SPY", "qty": None, "reason": "r"},
                                    sl, BOTS["A3"], "us_equity", True)
    assert ok and it["qty"] == 1.0
    sl["status"] = "benched"
    ok, why, _ = risk.check_intent({"side": "buy", "symbol": "QQQ", "notional": 100, "reason": "r"},
                                   sl, BOTS["A3"], "us_equity", True)
    assert not ok and "benched" in why
    kill = tmp_path / "KILL"
    kill.write_text("stop")
    monkeypatch.setattr(risk, "KILL_SWITCH", kill)
    ok, why, _ = risk.check_intent({"side": "sell", "symbol": "SPY", "qty": 1, "reason": "r"},
                                   sl, BOTS["A3"], "us_equity", True)
    assert not ok and "kill" in why
