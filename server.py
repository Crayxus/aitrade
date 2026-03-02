import os
import datetime
import numpy as np
import yfinance as yf
from flask import Flask, jsonify, send_from_directory

app = Flask(__name__, static_folder='.')
_cache = {}  # {date_str: strategies_list}

# ── 6 Instruments ────────────────────────────────────────────────────────────
STRATEGY_CONFIGS = [
    {
        "symbol": "XAUUSD",  "display_name": "Gold",          "ticker": "GC=F",
        "strategy": "ORB",      "win_rate": 4, "rr_ratio": "1:2.0",
        "sl_atr": 1.0, "tp_atr": 2.0, "exit_time": "20:00",
    },
    {
        "symbol": "NAS100",  "display_name": "Nasdaq 100",     "ticker": "^NDX",
        "strategy": "Gap & Go", "win_rate": 4, "rr_ratio": "1:2.0",
        "sl_atr": 1.0, "tp_atr": 2.0, "exit_time": "22:00",
    },
    {
        "symbol": "BTCUSD",  "display_name": "Bitcoin",        "ticker": "BTC-USD",
        "strategy": "Momentum", "win_rate": 3, "rr_ratio": "1:2.5",
        "sl_atr": 1.0, "tp_atr": 2.5, "exit_time": "22:00",
    },
    {
        "symbol": "HK50",    "display_name": "Hang Seng",      "ticker": "^HSI",
        "strategy": "Open ORB", "win_rate": 4, "rr_ratio": "1:2.0",
        "sl_atr": 1.0, "tp_atr": 2.0, "exit_time": "15:00",
    },
    {
        "symbol": "EURUSD",  "display_name": "Euro / USD",     "ticker": "EURUSD=X",
        "strategy": "Trend",    "win_rate": 3, "rr_ratio": "1:1.5",
        "sl_atr": 0.8, "tp_atr": 1.2, "exit_time": "15:00",
    },
    {
        "symbol": "USOIL",   "display_name": "Crude Oil WTI",  "ticker": "CL=F",
        "strategy": "Breakout", "win_rate": 3, "rr_ratio": "1:2.0",
        "sl_atr": 1.0, "tp_atr": 2.0, "exit_time": "21:30",
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


def build_strategies():
    """
    Fetch daily + intraday data for all instruments.
    Daily (30d): ATR, EMA, sparkline, gap signal.
    Intraday 5m (2d): today's open, real-time current price, ORB high/low.
    Direction logic per strategy type:
      ORB / Open ORB  → direction = side of intraday open vs prev close (gap direction)
      Gap & Go        → direction = gap direction (today open vs yesterday close)
      Momentum        → direction = 5-day momentum sign
      Trend           → direction = EMA20 vs EMA50
      Breakout        → direction = close vs 10-day high/low
    Entry is anchored to CURRENT intraday price (not yesterday close).
    """
    tickers_str = " ".join(c["ticker"] for c in STRATEGY_CONFIGS)

    # ── Daily data (30d) ──
    df_daily = yf.download(
        tickers_str, period="30d", interval="1d",
        progress=False, auto_adjust=True, group_by="ticker"
    )

    # ── Intraday 5-min (last 2 days) for real-time price + today's open ──
    df_intra = yf.download(
        tickers_str, period="2d", interval="5m",
        progress=False, auto_adjust=True, group_by="ticker"
    )

    results = []
    for cfg in STRATEGY_CONFIGS:
        try:
            t = cfg["ticker"]
            n = len(STRATEGY_CONFIGS)

            # Daily series
            dsub = (df_daily[t] if n > 1 else df_daily).dropna()
            if len(dsub) < 15:
                raise ValueError(f"daily: only {len(dsub)} rows")

            close_d = dsub["Close"].values.astype(float)
            high_d  = dsub["High"].values.astype(float)
            low_d   = dsub["Low"].values.astype(float)

            atr    = calc_atr(high_d, low_d, close_d)
            ema20  = calc_ema(close_d, 20)
            ema50  = calc_ema(close_d, min(50, len(close_d) - 1))
            prev_close = close_d[-2]   # yesterday close
            last_close = close_d[-1]   # last completed daily bar

            high10 = float(np.max(high_d[-10:]))
            low10  = float(np.min(low_d[-10:]))
            mom5   = (last_close - close_d[-6]) / close_d[-6]

            # Intraday series → real-time price & today open
            isub = (df_intra[t] if n > 1 else df_intra).dropna()
            if len(isub) >= 2:
                current = float(isub["Close"].iloc[-1])
                # Today's open = first bar of today (UTC date matching Beijing date)
                today_open = float(isub["Open"].iloc[0])
            else:
                current    = last_close
                today_open = last_close

            # ── Direction signal per strategy ──
            strat = cfg["strategy"]
            gap_pct = (today_open - prev_close) / prev_close

            if strat in ("ORB", "Open ORB"):
                # Trade in the direction of the gap / opening thrust
                direction = "LONG" if gap_pct >= 0 else "SHORT"
            elif strat == "Gap & Go":
                # Gap > 0.1% is significant; follow gap direction
                if abs(gap_pct) >= 0.001:
                    direction = "LONG" if gap_pct > 0 else "SHORT"
                else:
                    direction = "LONG" if current > ema20 else "SHORT"
            elif strat == "Momentum":
                direction = "LONG" if mom5 > 0 else "SHORT"
            elif strat == "Trend":
                direction = "LONG" if ema20 > ema50 else "SHORT"
            else:  # Breakout
                # Price near 10-day high → breakout long; near low → breakout short
                dist_high = abs(current - high10)
                dist_low  = abs(current - low10)
                direction = "LONG" if dist_high <= dist_low else "SHORT"

            # ── Levels anchored to CURRENT intraday price ──
            sl_m, tp_m = cfg["sl_atr"], cfg["tp_atr"]

            if direction == "LONG":
                entry_low   = fmt_price(current - 0.20 * atr, current)
                entry_high  = fmt_price(current + 0.08 * atr, current)
                stop_loss   = fmt_price(entry_low  - sl_m * atr, current)
                take_profit = fmt_price(entry_high + tp_m * atr, current)
                tp_pct = f"+{(take_profit - entry_high) / entry_high * 100:.2f}%"
                sl_pct = f"-{(entry_low   - stop_loss)  / entry_low  * 100:.2f}%"
            else:
                entry_high  = fmt_price(current + 0.20 * atr, current)
                entry_low   = fmt_price(current - 0.08 * atr, current)
                stop_loss   = fmt_price(entry_high + sl_m * atr, current)
                take_profit = fmt_price(entry_low  - tp_m * atr, current)
                tp_pct = f"+{(entry_low  - take_profit) / entry_low  * 100:.2f}%"
                sl_pct = f"-{(stop_loss  - entry_high)  / entry_high * 100:.2f}%"

            # mom_pct shown in ticker tape
            mom_pct = f"{gap_pct * 100:+.2f}%"  # show today's gap as daily change

            results.append({
                "symbol":       cfg["symbol"],
                "display_name": cfg["display_name"],
                "strategy":     strat,
                "direction":    direction,
                "entry_low":    entry_low,
                "entry_high":   entry_high,
                "take_profit":  take_profit,
                "tp_pct":       tp_pct,
                "stop_loss":    stop_loss,
                "sl_pct":       sl_pct,
                "exit_time":    cfg["exit_time"],
                "win_rate":     cfg["win_rate"],
                "rr_ratio":     cfg["rr_ratio"],
                "atr":          fmt_price(atr, current),
                "ema20":        fmt_price(ema20, current),
                "sparkline":    make_sparkline(close_d),
                "current":      fmt_price(current, current),
                "mom_pct":      mom_pct,
                "gap_pct":      round(gap_pct * 100, 3),
            })
        except Exception as e:
            print(f"[WARN] {cfg['symbol']}: {e}")

    if not results:
        raise RuntimeError("All strategy fetches failed")
    return results


def fetch_current_prices():
    """Fetch real-time prices using 5-min intraday data."""
    tickers_str = " ".join(c["ticker"] for c in STRATEGY_CONFIGS)
    df = yf.download(
        tickers_str, period="1d", interval="5m",
        progress=False, auto_adjust=True, group_by="ticker"
    )
    prices = {}
    n = len(STRATEGY_CONFIGS)
    for cfg in STRATEGY_CONFIGS:
        try:
            t = cfg["ticker"]
            sub = (df[t] if n > 1 else df).dropna()
            prices[cfg["symbol"]] = float(sub["Close"].iloc[-1])
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
        hit_tp, hit_sl = current_price >= take_profit, current_price <= stop_loss
    else:
        pnl_pct  = (entry_mid - current_price) / entry_mid * 100
        tp_dist  = entry_mid - take_profit
        progress = (entry_mid - current_price) / tp_dist if tp_dist else 0
        hit_tp, hit_sl = current_price <= take_profit, current_price >= stop_loss

    if hit_tp:        status = "hit_tp"
    elif hit_sl:      status = "hit_sl"
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
        return jsonify({"pnl": [r for r in results if r],
                        "updated_at": bj_now.strftime('%H:%M:%S')})
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
