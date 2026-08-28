#!/usr/bin/env python3
"""War Chest data plane: nightly EOD fetch for the frozen S1 universe.

Runs on GitHub Actions (open internet). Commits full-history CSVs to data/.
Primary source: Yahoo Finance via yfinance (dividend + split adjusted).
Fallback: Stooq CSV export (split-adjusted only; flagged in metadata).
Fail-closed: a ticker that fails both sources keeps its previous file and is
flagged in data/metadata.json. The engine decides NO SIGNAL, not this script.
"""
import io
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

TICKERS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "BIL"]
DATA = Path("data")
DATA.mkdir(exist_ok=True)
META = {"run_utc": datetime.now(timezone.utc).isoformat(), "sources": {}, "errors": {}}
MIN_ROWS = 900  # guards against junk/partial responses


def from_yahoo(ticker: str):
    import yfinance as yf

    df = yf.download(ticker, start="2003-01-01", interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or len(df) < MIN_ROWS:
        raise RuntimeError(f"yahoo returned {0 if df is None else len(df)} rows")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    out = df[["Close"]].rename(columns={"Close": "adj_close"})
    out.index.name = "date"
    return out, "yahoo_adjusted"


def from_stooq(ticker: str):
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": "war-chest-data/1.0"})
    raw = urllib.request.urlopen(req, timeout=30).read().decode()
    lines = [l for l in raw.strip().splitlines() if l]
    if len(lines) < MIN_ROWS or not lines[0].lower().startswith("date"):
        raise RuntimeError("stooq returned no usable csv")
    df = pd.read_csv(io.StringIO(raw), parse_dates=["Date"]).set_index("Date")
    out = df[["Close"]].rename(columns={"Close": "adj_close"})
    out.index.name = "date"
    return out, "stooq_split_adjusted_only"


for t in TICKERS:
    for source_fn in (from_yahoo, from_stooq):
        try:
            df, src = source_fn(t)
            df = df[~df.index.duplicated(keep="last")].sort_index()
            assert df.index.is_monotonic_increasing, "dates not sorted"
            assert df["adj_close"].gt(0).all(), "non-positive prices"
            df.to_csv(DATA / f"{t}.csv", float_format="%.4f")
            META["sources"][t] = {
                "source": src,
                "rows": int(len(df)),
                "first": str(df.index.min().date()),
                "last": str(df.index.max().date()),
            }
            break
        except Exception as e:
            META["errors"].setdefault(t, []).append(
                f"{source_fn.__name__}: {type(e).__name__}: {e}"
            )
            time.sleep(2)
    else:
        print(f"WARN {t}: all sources failed; previous file (if any) kept",
              file=sys.stderr)

(DATA / "metadata.json").write_text(json.dumps(META, indent=2))
missing = [t for t in TICKERS if t not in META["sources"]]
print(f"fetched {len(META['sources'])} of {len(TICKERS)} | failed: {missing or 'none'}")
# Exit 0 even on partial failure: engine-side integrity checks gate all signals.
