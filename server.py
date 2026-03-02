import os
import datetime
import numpy as np
import pandas as pd
import yfinance as yf
from flask import Flask, jsonify, send_from_directory

app = Flask(__name__, static_folder='.')
_cache = {}  # {date_str: strategies_list}

# ── 6 Instruments ────────────────────────────────────────────────────────────
# NAS100 uses NQ=F (futures, trades 24h) so Beijing 9:30 data exists
UNIFIED_EXIT = "03:00"   # Beijing time next morning — US session peak + overnight close
RISK_USD     = 200       # USD risk per trade (suitable for $10K–$20K account at 1–2% risk)

# USD value per 1-unit price move per 1 standard lot
LOT_VALUES = {
    "XAUUSD": 100,     # 100 oz/lot → $1 move = $100
    "NAS100": 1,       # CFD $1/pt per lot
    "BTCUSD": 1,       # 1 BTC/lot → $1 move = $1
    "HK50":   1.3,     # HKD 10/pt ≈ USD 1.3/pt per lot
    "EURUSD": 100000,  # 100K EUR/lot → 0.0001 move = $10
    "USOIL":  100,     # 100 barrels/lot → $1 move = $100
}

STRATEGY_CONFIGS = [
    {
        "symbol": "XAUUSD",  "display_name": "Gold",          "ticker": "GC=F",
        "strategy": "ORB",      "win_rate": 4, "rr_ratio": "1:2.0",
        "sl_atr": 1.0, "tp_atr": 2.0, "exit_time": UNIFIED_EXIT,
    },
    {
        "symbol": "NAS100",  "display_name": "Nasdaq 100",     "ticker": "NQ=F",
        "strategy": "Gap & Go", "win_rate": 4, "rr_ratio": "1:2.0",
        "sl_atr": 1.0, "tp_atr": 2.0, "exit_time": UNIFIED_EXIT,
    },
    {
        "symbol": "BTCUSD",  "display_name": "Bitcoin",        "ticker": "BTC-USD",
        "strategy": "Momentum", "win_rate": 3, "rr_ratio": "1:2.5",
        "sl_atr": 1.0, "tp_atr": 2.5, "exit_time": UNIFIED_EXIT,
    },
    {
        "symbol": "EURUSD",  "display_name": "Euro / USD",     "ticker": "EURUSD=X",
        "strategy": "Trend",    "win_rate": 3, "rr_ratio": "1:1.5",
        "sl_atr": 0.8, "tp_atr": 1.2, "exit_time": UNIFIED_EXIT,
    },
    {
        "symbol": "USOIL",   "display_name": "Crude Oil WTI",  "ticker": "CL=F",
        "strategy": "Breakout", "win_rate": 3, "rr_ratio": "1:2.0",
        "sl_atr": 1.0, "tp_atr": 2.0, "exit_time": UNIFIED_EXIT,
    },
]


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


def make_sparkline(close, n=12):
    data = [float(v) for v in close[-n:]]
    lo, hi = min(data), max(data)
    if hi == lo:
        return [50.0] * len(data)
    return [round((v - lo) / (hi - lo) * 100, 1) for v in data]


def get_window_bars(isub):
    """
    Return 5m bars that fall within Beijing 9:30–10:00 today.
    Beijing 9:30 = UTC 01:30 / Beijing 10:00 = UTC 02:00
    """
    try:
        df = isub.copy()
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        else:
            df.index = df.index.tz_convert('UTC')

        today_utc = pd.Timestamp.utcnow().normalize()
        win_start = today_utc + pd.Timedelta(hours=1, minutes=30)
        win_end   = today_utc + pd.Timedelta(hours=2)

        return df[(df.index >= win_start) & (df.index <= win_end)]
    except Exception:
        return pd.DataFrame()


def entry_from_window(isub, atr, current, direction, sl_m, tp_m):
    """
    Derive entry_low / entry_high from Beijing 9:30-10:00 bars.
    Falls back to ±ATR buffer around current price if window unavailable.
    Returns dict with entry_low, entry_high, entry_mid, entry_source.
    """
    window = get_window_bars(isub)

    if len(window) >= 2:
        closes = np.asarray(window['Close'], dtype=float).flatten()
        lo = float(np.min(closes))
        hi = float(np.max(closes))
        mid = float(np.mean(closes))
        src = "09:30–10:00 avg"
    else:
        # Window not yet reached (pre-9:30) or no data: use current price ±buffer
        if direction == "LONG":
            lo  = current - 0.20 * atr
            hi  = current + 0.08 * atr
        else:
            hi  = current + 0.20 * atr
            lo  = current - 0.08 * atr
        mid = (lo + hi) / 2
        src = "pre-window est."

    ref = mid
    entry_low  = fmt_price(lo,  ref)
    entry_high = fmt_price(hi,  ref)
    entry_mid  = fmt_price(mid, ref)

    if direction == "LONG":
        stop_loss   = fmt_price(entry_low  - sl_m * atr, ref)
        take_profit = fmt_price(entry_high + tp_m * atr, ref)
        tp_pct = f"+{(take_profit - entry_high) / entry_high * 100:.2f}%"
        sl_pct = f"-{(entry_low - stop_loss)   / entry_low  * 100:.2f}%"
    else:
        stop_loss   = fmt_price(entry_high + sl_m * atr, ref)
        take_profit = fmt_price(entry_low  - tp_m * atr, ref)
        tp_pct = f"+{(entry_low  - take_profit) / entry_low  * 100:.2f}%"
        sl_pct = f"-{(stop_loss  - entry_high)  / entry_high * 100:.2f}%"

    return {
        "entry_low":    entry_low,
        "entry_high":   entry_high,
        "entry_mid":    entry_mid,
        "stop_loss":    stop_loss,
        "take_profit":  take_profit,
        "tp_pct":       tp_pct,
        "sl_pct":       sl_pct,
        "entry_source": src,
    }


def build_strategies():
    tickers_str = " ".join(c["ticker"] for c in STRATEGY_CONFIGS)

    # Daily (30d) – ATR, EMA, sparkline
    df_daily = yf.download(
        tickers_str, period="30d", interval="1d",
        progress=False, auto_adjust=True, group_by="ticker"
    )

    # 5-min intraday (last 2 days) – real-time price + 9:30-10:00 window
    df_intra = yf.download(
        tickers_str, period="2d", interval="5m",
        progress=False, auto_adjust=True, group_by="ticker"
    )

    n = len(STRATEGY_CONFIGS)
    results = []

    for cfg in STRATEGY_CONFIGS:
        try:
            t = cfg["ticker"]
            dsub = (df_daily[t] if n > 1 else df_daily).dropna()
            isub = (df_intra[t] if n > 1 else df_intra).dropna()

            if len(dsub) < 15:
                raise ValueError(f"daily: {len(dsub)} rows")

            close_d  = np.asarray(dsub["Close"], dtype=float).flatten()
            high_d   = np.asarray(dsub["High"],  dtype=float).flatten()
            low_d    = np.asarray(dsub["Low"],   dtype=float).flatten()

            atr       = calc_atr(high_d, low_d, close_d)
            ema20     = calc_ema(close_d, 20)
            ema50     = calc_ema(close_d, min(50, len(close_d) - 1))
            prev_close = close_d[-2]
            mom5      = (close_d[-1] - close_d[-6]) / close_d[-6]
            high10    = float(np.max(high_d[-10:]))
            low10     = float(np.min(low_d[-10:]))

            # Real-time current price from intraday
            current    = float(np.asarray(isub["Close"], dtype=float).flatten()[-1]) if len(isub) > 0 else close_d[-1]
            today_open = float(np.asarray(isub["Open"],  dtype=float).flatten()[0])  if len(isub) > 0 else close_d[-1]
            gap_pct = (today_open - prev_close) / prev_close

            # Direction signal
            strat = cfg["strategy"]
            if strat in ("ORB", "Open ORB"):
                direction = "LONG" if gap_pct >= 0 else "SHORT"
            elif strat == "Gap & Go":
                direction = "LONG" if gap_pct > 0 else "SHORT"
            elif strat == "Momentum":
                direction = "LONG" if mom5 > 0 else "SHORT"
            elif strat == "Trend":
                direction = "LONG" if ema20 > ema50 else "SHORT"
            else:  # Breakout
                direction = "LONG" if abs(current - high10) <= abs(current - low10) else "SHORT"

            # Entry from 9:30-10:00 window
            levels = entry_from_window(isub, atr, current, direction,
                                       cfg["sl_atr"], cfg["tp_atr"])

            lots     = calc_recommended_lots(cfg["symbol"], levels["entry_mid"], levels["stop_loss"])
            risk_amt = abs(float(levels["entry_mid"]) - float(levels["stop_loss"])) \
                       * LOT_VALUES.get(cfg["symbol"], 1) * lots

            results.append({
                "symbol":            cfg["symbol"],
                "display_name":      cfg["display_name"],
                "strategy":          strat,
                "direction":         direction,
                "entry_low":         levels["entry_low"],
                "entry_high":        levels["entry_high"],
                "entry_mid":         levels["entry_mid"],
                "entry_source":      levels["entry_source"],
                "take_profit":       levels["take_profit"],
                "tp_pct":            levels["tp_pct"],
                "stop_loss":         levels["stop_loss"],
                "sl_pct":            levels["sl_pct"],
                "exit_time":         cfg["exit_time"],
                "win_rate":          cfg["win_rate"],
                "rr_ratio":          cfg["rr_ratio"],
                "atr":               fmt_price(atr, current),
                "ema20":             fmt_price(ema20, current),
                "sparkline":         make_sparkline(close_d),
                "current":           fmt_price(current, current),
                "mom_pct":           f"{gap_pct * 100:+.2f}%",
                "recommended_lots":  lots,
                "risk_usd":          round(risk_amt),
            })

        except Exception as e:
            print(f"[WARN] {cfg['symbol']}: {e}")

    if not results:
        raise RuntimeError("All fetches failed")
    return results


def fetch_current_prices():
    tickers_str = " ".join(c["ticker"] for c in STRATEGY_CONFIGS)
    df = yf.download(
        tickers_str, period="1d", interval="5m",
        progress=False, auto_adjust=True, group_by="ticker"
    )
    n = len(STRATEGY_CONFIGS)
    prices = {}
    for cfg in STRATEGY_CONFIGS:
        try:
            t = cfg["ticker"]
            sub = (df[t] if n > 1 else df).dropna()
            prices[cfg["symbol"]] = float(sub["Close"].iloc[-1])
        except Exception:
            prices[cfg["symbol"]] = None
    return prices


def calc_recommended_lots(symbol, entry_mid, stop_loss):
    """Compute recommended lot size for RISK_USD per trade."""
    sl_dist = abs(float(entry_mid) - float(stop_loss))
    lot_val = LOT_VALUES.get(symbol, 1)
    if sl_dist == 0 or lot_val == 0:
        return 0.10
    raw = RISK_USD / (sl_dist * lot_val)
    # Snap to standard CFD sizes
    for std in [0.01, 0.02, 0.05, 0.10, 0.20, 0.50, 1.0, 2.0, 5.0, 10.0]:
        if raw <= std * 1.6:
            return std
    return 1.0


def is_past_exit(exit_time_str):
    """
    Check if Beijing time has passed exit_time.
    Handles next-day exits (e.g. "03:00"):
      - If exit hour < 9, trigger only when we're past midnight (hour 0–8)
        AND at/past the exit minute — avoids false trigger before trading starts.
    """
    try:
        bj_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        h, m   = map(int, exit_time_str.split(":"))
        cur_min  = bj_now.hour * 60 + bj_now.minute
        exit_min = h * 60 + m
        if h < 9:   # next-day exit (after midnight)
            return bj_now.hour < 9 and cur_min >= exit_min
        else:
            return cur_min >= exit_min
    except Exception:
        return False


def calc_pnl(strategy, current_price):
    if current_price is None:
        return None
    direction   = strategy["direction"]
    entry_mid   = strategy["entry_mid"]
    take_profit = strategy["take_profit"]
    stop_loss   = strategy["stop_loss"]
    exit_time   = strategy.get("exit_time", "22:00")

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

    if hit_tp:
        status = "hit_tp"
    elif hit_sl:
        status = "hit_sl"
    elif is_past_exit(exit_time):
        # Time's up — force close at current price, whatever the P&L
        status = "time_exit"
    elif pnl_pct > 0:
        status = "winning"
    else:
        status = "losing"

    sign = "+" if pnl_pct >= 0 else ""
    return {
        "symbol":        strategy["symbol"],
        "current_price": fmt_price(current_price, current_price),
        "entry_mid":     fmt_price(entry_mid, entry_mid),
        "pnl_pct":       f"{sign}{pnl_pct:.2f}%",
        "pnl_value":     pnl_pct,
        "status":        status,
        "exit_time":     exit_time,
        "progress":      round(min(1.0, max(-0.5, progress)), 3),
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/css/<path:filename>')
def css(filename):
    return send_from_directory('css', filename)

@app.route('/js/<path:filename>')
def js(filename):
    return send_from_directory('js', filename)

@app.route('/api/strategies', methods=['POST'])
def strategies():
    utc_now  = datetime.datetime.utcnow()
    bj_now   = utc_now + datetime.timedelta(hours=8)
    date_str = bj_now.strftime('%Y-%m-%d')

    if date_str in _cache:
        return jsonify({"strategies": _cache[date_str], "cached": True, "date": date_str})

    try:
        data = build_strategies()
        _cache[date_str] = data
        return jsonify({"strategies": data, "cached": False, "date": date_str})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/pnl', methods=['POST'])
def pnl():
    utc_now  = datetime.datetime.utcnow()
    bj_now   = utc_now + datetime.timedelta(hours=8)
    date_str = bj_now.strftime('%Y-%m-%d')

    if date_str not in _cache:
        return jsonify({"error": "no_strategies"}), 404

    try:
        prices  = fetch_current_prices()
        results = [calc_pnl(s, prices.get(s["symbol"])) for s in _cache[date_str]]
        return jsonify({"pnl": [r for r in results if r],
                        "updated_at": bj_now.strftime('%H:%M:%S')})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/summary', methods=['POST'])
def summary():
    """Day-end summary: wins, losses, best/worst trade, net P&L."""
    utc_now  = datetime.datetime.utcnow()
    bj_now   = utc_now + datetime.timedelta(hours=8)
    date_str = bj_now.strftime('%Y-%m-%d')

    if date_str not in _cache:
        return jsonify({"error": "no_strategies"}), 404

    try:
        prices  = fetch_current_prices()
        pnl_list = [calc_pnl(s, prices.get(s["symbol"])) for s in _cache[date_str]]
        pnl_list = [p for p in pnl_list if p]

        wins   = [p for p in pnl_list if p["status"] in ("hit_tp", "winning", "time_exit") and p["pnl_value"] > 0]
        losses = [p for p in pnl_list if p["status"] in ("hit_sl", "losing",  "time_exit") and p["pnl_value"] <= 0]

        best  = max(pnl_list, key=lambda p: p["pnl_value"],  default=None)
        worst = min(pnl_list, key=lambda p: p["pnl_value"],  default=None)
        avg   = sum(p["pnl_value"] for p in pnl_list) / len(pnl_list) if pnl_list else 0

        day_done = is_past_exit(UNIFIED_EXIT)

        return jsonify({
            "date":       date_str,
            "day_done":   day_done,
            "total":      len(pnl_list),
            "wins":       len(wins),
            "losses":     len(losses),
            "win_rate":   round(len(wins) / len(pnl_list) * 100) if pnl_list else 0,
            "avg_pnl":    f"{avg:+.2f}%",
            "best":       {"symbol": best["symbol"],  "pnl": best["pnl_pct"]}  if best  else None,
            "worst":      {"symbol": worst["symbol"], "pnl": worst["pnl_pct"]} if worst else None,
            "detail":     pnl_list,
            "updated_at": bj_now.strftime('%H:%M:%S'),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/cache/clear', methods=['POST'])
def clear_cache():
    _cache.clear()
    return jsonify({"ok": True})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5051))
    print(f"RICH TRADER  →  http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
