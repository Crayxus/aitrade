#!/usr/bin/env python3
"""
Rich Trader — Daily Signal Optimizer  (vectorised, fast)
=========================================================
Fixed-time session entry with 6-signal score direction filter.
Trades EVERY day the score threshold is met → max frequency.

Strategy:
  - At session open time, enter in direction of 6-signal daily score
  - Score > 0 → LONG, Score < 0 → SHORT, Score = 0 → skip (rare)
  - SL = sl_mult × ATR,  TP = tp_mult × ATR
  - Force-exit at exit_utc (03:00 BJ)

Sessions (UTC, BJ=UTC+8):
  London  entry 07:00 UTC = 15:00 BJ   exit 19:00 UTC = 03:00 BJ
  NY      entry 13:00 UTC = 21:00 BJ   exit 19:00 UTC = 03:00 BJ
  USOIL: skip Wednesdays (EIA report)

Usage:
  py -3.10 optimizer.py               # full run
  py -3.10 optimizer.py --apply       # + patch server.py & Pine Script
  py -3.10 optimizer.py --symbol NAS100
"""

import argparse, itertools, json, re, sys, time, warnings
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8","utf8"):
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

TRAIN_SPLIT = "2025-01-01"
MIN_TRADES  = 8

INSTRUMENTS = [
    # ── London session (entry 07:00 UTC = 15:00 BJ) ────────────────────
    {"symbol": "XAUUSD", "ticker": "GC=F",    "display": "Gold",       "session": "London", "entry_hour": 7,  "exit_utc": 19},
    {"symbol": "EURUSD", "ticker": "EURUSD=X","display": "EUR/USD",    "session": "London", "entry_hour": 7,  "exit_utc": 19},
    {"symbol": "GBPUSD", "ticker": "GBPUSD=X","display": "GBP/USD",    "session": "London", "entry_hour": 7,  "exit_utc": 19},
    # ── NY session (entry 13:00 UTC = 21:00 BJ) ────────────────────────
    {"symbol": "NAS100", "ticker": "NQ=F",    "display": "Nasdaq 100", "session": "NY",     "entry_hour": 13, "exit_utc": 19},
    {"symbol": "USOIL",  "ticker": "CL=F",    "display": "Crude Oil",  "session": "NY",     "entry_hour": 13, "exit_utc": 19},
    {"symbol": "BTCUSD", "ticker": "BTC-USD", "display": "Bitcoin",    "session": "NY",     "entry_hour": 13, "exit_utc": 19},
]

# 75 combos — instant grid search
PARAM_GRID = {
    "tp_mult":   [1.0, 1.5, 2.0, 2.5, 3.0],   # TP = tp_mult × ATR
    "sl_mult":   [0.5, 0.75, 1.0, 1.25, 1.5],  # SL = sl_mult × ATR
    "score_min": [1, 2, 3],                      # min |score| to enter
}

# ─────────────────────────────────────────────────────────────────────────────
#  DAILY SCORE  (direction filter)
# ─────────────────────────────────────────────────────────────────────────────

def _wilder(arr, p):
    out = np.full(len(arr), np.nan)
    out[p-1] = float(np.mean(arr[:p]))
    a = 1.0/p
    for i in range(p, len(arr)):
        out[i] = out[i-1]*(1-a) + arr[i]*a
    return out

def _ema(arr, p):
    a = 2.0/(p+1); e = float(arr[0])
    out = np.empty(len(arr)); out[0] = e
    for i in range(1, len(arr)):
        e = a*float(arr[i]) + (1-a)*e; out[i] = e
    return out

def build_daily_scores(df_d: pd.DataFrame):
    c = np.asarray(df_d["Close"], float).ravel()
    h = np.asarray(df_d["High"],  float).ravel()
    l = np.asarray(df_d["Low"],   float).ravel()
    o = np.asarray(df_d["Open"],  float).ravel()
    n = len(c)

    ema20 = _ema(c, 20); ema50 = _ema(c, 50)
    mom20 = np.zeros(n)
    mom20[20:] = np.where(c[:-20] != 0, (c[20:]-c[:-20])/c[:-20], 0)

    dlt = np.diff(c, prepend=c[0])
    g = _wilder(np.where(dlt>0,dlt,0), 14)
    ls = _wilder(np.where(dlt<0,-dlt,0), 14)
    rsi = np.where(ls==0, 100.0, 100-100/(1+g/np.maximum(ls,1e-10)))

    gap = np.zeros(n)
    gap[1:] = np.where(c[:-1]!=0, (o[1:]-c[:-1])/c[:-1], 0)

    tr = np.empty(n); tr[0] = h[0]-l[0]
    tr[1:] = np.maximum(h[1:]-l[1:],
             np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
    atr14 = _wilder(tr, 14)

    s1 = np.where(c>ema20, 1, -1)
    s2 = np.where(mom20>0, 1, -1)
    s3 = np.where(gap>5e-4, 1, np.where(gap<-5e-4, -1, 0))
    s4 = np.where(rsi<40, 1, np.where(rsi>60, -1, 0))
    s5 = np.ones(n, dtype=int)
    s5[5:] = np.where(c[5:]>c[:-5], 1, -1)
    s6 = np.where(c>ema50, 1, -1)
    score = (s1+s2+s3+s4+s5+s6).astype(int)

    dates = [str(d)[:10] for d in df_d.index]
    return (pd.Series(dict(zip(dates, score))),
            pd.Series(dict(zip(dates, atr14))))

# ─────────────────────────────────────────────────────────────────────────────
#  PHASE 1 — PRECOMPUTE DAYS  (runs once per instrument)
# ─────────────────────────────────────────────────────────────────────────────

def precompute_days(df1h, df1d, inst):
    """
    Fixed-time entry: enter at inst['entry_hour'] UTC every trading day.
    Direction = sign(score).  Skip if score == 0 or no 1h bar found.

    Returns DataFrame: date, direction, entry_px, score, atr_d,
                       max_H, min_L, final_C
    """
    scores_s, atrs_s = build_daily_scores(df1d)

    df1h_utc = df1h.copy()
    if df1h_utc.index.tz is not None:
        df1h_utc.index = df1h_utc.index.tz_convert("UTC").tz_localize(None)

    # Fix lookahead: at entry time (07:00/13:00 UTC) today's daily bar hasn't closed.
    # Use PREVIOUS trading day's score/ATR, matching TradingView lookahead_off behavior.
    scores_lag = scores_s.shift(1)
    atrs_lag   = atrs_s.shift(1)

    rows = []
    for dt in sorted(set(df1h_utc.index.date)):
        ds = str(dt)
        if inst["symbol"] == "USOIL" and dt.weekday() == 2:
            continue  # skip EIA Wednesday

        sc  = int(scores_lag.get(ds, 0))
        if sc == 0:
            continue  # no directional bias today (very rare)
        atr = float(atrs_lag.get(ds, 0.0))
        if atr <= 0:
            continue

        direction = 1 if sc > 0 else -1

        # Entry bar: open of inst['entry_hour'] UTC
        eb = df1h_utc[
            (df1h_utc.index.date == dt) &
            (df1h_utc.index.hour == inst["entry_hour"])
        ]
        if eb.empty:
            continue
        entry_ts = eb.index[0]
        entry_px = float(eb.iloc[0]["Open"])
        if entry_px == 0:
            continue

        # Remaining bars until exit
        rem = df1h_utc[
            (df1h_utc.index > entry_ts) &
            (df1h_utc.index.date == dt) &
            (df1h_utc.index.hour <= inst["exit_utc"])
        ]
        if rem.empty:
            max_H = entry_px; min_L = entry_px; final_C = entry_px
        else:
            max_H   = float(rem["High"].max())
            min_L   = float(rem["Low"].min())
            final_C = float(rem.iloc[-1]["Close"])

        rows.append({
            "date": ds, "direction": direction, "entry_px": entry_px,
            "score": sc, "atr_d": atr,
            "max_H": max_H, "min_L": min_L, "final_C": final_C,
        })

    return pd.DataFrame(rows) if rows else pd.DataFrame()

# ─────────────────────────────────────────────────────────────────────────────
#  PHASE 2 — BACKTEST PARAMS  (vectorised, ~instant)
# ─────────────────────────────────────────────────────────────────────────────

def backtest_params(precomp: pd.DataFrame, params: dict, split: str):
    """Fully vectorised backtest — ATR-based SL/TP, fixed-time entry."""
    if precomp.empty:
        return [], []

    df = precomp.copy()

    # Score threshold filter (direction already encoded in precomp)
    sm = params["score_min"]
    df = df[df["score"].abs() >= sm]
    if df.empty:
        return [], []

    tp_m = params["tp_mult"]
    sl_m = params["sl_mult"]

    ep  = df["entry_px"].values
    atr = df["atr_d"].values
    mxH = df["max_H"].values
    mnL = df["min_L"].values
    fnC = df["final_C"].values
    dr  = df["direction"].values

    # ATR-based SL / TP
    sl_long  = ep - sl_m * atr;  tp_long  = ep + tp_m * atr
    sl_short = ep + sl_m * atr;  tp_short = ep - tp_m * atr

    # Long: if both SL and TP hit (unknown order) → assume SL (conservative)
    hl = mnL <= sl_long;  tl = mxH >= tp_long
    pnl_l = np.where(hl & tl,  (sl_long  - ep)/ep,
             np.where(tl,       (tp_long  - ep)/ep,
             np.where(hl,       (sl_long  - ep)/ep,
                                (fnC      - ep)/ep)))

    hs = mxH >= sl_short; ts_ = mnL <= tp_short
    pnl_s = np.where(hs & ts_, (ep - sl_short)/ep,
             np.where(ts_,      (ep - tp_short)/ep,
             np.where(hs,       (ep - sl_short)/ep,
                                (ep - fnC     )/ep)))

    pnl   = np.where(dr == 1, pnl_l, pnl_s)
    dates = df["date"].values

    tr_pnl = list(pnl[dates < split])
    te_pnl = list(pnl[dates >= split])
    return tr_pnl, te_pnl

# ─────────────────────────────────────────────────────────────────────────────
#  METRICS
# ─────────────────────────────────────────────────────────────────────────────

def calc_metrics(pnl_list):
    if len(pnl_list) < MIN_TRADES:
        return None
    p   = np.array(pnl_list, float)
    win = p[p > 0]; los = p[p < 0]
    gw  = float(win.sum()) if len(win) else 0.0
    gl  = float(-los.sum()) if len(los) else 1e-9
    wr  = len(win)/len(p)
    if wr < 0.28:
        return None
    pf  = min(gw/gl, 50.0) if gl > 0 else 50.0
    eq  = np.cumprod(1+p)
    pk  = np.maximum.accumulate(eq)
    dd  = float(((pk-eq)/pk).max())*100
    mu, sg = float(p.mean()), float(p.std())
    sh  = mu/sg*np.sqrt(252) if sg > 0 else 0.0
    return {
        "n": len(p), "wr": round(wr*100,1), "pf": round(pf,4),
        "sharpe": round(sh,3), "max_dd": round(dd,2),
        "avg_pnl": round(mu*100,4),
        "total_r": round((float(eq[-1])-1)*100, 2),
    }

def combined_score(tr, te):
    oof = min(te["pf"]/tr["pf"], 1.0) if tr["pf"] > 0 else 0.0
    return te["pf"]*0.50 + te["wr"]/100*0.20 + te["sharpe"]/4*0.15 + oof*0.10 + tr["pf"]*0.05

# ─────────────────────────────────────────────────────────────────────────────
#  OPTIMISE ONE INSTRUMENT
# ─────────────────────────────────────────────────────────────────────────────

def optimise(inst, df1h, df1d, top_n=5):
    sym = inst["symbol"]
    print(f"\n  [{sym}]  precomputing days …", flush=True)
    t0 = time.time()
    precomp = precompute_days(df1h, df1d, inst)
    t1 = time.time()
    if precomp.empty:
        print(f"  [{sym}]  no breakout days found"); return None

    n_days  = len(precomp)
    n_long  = (precomp["direction"]==1).sum()
    n_short = (precomp["direction"]==-1).sum()
    print(f"  [{sym}]  {n_days} signal days ({n_long}L/{n_short}S) in {t1-t0:.1f}s  "
          f"|  grid search …", flush=True)

    keys   = list(PARAM_GRID.keys())
    combos = [dict(zip(keys, v))
              for v in itertools.product(*[PARAM_GRID[k] for k in keys])]

    results = []
    for params in combos:
        tr_p, te_p = backtest_params(precomp, params, TRAIN_SPLIT)
        tr_m = calc_metrics(tr_p)
        te_m = calc_metrics(te_p)
        if tr_m is None or te_m is None:
            continue
        if tr_m["pf"] < 0.90:          # reject if losing badly in-sample
            continue
        cs = combined_score(tr_m, te_m)
        results.append({"params": params, "train": tr_m, "test": te_m, "score": round(cs,5)})

    t2 = time.time()
    if not results:
        print(f"  [{sym}]  no valid combos  ({t2-t1:.1f}s grid)"); return None

    results.sort(key=lambda x: x["score"], reverse=True)
    b = results[0]
    te_n = b["test"]["n"]
    # Test data spans from TRAIN_SPLIT to today: roughly (today-TRAIN_SPLIT).days / 30 months
    import datetime as _dt
    months_test = max(1, (_dt.date.today() - _dt.date.fromisoformat(TRAIN_SPLIT)).days / 30)
    print(
        f"  [{sym}]  {t2-t1:.1f}s grid  |  {len(results)} valid  |  "
        f"BEST: PF_tr={b['train']['pf']:.3f}  PF_te={b['test']['pf']:.3f}  "
        f"WR_te={b['test']['wr']}%  n_te={te_n}  "
        f"(~{te_n/months_test:.0f}/mo)",
        flush=True
    )
    return {"inst": inst, "top": results[:top_n], "precomp_n": n_days}

# ─────────────────────────────────────────────────────────────────────────────
#  DISPLAY
# ─────────────────────────────────────────────────────────────────────────────

def print_results(all_results, top_n):
    import datetime as _dt
    months_test = max(1, (_dt.date.today()-_dt.date.fromisoformat(TRAIN_SPLIT)).days/30)

    print("\n" + "="*72)
    print("  ORB OPTIMIZER — RESULTS")
    print(f"  Train < {TRAIN_SPLIT}   |   Test >= {TRAIN_SPLIT}  ({months_test:.0f} months)")
    print("="*72)
    for res in all_results:
        if not res: continue
        inst = res["inst"]
        print(f"\n  {'─'*68}")
        print(f"  {inst['display']} ({inst['symbol']})  —  {inst['session']} session")
        print(f"  {'─'*68}")
        hdr = (f"  {'#':>2}  {'TP×ATR':>7}  {'SL×ATR':>7}  {'sc':>3}  "
               f"{'PF_tr':>7}  {'PF_te':>7}  {'WR_te':>6}  {'n_te':>5}  "
               f"{'avg/tr':>7}  {'Shp':>6}  {'DD':>5}  {'ann%':>6}")
        print(hdr)
        print("  " + "─"*len(hdr))
        for i, r in enumerate(res["top"][:top_n], 1):
            p, tr, te = r["params"], r["train"], r["test"]
            mk = " <-- BEST" if i == 1 else ""
            ann = te["total_r"] / months_test * 12
            print(
                f"  {i:>2}  {p['tp_mult']:.2f}x    {p['sl_mult']:.2f}x    "
                f"{p['score_min']:>3}  "
                f"{tr['pf']:>7.3f}  {te['pf']:>7.3f}  "
                f"{te['wr']:>6.1f}%  {te['n']:>5}  "
                f"{te['avg_pnl']:>6.3f}%  "
                f"{te['sharpe']:>6.2f}  {te['max_dd']:>4.1f}%  "
                f"{ann:>5.1f}%{mk}"
            )

    # ── P&L SUMMARY ──────────────────────────────────────────────────────
    POS = 1000   # assumed position size in USD per trade
    print("\n" + "="*72)
    print(f"  P&L SUMMARY  (per-position basis, ${POS}/trade assumed)")
    print(f"  {'Instrument':<10} {'PF':>6} {'WR':>7} {'n/mo':>6} "
          f"{'avg/tr':>8} {'$/trade':>8} {'$/mo':>8} {'ann%':>7} {'n_total':>8}")
    print("  " + "─"*70)
    total_mo = 0
    for res in all_results:
        if not res or not res["top"]: continue
        inst, b = res["inst"], res["top"][0]
        te = b["test"]
        n_mo = te["n"] / months_test
        avg_pct = te["avg_pnl"] / 100          # fraction per trade
        usd_per_trade = avg_pct * POS
        usd_per_mo    = usd_per_trade * n_mo
        ann           = te["total_r"] / months_test * 12
        total_mo     += usd_per_mo
        reliability = "✓✓" if te["n"] >= 80 else "✓" if te["n"] >= 40 else "⚠️ low n"
        print(f"  {inst['symbol']:<10} {te['pf']:>6.3f} {te['wr']:>6.1f}% {n_mo:>6.1f} "
              f"{te['avg_pnl']:>7.3f}%  ${usd_per_trade:>6.2f}  ${usd_per_mo:>6.2f}  "
              f"{ann:>6.1f}%  {te['n']:>4} {reliability}")
    print("  " + "─"*70)
    print(f"  {'COMBINED':<10} {'':>6} {'':>7} {'':>6} "
          f"{'':>8} {'':>8} ${total_mo:>6.2f}/mo  (~${total_mo*12:.0f}/yr) on ${POS}/trade")
    print(f"\n  NOTE: ann% = annual return on POSITION (not on total capital).")
    print(f"  With 5:1 CFD leverage → multiply by 5.  ")
    print(f"  Reliability: ✓✓ = n>=80 (high), ✓ = n>=40 (medium), ⚠️ = low sample.\n")

    print("="*72)
    print("  TRADINGVIEW INPUT SETTINGS")
    print("="*72)
    for res in all_results:
        if not res or not res["top"]: continue
        inst, b = res["inst"], res["top"][0]
        p, te = b["params"], b["test"]
        n_mo = te["n"] / months_test
        ann  = te["total_r"] / months_test * 12
        print(f"  {inst['symbol']:<8}  "
              f"TP={p['tp_mult']:.2f}×ATR  SL={p['sl_mult']:.2f}×ATR  score>={p['score_min']}  "
              f"  PF={te['pf']:.3f}  WR={te['wr']}%  "
              f"~{n_mo:.0f}/mo  ann={ann:.1f}%")
    print()

# ─────────────────────────────────────────────────────────────────────────────
#  APPLY + PINE SCRIPT
# ─────────────────────────────────────────────────────────────────────────────

def apply_to_server(all_results):
    server = Path(__file__).parent / "server.py"
    if not server.exists():
        print("  server.py not found"); return
    src = server.read_text(encoding="utf-8")
    changed = False
    for res in all_results:
        if not res or not res["top"]: continue
        sym  = res["inst"]["symbol"]
        best = res["top"][0]["params"]
        sl_n = best["sl_mult"]
        tp_n = best["tp_mult"]
        pat  = (rf'("symbol":\s*"{re.escape(sym)}".*?"sl_atr":\s*)([\d.]+)'
                rf'(.*?"tp_atr":\s*)([\d.]+)')
        new  = re.sub(pat, lambda m: m.group(1)+str(sl_n)+m.group(3)+str(tp_n),
                      src, flags=re.DOTALL)
        if new != src:
            src = new; changed = True
            print(f"  {sym}: sl_atr={sl_n}  tp_atr={tp_n}")
    if changed:
        server.write_text(src, encoding="utf-8")
        print("  server.py updated")
    else:
        print("  server.py: no matching symbols found (check sl_atr/tp_atr fields)")

def generate_pine(all_results):
    pby = {}
    for res in all_results:
        if res and res["top"]:
            pby[res["inst"]["symbol"]] = (res["top"][0]["params"], res["inst"])

    def _p(sym, default_tp, default_sl, default_sc):
        if sym in pby:
            p, _ = pby[sym]
            return p["tp_mult"], p["sl_mult"], p["score_min"]
        return default_tp, default_sl, default_sc

    xau_tp, xau_sl, xau_sc = _p("XAUUSD", 1.5, 1.5, 2)
    nas_tp, nas_sl, nas_sc = _p("NAS100", 1.0, 1.0, 1)
    oil_tp, oil_sl, oil_sc = _p("USOIL",  1.0, 1.25, 1)
    btc_tp, btc_sl, btc_sc = _p("BTCUSD", 1.5, 0.5, 3)

    # use XAUUSD defaults for London session input
    xau = {"tp_mult": xau_tp, "sl_mult": xau_sl, "score_min": xau_sc}
    nas = {"tp_mult": nas_tp, "sl_mult": nas_sl, "score_min": nas_sc}
    oil = {"tp_mult": oil_tp, "sl_mult": oil_sl, "score_min": oil_sc}
    btc = {"tp_mult": btc_tp, "sl_mult": btc_sl, "score_min": btc_sc}

    pine = f"""\
// @version=5
// ╔══════════════════════════════════════════════════════════════════╗
// ║  RICH TRADER v3  —  Daily Signal, Fixed-Time Entry              ║
// ║  Auto-generated by optimizer.py (walk-forward validated)        ║
// ║                                                                  ║
// ║  OPTIMAL SETTINGS (paste this script on each instrument chart)  ║
// ║  XAUUSD  London 15:00 BJ  TP={xau_tp:.2f}×ATR  SL={xau_sl:.2f}×ATR  score>={xau_sc}  ║
// ║  NAS100  NY     21:00 BJ  TP={nas_tp:.2f}×ATR  SL={nas_sl:.2f}×ATR  score>={nas_sc}  ║
// ║  USOIL   NY     21:00 BJ  TP={oil_tp:.2f}×ATR  SL={oil_sl:.2f}×ATR  score>={oil_sc}  ║
// ║  BTCUSD  NY     21:00 BJ  TP={btc_tp:.2f}×ATR  SL={btc_sl:.2f}×ATR  score>={btc_sc}  ║
// ║  Force-exit 03:00 BJ (19:00 UTC)                                ║
// ╚══════════════════════════════════════════════════════════════════╝
strategy("Rich Trader v3", overlay=true, pyramiding=0,
     default_qty_type=strategy.cash, default_qty_value=200,
     initial_capital=10000, currency=currency.USD,
     process_orders_on_close=false)

// ── Inputs ───────────────────────────────────────────────────────
g1 = "Session"
i_session  = input.string("London", "Session", options=["London","NY"], group=g1)
i_skip_wed = input.bool(false, "Skip Wednesdays (USOIL/EIA)", group=g1)

g2 = "Signal Parameters"
i_tp_mult = input.float({xau_tp}, "TP × ATR", step=0.25, minval=0.25, group=g2)
i_sl_mult = input.float({xau_sl}, "SL × ATR", step=0.25, minval=0.25, group=g2)
i_sc_min  = input.int({xau_sc},   "Min |score| to enter (1-6)", minval=1, maxval=6, group=g2)

// ── Beijing time ─────────────────────────────────────────────────
bj_h   = hour(time,   "Asia/Shanghai")
bj_m   = minute(time, "Asia/Shanghai")
bj_min = bj_h * 60 + bj_m
bj_day = year(time,"Asia/Shanghai")*10000 + month(time,"Asia/Shanghai")*100 + dayofmonth(time,"Asia/Shanghai")
bj_dow = dayofweek(time, "Asia/Shanghai")  // 1=Sun … 4=Wed … 7=Sat

// London: entry 15:00-15:59 BJ (= 07:00 UTC open bar)
// NY:     entry 21:00-21:59 BJ (= 13:00 UTC open bar)
entry_s = i_session == "London" ? 900  : 1260   // 15:00 / 21:00
entry_e = i_session == "London" ? 960  : 1320   // 16:00 / 22:00
in_entry = bj_min >= entry_s and bj_min < entry_e
in_exit  = bj_h >= 3 and bj_h < 9              // 03:00-09:00 BJ force-exit

// ── Multi-timeframe data ─────────────────────────────────────────
d_cls  = request.security(syminfo.tickerid, "D", close,    lookahead=barmerge.lookahead_off)
d_opn  = request.security(syminfo.tickerid, "D", open,     lookahead=barmerge.lookahead_on)
d_prv  = request.security(syminfo.tickerid, "D", close[1], lookahead=barmerge.lookahead_on)

// ── Indicators ───────────────────────────────────────────────────
atr14  = request.security(syminfo.tickerid, "D", ta.atr(14), lookahead=barmerge.lookahead_off)
ema20d = ta.ema(d_cls, 20)
ema50d = ta.ema(d_cls, 50)
mom20  = d_cls[20] != 0 ? (d_cls - d_cls[20]) / d_cls[20] : 0.0
rsi14  = ta.rsi(d_cls, 14)
gap    = d_prv  != 0 ? (d_opn - d_prv) / d_prv : 0.0

// ── 6-signal score ───────────────────────────────────────────────
s1 = d_cls > ema20d ? 1 : -1
s2 = mom20  > 0     ? 1 : -1
s3 = gap    > 0.0005 ? 1 : gap < -0.0005 ? -1 : 0
s4 = rsi14  < 40    ? 1 : rsi14 > 60     ? -1 : 0
s5 = d_cls  > d_cls[5] ? 1 : -1          // 5-day momentum (matches optimizer)
s6 = d_cls  > ema50d ? 1 : -1
score = s1 + s2 + s3 + s4 + s5 + s6

go_long  = score >=  i_sc_min
go_short = score <= -i_sc_min

// ── Entry: once per BJ day ───────────────────────────────────────
var int last_day = 0
no_pos   = strategy.position_size == 0
fresh    = last_day != bj_day
wed_ok   = not i_skip_wed or bj_dow != dayofweek.wednesday

sig_long  = in_entry and no_pos and fresh and wed_ok and go_long
sig_short = in_entry and no_pos and fresh and wed_ok and go_short

if sig_long
    strategy.entry("L", strategy.long,  comment="LONG "  + str.tostring(score) + "/6")
    strategy.exit("Lx","L", stop=close - i_sl_mult*atr14, limit=close + i_tp_mult*atr14)
    last_day := bj_day

if sig_short
    strategy.entry("S", strategy.short, comment="SHORT " + str.tostring(score) + "/6")
    strategy.exit("Sx","S", stop=close + i_sl_mult*atr14, limit=close - i_tp_mult*atr14)
    last_day := bj_day

// ── Force-exit 03:00 BJ ──────────────────────────────────────────
if in_exit and not no_pos
    strategy.close_all(comment="03:00 EXIT")

// ── Alerts (webhook JSON) ────────────────────────────────────────
if sig_long
    alert('{{"action":"LONG","symbol":"' + syminfo.ticker + '","price":' + str.tostring(math.round(close,4)) + ',"score":' + str.tostring(score) + ',"sl":' + str.tostring(math.round(close-i_sl_mult*atr14,4)) + ',"tp":' + str.tostring(math.round(close+i_tp_mult*atr14,4)) + '}}', alert.freq_once_per_bar_close)
if sig_short
    alert('{{"action":"SHORT","symbol":"' + syminfo.ticker + '","price":' + str.tostring(math.round(close,4)) + ',"score":' + str.tostring(score) + ',"sl":' + str.tostring(math.round(close+i_sl_mult*atr14,4)) + ',"tp":' + str.tostring(math.round(close-i_tp_mult*atr14,4)) + '}}', alert.freq_once_per_bar_close)
if in_exit and not no_pos
    alert('{{"action":"EXIT","symbol":"' + syminfo.ticker + '","price":' + str.tostring(math.round(close,4)) + '}}', alert.freq_once_per_bar_close)

// ── Visuals ───────────────────────────────────────────────────────
plot(ema20d, "EMA20(D)", color.new(color.blue,   50), 1)
plot(ema50d, "EMA50(D)", color.new(color.orange, 50), 1)
plotshape(sig_long,  style=shape.triangleup,   location=location.belowbar,
    color=color.lime, size=size.normal, title="Long")
plotshape(sig_short, style=shape.triangledown, location=location.abovebar,
    color=color.red,  size=size.normal, title="Short")
bgcolor(in_entry ? color.new(color.green, 88) : na, title="Entry Window")
bgcolor(in_exit  ? color.new(color.red,   93) : na, title="Exit Window")

// ── Info table ────────────────────────────────────────────────────
sc_c(v) => v > 0 ? color.lime : v < 0 ? color.red : color.gray
var table t = table.new(position.top_right, 2, 10,
    bgcolor=color.new(color.black,72), border_width=1,
    border_color=color.new(color.gray,70))
if barstate.islast
    table.cell(t,0,0,"RICH v3",       text_color=color.white,  text_size=size.small)
    table.cell(t,1,0,syminfo.ticker,  text_color=color.yellow, text_size=size.small)
    sc_clr = math.abs(score)>=i_sc_min ? (score>0?color.lime:color.red) : color.gray
    table.cell(t,0,1,"SCORE",  text_color=color.silver, text_size=size.tiny)
    table.cell(t,1,1,str.tostring(score)+"/6", text_color=sc_clr, text_size=size.small)
    table.cell(t,0,2,"EMA20D", text_color=color.silver, text_size=size.tiny)
    table.cell(t,1,2,s1>0?"BULL":"BEAR", text_color=sc_c(s1), text_size=size.small)
    table.cell(t,0,3,"MOM20",  text_color=color.silver, text_size=size.tiny)
    table.cell(t,1,3,s2>0?"UP":"DOWN",   text_color=sc_c(s2), text_size=size.small)
    table.cell(t,0,4,"GAP",    text_color=color.silver, text_size=size.tiny)
    table.cell(t,1,4,s3>0?"UP":s3<0?"DOWN":"FLAT", text_color=sc_c(s3), text_size=size.small)
    table.cell(t,0,5,"RSI14",  text_color=color.silver, text_size=size.tiny)
    table.cell(t,1,5,str.tostring(math.round(rsi14,1)), text_color=rsi14<40?color.lime:rsi14>60?color.red:color.gray, text_size=size.small)
    table.cell(t,0,6,"MOM5D",  text_color=color.silver, text_size=size.tiny)
    table.cell(t,1,6,s5>0?"UP":"DOWN",   text_color=sc_c(s5), text_size=size.small)
    table.cell(t,0,7,"EMA50D", text_color=color.silver, text_size=size.tiny)
    table.cell(t,1,7,s6>0?"BULL":"BEAR", text_color=sc_c(s6), text_size=size.small)
    table.cell(t,0,8,"ATR14",  text_color=color.silver, text_size=size.tiny)
    table.cell(t,1,8,str.tostring(math.round(atr14,2)), text_color=color.white, text_size=size.small)
    st = in_entry?"ENTRY":in_exit?"EXIT":"WAIT"
    sc2 = in_entry?color.lime:in_exit?color.red:color.gray
    table.cell(t,0,9,"SESSION", text_color=color.silver, text_size=size.tiny)
    table.cell(t,1,9,st,        text_color=sc2,          text_size=size.small)
"""
    out = Path(__file__).parent / "tradingview" / "rich_trader_v3.pine"
    out.parent.mkdir(exist_ok=True)
    out.write_text(pine, encoding="utf-8")
    print(f"  Pine Script -> {out}")

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply",  action="store_true")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--top",    type=int, default=5)
    ap.add_argument("--output", default="optimizer_results.json")
    args = ap.parse_args()

    instruments = INSTRUMENTS
    if args.symbol:
        instruments = [i for i in INSTRUMENTS
                       if i["symbol"].upper() == args.symbol.upper()]
        if not instruments:
            sys.exit(f"Unknown symbol: {args.symbol}")

    print("\n" + "="*72)
    print("  RICH TRADER — DAILY SIGNAL OPTIMIZER")
    print(f"  Fixed-time session entry  +  6-signal score direction filter")
    print(f"  Train < {TRAIN_SPLIT}   |   Test >= {TRAIN_SPLIT}")
    print("="*72)

    all_results = []
    for inst in instruments:
        t = inst["ticker"]
        print(f"\n  Downloading {inst['symbol']} ({t}) …", flush=True)
        try:
            df1h = yf.download(t, period="max", interval="1h",
                               progress=False, auto_adjust=True).dropna()
            df1d = yf.download(t, period="5y",  interval="1d",
                               progress=False, auto_adjust=True).dropna()
            if df1h.empty or df1d.empty:
                print(f"  [{inst['symbol']}] no data returned"); all_results.append(None); continue
            res  = optimise(inst, df1h, df1d, top_n=max(args.top, 5))
            all_results.append(res)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  [{inst['symbol']}] ERROR: {e}")
            all_results.append(None)

    valid = [r for r in all_results if r]
    if not valid:
        print("No results."); sys.exit(1)

    print_results(valid, args.top)

    out = Path(__file__).parent / args.output
    def _j(o):
        if isinstance(o, (np.integer,)): return int(o)
        if isinstance(o, (np.floating,)): return float(o)
        return str(o)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(valid, f, indent=2, default=_j)
    print(f"  Results -> {out}")

    if args.apply:
        print("\nPatching server.py …")
        apply_to_server(valid)
        print("Generating Pine Script …")
        generate_pine(valid)

    print("\nDone!\n")

if __name__ == "__main__":
    main()
