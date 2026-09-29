"""Regression against the written record: the frozen S1 engine must reproduce the Sep 8, 2026
book and the -2.62% move to Sep 25 that the Sunday memos reported."""
import copy

import pandas as pd
import pytest

from sim import tournament as T
from sim.config import TOURNAMENT


@pytest.fixture(scope="module")
def engine():
    return T.load_engine()


def test_engine_hash_guard(tmp_path):
    bad = tmp_path / "s1_engine.py"
    bad.write_text("print('tampered')\n")
    with pytest.raises(T.EngineIntegrityError):
        T.load_engine(engine_path=bad)


def test_s1_reproduces_sep_8_book(engine):
    w = T.s1_weights(engine, pd.Timestamp("2026-09-08"), TOURNAMENT["models"]["S1"])
    assert round(w["EFA"] * 100, 2) == 38.99
    assert round(w["IWM"] * 100, 2) == 38.02
    assert round(w["EEM"] * 100, 2) == 23.00
    assert T.is_signal_day(engine, pd.Timestamp("2026-09-08"))       # Labor Day shifted it


def test_tournament_matches_memo_return_net_of_friction(engine):
    cfg = copy.deepcopy(TOURNAMENT)
    cfg["start_date"] = "2026-09-08"
    st = T.update(None, engine=engine, cfg=cfg)
    h = {x["date"]: x["equity"] for x in st["models"]["S1"]["history"]}
    net = h["2026-09-25"] / 2000 - 1
    assert net == pytest.approx(-0.0262 - 0.0007, abs=0.0003)         # memo gross -2.62%, minus 7 bps entry
    again = T.update(copy.deepcopy(st), engine=engine, cfg=cfg)       # re-running adds nothing
    assert len(again["models"]["S1"]["history"]) == len(st["models"]["S1"]["history"])
