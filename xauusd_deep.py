#!/usr/bin/env python3
"""
XAUUSD Deep Optimizer
=====================
专门针对黄金，系统搜索最优参数组合：
  - 黄金专属宏观因子（DXY, VIX, 美债, 油, 白银, 矿股）
  - 网格搜索 TP / SL / 置信度阈值
  - Walk-forward 验证防过拟合
  - 特征重要性分析
  - 两个时间段对比（伦敦开盘 vs 纽约开盘）

运行: py -3.10 xauusd_deep.py
"""

import sys, warnings, datetime, itertools
import numpy as np
import pandas as pd
import yfinance as yf
import lightgbm as lgb

warnings.filterwarnings("ignore")
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8","utf8"):
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass

# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════

GOLD_TICKER   = "GC=F"
WF_START      = "2024-01-01"    # walk-forward test start
TRAIN_MONTHS  = 12              # use 12 months to train
RETRAIN_DAYS  = 30              # retrain every 30 days

# Sessions to test
SESSIONS = {
    "London": {"entry_hour": 7,  "exit_hour": 19},
    "NY":     {"entry_hour": 13, "exit_hour": 23},
}

# Grid search parameters
TP_GRID   = [0.75, 1.0, 1.25, 1.5, 2.0, 2.5]
SL_GRID   = [0.5, 0.75, 1.0, 1.25, 1.5]
CONF_GRID = [0.52, 0.54, 0.56, 0.58, 0.60]

# Anti-martingale
WIN_MULT     = 1.5
MAX_SIZE_MULT = 3.0
BASE_USD     = 1000

# Gold-specific macro tickers
MACRO_GOLD = {
    "DXY":  "DX-Y.NYB",   # Dollar index (negative correlation)
    "VIX":  "^VIX",       # Fear index (positive correlation)
    "TNX":  "^TNX",       # 10Y yield (negative correlation)
    "OIL":  "CL=F",       # Crude oil (inflation proxy)
    "SILV": "SI=F",       # Silver (Gold/Silver ratio signal)
    "GDX":  "GDX",        # Gold miners ETF (leading indicator)
    "SP5":  "^GSPC",      # S&P 500 (risk sentiment)
    "TLT":  "TLT",        # Long bonds (flight-to-safety)
}

# ═══════════════════════════════════════════════════════════════════
#  INDICATORS
# ═══════════════════════════════════════════════════════════════════

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(close, n=14):
    delta = close.diff()
    up    = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    down  = (-delta).clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    rs    = up / down.replace(0, 1e-10)
    return 100 - 100 / (1 + rs)

def atr_wilder(high, low, close, n=14):
    tr = pd.concat([high-low,
                    (high-close.shift()).abs(),
                    (low -close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

# ═══════════════════════════════════════════════════════════════════
#  DATA
# ═══════════════════════════════════════════════════════════════════

def download(ticker, interval="1d", period="10y"):
    df = yf.download(ticker, period=period, interval=interval,
                     progress=False, auto_adjust=True).dropna()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def build_macro_features(macro_dict: dict) -> pd.DataFrame:
    """Download macro data, return daily DataFrame (shifted 1 = no lookahead)."""
    frames = {}
    for name, ticker in macro_dict.items():
        try:
            df = download(ticker, "1d", "10y")
            c = df["Close"]
            c.index = pd.to_datetime(c.index).normalize()
            # Absolute level (normalized)
            frames[f"{name}_lvl"]  = (c / c.rolling(60).mean() - 1).shift(1)
            # Returns
            frames[f"{name}_r1"]   = c.pct_change(1).shift(1)
            frames[f"{name}_r3"]   = c.pct_change(3).shift(1)
            frames[f"{name}_r5"]   = c.pct_change(5).shift(1)
            frames[f"{name}_r10"]  = c.pct_change(10).shift(1)
            # Momentum vs EMA
            frames[f"{name}_e20"]  = (c / ema(c,20) - 1).shift(1)
            # Volatility
            frames[f"{name}_vol"]  = c.pct_change(1).rolling(10).std().shift(1)
        except Exception as e:
            print(f"    {ticker} failed: {e}")
    return pd.DataFrame(frames) if frames else pd.DataFrame()


def build_gold_features(df1h, df1d, macro_df, entry_hour, exit_hour, tp, sl):
    """
    Build feature DataFrame for XAUUSD.
    Returns: one row per trading day, with features + targets.
    """
    dc = df1d["Close"]
    dh = df1d["High"]
    dl = df1d["Low"]
    do = df1d["Open"]
    dvol = df1d.get("Volume", pd.Series(0, index=df1d.index))

    d = pd.DataFrame(index=df1d.index)
    d.index = pd.to_datetime(d.index).normalize()

    # ── Price & Trend ─────────────────────────────────────────────
    for p in [5, 10, 20, 50, 100]:
        d[f"e{p}"] = (dc / ema(dc, p) - 1).shift(1)
    d["ema_align"] = sum(np.sign(d[f"e{p}"]) for p in [5,10,20,50])
    d["rsi14"]    = rsi(dc, 14).shift(1)
    d["rsi7"]     = rsi(dc, 7).shift(1)
    d["rsi21"]    = rsi(dc, 21).shift(1)

    # ── Momentum (multiple periods) ───────────────────────────────
    for p in [1,2,3,5,7,10,15,20,30]:
        d[f"m{p}"] = dc.pct_change(p).shift(1)

    # ── Volatility / ATR regime ───────────────────────────────────
    atr14 = atr_wilder(dh, dl, dc, 14)
    atr5  = atr_wilder(dh, dl, dc, 5)
    d["atr14"]   = atr14.shift(1)
    d["atr_r"]   = (atr14 / dc).shift(1)                      # ATR as % of price
    d["atr_reg"] = (atr5 / atr14).shift(1)                    # short vs long vol regime
    d["vol5"]    = dc.pct_change(1).rolling(5).std().shift(1)
    d["vol20"]   = dc.pct_change(1).rolling(20).std().shift(1)
    d["vol_r"]   = d["vol5"] / (d["vol20"] + 1e-10)

    # ── Candle features ───────────────────────────────────────────
    d["gap"]     = ((do - dc.shift(1)) / dc.shift(1))         # today's gap (known at open)
    d["gap_abs"] = d["gap"].abs()
    body = (do - dc).abs()
    rng  = (dh - dl).clip(lower=1e-10)
    d["body_r"]  = (body / rng).shift(1)                      # body/range ratio yesterday

    # ── Bollinger Band position ───────────────────────────────────
    bb_m = dc.rolling(20).mean()
    bb_s = dc.rolling(20).std()
    d["bb_pct"]  = ((dc - (bb_m - 2*bb_s)) / (4*bb_s + 1e-10)).shift(1).clip(0,1)
    d["bb_w"]    = (4*bb_s / (bb_m + 1e-10)).shift(1)         # band width

    # ── Gold/Silver ratio ─────────────────────────────────────────
    # (will be joined from macro_df if available)

    # ── Day of week / calendar ────────────────────────────────────
    d["dow"]     = pd.to_datetime(d.index).dayofweek
    d["week_of_month"] = (pd.to_datetime(d.index).day - 1) // 7

    # ── Previous session result (momentum in results) ──────────────
    # (filled after we compute per-day targets — placeholder for now)
    d["prev_win"] = 0.0

    # ── Join macro features ───────────────────────────────────────
    if macro_df is not None and not macro_df.empty:
        macro_df.index = pd.to_datetime(macro_df.index).normalize()
        d = d.join(macro_df, how="left")

    # ══════════════════════════════════════════════════════════════
    #  Per-day trade simulation
    # ══════════════════════════════════════════════════════════════
    df1h_utc = df1h.copy()
    if df1h_utc.index.tz is not None:
        df1h_utc.index = df1h_utc.index.tz_convert("UTC").tz_localize(None)

    rows = []
    prev_win = 0.0  # track previous session result

    for dt in sorted(set(df1h_utc.index.date)):
        ds = pd.Timestamp(dt)
        if ds not in d.index:
            continue
        row_d = d.loc[ds].copy()
        if row_d.isna().sum() > len(row_d) * 0.3:  # skip if >30% NaN
            continue
        row_d["prev_win"] = prev_win

        atr_d = float(row_d.get("atr14", 0))
        if atr_d <= 0:
            continue

        # Entry bar
        eb = df1h_utc[(df1h_utc.index.date == dt) &
                      (df1h_utc.index.hour == entry_hour)]
        if eb.empty:
            continue
        entry_px = float(eb.iloc[0]["Close"])
        if entry_px <= 0:
            continue

        # Remaining bars
        rem = df1h_utc[(df1h_utc.index > eb.index[-1]) &
                       ((df1h_utc.index.date == dt) |
                        (df1h_utc.index.date == dt + datetime.timedelta(days=1))) &
                       (df1h_utc.index.hour < exit_hour)]

        if rem.empty:
            max_h = min_l = final_c = entry_px
        else:
            max_h   = float(rem["High"].max())
            min_l   = float(rem["Low"].min())
            final_c = float(rem.iloc[-1]["Close"])

        tp_l = entry_px + tp * atr_d;  sl_l = entry_px - sl * atr_d
        tp_s = entry_px - tp * atr_d;  sl_s = entry_px + sl * atr_d

        def pnl_long():
            if min_l <= sl_l and max_h >= tp_l: return (sl_l-entry_px)/entry_px
            if max_h >= tp_l: return (tp_l-entry_px)/entry_px
            if min_l <= sl_l: return (sl_l-entry_px)/entry_px
            return (final_c-entry_px)/entry_px

        def pnl_short():
            if max_h >= sl_s and min_l <= tp_s: return (entry_px-sl_s)/entry_px
            if min_l <= tp_s: return (entry_px-tp_s)/entry_px
            if max_h >= sl_s: return (entry_px-sl_s)/entry_px
            return (entry_px-final_c)/entry_px

        pnl_l = pnl_long()
        pnl_s = pnl_short()

        if pnl_l > 0 and pnl_l >= pnl_s:
            target = 1
        elif pnl_s > 0 and pnl_s > pnl_l:
            target = -1
        else:
            target = 0

        feat = row_d.to_dict()
        feat.update({
            "date": pd.Timestamp(dt), "entry_px": entry_px, "atr_d": atr_d,
            "pnl_l": pnl_l, "pnl_s": pnl_s, "target": target,
            "max_h": max_h, "min_l": min_l, "final_c": final_c,
        })
        rows.append(feat)
        prev_win = 1.0 if (target == 1 and pnl_l > 0) or (target == -1 and pnl_s > 0) else -1.0

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ═══════════════════════════════════════════════════════════════════
#  ML MODEL
# ═══════════════════════════════════════════════════════════════════

def get_feat_cols(df):
    skip = {"date","entry_px","atr_d","pnl_l","pnl_s","target",
            "max_h","min_l","final_c"}
    cols = [c for c in df.columns if c not in skip and df[c].dtype in [np.float64, np.float32, np.int64, np.int32, float, int]]
    return cols


def train(df_tr, feat_cols):
    df_tr = df_tr.dropna(subset=feat_cols)
    X = df_tr[feat_cols].values

    results = {}
    for name, y_vals in [("long", (df_tr["target"]==1).astype(int).values),
                          ("short",(df_tr["target"]==-1).astype(int).values)]:
        m = lgb.LGBMClassifier(
            n_estimators=300, learning_rate=0.03, num_leaves=20,
            min_child_samples=8, subsample=0.8, colsample_bytree=0.7,
            reg_alpha=0.1, reg_lambda=0.1,
            random_state=42, verbose=-1, n_jobs=-1,
        )
        m.fit(X, y_vals)
        results[name] = m
    return results["long"], results["short"]


# ═══════════════════════════════════════════════════════════════════
#  WALK-FORWARD BACKTEST
# ═══════════════════════════════════════════════════════════════════

def walk_forward(df, conf_thresh):
    df = df.sort_values("date").reset_index(drop=True)
    start = pd.Timestamp(WF_START)
    feat_cols = get_feat_cols(df)

    trades = []
    model_l = model_s = None
    last_train = None

    for _, row in df[df["date"] >= start].iterrows():
        dt = row["date"]

        need_train = (last_train is None or
                      (dt - last_train).days >= RETRAIN_DAYS)
        if need_train:
            t_start = dt - pd.DateOffset(months=TRAIN_MONTHS)
            df_tr = df[(df["date"] >= t_start) & (df["date"] < dt)]
            if len(df_tr) >= 40:
                model_l, model_s = train(df_tr, feat_cols)
                last_train = dt

        if model_l is None:
            continue

        x_vals = []
        for f in feat_cols:
            v = row.get(f, np.nan)
            x_vals.append(float(v) if pd.notna(v) else np.nan)
        if any(np.isnan(v) for v in x_vals):
            continue

        x = np.array([x_vals])
        p_l = model_l.predict_proba(x)[0][1]
        p_s = model_s.predict_proba(x)[0][1]

        direction = conf = 0
        if p_l >= conf_thresh and p_l >= p_s:
            direction, conf = 1, p_l
        elif p_s >= conf_thresh and p_s > p_l:
            direction, conf = -1, p_s
        else:
            continue

        pnl = row["pnl_l"] if direction == 1 else row["pnl_s"]
        trades.append({
            "date": dt, "direction": direction, "conf": conf,
            "entry_px": row["entry_px"], "atr_d": row["atr_d"],
            "pnl_pct": pnl, "target": row["target"],
        })

    return pd.DataFrame(trades)


# ═══════════════════════════════════════════════════════════════════
#  METRICS
# ═══════════════════════════════════════════════════════════════════

def metrics(trades_df, use_antimartingale=True):
    if len(trades_df) < 10:
        return None
    df = trades_df.copy()

    if use_antimartingale:
        df["mult"] = 1.0
        mult = 1.0
        for i, row in df.iterrows():
            df.at[i, "mult"] = mult
            mult = min(mult * WIN_MULT, MAX_SIZE_MULT) if row["pnl_pct"] > 0 else 1.0
        df["pnl_usd"] = df["pnl_pct"] * df["mult"] * BASE_USD
    else:
        df["mult"]    = 1.0
        df["pnl_usd"] = df["pnl_pct"] * BASE_USD

    p    = df["pnl_usd"].values
    wins = p[p>0]; loss = p[p<0]
    wr   = len(wins)/len(p)
    gw   = wins.sum() if len(wins) else 0
    gl   = -loss.sum() if len(loss) else 1e-9
    pf   = min(gw/gl, 99) if gl>0 else 99

    pct  = (df["pnl_pct"] * df["mult"]).values
    mu   = pct.mean(); sg = pct.std()
    sh   = mu/sg*np.sqrt(252) if sg>0 else 0

    eq   = np.cumprod(1+pct)
    pk   = np.maximum.accumulate(eq)
    dd   = ((pk-eq)/pk).max()*100

    months = max(1, (df["date"].max()-df["date"].min()).days/30)
    acc    = (df["direction"]==df["target"]).mean()*100

    return {
        "n": len(df), "n_mo": round(len(df)/months, 1),
        "wr": round(wr*100, 1), "pf": round(pf, 3),
        "sharpe": round(sh, 3), "dd": round(dd, 2),
        "acc": round(acc, 1),
        "net_usd": round(p.sum(), 0),
        "ann_pct": round((eq[-1]-1)/months*12*100, 1),
    }


# ═══════════════════════════════════════════════════════════════════
#  FEATURE IMPORTANCE
# ═══════════════════════════════════════════════════════════════════

def show_importance(df, feat_cols, top_n=15):
    df_tr = df.dropna(subset=feat_cols)
    X = df_tr[feat_cols].values
    y = (df_tr["target"]==1).astype(int).values
    m = lgb.LGBMClassifier(n_estimators=300, num_leaves=20,
                            random_state=42, verbose=-1, n_jobs=-1)
    m.fit(X, y)
    imp = sorted(zip(feat_cols, m.feature_importances_),
                 key=lambda x: x[1], reverse=True)
    print(f"\n  Top {top_n} features by importance:")
    for name, score in imp[:top_n]:
        bar = "█" * int(score/max(v for _,v in imp)*20)
        print(f"    {name:<22} {score:>5.0f}  {bar}")


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    print("\n" + "="*65)
    print("  XAUUSD DEEP OPTIMIZER")
    print(f"  Walk-forward from {WF_START}  |  Train window: {TRAIN_MONTHS}mo")
    print("="*65)

    # ── Download data ──────────────────────────────────────────────
    print("\n  [1] Downloading gold data ...", flush=True)
    df1h = download(GOLD_TICKER, "1h", "max")
    df1d = download(GOLD_TICKER, "1d", "10y")
    print(f"      1h: {len(df1h)} bars  |  Daily: {len(df1d)} bars")

    print("\n  [2] Downloading macro factors ...", flush=True)
    macro_df = build_macro_features(MACRO_GOLD)
    print(f"      Macro features: {len(macro_df.columns)} columns")

    # ── Grid search over TP / SL / Session / Confidence ───────────
    print("\n  [3] Grid search  "
          f"({len(TP_GRID)}×{len(SL_GRID)}×{len(SESSIONS)}×{len(CONF_GRID)} combos) ...",
          flush=True)

    best = None
    all_results = []
    combo_total = len(TP_GRID)*len(SL_GRID)*len(SESSIONS)*len(CONF_GRID)
    done = 0

    # Cache feature DataFrames per (session, tp, sl) to avoid rebuilding
    feat_cache = {}

    for session_name, sess_cfg in SESSIONS.items():
        for tp in TP_GRID:
            for sl in SL_GRID:
                key = (session_name, tp, sl)
                if key not in feat_cache:
                    df_feat = build_gold_features(
                        df1h, df1d, macro_df,
                        sess_cfg["entry_hour"], sess_cfg["exit_hour"],
                        tp, sl)
                    feat_cache[key] = df_feat

                df_feat = feat_cache[key]
                if df_feat.empty:
                    done += len(CONF_GRID); continue

                for conf in CONF_GRID:
                    trades = walk_forward(df_feat, conf)
                    m = metrics(trades)
                    done += 1

                    if done % 20 == 0:
                        print(f"    {done}/{combo_total} ...", flush=True)

                    if m is None or m["n"] < 50:
                        continue

                    score = m["pf"]*0.40 + m["sharpe"]/2*0.25 + \
                            m["wr"]/100*0.20 - m["dd"]/100*0.15
                    row = {
                        "session": session_name, "tp": tp, "sl": sl,
                        "conf": conf, **m, "score": round(score, 4)
                    }
                    all_results.append(row)
                    if best is None or score > best["score"]:
                        best = row

    # ── Show top 20 results ────────────────────────────────────────
    if not all_results:
        print("  No valid results."); return

    all_results.sort(key=lambda x: x["score"], reverse=True)
    top = all_results[:20]

    print("\n" + "="*90)
    print("  GRID SEARCH RESULTS (top 20)")
    print("="*90)
    print(f"  {'#':>2}  {'Sess':>7}  {'TP':>5}  {'SL':>5}  {'Conf':>5}  "
          f"{'PF':>6}  {'WR%':>5}  {'Shp':>5}  {'DD%':>5}  "
          f"{'n/mo':>5}  {'Net$':>7}  {'Ann%':>6}")
    print("  " + "-"*85)
    for i, r in enumerate(top, 1):
        mk = " ◄" if i == 1 else ""
        print(f"  {i:>2}  {r['session']:>7}  {r['tp']:.2f}x  {r['sl']:.2f}x  "
              f"{r['conf']:.2f}  {r['pf']:>6.3f}  {r['wr']:>5.1f}  "
              f"{r['sharpe']:>5.2f}  {r['dd']:>5.1f}  "
              f"{r['n_mo']:>5.1f}  ${r['net_usd']:>6.0f}  "
              f"{r['ann_pct']:>5.1f}%{mk}")

    # ── Best result details ────────────────────────────────────────
    b = all_results[0]
    print(f"\n{'='*65}")
    print(f"  BEST COMBO")
    print(f"{'='*65}")
    print(f"  Session    : {b['session']}")
    print(f"  TP × ATR   : {b['tp']}")
    print(f"  SL × ATR   : {b['sl']}")
    print(f"  Conf thresh: {b['conf']}")
    print(f"  ─────────────────────────")
    print(f"  Profit Factor : {b['pf']:.3f}")
    print(f"  Win Rate      : {b['wr']}%")
    print(f"  Direction Acc : {b['acc']}%")
    print(f"  Sharpe Ratio  : {b['sharpe']:.2f}")
    print(f"  Max Drawdown  : {b['dd']:.1f}%")
    print(f"  Trades/month  : {b['n_mo']}")
    print(f"  Net P&L (USD) : ${b['net_usd']:+.0f} (${BASE_USD}/trade base)")
    print(f"  Ann% on pos   : {b['ann_pct']:+.1f}%")

    # ── Monthly breakdown of best ──────────────────────────────────
    key = (b["session"], b["tp"], b["sl"])
    df_best = feat_cache[key]
    trades_best = walk_forward(df_best, b["conf"])
    trades_best = trades_best.copy()
    mult = 1.0
    usd_list = []
    for _, row in trades_best.iterrows():
        usd_list.append(row["pnl_pct"] * mult * BASE_USD)
        mult = min(mult*WIN_MULT, MAX_SIZE_MULT) if row["pnl_pct"]>0 else 1.0
    trades_best["pnl_usd"] = usd_list
    trades_best["ym"] = trades_best["date"].dt.to_period("M")
    monthly = trades_best.groupby("ym")["pnl_usd"].sum()

    print(f"\n  Monthly P&L (anti-martingale x{WIN_MULT}, ${BASE_USD}/base):")
    total = 0
    for ym, val in monthly.items():
        total += val
        bar = "█" * min(int(abs(val)/40), 20)
        sign = "+" if val >= 0 else "-"
        cum = f"(cum ${total:+.0f})"
        print(f"    {ym}  {sign}${abs(val):6.0f}  {bar:<20}  {cum}")

    # ── Feature importance ─────────────────────────────────────────
    feat_cols = get_feat_cols(df_best)
    show_importance(df_best, feat_cols, top_n=15)

    print(f"\n  Done.\n")


if __name__ == "__main__":
    main()
