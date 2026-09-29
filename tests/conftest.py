import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def bars(closes, end: date, volumes=None) -> pd.DataFrame:
    """Daily bars on business days ending at `end` (inclusive)."""
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes)).date
    v = volumes if volumes is not None else [1_000_000] * len(closes)
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes,
                         "volume": v}, index=pd.Index(idx, name="date"))


class FakeHub:
    def __init__(self, stock_bars=None, stock_latest=None, crypto_bars=None, crypto_latest=None,
                 congress=None, sp500=None):
        self.sb = stock_bars or {}
        self.sl = stock_latest or {}
        self.cb = crypto_bars or {}
        self.cl = crypto_latest or {}
        self.cg = congress or []
        self.sp = sp500 or []

    def stock_daily(self, symbols, lookback_days=420):
        return {s: self.sb.get(s) for s in symbols}

    def stock_latest(self, symbols):
        return {s: self.sl[s] for s in symbols if s in self.sl}

    def crypto_daily(self, symbols, lookback_days=160):
        return {s: self.cb.get(s) for s in symbols}

    def crypto_latest(self, symbols):
        return {s: self.cl[s] for s in symbols if s in self.cl}

    def congress(self, since):
        return self.cg

    def sp500(self):
        return self.sp

    def cash_dividends(self, symbols, start, end):
        return []


@pytest.fixture
def fake_hub():
    return FakeHub


@pytest.fixture
def mkbars():
    return bars
