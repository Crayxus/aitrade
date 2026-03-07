import os
import json
import time
import threading
import datetime
import numpy as np
import pandas as pd
import yfinance as yf
from pathlib import Path
from flask import Flask, jsonify, send_from_directory, request

app = Flask(__name__, static_folder='.')
_cache          = {}   # {date_str: strategies_list}
_pnl_snap       = {}   # {date_str: final_pnl}  in-memory only
# ── Persistent history ────────────────────────────────────────────────────────
HISTORY_FILE = Path(__file__).parent / "data" / "history.json"

def load_history():
    try:
        HISTORY_FILE.parent.mkdir(exist_ok=True)
        if HISTORY_FILE.exists():
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def save_history(history):
    try:
        HISTORY_FILE.parent.mkdir(exist_ok=True)
        HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[WARN] save_history: {e}")

_history = load_history()   # {date_str: {wins, losses, avg_pnl, detail, ...}}

# ── Intraday P&L Range (persistent) ──────────────────────────────────────────
PNL_RANGE_FILE = Path(__file__).parent / "data" / "pnl_range.json"

def _load_pnl_range():
    try:
        PNL_RANGE_FILE.parent.mkdir(exist_ok=True)
        if PNL_RANGE_FILE.exists():
            return json.loads(PNL_RANGE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _save_pnl_range():
    try:
        PNL_RANGE_FILE.write_text(json.dumps(_today_pnl_range, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[WARN] _save_pnl_range: {e}")

_today_pnl_range = _load_pnl_range()  # {date_str: {"high": float, "low": float}}

def _parse_pnl_usd(s):
    """Parse '+$200' or '$-150' → float."""
    try:
        return float(str(s).replace('$', '').replace('+', '').strip())
    except Exception:
        return 0.0

def _update_pnl_range(date_str, total_pnl):
    """Track intraday high/low of combined portfolio P&L, persisted to disk."""
    # Purge entries older than 2 days to avoid unbounded growth
    bj_today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d")
    for old in [d for d in list(_today_pnl_range) if d < bj_today]:
        del _today_pnl_range[old]

    if date_str not in _today_pnl_range:
        _today_pnl_range[date_str] = {"high": total_pnl, "low": total_pnl}
    else:
        r = _today_pnl_range[date_str]
        if total_pnl > r["high"]: r["high"] = total_pnl
        if total_pnl < r["low"]:  r["low"]  = total_pnl
    _save_pnl_range()

# ── 6 Instruments ────────────────────────────────────────────────────────────
UNIFIED_EXIT = "03:00"
RISK_USD     = 200

LOT_VALUES = {
    "XAUUSD": 100,
    "NAS100": 1,
    "BTCUSD": 1,
    "EURUSD": 100000,
    "GBPUSD": 100000,
    "USOIL":  100,
}

STRATEGY_CONFIGS = [
    # XAUUSD — London primary session (15:00-15:45 BJ)
    # NY backup (21:00 BJ) is computed separately for the hero panel only
    {
        "symbol": "XAUUSD", "display_name": "Gold",      "ticker": "XAUUSD=X",
        "strategy": "Intraday", "win_rate": 4, "rr_ratio": "1:1.5",
        "sl_atr": 1.0, "tp_atr": 1.5, "exit_time": UNIFIED_EXIT,
        "entry_start": "15:00", "entry_end": "15:45",
        "is_xau": True, "session": "London",
    },
    # NAS100 — NY session (21:00-21:45 BJ)
    {
        "symbol": "NAS100",  "display_name": "Nasdaq 100", "ticker": "NQ=F",
        "strategy": "Intraday", "win_rate": 4, "rr_ratio": "1:2.0",
        "sl_atr": 0.75, "tp_atr": 1.5, "exit_time": UNIFIED_EXIT,
        "entry_start": "21:00", "entry_end": "21:45",
        "session": "NY",
    },
    # BTCUSD — NY session (21:00-21:30 BJ)
    {
        "symbol": "BTCUSD",  "display_name": "Bitcoin",    "ticker": "BTC-USD",
        "strategy": "Intraday", "win_rate": 3, "rr_ratio": "1:4.0",
        "sl_atr": 0.5, "tp_atr": 2.0, "exit_time": UNIFIED_EXIT,
        "entry_start": "21:00", "entry_end": "21:30",
        "session": "NY",
    },
]

# NY backup config — used only by /api/xauusd hero panel, not shown as a card
XAUUSD_NY_CONFIG = {
    "symbol": "XAUUSD", "display_name": "Gold · NY Backup", "ticker": "XAUUSD=X",
    "strategy": "Intraday NY", "win_rate": 3, "rr_ratio": "1:1.5",
    "sl_atr": 1.0, "tp_atr": 1.5, "exit_time": UNIFIED_EXIT,
    "entry_start": "21:00", "entry_end": "21:45",
    "is_xau": True, "session": "NY",
}

# ── Technical Helpers ─────────────────────────────────────────────────────────

def fmt_price(n, ref):
    if ref >= 10000: return round(float(n), 0)
    if ref >= 1000:  return round(float(n), 1)
    if ref >= 10:    return round(float(n), 2)
    if ref >= 1:     return round(float(n), 4)
    return round(float(n), 5)

def calc_atr(high, low, close, period=14):
    tr = np.maximum(high[1:] - low[1:],
         np.maximum(np.abs(high[1:] - close[:-1]),
                    np.abs(low[1:] - close[:-1])))
    return float(np.mean(tr[-period:]))

def calc_ema(prices, period):
    alpha = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = alpha * float(p) + (1 - alpha) * ema
    return float(ema)

def calc_rsi(prices, period=14):
    d = np.diff(prices.astype(float))
    gains  = np.where(d > 0, d, 0.0)
    losses = np.where(d < 0, -d, 0.0)
    ag = np.mean(gains[-period:])
    al = np.mean(losses[-period:])
    if al == 0: return 100.0
    return float(100 - 100 / (1 + ag / al))

def make_sparkline(close, n=12):
    data = [float(v) for v in close[-n:]]
    lo, hi = min(data), max(data)
    if hi == lo: return [50.0] * len(data)
    return [round((v - lo) / (hi - lo) * 100, 1) for v in data]

def calc_recommended_lots(symbol, entry_mid, stop_loss):
    sl_dist = abs(float(entry_mid) - float(stop_loss))
    lot_val = LOT_VALUES.get(symbol, 1)
    if sl_dist == 0 or lot_val == 0: return 0.10
    raw = RISK_USD / (sl_dist * lot_val)
    for std in [0.01, 0.02, 0.05, 0.10, 0.20, 0.50, 1.0, 2.0, 5.0, 10.0]:
        if raw <= std * 1.6: return std
    return 1.0

# ── Enhanced Direction Signal ─────────────────────────────────────────────────

def score_direction(close_d, high_d, low_d, close_h, gap_pct, rsi, cfg):
    """
    Multi-signal direction scoring. Returns (direction, confidence, signals_dict).
    Signals: daily trend, weekly momentum, gap, RSI mean-reversion, hourly trend.
    All must be >= 3/5 agreement to trade; otherwise direction still chosen by majority.
    """
    ema20_d = calc_ema(close_d, 20)
    ema50_d = calc_ema(close_d, min(50, len(close_d)-1))
    mom5    = (close_d[-1] - close_d[-6]) / close_d[-6]
    mom20   = (close_d[-1] - close_d[-21]) / close_d[-21] if len(close_d) > 21 else mom5

    # Hourly trend (last 20 x 1h bars)
    ema20_h = calc_ema(close_h, min(20, len(close_h)-1)) if len(close_h) >= 5 else close_h[-1]

    # 1  Daily trend (price vs EMA20)
    s1 = 1 if close_d[-1] > ema20_d else -1
    # 2  Weekly momentum (20-day return)
    s2 = 1 if mom20 > 0 else -1
    # 3  Gap direction (today open vs yesterday close)
    s3 = 1 if gap_pct > 0.0005 else (-1 if gap_pct < -0.0005 else 0)
    # 4  RSI mean-reversion (oversold → bullish bias; overbought → bearish)
    s4 = 1 if rsi < 40 else (-1 if rsi > 60 else 0)
    # 5  Hourly trend
    s5 = 1 if close_h[-1] > ema20_h else -1
    # 6  Price vs EMA50 (longer trend)
    s6 = 1 if close_d[-1] > ema50_d else -1

    signals = {"daily_ema": s1, "weekly_mom": s2, "gap": s3,
               "rsi": s4, "hourly": s5, "ema50": s6}
    score = sum(signals.values())
    direction  = "LONG" if score >= 0 else "SHORT"
    confidence = round(abs(score) / 6, 2)   # 0.0 – 1.0

    return direction, confidence, signals

# ── Entry Window ─────────────────────────────────────────────────────────────

def get_window_bars(isub, entry_start="15:00", entry_end="15:30"):
    """Return 5m bars within a Beijing time window (converts BJ→UTC automatically)."""
    try:
        h_s, m_s = map(int, entry_start.split(":"))
        h_e, m_e = map(int, entry_end.split(":"))
        # Beijing = UTC+8  →  UTC = Beijing − 8
        utc_s_h, utc_e_h = h_s - 8, h_e - 8
        df = isub.copy()
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        else:
            df.index = df.index.tz_convert('UTC')
        today_utc = pd.Timestamp.utcnow().normalize()
        win_start = today_utc + pd.Timedelta(hours=utc_s_h, minutes=m_s)
        win_end   = today_utc + pd.Timedelta(hours=utc_e_h, minutes=m_e)
        return df[(df.index >= win_start) & (df.index <= win_end)]
    except Exception:
        return pd.DataFrame()

def entry_from_window(isub, atr, current, direction, sl_m, tp_m,
                      entry_start="15:00", entry_end="15:30"):
    window = get_window_bars(isub, entry_start, entry_end)
    src_label = f"{entry_start}–{entry_end} BJ"
    if len(window) >= 2:
        # True ORB: use session High/Low (not closes) as the breakout range
        highs  = np.asarray(window['High'],  dtype=float).flatten()
        lows   = np.asarray(window['Low'],   dtype=float).flatten()
        closes = np.asarray(window['Close'], dtype=float).flatten()
        lo  = float(np.min(lows))
        hi  = float(np.max(highs))
        mid = float(np.mean(closes))
        src = src_label
    else:
        lo  = current - (0.20 if direction == "LONG" else -0.08) * atr
        hi  = current + (0.08 if direction == "LONG" else  0.20) * atr
        mid = (lo + hi) / 2
        src = f"est · opens {entry_start} BJ"

    ref = mid
    el, eh = fmt_price(lo, ref), fmt_price(hi, ref)
    if direction == "LONG":
        sl = fmt_price(lo  - sl_m * atr, ref)
        tp = fmt_price(hi  + tp_m * atr, ref)
        tp_pct = f"+{(tp - eh) / eh * 100:.2f}%"
        sl_pct = f"-{(el - sl) / el * 100:.2f}%"
    else:
        sl = fmt_price(hi  + sl_m * atr, ref)
        tp = fmt_price(lo  - tp_m * atr, ref)
        tp_pct = f"+{(el - tp) / el * 100:.2f}%"
        sl_pct = f"-{(sl - eh) / eh * 100:.2f}%"

    return dict(entry_low=el, entry_high=eh, entry_mid=fmt_price(mid, ref),
                stop_loss=sl, take_profit=tp, tp_pct=tp_pct, sl_pct=sl_pct,
                entry_source=src)

# ── Build Strategies ──────────────────────────────────────────────────────────

TICKER_FALLBACKS = {
    "XAUUSD=X": ["GC=F"],
    "GC=F":     ["XAUUSD=X"],
    "NQ=F":     ["QQQ"],
    "BTC-USD":  ["BTC-USD"],
}

def _fetch_ticker_frames(ticker_sym):
    """Download daily / 5m / 1h frames for one ticker using yf.Ticker.history()
    which is more reliable on cloud servers than yf.download()."""
    tickers_to_try = [ticker_sym] + TICKER_FALLBACKS.get(ticker_sym, [])
    last_err = None
    for sym in tickers_to_try:
        try:
            t    = yf.Ticker(sym)
            df_d = t.history(period="60d", interval="1d").dropna()
            if len(df_d) < 10:
                raise ValueError(f"{sym}: daily data too short ({len(df_d)} rows)")
            df_i = t.history(period="2d",  interval="5m").dropna()
            df_h = t.history(period="5d",  interval="1h").dropna()
            for df in (df_d, df_i, df_h):
                df.columns = [c.capitalize() for c in df.columns]
            print(f"[FETCH] {sym} ok  daily={len(df_d)} 5m={len(df_i)} 1h={len(df_h)}")
            return df_d, df_i, df_h
        except Exception as e:
            last_err = e
            print(f"[FETCH] {sym} failed: {e}")
    raise RuntimeError(f"All tickers failed for {ticker_sym}: {last_err}")


def build_strategies():
    # Cache raw frames per ticker so we don't re-download for configs sharing a symbol
    frames_cache = {}
    errors = []
    results = []

    for cfg in STRATEGY_CONFIGS:
        try:
            t = cfg["ticker"]
            if t not in frames_cache:
                frames_cache[t] = _fetch_ticker_frames(t)
            dsub, isub, hsub = frames_cache[t]

            if len(dsub) < 21: raise ValueError(f"daily data too short: {len(dsub)} rows")

            close_d = np.asarray(dsub["Close"], dtype=float).flatten()
            high_d  = np.asarray(dsub["High"],  dtype=float).flatten()
            low_d   = np.asarray(dsub["Low"],   dtype=float).flatten()
            close_h = np.asarray(hsub["Close"], dtype=float).flatten() if len(hsub) > 5 else close_d[-20:]

            atr        = calc_atr(high_d, low_d, close_d)
            atr_avg20  = calc_atr(high_d[-40:], low_d[-40:], close_d[-40:], period=20)
            rsi        = calc_rsi(close_d)
            prev_close = close_d[-2]

            current    = float(np.asarray(isub["Close"], dtype=float).flatten()[-1]) if len(isub) > 0 else close_d[-1]
            today_open = float(np.asarray(isub["Open"],  dtype=float).flatten()[0])  if len(isub) > 0 else close_d[-1]
            gap_pct    = (today_open - prev_close) / prev_close

            # Volatility filter: skip if ATR is extreme (< 0.3x or > 3x average)
            atr_ratio = atr / atr_avg20 if atr_avg20 > 0 else 1.0
            vol_ok = 0.3 <= atr_ratio <= 3.0

            direction, confidence, signals = score_direction(
                close_d, high_d, low_d, close_h, gap_pct, rsi, cfg)

            levels = entry_from_window(isub, atr, current, direction,
                                       cfg["sl_atr"], cfg["tp_atr"],
                                       cfg.get("entry_start", "15:00"),
                                       cfg.get("entry_end",   "15:30"))

            lots       = calc_recommended_lots(cfg["symbol"], levels["entry_mid"], levels["stop_loss"])
            lv         = LOT_VALUES.get(cfg["symbol"], 1)
            risk_amt   = abs(float(levels["entry_mid"]) - float(levels["stop_loss"])) * lv * lots
            profit_amt = abs(float(levels["take_profit"]) - float(levels["entry_mid"])) * lv * lots

            # Signal summary for display
            sig_icons = {k: ("▲" if v > 0 else "▼" if v < 0 else "—") for k, v in signals.items()}

            results.append({
                "symbol":           cfg["symbol"],
                "display_name":     cfg["display_name"],
                "strategy":         cfg["strategy"],
                "direction":        direction,
                "confidence":       confidence,
                "confidence_pct":   f"{int(confidence * 100)}%",
                "signals":          sig_icons,
                "vol_ok":           vol_ok,
                "rsi":              round(rsi, 1),
                "entry_low":        levels["entry_low"],
                "entry_high":       levels["entry_high"],
                "entry_mid":        levels["entry_mid"],
                "entry_source":     levels["entry_source"],
                "take_profit":      levels["take_profit"],
                "tp_pct":           levels["tp_pct"],
                "stop_loss":        levels["stop_loss"],
                "sl_pct":           levels["sl_pct"],
                "entry_start":      cfg.get("entry_start", "15:00"),
                "entry_end":        cfg.get("entry_end",   "15:30"),
                "exit_time":        cfg["exit_time"],
                "win_rate":         cfg["win_rate"],
                "rr_ratio":         cfg["rr_ratio"],
                "atr":              fmt_price(atr, current),
                "sparkline":        make_sparkline(close_d),
                "current":          fmt_price(current, current),
                "mom_pct":          f"{gap_pct * 100:+.2f}%",
                "recommended_lots": lots,
                "risk_usd":         round(risk_amt),
                "profit_usd":       round(profit_amt),
                "session":          cfg.get("session"),
                "is_xau":           cfg.get("is_xau", False),
            })
        except Exception as e:
            msg = f"{cfg['symbol']}: {e}"
            print(f"[WARN] {msg}")
            errors.append(msg)

    if not results:
        raise RuntimeError("All fetches failed — " + " | ".join(errors))
    return results

# ── Live Prices ───────────────────────────────────────────────────────────────

def fetch_current_prices():
    prices = {}
    seen = set()
    for cfg in STRATEGY_CONFIGS:
        t = cfg["ticker"]
        if t not in seen:
            tickers_to_try = [t] + TICKER_FALLBACKS.get(t, [])
            val = None
            for sym in tickers_to_try:
                try:
                    sub = yf.Ticker(sym).history(period="1d", interval="5m").dropna()
                    sub.columns = [c.capitalize() for c in sub.columns]
                    if len(sub) > 0:
                        val = float(sub["Close"].iloc[-1])
                        break
                except Exception:
                    continue
            seen.add(t)
        else:
            val = prices.get(cfg["symbol"])
        prices[cfg["symbol"]] = val
    return prices

# ── Exit Time ─────────────────────────────────────────────────────────────────

def is_past_exit(exit_time_str):
    try:
        bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        h, m = map(int, exit_time_str.split(":"))
        cur = bj.hour * 60 + bj.minute
        ext = h * 60 + m
        if h < 9:   # next-day exit
            return bj.hour < 9 and cur >= ext
        return cur >= ext
    except Exception:
        return False

# ── P&L Calc ─────────────────────────────────────────────────────────────────

def calc_pnl(strategy, current_price):
    if current_price is None: return None

    # Not yet in position — entry window hasn't opened
    entry_start = strategy.get("entry_start", "00:00")
    bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    bj_hm = bj.hour * 60 + bj.minute
    es_h, es_m = map(int, entry_start.split(":"))
    if bj_hm < es_h * 60 + es_m:
        return {
            "symbol":        strategy["symbol"],
            "current_price": fmt_price(current_price, current_price),
            "entry_mid":     fmt_price(strategy["entry_mid"], strategy["entry_mid"]),
            "entry_start":   entry_start,
            "pnl_pct":       "–",
            "pnl_usd":       "–",
            "pnl_value":     0,
            "status":        "pending",
            "exit_time":     strategy.get("exit_time", UNIFIED_EXIT),
            "progress":      0,
        }

    direction   = strategy["direction"]
    entry_mid   = strategy["entry_mid"]
    take_profit = strategy["take_profit"]
    stop_loss   = strategy["stop_loss"]
    exit_time   = strategy.get("exit_time", UNIFIED_EXIT)

    if direction == "LONG":
        pnl_pct  = (current_price - entry_mid) / entry_mid * 100
        tp_dist  = take_profit - entry_mid
        progress = (current_price - entry_mid) / tp_dist if tp_dist else 0
        hit_tp, hit_sl = current_price >= take_profit, current_price <= stop_loss
    else:
        pnl_pct  = (entry_mid - current_price) / entry_mid * 100
        tp_dist  = entry_mid - take_profit
        progress = (entry_mid - current_price) / tp_dist if tp_dist else 0
        hit_tp, hit_sl = current_price <= take_profit, current_price >= stop_loss

    if hit_tp:        status = "hit_tp"
    elif hit_sl:      status = "hit_sl"
    elif is_past_exit(exit_time): status = "time_exit"
    elif pnl_pct > 0: status = "winning"
    else:             status = "losing"

    sign = "+" if pnl_pct >= 0 else ""
    lv   = LOT_VALUES.get(strategy["symbol"], 1)
    lots = strategy.get("recommended_lots", 0.1)
    pnl_usd = round(pnl_pct / 100 * entry_mid * lv * lots)

    return {
        "symbol":        strategy["symbol"],
        "current_price": fmt_price(current_price, current_price),
        "entry_mid":     fmt_price(entry_mid, entry_mid),
        "pnl_pct":       f"{sign}{pnl_pct:.2f}%",
        "pnl_usd":       f"{'+' if pnl_usd >= 0 else ''}${pnl_usd}",
        "pnl_value":     pnl_pct,
        "status":        status,
        "exit_time":     exit_time,
        "progress":      round(min(1.0, max(-0.5, progress)), 3),
    }

# ── History Snapshot ──────────────────────────────────────────────────────────

def snapshot_day(date_str, pnl_list):
    """Save end-of-day result to persistent history."""
    global _history
    wins   = [p for p in pnl_list if p["pnl_value"] > 0]
    losses = [p for p in pnl_list if p["pnl_value"] <= 0]
    avg    = sum(p["pnl_value"] for p in pnl_list) / len(pnl_list) if pnl_list else 0
    best   = max(pnl_list, key=lambda p: p["pnl_value"], default=None)
    worst  = min(pnl_list, key=lambda p: p["pnl_value"], default=None)
    total_usd = sum(
        int(p["pnl_usd"].replace("+$","").replace("-$","").replace("$","").replace("+",""))
        * (1 if not p["pnl_usd"].startswith("-") else -1)
        for p in pnl_list
    )

    rng = _today_pnl_range.get(date_str, {})
    record = {
        "date":           date_str,
        "wins":           len(wins),
        "losses":         len(losses),
        "total":          len(pnl_list),
        "win_rate":       round(len(wins) / len(pnl_list) * 100) if pnl_list else 0,
        "avg_pnl":        f"{avg:+.2f}%",
        "total_usd":      f"{'+' if total_usd >= 0 else ''}${total_usd}",
        "best":           {"symbol": best["symbol"],  "pnl": best["pnl_pct"]}  if best  else None,
        "worst":          {"symbol": worst["symbol"], "pnl": worst["pnl_pct"]} if worst else None,
        "detail":         [{k: p[k] for k in ("symbol","pnl_pct","pnl_usd","status")} for p in pnl_list],
        "pnl_range_high": rng.get("high"),
        "pnl_range_low":  rng.get("low"),
    }
    _history[date_str] = record
    save_history(_history)
    return record

# ── XAUUSD NY signal cache (separate from main grid cache) ───────────────────
_ny_cache = {}   # {date_str: strategy_dict}

def build_ny_signal():
    """Build the NY-session XAUUSD signal using XAUUSD_NY_CONFIG."""
    cfg = XAUUSD_NY_CONFIG
    dsub, isub, hsub = _fetch_ticker_frames(cfg["ticker"])

    close_d = np.asarray(dsub["Close"], dtype=float).flatten()
    high_d  = np.asarray(dsub["High"],  dtype=float).flatten()
    low_d   = np.asarray(dsub["Low"],   dtype=float).flatten()
    close_h = np.asarray(hsub["Close"], dtype=float).flatten() if len(hsub) > 5 else close_d[-20:]

    atr       = calc_atr(high_d, low_d, close_d)
    atr_avg20 = calc_atr(high_d[-40:], low_d[-40:], close_d[-40:], period=20)
    rsi       = calc_rsi(close_d)

    current    = float(np.asarray(isub["Close"], dtype=float).flatten()[-1]) if len(isub) > 0 else close_d[-1]
    today_open = float(np.asarray(isub["Open"],  dtype=float).flatten()[0])  if len(isub) > 0 else close_d[-1]
    gap_pct    = (today_open - close_d[-2]) / close_d[-2]
    atr_ratio  = atr / atr_avg20 if atr_avg20 > 0 else 1.0

    direction, confidence, signals = score_direction(close_d, high_d, low_d, close_h, gap_pct, rsi, cfg)
    levels = entry_from_window(isub, atr, current, direction, cfg["sl_atr"], cfg["tp_atr"],
                               cfg["entry_start"], cfg["entry_end"])
    lots    = calc_recommended_lots(cfg["symbol"], levels["entry_mid"], levels["stop_loss"])
    lv      = LOT_VALUES.get(cfg["symbol"], 1)
    risk_amt   = abs(float(levels["entry_mid"]) - float(levels["stop_loss"])) * lv * lots
    profit_amt = abs(float(levels["take_profit"]) - float(levels["entry_mid"])) * lv * lots
    sig_icons  = {k: ("▲" if v > 0 else "▼" if v < 0 else "—") for k, v in signals.items()}

    return {
        "symbol": cfg["symbol"], "display_name": cfg["display_name"],
        "strategy": cfg["strategy"], "direction": direction,
        "confidence": confidence, "confidence_pct": f"{int(confidence*100)}%",
        "signals": sig_icons, "vol_ok": 0.3 <= atr_ratio <= 3.0,
        "rsi": round(rsi, 1),
        "entry_low": levels["entry_low"], "entry_high": levels["entry_high"],
        "entry_mid": levels["entry_mid"], "entry_source": levels["entry_source"],
        "take_profit": levels["take_profit"], "tp_pct": levels["tp_pct"],
        "stop_loss": levels["stop_loss"],     "sl_pct": levels["sl_pct"],
        "entry_start": cfg["entry_start"], "entry_end": cfg["entry_end"],
        "exit_time": cfg["exit_time"], "win_rate": cfg["win_rate"],
        "rr_ratio": cfg["rr_ratio"], "atr": fmt_price(atr, current),
        "sparkline": make_sparkline(close_d), "current": fmt_price(current, current),
        "mom_pct": f"{gap_pct*100:+.2f}%",
        "recommended_lots": lots, "risk_usd": round(risk_amt), "profit_usd": round(profit_amt),
        "session": "NY", "is_xau": True,
    }

# ── Strategy Disk Cache (for history reconstruction on restart) ───────────────
STRATEGY_CACHE_FILE = Path(__file__).parent / "data" / "strategy_cache.json"

def _save_strategy_cache(date_str, strategies):
    """Persist today's strategy params so history can be reconstructed after restart."""
    try:
        STRATEGY_CACHE_FILE.parent.mkdir(exist_ok=True)
        try:
            disk = json.loads(STRATEGY_CACHE_FILE.read_text(encoding="utf-8")) if STRATEGY_CACHE_FILE.exists() else {}
        except Exception:
            disk = {}
        disk[date_str] = [{
            "symbol":            s["symbol"],
            "direction":         s["direction"],
            "entry_mid":         s["entry_mid"],
            "stop_loss":         s["stop_loss"],
            "take_profit":       s["take_profit"],
            "recommended_lots":  s.get("recommended_lots", 0.1),
            "ticker":            next((c["ticker"] for c in STRATEGY_CONFIGS if c["symbol"] == s["symbol"]), None),
        } for s in strategies]
        # Keep last 7 days to avoid unbounded growth
        cutoff = (datetime.datetime.utcnow() + datetime.timedelta(hours=8) - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        disk = {d: v for d, v in disk.items() if d >= cutoff}
        STRATEGY_CACHE_FILE.write_text(json.dumps(disk, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[WARN] _save_strategy_cache: {e}")

# ── XAUUSD Daily Log ─────────────────────────────────────────────────────────
XAU_LOG_FILE = Path(__file__).parent / "data" / "xauusd_log.json"

def load_xau_log():
    try:
        XAU_LOG_FILE.parent.mkdir(exist_ok=True)
        if XAU_LOG_FILE.exists():
            return json.loads(XAU_LOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def save_xau_log(log):
    try:
        XAU_LOG_FILE.parent.mkdir(exist_ok=True)
        XAU_LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[WARN] save_xau_log: {e}")

_xau_log = load_xau_log()  # {date_str: {session, direction, score, entry, sl, tp, atr, ...}}

def _upsert_xau_today(strategies, session_label):
    """Save today's XAUUSD signal to persistent log (called by scheduler)."""
    global _xau_log
    bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    date_str = bj.strftime('%Y-%m-%d')

    xau = next((s for s in strategies
                if s.get("symbol") == "XAUUSD" and s.get("session") == session_label), None)
    if xau is None:
        return

    entry = {
        "date":      date_str,
        "session":   session_label,
        "direction": xau["direction"],
        "score":     None,          # score not stored in strategy dict directly
        "entry":     xau["entry_mid"],
        "sl":        xau["stop_loss"],
        "tp":        xau["take_profit"],
        "atr":       xau["atr"],
        "confidence":xau.get("confidence_pct", "–"),
        "signals":   xau.get("signals", {}),
        "status":    "open",
        "pnl_pct":   None,
        "pnl_usd":   None,
        "close_px":  None,
    }

    # Merge — don't overwrite if already have P&L
    existing = _xau_log.get(date_str, {})
    if existing.get("status") in ("hit_tp", "hit_sl", "time_exit"):
        return   # already finalised for today
    _xau_log[date_str] = {**existing, **entry}
    save_xau_log(_xau_log)
    print(f"[XAU LOG] {date_str} {session_label}: {xau['direction']} entry={xau['entry_mid']}")

def _finalise_xau_today():
    """Snapshot XAUUSD P&L at 03:30 BJ (called by scheduler).
    Trade is entered the previous calendar day → look at yesterday's log entry."""
    global _xau_log
    bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    # Trade entry date is the previous day (exit at 03:00 BJ is next calendar day)
    yesterday = (bj - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    date_str = yesterday

    if date_str not in _xau_log:
        return
    if _xau_log[date_str].get("status") in ("hit_tp", "hit_sl", "time_exit"):
        return   # already done

    try:
        prices = fetch_current_prices()
        cur = prices.get("XAUUSD")
        if cur is None:
            return
        entry_mid = float(_xau_log[date_str]["entry"])
        direction = _xau_log[date_str]["direction"]
        sl  = float(_xau_log[date_str]["sl"])
        tp  = float(_xau_log[date_str]["tp"])

        if direction == "LONG":
            pnl_pct = (cur - entry_mid) / entry_mid * 100
            status  = "hit_tp" if cur >= tp else "hit_sl" if cur <= sl else "time_exit"
        else:
            pnl_pct = (entry_mid - cur) / entry_mid * 100
            status  = "hit_tp" if cur <= tp else "hit_sl" if cur >= sl else "time_exit"

        pnl_usd = round(pnl_pct / 100 * entry_mid * 100 * 0.1)  # 0.1 lot = 10 oz
        sign = "+" if pnl_pct >= 0 else ""
        _xau_log[date_str].update({
            "status":   status,
            "close_px": fmt_price(cur, cur),
            "pnl_pct":  f"{sign}{pnl_pct:.2f}%",
            "pnl_usd":  f"{'+' if pnl_usd >= 0 else ''}${pnl_usd}",
        })
        save_xau_log(_xau_log)
        print(f"[XAU LOG] finalised {date_str}: {status} pnl={sign}{pnl_pct:.2f}%")
    except Exception as e:
        print(f"[XAU LOG] finalise error: {e}")

# ── Startup: finalize any past open xau entries ───────────────────────────────
def _startup_finalize_open_entries():
    """On startup, resolve xau_log entries from previous days still showing 'open'."""
    global _xau_log
    bj_today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d')
    open_past = {d: e for d, e in _xau_log.items()
                 if d < bj_today and e.get("status") not in ("hit_tp", "hit_sl", "time_exit")}
    if not open_past:
        return
    print(f"[STARTUP] Finalizing {len(open_past)} unresolved past xau entries: {list(open_past.keys())}")
    try:
        df = None
        for sym in ["GC=F", "XAUUSD=X"]:
            try:
                t = yf.Ticker(sym)
                df = t.history(period="30d", interval="1d").dropna()
                df.columns = [c.capitalize() for c in df.columns]
                if len(df) > 0:
                    break
            except Exception:
                continue
        if df is None or len(df) == 0:
            return
        if df.index.tz:
            df.index = df.index.tz_convert(None)
        for date_str, entry in open_past.items():
            try:
                direction = entry.get("direction")
                entry_mid = float(entry.get("entry", 0))
                sl = float(entry.get("sl", 0))
                tp = float(entry.get("tp", 0))
                if not entry_mid or not direction:
                    continue
                target = pd.Timestamp(date_str)
                day_rows = df[df.index.normalize() == target]
                if len(day_rows) == 0:
                    print(f"[STARTUP] No bar for {date_str}, skipping")
                    continue
                day_high  = float(day_rows["High"].max())
                day_low   = float(day_rows["Low"].min())
                day_close = float(day_rows["Close"].iloc[-1])
                if direction == "LONG":
                    if day_high >= tp:   status, close_px = "hit_tp",   tp
                    elif day_low <= sl:  status, close_px = "hit_sl",   sl
                    else:               status, close_px = "time_exit", day_close
                    pnl_pct = (close_px - entry_mid) / entry_mid * 100
                else:
                    if day_low <= tp:    status, close_px = "hit_tp",   tp
                    elif day_high >= sl: status, close_px = "hit_sl",   sl
                    else:               status, close_px = "time_exit", day_close
                    pnl_pct = (entry_mid - close_px) / entry_mid * 100
                pnl_usd = round(pnl_pct / 100 * entry_mid * 100 * 0.1)
                sign = "+" if pnl_pct >= 0 else ""
                _xau_log[date_str].update({
                    "status":   status,
                    "close_px": fmt_price(close_px, close_px),
                    "pnl_pct":  f"{sign}{pnl_pct:.2f}%",
                    "pnl_usd":  f"{'+' if pnl_usd >= 0 else ''}${pnl_usd}",
                })
                save_xau_log(_xau_log)
                print(f"[STARTUP] Finalized {date_str}: {direction} {status} pnl={sign}{pnl_pct:.2f}%")
            except Exception as e:
                print(f"[STARTUP] Could not finalize {date_str}: {e}")
    except Exception as e:
        print(f"[STARTUP] Data fetch error: {e}")

threading.Thread(target=_startup_finalize_open_entries, daemon=True).start()

def _startup_finalize_history():
    """On restart, reconstruct history for all past days missing from _history."""
    global _history
    try:
        if not STRATEGY_CACHE_FILE.exists():
            print("[STARTUP] No strategy cache file — skipping history reconstruction")
            return
        all_caches = json.loads(STRATEGY_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[STARTUP] Could not load strategy cache: {e}")
        return

    bj_today = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d")
    missing = sorted([d for d in all_caches if d < bj_today and d not in _history])
    if not missing:
        return

    print(f"[STARTUP] Reconstructing history for {len(missing)} missing day(s): {missing}")

    # Pre-fetch OHLC for all unique tickers (covers all missing days in one pass)
    # Falls back to 1h data when daily bar is missing for a specific date (e.g. BTC)
    all_tickers = {s.get("ticker") for d in missing for s in all_caches.get(d, []) if s.get("ticker")}
    ticker_frames    = {}   # primary daily frames
    ticker_frames_1h = {}   # 1h fallback frames
    for t in all_tickers:
        for sym in [t] + TICKER_FALLBACKS.get(t, []):
            try:
                df = yf.Ticker(sym).history(period="30d", interval="1d").dropna()
                df.columns = [c.capitalize() for c in df.columns]
                if len(df) > 0:
                    if df.index.tz:
                        df.index = df.index.tz_convert(None)
                    ticker_frames[t] = df
                    break
            except Exception:
                continue
        # Always fetch 1h fallback in case daily bar is absent for a date
        for sym in [t] + TICKER_FALLBACKS.get(t, []):
            try:
                df1h = yf.Ticker(sym).history(period="7d", interval="1h").dropna()
                df1h.columns = [c.capitalize() for c in df1h.columns]
                if len(df1h) > 0:
                    if df1h.index.tz:
                        df1h.index = df1h.index.tz_convert(None)
                    ticker_frames_1h[t] = df1h
                    break
            except Exception:
                continue

    for yesterday in missing:
        strats = all_caches.get(yesterday)
        if not strats:
            continue

        print(f"[STARTUP] Processing {yesterday} ({len(strats)} strategies)")
        pnl_list = []
        for s in strats:
            try:
                symbol    = s["symbol"]
                direction = s["direction"]
                entry_mid = float(s["entry_mid"])
                sl        = float(s["stop_loss"])
                tp        = float(s["take_profit"])
                lots      = float(s.get("recommended_lots", 0.1))
                ticker    = s.get("ticker")

                df = ticker_frames.get(ticker)
                target   = pd.Timestamp(yesterday)
                day_rows = df[df.index.normalize() == target] if df is not None else pd.DataFrame()

                # Fallback to 1h data if daily bar missing for this date
                if len(day_rows) == 0:
                    df1h = ticker_frames_1h.get(ticker)
                    if df1h is not None:
                        day_rows = df1h[df1h.index.normalize() == target]
                        if len(day_rows) > 0:
                            print(f"[STARTUP] {symbol}: using 1h fallback for {yesterday}")

                if len(day_rows) == 0:
                    print(f"[STARTUP] No bar for {yesterday} ({symbol}), skipping")
                    continue

                day_high  = float(day_rows["High"].max())
                day_low   = float(day_rows["Low"].min())
                day_close = float(day_rows["Close"].iloc[-1])

                if direction == "LONG":
                    if day_high >= tp:   status, close_px = "hit_tp",   tp
                    elif day_low <= sl:  status, close_px = "hit_sl",   sl
                    else:               status, close_px = "time_exit", day_close
                    pnl_pct = (close_px - entry_mid) / entry_mid * 100
                else:
                    if day_low <= tp:    status, close_px = "hit_tp",   tp
                    elif day_high >= sl: status, close_px = "hit_sl",   sl
                    else:               status, close_px = "time_exit", day_close
                    pnl_pct = (entry_mid - close_px) / entry_mid * 100

                lv      = LOT_VALUES.get(symbol, 1)
                pnl_usd = round(pnl_pct / 100 * entry_mid * lv * lots)
                sign    = "+" if pnl_pct >= 0 else ""
                pnl_list.append({
                    "symbol":    symbol,
                    "pnl_pct":   f"{sign}{pnl_pct:.2f}%",
                    "pnl_usd":   f"{'+' if pnl_usd >= 0 else ''}${pnl_usd}",
                    "pnl_value": pnl_pct,
                    "status":    status,
                })
                print(f"[STARTUP] History {yesterday} {symbol}: {direction} {status} pnl={sign}{pnl_pct:.2f}%")
            except Exception as e:
                print(f"[STARTUP] History error for {s.get('symbol')}: {e}")

        if pnl_list:
            snapshot_day(yesterday, pnl_list)
            print(f"[STARTUP] History saved for {yesterday}: {len(pnl_list)} positions")

threading.Thread(target=_startup_finalize_history, daemon=True).start()

# ── Background Scheduler ──────────────────────────────────────────────────────
def _scheduler():
    """
    Auto-trigger strategy computation at key Beijing times:
      15:00 BJ → London open (XAUUSD primary entry)
      21:00 BJ → NY open    (XAUUSD backup entry)
      03:30 BJ → Finalise today's XAUUSD P&L
    """
    triggered = set()
    print("[SCHEDULER] started — waiting for 15:00 / 21:00 / 03:30 BJ")
    while True:
        try:
            bj  = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            ds  = bj.strftime('%Y-%m-%d')
            hm  = bj.hour * 60 + bj.minute

            # London 15:00-15:09 BJ
            k_lon = f"{ds}-london"
            if 900 <= hm < 910 and k_lon not in triggered:
                print(f"[AUTO] London open {ds} 15:00")
                try:
                    data = build_strategies()
                    _cache[ds] = data
                    _save_strategy_cache(ds, data)
                    _upsert_xau_today(data, "London")
                except Exception as e:
                    print(f"[AUTO ERROR] London: {e}")
                triggered.add(k_lon)

            # NY 21:00-21:09 BJ
            k_ny = f"{ds}-ny"
            if 1260 <= hm < 1270 and k_ny not in triggered:
                print(f"[AUTO] NY open {ds} 21:00")
                try:
                    ny_sig = build_ny_signal()
                    _ny_cache[ds] = ny_sig
                    _upsert_xau_today([ny_sig], "NY")
                except Exception as e:
                    print(f"[AUTO ERROR] NY: {e}")
                triggered.add(k_ny)

            # Finalise 03:30 BJ
            k_snap = f"{ds}-snap"
            if bj.hour == 3 and bj.minute == 30 and k_snap not in triggered:
                print(f"[AUTO] Finalising {ds} at 03:30")
                _finalise_xau_today()
                # Snapshot all-symbol history for previous day
                prev_ds = (bj - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
                if prev_ds not in _history:
                    threading.Thread(target=_startup_finalize_history, daemon=True).start()
                triggered.add(k_snap)

        except Exception as e:
            print(f"[SCHEDULER ERROR] {e}")
        time.sleep(30)

threading.Thread(target=_scheduler, daemon=True).start()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index(): return send_from_directory('.', 'index.html')

@app.route('/css/<path:f>')
def css(f): return send_from_directory('css', f)

@app.route('/js/<path:f>')
def js(f): return send_from_directory('js', f)

@app.route('/api/strategies', methods=['POST'])
def strategies():
    bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    date_str = bj.strftime('%Y-%m-%d')
    if date_str in _cache:
        return jsonify({"strategies": _cache[date_str], "cached": True, "date": date_str})
    try:
        data = build_strategies()
        _cache[date_str] = data
        _save_strategy_cache(date_str, data)
        return jsonify({"strategies": data, "cached": False, "date": date_str})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/pnl', methods=['POST'])
def pnl():
    bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    date_str = bj.strftime('%Y-%m-%d')
    if date_str not in _cache:
        return jsonify({"error": "no_strategies"}), 404
    try:
        prices  = fetch_current_prices()
        results = [calc_pnl(s, prices.get(s["symbol"])) for s in _cache[date_str]]
        results = [r for r in results if r]

        # Auto-snapshot when past exit time
        if is_past_exit(UNIFIED_EXIT) and date_str not in _history:
            snapshot_day(date_str, results)

        # Track intraday P&L high/low
        active_pnl = sum(_parse_pnl_usd(r.get("pnl_usd", "0"))
                         for r in results if r.get("status") != "pending")
        _update_pnl_range(date_str, active_pnl)
        rng = _today_pnl_range.get(date_str, {})

        return jsonify({
            "pnl":        results,
            "updated_at": bj.strftime('%H:%M:%S'),
            "pnl_range":  {
                "high":    rng.get("high"),
                "low":     rng.get("low"),
                "current": active_pnl,
            },
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/summary', methods=['POST'])
def summary():
    bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    date_str = bj.strftime('%Y-%m-%d')
    if date_str not in _cache:
        return jsonify({"error": "no_strategies"}), 404
    try:
        prices   = fetch_current_prices()
        pnl_list = [calc_pnl(s, prices.get(s["symbol"])) for s in _cache[date_str]]
        pnl_list = [p for p in pnl_list if p]
        wins   = [p for p in pnl_list if p["pnl_value"] > 0]
        losses = [p for p in pnl_list if p["pnl_value"] <= 0]
        avg    = sum(p["pnl_value"] for p in pnl_list) / len(pnl_list) if pnl_list else 0
        best   = max(pnl_list, key=lambda p: p["pnl_value"], default=None)
        worst  = min(pnl_list, key=lambda p: p["pnl_value"], default=None)
        day_done = is_past_exit(UNIFIED_EXIT)
        if day_done and date_str not in _history:
            snapshot_day(date_str, pnl_list)
        return jsonify({
            "date": date_str, "day_done": day_done,
            "total": len(pnl_list), "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins)/len(pnl_list)*100) if pnl_list else 0,
            "avg_pnl": f"{avg:+.2f}%",
            "best":  {"symbol": best["symbol"],  "pnl": best["pnl_pct"]}  if best  else None,
            "worst": {"symbol": worst["symbol"], "pnl": worst["pnl_pct"]} if worst else None,
            "detail": pnl_list,
            "updated_at": bj.strftime('%H:%M:%S'),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/history', methods=['GET'])
def history():
    """Return last 30 days of saved results, newest first."""
    sorted_days = sorted(_history.keys(), reverse=True)[:30]
    return jsonify({"history": [_history[d] for d in sorted_days]})

@app.route('/api/xauusd', methods=['GET'])
def xauusd():
    """XAUUSD focus endpoint — today's signal + 30-day daily log."""
    bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    date_str = bj.strftime('%Y-%m-%d')
    hm = bj.hour * 60 + bj.minute

    # Determine current status
    if hm >= 900 and hm < 945:
        session_now = "London entry window"
    elif hm >= 1260 and hm < 1305:
        session_now = "NY entry window"
    elif bj.hour >= 3 and bj.hour < 9:
        session_now = "Closed (03:00)"
    elif hm >= 945 and hm < 1260:
        session_now = "Waiting for NY 21:00"
    elif hm < 900:
        session_now = "London opens 15:00"
    else:
        session_now = "In position → exit 03:00"

    # Today's cached signals — London from main cache, NY from separate cache
    today_signals = []
    if date_str in _cache:
        today_signals += [s for s in _cache[date_str] if s.get("symbol") == "XAUUSD"]
    if date_str in _ny_cache:
        today_signals.append(_ny_cache[date_str])

    # If still empty (page load before /api/strategies was called), build now
    if not today_signals:
        try:
            strats = build_strategies()
            _cache[date_str] = strats
            today_signals = [s for s in strats if s.get("symbol") == "XAUUSD"]
        except Exception as e:
            print(f"[XAUUSD] on-demand build failed: {e}")

    # Build 30-day log newest first
    log_days = sorted(_xau_log.keys(), reverse=True)[:30]
    log = [_xau_log[d] for d in log_days]

    # Today's log entry
    today_log = _xau_log.get(date_str)

    return jsonify({
        "date":         date_str,
        "session_now":  session_now,
        "bj_time":      bj.strftime('%H:%M:%S'),
        "today":        today_log,
        "signals":      today_signals,
        "log":          log,
    })

@app.route('/api/cache/clear', methods=['POST'])
def clear_cache():
    _cache.clear()
    return jsonify({"ok": True})

# ── TradingView Webhook ────────────────────────────────────────────────────────
_tv_signals = {}   # {symbol: {action, price, score, atr, sl_dist, tp_dist, time}}

@app.route('/api/tv/webhook', methods=['POST'])
def tv_webhook():
    """Receives JSON alerts from TradingView Pine Script strategy."""
    payload = request.get_json(silent=True) or {}
    sym    = str(payload.get("symbol", "")).upper().replace("OANDA:", "").replace("FX:", "")
    action = str(payload.get("action", "")).upper()
    if not sym or action not in ("LONG", "SHORT", "EXIT"):
        return jsonify({"error": "invalid payload"}), 400

    bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    _tv_signals[sym] = {
        "symbol":   sym,
        "action":   action,
        "price":    payload.get("price"),
        "score":    payload.get("score"),
        "atr":      payload.get("atr"),
        "sl_dist":  payload.get("sl_dist"),
        "tp_dist":  payload.get("tp_dist"),
        "tv_time":  payload.get("time"),
        "recv_at":  bj.strftime("%H:%M:%S"),
    }
    print(f"[TV] {action} {sym} @ {payload.get('price')} (score={payload.get('score')})")
    return jsonify({"ok": True})

@app.route('/api/tv/signals', methods=['GET'])
def tv_signals():
    """Return latest TradingView signals received via webhook."""
    return jsonify({"signals": list(_tv_signals.values())})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5051))
    print(f"RICH TRADER  →  http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
