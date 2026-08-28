#!/usr/bin/env python3
"""S1-1.0 "Regime Rotation" deterministic engine and pre-registered backtest grid.

Frozen spec (V3.1 Accord, registered 2026-08-28 BEFORE any testing):
  Universe (risk assets): SPY QQQ IWM EFA EEM TLT IEF GLD. Cash proxy: BIL.
  Signal, monthly on the first trading Monday (or next trading day if holiday):
    - absolute filter: last month-end close > mean of last N month-end closes (N=10 headline)
    - relative rank: 12-1 momentum (month-end t-1 vs t-13; headline) among qualifiers
    - hold top K (K=3 headline) at inverse 6-month realized-vol weights, 50% single cap
    - unfilled weight -> cash proxy (BIL total return; 0% before BIL inception 2007-05)
  Friction: 7 bps per side on traded notional (5 slippage + 2 spread), zero commission.
  Pre-registered grid (the ONLY combos ever tested): SMA {8,10,12} x top {2,3} x mom {6-1,12-1}.
  Periods: DEV 2004-02..2015-12, VAL 2016-01..2020-12, HOLDOUT 2021-01..present (sealed;
  opened once, headline spec only, only if the validation promotion bar is met).

Implementation notes fixed before results were seen:
  - Month-end series = last trading day close per calendar month.
  - SMA filter is inclusive of the most recent completed month-end (Faber convention).
  - Momentum m-1: monthend[t-2]/monthend[t-1-m] - 1 relative to the signal month
    (i.e., skip the most recent completed month... see mom() below: 12-1 uses ME(-2)/ME(-13)).
  - Rebalance at the signal day's close; asset returns accrue close-to-close between signal days.
  - Turnover per rebalance = 0.5 * sum|w_new - w_drifted|; cost = 2 * turnover * per_side.
    (Equivalently: per-side cost on traded notional, both sides of each swap.)
  - Inverse-vol weights use trailing 126 trading-day daily-return stdev as of the day
    before the signal day. Assets lacking history are ineligible.
  - Cash earns BIL close-to-close when BIL data exists, else 0%.
  - Sharpe uses monthly period returns vs 0% rf, annualized by sqrt(12); reported for
    comparison only. Max drawdown from the period-return equity curve.
"""
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(sys.argv[1] if len(sys.argv) > 1 else "wc-data/data")
RISK = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD"]
CASH = "BIL"
PER_SIDE = 0.0007  # 7 bps
DEV = ("2004-02-01", "2015-12-31")
VAL = ("2016-01-01", "2020-12-31")
HOLDOUT = ("2021-01-01", "2099-01-01")

# ---------- data ----------
px = {}
for t in RISK + [CASH]:
    df = pd.read_csv(DATA / f"{t}.csv", parse_dates=["date"]).set_index("date")
    px[t] = df["adj_close"].astype(float)
px = pd.DataFrame(px).sort_index()
cal = px["SPY"].dropna().index                      # trading calendar
px = px.reindex(cal)
me = px.resample("ME").last()                        # month-end closes
me.index = me.index.to_period("M")

def first_trading_monday(year, month):
    """First Monday of the month that is a trading day; else next trading day after it."""
    d = pd.Timestamp(year=year, month=month, day=1)
    while d.weekday() != 0:
        d += pd.Timedelta(days=1)
    later = cal[cal >= d]
    return later[0] if len(later) else None

signal_days = []
for p in pd.period_range("2004-02", px.index[-1].to_period("M"), freq="M"):
    d = first_trading_monday(p.year, p.month)
    if d is not None and d.to_period("M") == p:
        signal_days.append(d)
signal_days = pd.DatetimeIndex(signal_days)

daily_ret = px.pct_change()
vol126 = daily_ret[RISK].rolling(126).std()

def target_weights(sig_day, sma_n, top_k, mom_m):
    """Weights decided at sig_day using data through the PRIOR trading day / completed month-ends."""
    p = sig_day.to_period("M")
    hist = me.loc[:p - 1]                            # completed month-ends only
    if len(hist) < max(sma_n, mom_m + 1) + 1:
        return {}
    last = hist.iloc[-1]
    sma = hist.iloc[-sma_n:].mean()
    mom = hist.iloc[-2] if mom_m else None           # placeholder, real calc below
    mom = hist.iloc[-1] / hist.iloc[-(mom_m + 1)] - 1  # m-1 momentum: ME(-1) vs ME(-(m+1)), skipping nothing yet
    # skip-month convention: use ME(-2) vs ME(-(m+2)) is the alternative; the accord's
    # "12-1" = 12-month return excluding the latest month = ME(-2)/ME(-14)? Convention fixed:
    # 12-1 momentum = return over months t-12..t-1 excluding month t-1 => ME(-2)/ME(-13).
    mom = hist.iloc[-2] / hist.iloc[-(mom_m + 2)] - 1
    elig = []
    for t in RISK:
        if np.isfinite(last[t]) and np.isfinite(sma[t]) and np.isfinite(mom[t]):
            n_ok = hist[t].iloc[-sma_n:].notna().all() and hist[t].iloc[-(mom_m + 2):].notna().all()
            if n_ok and last[t] > sma[t] and mom[t] > 0:
                elig.append(t)
    if not elig:
        return {}
    ranked = sorted(elig, key=lambda t: mom[t], reverse=True)[:top_k]
    prior = cal[cal < sig_day]
    if not len(prior):
        return {}
    v = vol126.loc[prior[-1], ranked]
    if v.isna().any() or (v <= 0).any():
        return {t: 1.0 / len(ranked) for t in ranked}
    iv = 1.0 / v
    w = iv / iv.sum()
    w = w.clip(upper=0.50)
    w = w / w.sum() if w.sum() > 1.0 else w          # renormalize only if caps cut total above 1
    # (cap then renormalize proportionally among capped set; simple two-pass)
    for _ in range(3):
        over = w[w > 0.50]
        if over.empty:
            break
        excess = (over - 0.50).sum()
        w[over.index] = 0.50
        under = w[w < 0.50]
        if under.empty:
            break
        w[under.index] += excess * (w[under.index] / w[under.index].sum())
    return w.to_dict()

def run(sma_n, top_k, mom_m, start, end, per_side=PER_SIDE):
    days = signal_days[(signal_days >= start) & (signal_days <= end)]
    if len(days) < 12:
        return None
    equity, eq = [], 1.0
    w_prev = {}
    dates, turnover_sum = [], 0.0
    for i, d in enumerate(days):
        w_new = target_weights(d, sma_n, top_k, mom_m)
        # drift w_prev from previous signal day to d
        if i > 0:
            seg = px.loc[days[i - 1]:d]
            gross = {}
            for t, w in w_prev.items():
                r = seg[t].iloc[-1] / seg[t].iloc[0] - 1 if t != "CASH" else cash_ret(seg.index[0], seg.index[-1])
                gross[t] = w * (1 + r)
            tot = sum(gross.values()) if gross else 1.0
            eq *= tot
            w_drift = {t: g / tot for t, g in gross.items()}
        else:
            w_drift = {}
        wn = dict(w_new)
        wn["CASH"] = max(0.0, 1.0 - sum(w_new.values()))
        wd = dict(w_drift) if w_drift else {"CASH": 1.0}
        keys = set(wn) | set(wd)
        turn = 0.5 * sum(abs(wn.get(k, 0) - wd.get(k, 0)) for k in keys)
        cost = 2 * turn * per_side
        eq *= (1 - cost)
        turnover_sum += turn
        w_prev = wn
        dates.append(d)
        equity.append(eq)
    # final segment to end-date close
    seg = px.loc[dates[-1]:min(pd.Timestamp(end), px.index[-1])]
    if len(seg) > 1:
        gross = {}
        for t, w in w_prev.items():
            r = seg[t].iloc[-1] / seg[t].iloc[0] - 1 if t != "CASH" else cash_ret(seg.index[0], seg.index[-1])
            gross[t] = w * (1 + r)
        eq *= sum(gross.values())
        dates.append(seg.index[-1]); equity.append(eq)
    s = pd.Series(equity, index=dates)
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    rets = s.pct_change().dropna()
    dd = (s / s.cummax() - 1).min()
    return {
        "total": s.iloc[-1] - 1,
        "cagr": s.iloc[-1] ** (1 / yrs) - 1,
        "maxdd": dd,
        "sharpe": (rets.mean() / rets.std() * np.sqrt(12)) if rets.std() > 0 else np.nan,
        "turnover_yr": turnover_sum / yrs,
        "curve": s,
    }

def cash_ret(d0, d1):
    b = px[CASH].loc[d0:d1].dropna()
    if len(b) < 2:
        return 0.0
    return b.iloc[-1] / b.iloc[0] - 1

def bench(ticker, start, end):
    days = signal_days[(signal_days >= start) & (signal_days <= end)]
    s = px[ticker].loc[days[0]:min(pd.Timestamp(end), px.index[-1])].dropna()
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    dd = (s / s.cummax() - 1).min()
    m = s.resample("ME").last().pct_change().dropna()
    return {"total": s.iloc[-1] / s.iloc[0] - 1, "cagr": (s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1,
            "maxdd": dd, "sharpe": (m.mean() / m.std() * np.sqrt(12)) if m.std() > 0 else np.nan,
            "turnover_yr": 0.0}

def fmt(r):
    return f"total {r['total']*100:8.1f}%  cagr {r['cagr']*100:6.2f}%  maxDD {r['maxdd']*100:6.1f}%  sharpe {r['sharpe']:5.2f}  turn/yr {r['turnover_yr']:4.2f}"

if __name__ == "__main__":
    GRID = list(itertools.product([8, 10, 12], [2, 3], [6, 12]))
    HEAD = (10, 3, 12)
    out = {}
    for label, (start, end) in {"DEV": DEV, "VAL": VAL}.items():
        print(f"\n=== {label} {start[:7]} .. {end[:7]} ===")
        b = bench("SPY", start, end)
        print(f"B0 SPY buy-hold      : {fmt(b)}")
        out[label] = {"B0": b}
        for sma_n, top_k, mom_m in GRID:
            tag = f"SMA{sma_n:2d} top{top_k} mom{mom_m:2d}-1"
            r = run(sma_n, top_k, mom_m, start, end)
            star = "  <== HEADLINE (frozen)" if (sma_n, top_k, mom_m) == HEAD else ""
            print(f"{tag}: {fmt(r)}{star}")
            out[label][tag] = r
        hs = run(*HEAD, start, end, per_side=2 * PER_SIDE)
        print(f"HEADLINE @2x friction: {fmt(hs)}")
        out[label]["HEADLINE_2x"] = hs
    json_out = {lab: {k: {m: (float(v[m]) if m != 'curve' else None) for m in ('total','cagr','maxdd','sharpe','turnover_yr')} for k, v in d.items()} for lab, d in out.items()}
    Path("/home/claude/engine/results.json").write_text(json.dumps(json_out, indent=2))
    print("\nresults.json written")
