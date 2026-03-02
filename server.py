import os
import json
import datetime
from flask import Flask, jsonify, request, send_from_directory
from openai import OpenAI

app = Flask(__name__, static_folder='.')

ARK_API_KEY = os.environ.get("ARK_API_KEY", "")
_cache = {}  # {date_str: strategies_list}

SYSTEM_PROMPT = """你是一位专业的CFD日内交易分析师，擅长外汇、贵金属、大宗商品、股指CFD和加密货币CFD的技术分析与基本面分析。

交易规则：
- 交易时间：北京时间 9:30-10:30 入场，当日 22:00 前平仓
- 不含A股（T+1不适合日内）
- 品种范围：EUR/USD, GBP/USD, USD/JPY, AUD/USD, 黄金(XAUUSD), 白银(XAGUSD), 原油(USOIL), 天然气(NATGAS), 恒指(HK50), 日经(JP225), 道琼斯(US30), 纳指(NAS100), 德指(DE40), BTC/USD, ETH/USD

你的任务：搜索今日最新市场新闻、技术分析报告和关键价位，为用户生成9个最优CFD日内交易策略。

输出要求：严格输出JSON数组，不含任何其他文字，格式如下：
[
  {
    "symbol": "XAUUSD",
    "display_name": "黄金 (XAUUSD)",
    "direction": "LONG",
    "entry_low": 2650,
    "entry_high": 2660,
    "take_profit": 2700,
    "tp_pct": "+1.9%",
    "stop_loss": 2630,
    "sl_pct": "-0.75%",
    "entry_time": "北京 9:30-10:00",
    "exit_time": "当日 17:00 前",
    "logic": "基于今日XX新闻/技术形态的具体分析，包含关键支撑/压力位",
    "win_rate": 4,
    "rr_ratio": "1:2.5"
  }
]

win_rate为1-5的整数（★颗数）。direction只能是LONG或SHORT。每次搜索当日最新行情，给出具体合理的当前价格位。"""

def get_beijing_time():
    utc_now = datetime.datetime.utcnow()
    beijing_now = utc_now + datetime.timedelta(hours=8)
    return beijing_now

def call_doubao(date_str, time_str):
    client = OpenAI(
        api_key=ARK_API_KEY,
        base_url="https://ark.cn-beijing.volces.com/api/v3"
    )
    user_msg = f"今天是 {date_str}，当前北京时间 {time_str}。请根据你的知识库和最新市场数据，为我生成9个最优CFD日内交易策略，严格输出JSON数组，不含任何其他文字。"
    response = client.chat.completions.create(
        model="doubao-1-5-vision-pro-32k-250115",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg}
        ],
        temperature=0.3,
    )
    content = response.choices[0].message.content.strip()
    # Extract JSON array from response
    start = content.find('[')
    end = content.rfind(']') + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON array found in Doubao response: " + content[:200])
    return json.loads(content[start:end])

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
    if not ARK_API_KEY:
        return jsonify({"error": "ARK_API_KEY not set"}), 500

    beijing_now = get_beijing_time()
    date_str = beijing_now.strftime('%Y-%m-%d')
    time_str = beijing_now.strftime('%H:%M')

    # Return cached strategies for the same day
    if date_str in _cache:
        return jsonify({"strategies": _cache[date_str], "cached": True, "date": date_str})

    try:
        strategies_list = call_doubao(date_str, time_str)
        _cache[date_str] = strategies_list
        return jsonify({"strategies": strategies_list, "cached": False, "date": date_str})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/cache/clear', methods=['POST'])
def clear_cache():
    _cache.clear()
    return jsonify({"ok": True})

if __name__ == '__main__':
    print("RICH TRADER 启动中...")
    print("访问 http://localhost:5051")
    if not ARK_API_KEY:
        print("⚠️  警告: ARK_API_KEY 未设置，请设置环境变量后重启")
    port = int(os.environ.get("PORT", 5051))
    app.run(host='0.0.0.0', port=port, debug=False)
