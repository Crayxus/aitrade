import os
import datetime
import numpy as np
import yfinance as yf
from flask import Flask, jsonify, send_from_directory

app = Flask(__name__, static_folder='.')
_cache = {}

# ── 6 Best Intraday Strategies ───────────────────────────────────────────────
STRATEGY_CONFIGS = [
    {
        "symbol": "XAUUSD", "display_name": "Gold", "ticker": "GC=F",
        "strategy": "ORB", "entry_time": "09:30–10:00", "exit_time": "20:00",
        "win_rate": 4, "rr_ratio": "1:2.0", "sl_atr": 1.0, "tp_atr": 2.0,
    },
    {
        "symbol": "NAS100", "display_name": "Nasdaq 100", "ticker": "^IXIC",
        "strategy": "Gap & Go", "entry_time": "09:30–10:00", "exit_time": "22:00",
        "win_rate": 4, "rr_ratio": "1:2.0", "sl_atr": 1.0, "tp_atr": 2.0,
    },
    {
        "symbol": "BTCUSD", "display_name": "Bitcoin", "ticker": "BTC-USD",
        "strategy": "Momentum", "entry_time": "09:30–10:30", "exit_time": "22:00",
        "win_rate": 3, "rr_ratio": "1:2.5", "sl_atr": 1.0, "tp_atr": 2.5,
    },
    {
        "symbol": "HK50", "display_name": "Hang Seng", "ticker": "^HSI",
        "strategy": "Open ORB", "entry_time": "09:30–10:30", "exit_time": "15:00",
        "win_rate": 4, "rr_ratio": "1:2.0", "sl_atr": 1.0, "tp_atr": 2.0,
    },
    {
        "symbol": "EURUSD", "display_name": "Euro / USD", "ticker": "EURUSD=X",
        "strategy": "Range Fade", "entry_time": "09:30–10:30", "exit_time": "15:00",
        "win_rate": 3, "rr_ratio": "1:1.5", "sl_atr": 0.8, "tp_atr": 1.2,
    },
    {
        "symbol": "USOIL", "display_name": "Crude Oil WTI", "ticker": "CL=F",
        "strategy": "ATR Breakout", "entry_time": "09:30–10:30", "exit_time": "21:30",
        "win_rate": 3, "rr_ratio": "1:2.0", "sl_atr": 1.0, "tp_atr": 2.0,
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


def calc_ema(prices, period=20):
    alpha = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = alpha * float(p) + (1 - alpha) * ema
    return float(ema)


def make_sparkline(close, n=12):
    """Return list of 0–100 normalized values for the last n closes."""
    data = close[-n:].tolist()
    lo, hi = min(data), max(data)
    if hi == lo:
        return [50.0] * len(data)
    return [round((v - lo) / (hi - lo) * 100, 1) for v in data]


def build_strategies():
    tickers = " ".join(cfg["ticker"] for cfg in STRATEGY_CONFIGS)
    df_all = yf.download(
        tickers, period="30d", interval="1d",
        progress=False, auto_adjust=True, group_by="ticker"
    )

    results = []
    for config in STRATEGY_CONFIGS:
        try:
            t = config["ticker"]
            sub = df_all[t].dropna() if len(STRATEGY_CONFIGS) > 1 else df_all.dropna()
            if len(sub) < 10:
                raise ValueError(f"only {len(sub)} rows")

            close = sub['Close'].values.astype(float)
            high  = sub['High'].values.astype(float)
            low   = sub['Low'].values.astype(float)
            current = close[-1]

            atr   = calc_atr(high, low, close)
            ema20 = calc_ema(close, 20)
            ema50 = calc_ema(close, min(50, len(close) - 1))
            mom   = (close[-1] - close[-6]) / close[-6]

            bullish   = sum([current > ema20, current > ema50, mom > 0])
            direction = "LONG" if bullish >= 2 else "SHORT"

            sl_m, tp_m = config["sl_atr"], config["tp_atr"]

            if direction == "LONG":
                entry_low   = fmt_price(current - 0.25 * atr, current)
                entry_high  = fmt_price(current + 0.10 * atr, current)
                stop_loss   = fmt_price(entry_low  - sl_m * atr, current)
                take_profit = fmt_price(entry_high + tp_m * atr, current)
                tp_pct = f"+{(take_profit - entry_high) / entry_high * 100:.2f}%"
                sl_pct = f"-{(entry_low - stop_loss) / entry_low * 100:.2f}%"
            else:
                entry_high  = fmt_price(current + 0.25 * atr, current)
                entry_low   = fmt_price(current - 0.10 * atr, current)
                stop_loss   = fmt_price(entry_high + sl_m * atr, current)
                take_profit = fmt_price(entry_low  - tp_m * atr, current)
                tp_pct = f"+{(entry_low - take_profit) / entry_low * 100:.2f}%"
                sl_pct = f"-{(stop_loss - entry_high) / entry_high * 100:.2f}%"

            results.append({
                "symbol":       config["symbol"],
                "display_name": config["display_name"],
                "strategy":     config["strategy"],
                "direction":    direction,
                "entry_low":    entry_low,
                "entry_high":   entry_high,
                "take_profit":  take_profit,
                "tp_pct":       tp_pct,
                "stop_loss":    stop_loss,
                "sl_pct":       sl_pct,
                "entry_time":   config["entry_time"],
                "exit_time":    config["exit_time"],
                "win_rate":     config["win_rate"],
                "rr_ratio":     config["rr_ratio"],
                "atr":          fmt_price(atr, current),
                "ema20":        fmt_price(ema20, current),
                "sparkline":    make_sparkline(close),
                "current":      fmt_price(current, current),
                "mom_pct":      f"{mom * 100:+.2f}%",
            })
        except Exception as e:
            print(f"[WARN] {config['symbol']}: {e}")

    if not results:
        raise RuntimeError("All fetches failed")
    return results


def fetch_current_prices():
    tickers = " ".join(cfg["ticker"] for cfg in STRATEGY_CONFIGS)
    df = yf.download(
        tickers, period="1d", interval="2m",
        progress=False, auto_adjust=True, group_by="ticker"
    )
    prices = {}
    for cfg in STRATEGY_CONFIGS:
        try:
            t = cfg["ticker"]
            sub = df[t].dropna() if len(STRATEGY_CONFIGS) > 1 else df.dropna()
            prices[cfg["symbol"]] = float(sub['Close'].iloc[-1])
        except Exception:
            prices[cfg["symbol"]] = None
    return prices


def calc_pnl(strategy, current_price):
    if current_price is None:
        return None
    direction   = strategy["direction"]
    entry_mid   = (strategy["entry_low"] + strategy["entry_high"]) / 2
    take_profit = strategy["take_profit"]
    stop_loss   = strategy["stop_loss"]

    if direction == "LONG":
        pnl_pct  = (current_price - entry_mid) / entry_mid * 100
        tp_dist  = take_profit - entry_mid
        progress = (current_price - entry_mid) / tp_dist if tp_dist else 0
        hit_tp   = current_price >= take_profit
        hit_sl   = current_price <= stop_loss
    else:
        pnl_pct  = (entry_mid - current_price) / entry_mid * 100
        tp_dist  = entry_mid - take_profit
        progress = (entry_mid - current_price) / tp_dist if tp_dist else 0
        hit_tp   = current_price <= take_profit
        hit_sl   = current_price >= stop_loss

    if hit_tp:   status = "hit_tp"
    elif hit_sl: status = "hit_sl"
    elif pnl_pct > 0: status = "winning"
    else:             status = "losing"

    sign = "+" if pnl_pct >= 0 else ""
    return {
        "symbol":        strategy["symbol"],
        "current_price": fmt_price(current_price, current_price),
        "entry_mid":     fmt_price(entry_mid, entry_mid),
        "pnl_pct":       f"{sign}{pnl_pct:.2f}%",
        "pnl_value":     pnl_pct,
        "status":        status,
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
        results = [r for r in results if r]
        return jsonify({"pnl": results, "updated_at": bj_now.strftime('%H:%M:%S')})
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
