# War Chest Paper Sim

Paper-trading engine for the War Chest Protocol (design: the project doc `claude/paper-sim-blueprint.md`; governance: V3.1 Accord plus amendments A8 to A14). Paper and dry-run only. Live trading does not exist in this code: adding it requires a code change approved by the principal. Educational research, not financial advice.

## What runs where

| Piece | File | When |
|---|---|---|
| Nightly prices (unchanged) | `fetch_data.py`, `.github/workflows/data.yml` | 02:30 UTC Tue to Sat |
| Bots, ledgers, board state | `sim/`, `.github/workflows/sim.yml` | morning, pre-close, hourly crypto, hourly snapshots, end of day |
| Tests | `tests/`, `.github/workflows/tests.yml` | every push to `sim/` or `tests/` |
| Frozen S1 engine (never edited) | `engine/s1_engine.py` | read by the Tournament, sha256 checked |

## Books and bots

| Book | Alpaca paper account | Start | Bots |
|---|---|---|---|
| War Chest (Vault) | account 1 | $2,000 | B0 Control (SPY, buy and hold) |
| Arcade Stocks | account 2 | $6,000 (three $2,000 sleeves) | A1 Congress copycat, A2 Momentum swing, A3 Dip buyer |
| Arcade Crypto | account 3 (Alpaca's original $100,000 paper account; the book manages only $2,000 and never touches the rest) | $2,000 | A4 Crypto trend |
| Tournament (our simulator) | none | $2,000 virtual each | B0, S1 Regime Rotation |

Every Alpaca fill is mirrored in a shadow ledger at 7 bps per side (crypto 25 bps) plus dividends, so the board shows the paper number and a realistic number side by side.

## One-time setup (Brendan, about 15 minutes)

1. Sign up for an Alpaca paper-only account with email at alpaca.markets.
2. In the paper dashboard, create paper accounts named War Chest ($2,000) and Arcade Stocks ($6,000). Alpaca's original $100,000 paper account serves as Arcade Crypto.
3. For each account, generate API keys and add them in this repo under Settings, Secrets and variables, Actions, as:
   `ALPACA_WC_KEY_ID`, `ALPACA_WC_SECRET`, `ALPACA_ARCADE_KEY_ID`, `ALPACA_ARCADE_SECRET`, `ALPACA_CRYPTO_KEY_ID`, `ALPACA_CRYPTO_SECRET`.
   Keys are typed only into GitHub. Never into chat, never into a file.
4. Optional: a free key for the congressional feed as `CONGRESS_API_KEY`.

Until the keys exist, the sim workflow does nothing.

## Controls

- Mode: `sim/mode.json` sets each book to `dry-run` (orders simulated, nothing sent) or `paper` (orders go to Alpaca paper). Flipping a book archives its dry-run ledger and starts a clean one.
- Kill switch: create the file `state/sim/KILL` (any content) and no bot trades until it is deleted.
- Manual run: Actions tab, war-chest-sim, Run workflow, pick a job.
- `check`: confirms every key, paper account balance, and data feed. Never trades. Results in `state/sim/health.json`.
- `rehearsal`: every bot decides on live prices with simulated fills, ignoring time windows and start dates. Results in `state/sim/rehearsal/`; the real ledgers are never touched.

## State files the board reads (`state/sim/`)

`accounts.json`, `bots.json`, `trades.json`, `equity_history.json`, `health.json`, `tournament.json`, plus one `ledger_<book>.json` per book.

## Fail closed

Missing keys, stale data, a position the ledger cannot explain, a closed market, or a run outside its time window all mean no trade, with the reason written to `state/sim/health.json`.
