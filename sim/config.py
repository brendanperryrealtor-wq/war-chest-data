"""War Chest paper sim: configuration.

Every number here is part of a registered spec (claude/paper-sim-blueprint.md and
Accord amendments A8 to A14). Changing a bot's parameters is a new version, not an edit.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / "state" / "sim"
UNIVERSE_DIR = ROOT / "sim" / "universe"
ENGINE_PATH = ROOT / "engine" / "s1_engine.py"
ENGINE_SHA256_PREFIX = "9448148b8ae51b98"   # frozen S1-1.0 engine; any other hash = NO SIGNAL
KILL_SWITCH = STATE_DIR / "KILL"            # if this file exists, no bot trades
MODE_FILE = ROOT / "sim" / "mode.json"      # per-book: "dry-run" or "paper"

# Only the PAPER endpoint exists in code. Live trading requires a code change
# approved by the principal (Accord A9), never a config flip.
PAPER_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"
CONGRESS_BASE = "https://www.bargo.ai/free-apis/congress/v1"
SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

OPENING_BELL = "2026-10-05"        # stock books and the Tournament start here
CRYPTO_START = "2026-10-03"        # Arcade Crypto goes first

# Shadow ledger friction (Accord Article 6 friction model; A8)
FRICTION_EQUITY = 0.0007           # 5 bps slippage + 2 bps spread, per side
FRICTION_CRYPTO = 0.0025           # conservative crypto fee + spread per side (confirm vs Alpaca schedule)

ARCADE_BENCH_DD = 0.25             # principal ruling Sep 29: bench an Arcade bot at 25% drawdown
VAULT_LADDER = [                   # Accord Article 8: (drawdown below, risk multiplier)
    (0.04, 1.00),
    (0.07, 0.75),
    (0.10, 0.50),
    (0.12, 0.00),                  # 10-12%: no new entries
]                                  # beyond 12%: suspended for audit

MIN_ORDER_USD = 1.00               # Alpaca fractional minimum
CASH_BUFFER = 0.995                # never commit the last 0.5% of a sleeve's cash

BOOKS = {
    "war_chest": {
        "label": "War Chest", "lane": "vault", "env": "WC", "asset_class": "us_equity",
        "start_cash": 2000.00, "start_date": OPENING_BELL, "bots": ["B0"],
    },
    "arcade_stocks": {
        "label": "Arcade Stocks", "lane": "arcade", "env": "ARCADE", "asset_class": "us_equity",
        "start_cash": 6000.00, "start_date": OPENING_BELL, "bots": ["A1", "A2", "A3"],
    },
    "arcade_crypto": {
        "label": "Arcade Crypto", "lane": "arcade", "env": "CRYPTO", "asset_class": "crypto",
        "start_cash": 2000.00, "start_date": CRYPTO_START, "bots": ["A4"],
    },
}

BOTS = {
    "B0": {
        "name": "B0 Control", "version": "1.0", "lane": "vault", "book": "war_chest",
        "start_cash": 2000.00, "jobs": ["preclose"],
        "params": {"symbol": "SPY"},
        "blurb": "Buys the whole market (SPY) and holds it. The bar every other bot has to beat.",
    },
    "A1": {
        "name": "Congress copycat", "version": "0.1-draft", "lane": "arcade", "book": "arcade_stocks",
        "start_cash": 2000.00, "jobs": ["morning", "preclose"],
        "params": {"max_positions": 10, "position_usd": 200.00, "max_filing_lag_days": 45,
                   "new_disclosure_days": 5, "max_hold_days": 90, "stop_loss": 0.15},
        "blurb": "Buys stocks members of Congress disclose buying; sells when they sell, after 90 days, or at -15%.",
    },
    "A2": {
        "name": "Momentum swing", "version": "0.1-draft", "lane": "arcade", "book": "arcade_stocks",
        "start_cash": 2000.00, "jobs": ["preclose"],
        "params": {"max_positions": 8, "position_usd": 250.00, "lookback_high": 252, "vol_avg": 50,
                   "vol_mult": 1.5, "sma": 50, "trail": 0.10},
        "blurb": "Buys S&P 500 stocks closing at a 52-week high on heavy volume; rides them with a 10% trailing stop.",
    },
    "A3": {
        "name": "Dip buyer", "version": "0.1-draft", "lane": "arcade", "book": "arcade_stocks",
        "start_cash": 2000.00, "jobs": ["preclose"],
        "params": {"symbols": ["SPY", "QQQ", "IWM", "DIA"], "max_positions": 4, "position_usd": 500.00,
                   "trend_sma": 200, "rsi_period": 2, "rsi_entry": 10.0, "exit_sma": 5, "max_hold_days": 10},
        "blurb": "Buys big index ETFs after a sharp 2-day drop while the long trend is up; sells the bounce.",
    },
    "A4": {
        "name": "Crypto trend", "version": "0.1-draft", "lane": "arcade", "book": "arcade_crypto",
        "start_cash": 2000.00, "jobs": ["crypto"],
        "params": {"symbols": ["BTC/USD", "ETH/USD"], "position_usd": 1000.00, "fast": 20, "slow": 50},
        "blurb": "Holds Bitcoin or Ether while its 20-day trend is above its 50-day trend; steps aside otherwise.",
    },
}

# Vault Tournament (our own simulator, Accord A8): $2,000 virtual per registered model.
TOURNAMENT = {
    "start_date": OPENING_BELL,
    "start_cash": 2000.00,
    "models": {
        "B0": {"name": "B0 Control", "version": "1.0", "kind": "buy_hold", "symbol": "SPY"},
        "S1": {"name": "S1 Regime Rotation", "version": "1.0", "kind": "s1",
               "sma_n": 10, "top_k": 3, "mom_m": 12},
    },
}

# Job windows in America/Los_Angeles (GitHub cron can start late; outside the window = skip, logged)
WINDOWS_PT = {
    "morning": ("06:35", "09:30"),
    "preclose": ("11:45", "12:58"),
}
