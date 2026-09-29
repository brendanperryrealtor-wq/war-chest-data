"""Market data: Alpaca data API (runs on GitHub Actions), the repo's own EOD CSVs,
the free congressional-disclosure feed, and the S&P 500 member list.

Fail closed: every fetcher raises on a bad response; callers turn that into NO TRADE.
"""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import requests

from .config import DATA_BASE, DATA_DIR, CONGRESS_BASE, SP500_URL, UNIVERSE_DIR
from .util import ET


class DataError(RuntimeError):
    pass


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def bars_to_frame(rows: list[dict], crypto: bool = False) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    ts = pd.to_datetime(df["t"], utc=True)
    df.index = ts.dt.tz_convert("UTC" if crypto else ET).dt.date
    df.index.name = "date"
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["_start"] = ts.values
    return df[["open", "high", "low", "close", "volume", "_start"]].sort_index()


class AlpacaData:
    def __init__(self, key_id: str, secret: str, base: str = DATA_BASE, timeout: int = 30):
        if not key_id or not secret:
            raise DataError("missing Alpaca keys for market data")
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret})
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        r = self.s.get(self.base + path, params=params, timeout=self.timeout)
        if r.status_code >= 400:
            raise DataError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _paged(self, path: str, params: dict, key: str) -> dict:
        out: dict[str, list] = {}
        token = None
        for _ in range(200):                       # hard stop on runaway paging
            p = dict(params)
            if token:
                p["page_token"] = token
            j = self._get(path, p)
            for sym, rows in (j.get(key) or {}).items():
                out.setdefault(sym, []).extend(rows)
            token = j.get("next_page_token")
            if not token:
                return out
        raise DataError(f"paging did not terminate for {path}")

    def daily_bars(self, symbols: Iterable[str], start: date, end: date,
                   feed: str = "sip", adjustment: str = "all") -> dict[str, pd.DataFrame]:
        """Completed daily bars (end should be yesterday or earlier on the free plan)."""
        out = {}
        for chunk in _chunks(sorted(set(symbols)), 100):
            raw = self._paged("/v2/stocks/bars", {
                "symbols": ",".join(chunk), "timeframe": "1Day", "start": start.isoformat(),
                "end": end.isoformat(), "adjustment": adjustment, "feed": feed, "limit": 10000,
            }, "bars")
            for sym, rows in raw.items():
                out[sym] = bars_to_frame(rows)
        return out

    def latest_prices(self, symbols: Iterable[str], feed: str = "iex") -> dict[str, float]:
        out = {}
        for chunk in _chunks(sorted(set(symbols)), 100):
            j = self._get("/v2/stocks/trades/latest", {"symbols": ",".join(chunk), "feed": feed})
            for sym, t in (j.get("trades") or {}).items():
                if t and t.get("p"):
                    out[sym] = float(t["p"])
        return out

    def crypto_daily_bars(self, symbols: Iterable[str], start: date,
                          now_utc: Optional[datetime] = None) -> dict[str, pd.DataFrame]:
        """Only COMPLETED daily bars: a bar counts once its 24 hours have fully elapsed."""
        now_utc = now_utc or datetime.now(timezone.utc)
        raw = self._paged("/v1beta3/crypto/us/bars", {
            "symbols": ",".join(symbols), "timeframe": "1Day", "start": start.isoformat(), "limit": 10000,
        }, "bars")
        out = {}
        for sym, rows in raw.items():
            df = bars_to_frame(rows, crypto=True)
            done = pd.to_datetime(df["_start"], utc=True) + pd.Timedelta(days=1) <= pd.Timestamp(now_utc)
            out[sym] = df[done.values]
        return out

    def crypto_latest(self, symbols: Iterable[str]) -> dict[str, float]:
        j = self._get("/v1beta3/crypto/us/latest/trades", {"symbols": ",".join(symbols)})
        return {s: float(t["p"]) for s, t in (j.get("trades") or {}).items() if t and t.get("p")}

    def cash_dividends(self, symbols: Iterable[str], start: date, end: date) -> list[dict]:
        syms = sorted(set(symbols))
        if not syms:
            return []
        out, token = [], None
        for _ in range(50):
            p = {"symbols": ",".join(syms), "types": "cash_dividend", "start": start.isoformat(),
                 "end": end.isoformat(), "limit": 1000}
            if token:
                p["page_token"] = token
            j = self._get("/v1/corporate-actions", p)
            out.extend((j.get("corporate_actions") or {}).get("cash_dividends") or [])
            token = j.get("next_page_token")
            if not token:
                return out
        raise DataError("corporate actions paging did not terminate")


# ---------- the repo's own EOD data plane (nine ETFs, adjusted closes) ----------

def repo_closes(tickers: Iterable[str], data_dir: Path = DATA_DIR) -> pd.DataFrame:
    frames = {}
    for t in tickers:
        f = Path(data_dir) / f"{t}.csv"
        if not f.exists():
            raise DataError(f"repo data missing {t}")
        df = pd.read_csv(f, parse_dates=["date"]).set_index("date")
        frames[t] = df["adj_close"].astype(float)
    px = pd.DataFrame(frames).sort_index()
    if px.empty:
        raise DataError("repo data empty")
    return px


# ---------- free congressional-disclosure feed (House Clerk + Senate eFD records) ----------

def congress_trades(since: date, api_key: Optional[str] = None, limit: int = 100,
                    session: Optional[requests.Session] = None) -> list[dict]:
    s = session or requests.Session()
    headers = {"User-Agent": "war-chest-sim/1.0"}
    if api_key:
        headers["X-API-Key"] = api_key
    r = s.get(f"{CONGRESS_BASE}/trades", params={"from": since.isoformat(), "limit": limit},
              headers=headers, timeout=30)
    if r.status_code >= 400:
        raise DataError(f"congress feed -> {r.status_code}: {r.text[:200]}")
    j = r.json()
    trades = j.get("trades")
    if not isinstance(trades, list):
        raise DataError("congress feed: no trades list in response")
    return trades


# ---------- S&P 500 member list (public dataset on GitHub) ----------

def sp500_symbols(refresh: bool = False, max_age_days: int = 7) -> list[str]:
    f = UNIVERSE_DIR / "sp500.csv"
    stale = (not f.exists()) or (
        datetime.now(timezone.utc).timestamp() - f.stat().st_mtime > max_age_days * 86400)
    if refresh or stale:
        try:
            r = requests.get(SP500_URL, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            if "Symbol" not in df.columns or len(df) < 450:
                raise DataError("S&P 500 list looks wrong")
            f.parent.mkdir(parents=True, exist_ok=True)
            df[["Symbol", "Security", "GICS Sector"]].to_csv(f, index=False)
        except Exception:
            if not f.exists():
                raise
    df = pd.read_csv(f)
    return sorted(df["Symbol"].astype(str).str.strip().unique().tolist())
