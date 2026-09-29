"""Caching facade over market data so each run fetches every series at most once."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Optional

from .marketdata import AlpacaData, congress_trades, sp500_symbols


class DataHub:
    def __init__(self, alpaca: Optional[AlpacaData], now_utc: datetime, today: date,
                 congress_fn: Optional[Callable] = None, sp500_fn: Optional[Callable] = None,
                 congress_key: Optional[str] = None):
        self.a = alpaca
        self.now_utc = now_utc
        self.today = today
        self._congress_fn = congress_fn or (lambda since: congress_trades(since, api_key=congress_key))
        self._sp500_fn = sp500_fn or sp500_symbols
        self._cache: dict = {}

    def _need(self):
        if self.a is None:
            raise RuntimeError("no market data source configured (Alpaca keys missing)")
        return self.a

    def stock_daily(self, symbols, lookback_days: int = 420) -> dict:
        key = ("sd", lookback_days)
        have = self._cache.setdefault(key, {})
        missing = sorted(set(symbols) - set(have))
        if missing:
            end = self.today - timedelta(days=1)            # completed sessions only
            start = self.today - timedelta(days=lookback_days)
            have.update(self._need().daily_bars(missing, start, end))
            for s in missing:
                have.setdefault(s, None)
        return {s: have.get(s) for s in symbols}

    def stock_latest(self, symbols) -> dict:
        have = self._cache.setdefault("sl", {})
        missing = sorted(set(symbols) - set(have))
        if missing:
            have.update(self._need().latest_prices(missing))
            for s in missing:
                have.setdefault(s, None)
        return {s: have[s] for s in symbols if have.get(s)}

    def crypto_daily(self, symbols, lookback_days: int = 160) -> dict:
        key = ("cd", tuple(sorted(symbols)), lookback_days)
        if key not in self._cache:
            self._cache[key] = self._need().crypto_daily_bars(
                list(symbols), self.today - timedelta(days=lookback_days), now_utc=self.now_utc)
        return self._cache[key]

    def crypto_latest(self, symbols) -> dict:
        have = self._cache.setdefault("cl", {})
        missing = sorted(set(symbols) - set(have))
        if missing:
            have.update(self._need().crypto_latest(missing))
            for s in missing:
                have.setdefault(s, None)
        return {s: have[s] for s in symbols if have.get(s)}

    def congress(self, since: date) -> list:
        key = ("cg", since)
        if key not in self._cache:
            self._cache[key] = self._congress_fn(since)
        return self._cache[key]

    def sp500(self) -> list:
        if "sp" not in self._cache:
            self._cache["sp"] = self._sp500_fn()
        return self._cache["sp"]

    def cash_dividends(self, symbols, start: date, end: date) -> list:
        if not symbols or self.a is None:
            return []
        return self.a.cash_dividends(symbols, start, end)
