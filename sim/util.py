"""Small shared helpers: time zones, JSON files, money rounding."""
import json
import math
import os
import tempfile
from datetime import datetime, timezone, date, time
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def market_date(now_utc: datetime) -> date:
    """The US market's calendar date (Eastern time)."""
    return now_utc.astimezone(ET).date()


def pt_clock(now_utc: datetime) -> time:
    return now_utc.astimezone(PT).time()


def in_window(now_utc: datetime, window: tuple[str, str]) -> bool:
    lo, hi = (time.fromisoformat(w) for w in window)
    return lo <= pt_clock(now_utc) <= hi


def floor_cents(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100.0


def round_qty(q: float) -> float:
    """Alpaca accepts up to 9 decimal places; truncate so we never sell more than held."""
    return math.floor(q * 1e9 + 1e-6) / 1e9


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def write_json(path: Path, obj) -> None:
    """Atomic write so a crashed run never leaves half a state file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=False, default=str)
        f.write("\n")
    os.replace(tmp, path)


def safe_id(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum() or ch in "-_")[:120]
