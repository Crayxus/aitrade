#!/usr/bin/env python3
"""
Rich Trader — ML Strategy Engine
=================================
纯策略，不依赖任何平台。

思路：
  1. 下载 XAUUSD / NAS100 / BTCUSD 历史1小时数据
  2. 特征工程（全部无lookahead，模拟真实入场条件）
  3. LightGBM 分类器预测当日方向（胜率 > 55% 才入场）
  4. Walk-forward 滚动训练（每月重训，防止过拟合）
  5. ATR 动态止损止盈
  6. 反马丁仓位管理（连赢加仓，亏损复位）
  7. 输出详细绩效报告

运行: py -3.10 strategy_engine.py
"""

import sys, warnings, datetime
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8","utf8"):
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass

try:
    import lightgbm as lgb
    HAS_LGB = True
except ImportError:
    HAS_LGB = False
    print("  [!] lightgbm not found — pip install lightgbm")

# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════

INSTRUMENTS = {
    "XAUUSD": {"ticker": "GC=F",    "entry_hour": 7,  "exit_hour": 19,
               "macro": ["DX-Y.NYB", "^VIX", "^TNX", "^GSPC"]},
    "NAS100": {"ticker": "NQ=F",    "entry_hour": 13, "exit_hour": 19,
               "macro": ["^VIX", "^TNX", "DX-Y.NYB"]},
    "BTCUSD": {"ticker": "BTC-USD", "entry_hour": 13, "exit_hour": 23,
               "macro": ["^VIX", "DX-Y.NYB", "ETH-USD"]},
}

TRAIN_MONTHS   = 9      # walk-forward: retrain every month with this much history
RETRAIN_EVERY  = 1      # months between retrains
CONF_THRESH    = 0.54   # minimum model confidence to trade (tune: 0.52-0.60)
TP_ATR         = 1.5    # take-profit × daily ATR
SL_ATR         = 1.0    # stop-loss   × daily ATR

# Anti-martingale: multiply size by this after a win (max cap = MAX_SIZE_MULT)
WIN_MULT       = 1.5
MAX_SIZE_MULT  = 3.0
BASE_SIZE_USD  = 1000   # base position value in USD

# Walk-forward test starts after enough training data
WF_START       = "2024-06-01"

# ═══════════════════════════════════════════════════════════════════
#  INDICATORS (no ta-lib dependency)
# ═══════════════════════════════════════════════════════════════════

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def wilder_rsi(close, n=14):
    delta = close.diff()
    up   = delta.clip(lower=0)
    down = (-delta).clip(lower=0)
    rs   = up.ewm(alpha=1/n, adjust=False).mean() / \
           down.ewm(alpha=1/n, adjust=False).mean().replace(0, 1e-10)
    return 100 - 100 / (1 + rs)

def atr(high, low, close, n=14):
    tr = pd.concat([high-low,
                    (high-close.shift()).abs(),
                    (low -close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def bollinger(close, n=20, k=2):
    m  = close.rolling(n).mean()
    s  = close.rolling(n).std()
    return (m+k*s), m, (m-k*s)

# ═══════════════════════════════════════════════════════════════════
#  FEATURE ENGINEERING
#  All features computed from data available at entry_hour bar close
#  (= end of first session bar — no lookahead)
# ═══════════════════════════════════════════════════════════════════

def load_macro(tickers: list) -> pd.DataFrame:
    """Download daily macro series, return aligned DataFrame (no lookahead: shift 1)."""
    frames = {}
    for t in tickers:
        try:
            d = yf.download(t, period="10y", interval="1d",
                            progress=False, auto_adjust=True).dropna()
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            close = d["Close"]
            close.index = pd.to_datetime(close.index).normalize()
            tag = t.replace("^","").replace("-","").replace(".","")
            frames[f"m_{tag}_ret1"]  = close.pct_change(1).shift(1)   # yesterday's 1d return
            frames[f"m_{tag}_ret3"]  = close.pct_change(3).shift(1)   # 3d return
            frames[f"m_{tag}_ret5"]  = close.pct_change(5).shift(1)
            frames[f"m_{tag}_vol5"]  = close.pct_change(1).rolling(5).std().shift(1)
            frames[f"m_{tag}_lvl"]   = (close / close.rolling(20).mean() - 1).shift(1)
        except Exception as e:
            print(f"    macro {t} failed: {e}")
    return pd.DataFrame(frames) if frames else pd.DataFrame()


def build_features(df1h: pd.DataFrame, df1d: pd.DataFrame,
                   entry_hour: int, exit_hour: int,
                   macro_tickers: list = None) -> pd.DataFrame:
    """
    Returns one row per trading day with:
      - features  (X columns)
      - target    (y=1 if entry→exit P&L > 0 else 0)
      - entry_px, sl, tp, atr_d, direction (for backtest)
    """
    # ── Daily indicators (shift 1 = lookahead_off, uses yesterday) ──
    dc = df1d["Close"].rename("dc")
    dh = df1d["High"]
    dl = df1d["Low"]
    do = df1d["Open"]

    d = pd.DataFrame(index=df1d.index)
    d["dc"]    = dc
    d["atr_d"] = atr(dh, dl, dc, 14).shift(1)
    d["ema5"]  = ema(dc, 5).shift(1)
    d["ema10"] = ema(dc, 10).shift(1)
    d["ema20"] = ema(dc, 20).shift(1)
    d["ema50"] = ema(dc, 50).shift(1)
    d["rsi14"] = wilder_rsi(dc, 14).shift(1)
    bb_u, bb_m, bb_l = bollinger(dc, 20, 2)
    d["bb_pct"] = ((dc.shift(1) - bb_l.shift(1)) /
                   (bb_u.shift(1) - bb_l.shift(1) + 1e-10)).clip(0, 1)
    d["gap"]    = ((do - dc.shift(1)) / dc.shift(1)).shift(0)  # today's gap known at open
    d["mom1"]   = dc.pct_change(1).shift(1)
    d["mom3"]   = dc.pct_change(3).shift(1)
    d["mom5"]   = dc.pct_change(5).shift(1)
    d["mom10"]  = dc.pct_change(10).shift(1)
    d["mom20"]  = dc.pct_change(20).shift(1)
    d["vol5"]   = dc.pct_change(1).rolling(5).std().shift(1)
    d["vol20"]  = dc.pct_change(1).rolling(20).std().shift(1)
    d["vol_r"]  = d["vol5"] / (d["vol20"] + 1e-10)  # volatility regime
    d["atr_r"]  = d["atr_d"] / (dc.shift(1) + 1e-10)  # ATR as % of price
    # Price relative to EMAs
    d["c_e5"]   = (dc.shift(1) / (d["ema5"]  + 1e-10) - 1)
    d["c_e10"]  = (dc.shift(1) / (d["ema10"] + 1e-10) - 1)
    d["c_e20"]  = (dc.shift(1) / (d["ema20"] + 1e-10) - 1)
    d["c_e50"]  = (dc.shift(1) / (d["ema50"] + 1e-10) - 1)
    # EMA alignment score
    d["ema_align"] = (np.sign(d["c_e5"]) + np.sign(d["c_e10"]) +
                      np.sign(d["c_e20"]) + np.sign(d["c_e50"]))
    # RSI features
    d["rsi_hi"] = (d["rsi14"] > 60).astype(int)
    d["rsi_lo"] = (d["rsi14"] < 40).astype(int)
    d["dow"]    = pd.to_datetime(df1d.index).dayofweek  # 0=Mon

    d.index = pd.to_datetime(d.index).normalize()

    # ── Macro features ────────────────────────────────────────────
    macro_df = None
    if macro_tickers:
        print(f"    Loading macro: {macro_tickers}", flush=True)
        macro_df = load_macro(macro_tickers)
        macro_df.index = pd.to_datetime(macro_df.index).normalize()

    # ── 1h: entry bar (entry_hour UTC) ──
    df1h_utc = df1h.copy()
    if df1h_utc.index.tz is not None:
        df1h_utc.index = df1h_utc.index.tz_convert("UTC").tz_localize(None)

    rows = []
    for dt in sorted(set(df1h_utc.index.date)):
        ds = pd.Timestamp(dt)

        # Entry bar: close of entry_hour bar
        eb = df1h_utc[(df1h_utc.index.date == dt) &
                      (df1h_utc.index.hour == entry_hour)]
        if eb.empty:
            continue
        entry_px = float(eb.iloc[0]["Close"])
        if entry_px <= 0:
            continue

        # Daily features for this date
        if ds not in d.index:
            continue
        row_d = d.loc[ds]
        if row_d.isna().any():
            continue
        atr_d = float(row_d["atr_d"])
        if atr_d <= 0:
            continue

        # Remaining 1h bars for P&L calculation
        rem = df1h_utc[(df1h_utc.index > eb.index[-1]) &
                       (df1h_utc.index.date == dt) &
                       (df1h_utc.index.hour < exit_hour)]

        if rem.empty:
            final_c = entry_px
            max_h = entry_px; min_l = entry_px
        else:
            final_c = float(rem.iloc[-1]["Close"])
            max_h   = float(rem["High"].max())
            min_l   = float(rem["Low"].min())

        # SL / TP levels
        tp_long = entry_px + TP_ATR * atr_d
        sl_long = entry_px - SL_ATR * atr_d
        tp_shrt = entry_px - TP_ATR * atr_d
        sl_shrt = entry_px + SL_ATR * atr_d

        # Compute actual P&L for both directions (for target labelling)
        def pnl_long():
            if min_l <= sl_long and max_h >= tp_long:
                return (sl_long - entry_px) / entry_px  # SL wins conflict → conservative
            if max_h >= tp_long:  return (tp_long - entry_px) / entry_px
            if min_l <= sl_long:  return (sl_long - entry_px) / entry_px
            return (final_c - entry_px) / entry_px

        def pnl_shrt():
            if max_h >= sl_shrt and min_l <= tp_shrt:
                return (entry_px - sl_shrt) / entry_px
            if min_l <= tp_shrt:  return (entry_px - tp_shrt) / entry_px
            if max_h >= sl_shrt:  return (entry_px - sl_shrt) / entry_px
            return (entry_px - final_c) / entry_px

        pnl_l = pnl_long()
        pnl_s = pnl_shrt()

        # Target: 1=go long, -1=go short, 0=skip
        # We label based on which direction is profitable
        if pnl_l > 0 and pnl_l >= pnl_s:
            target = 1
        elif pnl_s > 0 and pnl_s > pnl_l:
            target = -1
        else:
            target = 0  # both would lose — skip (but we still record it)

        feat = dict(row_d)
        # Attach macro features if available
        if macro_df is not None and not macro_df.empty:
            if ds in macro_df.index:
                mrow = macro_df.loc[ds]
                feat.update(mrow.to_dict())
            else:
                # fill with NaN — will be dropped later
                for col in macro_df.columns:
                    feat[col] = np.nan
        feat.update({
            "date": str(dt), "entry_px": entry_px,
            "atr_d": atr_d, "pnl_l": pnl_l, "pnl_s": pnl_s,
            "target": target,
            "max_h": max_h, "min_l": min_l, "final_c": final_c,
        })
        rows.append(feat)

    return pd.DataFrame(rows) if rows else pd.DataFrame()

# ═══════════════════════════════════════════════════════════════════
#  WALK-FORWARD ML ENGINE
# ═══════════════════════════════════════════════════════════════════

BASE_FEAT_COLS = [
    "atr_r", "vol_r", "rsi14", "rsi_hi", "rsi_lo",
    "bb_pct", "gap", "ema_align",
    "mom1", "mom3", "mom5", "mom10", "mom20",
    "c_e5", "c_e10", "c_e20", "c_e50",
    "vol5", "vol20", "dow",
]

def get_feat_cols(df):
    """Get all valid feature columns (base + any macro columns that exist)."""
    macro_cols = [c for c in df.columns if c.startswith("m_")]
    return BASE_FEAT_COLS + macro_cols


def train_model(df_train):
    """Train LightGBM binary classifier: will price go UP (long profitable)?"""
    feat_cols = get_feat_cols(df_train)
    df_train  = df_train.dropna(subset=feat_cols)
    X = df_train[feat_cols].values
    y = (df_train["target"] == 1).astype(int).values

    model_l = lgb.LGBMClassifier(
        n_estimators=200, learning_rate=0.05, num_leaves=15,
        min_child_samples=10, subsample=0.8, colsample_bytree=0.8,
        random_state=42, verbose=-1, n_jobs=1,
    )
    model_l.fit(X, y)

    # Short model
    y_s = (df_train["target"] == -1).astype(int).values
    model_s = lgb.LGBMClassifier(
        n_estimators=200, learning_rate=0.05, num_leaves=15,
        min_child_samples=10, subsample=0.8, colsample_bytree=0.8,
        random_state=42, verbose=-1, n_jobs=1,
    )
    model_s.fit(X, y_s)
    return model_l, model_s, feat_cols


def walk_forward_backtest(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Monthly walk-forward:
      - Train on TRAIN_MONTHS months of data
      - Predict next month
      - Roll forward RETRAIN_EVERY months
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    start = pd.Timestamp(WF_START)
    end   = df["date"].max()

    trades = []
    model_l = model_s = None
    active_feat_cols = BASE_FEAT_COLS
    last_train = None

    df_test = df[df["date"] >= start].copy().reset_index(drop=True)

    for _, row in df_test.iterrows():
        dt = row["date"]

        # Retrain if needed
        need_retrain = (last_train is None or
                        (dt - last_train).days >= RETRAIN_EVERY * 30)

        if need_retrain:
            train_end   = dt
            train_start = dt - pd.DateOffset(months=TRAIN_MONTHS)
            df_tr = df[(df["date"] >= train_start) & (df["date"] < train_end)]
            if len(df_tr) >= 30:
                model_l, model_s, active_feat_cols = train_model(df_tr)
                last_train = dt

        if model_l is None:
            continue

        # Build feature vector safely
        x_vals = [row.get(f, np.nan) if f in row.index else np.nan
                  for f in active_feat_cols]
        if any(pd.isna(v) for v in x_vals):
            continue
        x = np.array([x_vals])

        prob_l = model_l.predict_proba(x)[0][1]
        prob_s = model_s.predict_proba(x)[0][1]

        # Decision
        direction = 0
        confidence = 0.0
        if prob_l >= CONF_THRESH and prob_l >= prob_s:
            direction = 1; confidence = prob_l
        elif prob_s >= CONF_THRESH and prob_s > prob_l:
            direction = -1; confidence = prob_s
        else:
            continue  # skip day — not confident

        pnl = row["pnl_l"] if direction == 1 else row["pnl_s"]
        trades.append({
            "date": dt, "direction": direction,
            "confidence": round(confidence, 3),
            "entry_px": row["entry_px"], "atr_d": row["atr_d"],
            "pnl_pct": pnl, "target": row["target"],
        })

    return pd.DataFrame(trades)


# ═══════════════════════════════════════════════════════════════════
#  ANTI-MARTINGALE SIZING
# ═══════════════════════════════════════════════════════════════════

def apply_antimartingale(trades: pd.DataFrame) -> pd.DataFrame:
    """
    After each win, multiply next trade size by WIN_MULT (cap at MAX_SIZE_MULT).
    After each loss, reset to 1.0x.
    """
    df = trades.copy()
    df["size_mult"] = 1.0
    df["pnl_usd"]   = 0.0

    mult = 1.0
    for i, row in df.iterrows():
        df.at[i, "size_mult"] = mult
        pnl_usd = row["pnl_pct"] * BASE_SIZE_USD * mult
        df.at[i, "pnl_usd"] = pnl_usd
        if pnl_usd > 0:
            mult = min(mult * WIN_MULT, MAX_SIZE_MULT)
        else:
            mult = 1.0

    return df


# ═══════════════════════════════════════════════════════════════════
#  METRICS
# ═══════════════════════════════════════════════════════════════════

def report(trades: pd.DataFrame, symbol: str):
    if trades.empty:
        print(f"  [{symbol}] No trades generated.")
        return

    trades = apply_antimartingale(trades)
    p = trades["pnl_usd"].values
    wins = p[p > 0]; loss = p[p < 0]
    gw   = wins.sum() if len(wins) else 0
    gl   = -loss.sum() if len(loss) else 1e-9
    wr   = len(wins) / len(p) * 100
    pf   = min(gw / gl, 99) if gl > 0 else 99

    equity = np.cumprod(1 + trades["pnl_pct"].values * trades["size_mult"].values * BASE_SIZE_USD / 10000)
    peak   = np.maximum.accumulate(equity)
    dd     = float(((peak - equity) / peak).max()) * 100

    pct_arr= trades["pnl_pct"].values * trades["size_mult"].values
    mu  = pct_arr.mean()
    sig = pct_arr.std()
    sharpe = mu / sig * np.sqrt(252) if sig > 0 else 0

    months = max(1, (trades["date"].max() - trades["date"].min()).days / 30)
    n_mo   = len(trades) / months
    total_usd = p.sum()
    ann_pct   = (equity[-1] - 1) / months * 12 * 100

    accuracy  = (trades["direction"] == trades["target"]).mean() * 100

    print(f"\n  ┌─────────────────────────────────────────────┐")
    print(f"  │  {symbol:<10}  Walk-Forward Results            │")
    print(f"  ├─────────────────────────────────────────────┤")
    print(f"  │  Trades total  : {len(trades):>4}  ({n_mo:.1f}/month)         │")
    print(f"  │  Profit Factor : {pf:>6.3f}                        │")
    print(f"  │  Win Rate      : {wr:>5.1f}%                       │")
    print(f"  │  Direction Acc : {accuracy:>5.1f}%  (model accuracy)   │")
    print(f"  │  Sharpe Ratio  : {sharpe:>6.2f}                       │")
    print(f"  │  Max Drawdown  : {dd:>5.1f}%                       │")
    print(f"  │  Net P&L (USD) : ${total_usd:>+8.0f} (${BASE_SIZE_USD}/trade)   │")
    print(f"  │  Ann% (equity) : {ann_pct:>+6.1f}%                      │")
    print(f"  │  Anti-Martingale: ON (x{WIN_MULT} after win, cap {MAX_SIZE_MULT}x) │")
    print(f"  └─────────────────────────────────────────────┘")

    # Monthly breakdown
    trades["ym"] = trades["date"].dt.to_period("M")
    monthly = trades.groupby("ym")["pnl_usd"].sum()
    print(f"\n  Monthly P&L (anti-martingale, ${BASE_SIZE_USD}/base trade):")
    for ym, val in monthly.items():
        bar = "█" * int(abs(val) / 50) if abs(val) >= 50 else "▌"
        sign = "+" if val >= 0 else "-"
        print(f"    {ym}  {sign}${abs(val):6.0f}  {bar}")

    return trades


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

def run_instrument(name, cfg):
    ticker = cfg["ticker"]
    print(f"\n{'='*60}")
    print(f"  {name}  ({ticker})")
    print(f"{'='*60}")

    print(f"  Downloading data ...", flush=True)
    try:
        df1h = yf.download(ticker, period="max", interval="1h",
                           progress=False, auto_adjust=True).dropna()
        df1d = yf.download(ticker, period="10y",  interval="1d",
                           progress=False, auto_adjust=True).dropna()
    except Exception as e:
        print(f"  Download failed: {e}"); return

    if df1h.empty or df1d.empty:
        print(f"  No data."); return

    # Flatten multi-level columns from newer yfinance versions
    if isinstance(df1h.columns, pd.MultiIndex):
        df1h.columns = df1h.columns.get_level_values(0)
    if isinstance(df1d.columns, pd.MultiIndex):
        df1d.columns = df1d.columns.get_level_values(0)

    print(f"  1h bars: {len(df1h)}  Daily bars: {len(df1d)}")
    print(f"  Building features ...", flush=True)
    df = build_features(df1h, df1d, cfg["entry_hour"], cfg["exit_hour"],
                        macro_tickers=cfg.get("macro", []))
    if df.empty:
        print(f"  No feature rows."); return
    print(f"  Feature rows: {len(df)}", flush=True)

    print(f"  Walk-forward backtest (from {WF_START}) ...", flush=True)
    trades = walk_forward_backtest(df, name)
    if trades.empty:
        print(f"  No trades above confidence threshold."); return

    result = report(trades, name)
    return result


def main():
    if not HAS_LGB:
        print("\nInstall lightgbm first:  pip install lightgbm")
        return

    print("\n" + "="*60)
    print("  RICH TRADER — ML STRATEGY ENGINE")
    print(f"  Walk-forward start: {WF_START}")
    print(f"  Confidence threshold: {CONF_THRESH}")
    print(f"  TP={TP_ATR}×ATR  SL={SL_ATR}×ATR")
    print(f"  Anti-martingale: x{WIN_MULT} after win, cap {MAX_SIZE_MULT}x")
    print("="*60)

    for name, cfg in INSTRUMENTS.items():
        run_instrument(name, cfg)

    print("\n  Done.\n")


if __name__ == "__main__":
    main()
