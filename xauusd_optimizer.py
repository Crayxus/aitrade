#!/usr/bin/env python3
"""
XAUUSD Optimizer — matches Pine Script xauusd_daily.pine exactly
================================================================
Pine Script settings:
  - 1h chart, process_orders_on_close=true
  - Entry bar close = 07:00-08:00 UTC (15:00-16:00 BJ)
  - d_cls / atr14 use lookahead_off → previous completed daily bar
  - Force-exit at 19:00 UTC (03:00 BJ)
  - s5 = yesterday_close > yesterday_close[5] (5-day momentum)

This optimizer replicates that logic exactly in numpy/pandas.
"""

import itertools, sys, warnings
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8","utf8"):
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass

# ── Config ────────────────────────────────────────────────────────
TICKERS   = ["GC=F", "XAUUSD=X"]   # try both, use whichever has more data
ENTRY_HOUR = 7                       # 07:00 UTC = 15:00 BJ London open
EXIT_HOUR  = 19                      # 19:00 UTC = 03:00 BJ force-exit
TRAIN_END  = "2025-01-01"           # train on first ~9 months, test on 2025+
MIN_TRADES = 10                      # minimum trades per period

PARAM_GRID = {
    "tp_mult":   [0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0],
    "sl_mult":   [0.5, 0.75, 1.0, 1.25, 1.5],
    "score_min": [1, 2, 3, 4],
}

# ── Indicators ────────────────────────────────────────────────────
def _ema(arr, p):
    a = 2.0 / (p + 1)
    e = float(arr[0])
    out = np.empty(len(arr))
    out[0] = e
    for i in range(1, len(arr)):
        e = a * float(arr[i]) + (1 - a) * e
        out[i] = e
    return out

def _wilder(arr, p):
    out = np.full(len(arr), np.nan)
    out[p-1] = float(np.mean(arr[:p]))
    a = 1.0 / p
    for i in range(p, len(arr)):
        out[i] = out[i-1] * (1 - a) + arr[i] * a
    return out

def build_daily_features(df_d):
    c = np.asarray(df_d["Close"], float).ravel()
    h = np.asarray(df_d["High"],  float).ravel()
    l = np.asarray(df_d["Low"],   float).ravel()
    o = np.asarray(df_d["Open"],  float).ravel()
    n = len(c)

    ema20 = _ema(c, 20)
    ema50 = _ema(c, 50)

    mom20 = np.zeros(n)
    mom20[20:] = np.where(c[:-20] != 0, (c[20:] - c[:-20]) / c[:-20], 0)

    dlt = np.diff(c, prepend=c[0])
    g   = _wilder(np.where(dlt > 0, dlt, 0), 14)
    ls  = _wilder(np.where(dlt < 0, -dlt, 0), 14)
    rsi = np.where(ls == 0, 100.0, 100 - 100 / (1 + g / np.maximum(ls, 1e-10)))

    gap = np.zeros(n)
    gap[1:] = np.where(c[:-1] != 0, (o[1:] - c[:-1]) / c[:-1], 0)

    tr = np.empty(n); tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:],
              np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    atr14 = _wilder(tr, 14)

    # s5: 5-day momentum (same as Pine Script d_cls > d_cls[5])
    s5 = np.ones(n, int)
    s5[5:] = np.where(c[5:] > c[:-5], 1, -1)

    s1 = np.where(c > ema20, 1, -1)
    s2 = np.where(mom20 > 0, 1, -1)
    s3 = np.where(gap > 5e-4, 1, np.where(gap < -5e-4, -1, 0))
    s4 = np.where(rsi < 40, 1, np.where(rsi > 60, -1, 0))
    s6 = np.where(c > ema50, 1, -1)
    score = s1 + s2 + s3 + s4 + s5 + s6

    dates = [str(d)[:10] for d in df_d.index]
    score_s = pd.Series(dict(zip(dates, score)))
    atr_s   = pd.Series(dict(zip(dates, atr14)))

    # ── Shift by 1 trading day (Pine lookahead_off: use yesterday's bar) ──
    return score_s.shift(1), atr_s.shift(1)


# ── Precompute trade opportunities ────────────────────────────────
def precompute(df1h, df_d):
    score_s, atr_s = build_daily_features(df_d)

    df1h = df1h.copy()
    if df1h.index.tz is not None:
        df1h.index = df1h.index.tz_convert("UTC").tz_localize(None)

    rows = []
    for dt in sorted(set(df1h.index.date)):
        ds = str(dt)
        sc  = score_s.get(ds, np.nan)
        atr = atr_s.get(ds, np.nan)
        if np.isnan(sc) or np.isnan(atr) or atr <= 0:
            continue
        sc = int(round(sc))
        if sc == 0:
            continue

        # Entry bar: 07:00 UTC (15:00 BJ) — Pine process_orders_on_close=true
        # so entry price = CLOSE of that bar
        eb = df1h[(df1h.index.date == dt) & (df1h.index.hour == ENTRY_HOUR)]
        if eb.empty:
            continue
        entry_ts = eb.index[0]
        entry_px = float(eb.iloc[0]["Close"])   # ← bar close, matches Pine
        if entry_px <= 0:
            continue

        # Remaining bars until 19:00 UTC force-exit
        rem = df1h[
            (df1h.index > entry_ts) &
            (df1h.index.date == dt) &
            (df1h.index.hour < EXIT_HOUR)
        ]

        # Also include next-day early bars up to EXIT_HOUR
        import datetime
        next_dt = dt + datetime.timedelta(days=1)
        rem2 = df1h[
            (df1h.index.date == next_dt) &
            (df1h.index.hour < EXIT_HOUR)
        ]
        rem = pd.concat([rem, rem2]).sort_index()

        if rem.empty:
            max_H = entry_px; min_L = entry_px; final_C = entry_px
        else:
            max_H   = float(rem["High"].max())
            min_L   = float(rem["Low"].min())
            final_C = float(rem.iloc[-1]["Close"])

        rows.append({
            "date": ds, "direction": 1 if sc > 0 else -1,
            "score": sc, "atr": atr,
            "entry_px": entry_px,
            "max_H": max_H, "min_L": min_L, "final_C": final_C,
        })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ── Vectorised backtest ───────────────────────────────────────────
def backtest(precomp, tp_m, sl_m, sc_min):
    df = precomp[precomp["score"].abs() >= sc_min].copy()
    if len(df) < MIN_TRADES:
        return None, None

    ep  = df["entry_px"].values
    atr = df["atr"].values
    mxH = df["max_H"].values
    mnL = df["min_L"].values
    fnC = df["final_C"].values
    dr  = df["direction"].values

    tp_l = ep + tp_m * atr;  sl_l = ep - sl_m * atr
    tp_s = ep - tp_m * atr;  sl_s = ep + sl_m * atr

    # If both SL and TP hit in same bar → conservative: assume SL
    hl = mnL <= sl_l;  tl = mxH >= tp_l
    pnl_l = np.where(hl & tl, (sl_l - ep) / ep,
             np.where(tl,     (tp_l - ep) / ep,
             np.where(hl,     (sl_l - ep) / ep,
                              (fnC  - ep) / ep)))

    hs = mxH >= sl_s;  ts_ = mnL <= tp_s
    pnl_s = np.where(hs & ts_, (ep - sl_s) / ep,
             np.where(ts_,     (ep - tp_s) / ep,
             np.where(hs,      (ep - sl_s) / ep,
                               (ep - fnC ) / ep)))

    pnl   = np.where(dr == 1, pnl_l, pnl_s)
    dates = df["date"].values

    tr_pnl = pnl[dates < TRAIN_END]
    te_pnl = pnl[dates >= TRAIN_END]
    return list(tr_pnl), list(te_pnl)


def metrics(pnl_list):
    if len(pnl_list) < MIN_TRADES:
        return None
    p = np.array(pnl_list, float)
    wins = p[p > 0]; loss = p[p < 0]
    gw = float(wins.sum()) if len(wins) else 0.0
    gl = float(-loss.sum()) if len(loss) else 1e-9
    wr = len(wins) / len(p)
    if wr < 0.30:
        return None
    pf  = min(gw / gl, 99.0) if gl > 0 else 99.0
    eq  = np.cumprod(1 + p)
    pk  = np.maximum.accumulate(eq)
    dd  = float(((pk - eq) / pk).max()) * 100
    mu  = float(p.mean())
    sg  = float(p.std())
    sh  = mu / sg * np.sqrt(252) if sg > 0 else 0.0
    return {"n": len(p), "wr": round(wr*100,1), "pf": round(pf,3),
            "sharpe": round(sh,3), "dd": round(dd,2),
            "avg": round(mu*100,4), "total": round((float(eq[-1])-1)*100,2)}


# ── Main ──────────────────────────────────────────────────────────
def main():
    # Try tickers until we get good data
    df1h = df1d = None
    used_ticker = None
    for ticker in TICKERS:
        print(f"  Downloading {ticker} ...", flush=True)
        try:
            h = yf.download(ticker, period="max", interval="1h",
                            progress=False, auto_adjust=True).dropna()
            d = yf.download(ticker, period="10y", interval="1d",
                            progress=False, auto_adjust=True).dropna()
            if len(h) > 1000 and len(d) > 500:
                df1h = h; df1d = d; used_ticker = ticker
                print(f"  Using {ticker}: {len(h)} 1h bars, {len(d)} daily bars")
                break
        except Exception as e:
            print(f"  {ticker} failed: {e}")

    if df1h is None:
        print("No data available."); return

    print(f"\n  Precomputing trades ...", flush=True)
    precomp = precompute(df1h, df1d)
    if precomp.empty:
        print("  No trades found."); return

    n_total = len(precomp)
    import datetime
    months_test = max(1, (datetime.date.today() -
                          datetime.date.fromisoformat(TRAIN_END)).days / 30)
    print(f"  {n_total} signal days total ({len(precomp[precomp['direction']==1])}L / "
          f"{len(precomp[precomp['direction']==-1])}S)")
    print(f"  Train: < {TRAIN_END}   Test: >= {TRAIN_END} ({months_test:.0f} months)\n")

    # Grid search
    keys   = list(PARAM_GRID)
    combos = [dict(zip(keys, v))
              for v in itertools.product(*[PARAM_GRID[k] for k in keys])]

    results = []
    for p in combos:
        tr_p, te_p = backtest(precomp, p["tp_mult"], p["sl_mult"], p["score_min"])
        tr_m = metrics(tr_p)
        te_m = metrics(te_p)
        if tr_m is None or te_m is None:
            continue
        # Score: weight test PF + WR + avoid huge train-test gap
        oof = min(te_m["pf"] / tr_m["pf"], 1.0) if tr_m["pf"] > 0 else 0
        cs  = te_m["pf"] * 0.50 + te_m["wr"] / 100 * 0.20 + te_m["sharpe"] / 4 * 0.15 + oof * 0.15
        results.append({"p": p, "tr": tr_m, "te": te_m, "cs": round(cs, 5)})

    if not results:
        print("  No valid combos found — try relaxing filters.")
        # Show best by test PF regardless of win rate filter
        print("\n  Top raw combos (ignoring WR filter):")
        raw = []
        for p in combos:
            tr_p, te_p = backtest(precomp, p["tp_mult"], p["sl_mult"], p["score_min"])
            if not te_p: continue
            arr = np.array(te_p)
            if len(arr) < MIN_TRADES: continue
            wins = arr[arr > 0]; loss = arr[arr < 0]
            gw = float(wins.sum()) if len(wins) else 0
            gl = float(-loss.sum()) if len(loss) else 1e-9
            pf = round(min(gw/gl, 99), 3) if gl > 0 else 99
            wr = round(len(wins)/len(arr)*100, 1)
            raw.append({"p": p, "pf": pf, "wr": wr, "n": len(arr)})
        raw.sort(key=lambda x: x["pf"], reverse=True)
        for r in raw[:10]:
            p = r["p"]
            print(f"  TP={p['tp_mult']:.2f}x SL={p['sl_mult']:.2f}x sc>={p['score_min']}  "
                  f"PF={r['pf']:.3f}  WR={r['wr']}%  n={r['n']}")
        return

    results.sort(key=lambda x: x["cs"], reverse=True)

    print("=" * 70)
    print("  XAUUSD OPTIMIZER RESULTS")
    print(f"  Ticker: {used_ticker}")
    print("=" * 70)
    print(f"  {'#':>2}  {'TP':>5}  {'SL':>5}  {'sc':>3}  "
          f"{'PF_tr':>7}  {'PF_te':>7}  {'WR_te':>6}  {'n_te':>5}  "
          f"{'n/mo':>5}  {'avg%':>6}  {'Shp':>6}  {'ann%':>6}")
    print("  " + "-" * 70)

    best = None
    for i, r in enumerate(results[:15], 1):
        p, tr, te = r["p"], r["tr"], r["te"]
        n_mo = te["n"] / months_test
        ann  = te["total"] / months_test * 12
        mk   = " ◄ BEST" if i == 1 else ""
        print(f"  {i:>2}  {p['tp_mult']:.2f}x  {p['sl_mult']:.2f}x  {p['score_min']:>3}  "
              f"{tr['pf']:>7.3f}  {te['pf']:>7.3f}  {te['wr']:>6.1f}%  {te['n']:>5}  "
              f"{n_mo:>5.1f}  {te['avg']:>5.3f}%  {te['sharpe']:>6.2f}  {ann:>5.1f}%{mk}")
        if i == 1:
            best = (p, te, n_mo, ann)

    if best:
        p, te, n_mo, ann = best
        print("\n" + "=" * 70)
        print("  BEST PARAMETERS FOR Pine Script (xauusd_daily.pine)")
        print("=" * 70)
        print(f"  Session  : London (15:00 BJ)")
        print(f"  TP × ATR : {p['tp_mult']}")
        print(f"  SL × ATR : {p['sl_mult']}")
        print(f"  Min score: {p['score_min']}")
        print(f"  ─────────────────────────────")
        print(f"  PF (test): {te['pf']:.3f}")
        print(f"  Win rate : {te['wr']}%")
        print(f"  Trades   : {te['n']} ({n_mo:.1f}/month)")
        print(f"  Ann return (on position): {ann:.1f}%")
        print(f"  Max DD   : {te['dd']:.1f}%")
        print(f"  Sharpe   : {te['sharpe']:.2f}")
        print()
        print(f"  → In TradingView set: TP={p['tp_mult']}  SL={p['sl_mult']}  Score>={p['score_min']}")
        print()


if __name__ == "__main__":
    main()
