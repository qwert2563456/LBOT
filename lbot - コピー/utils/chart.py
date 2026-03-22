"""
LTC/JPY Chart Module
CoinGecko APIから過去データを取得してチャートを描画する
（メモリ/SSD保存不要 — APIから取得＋画像キャッシュで省エネ）
"""
import io
import time
import datetime
import aiohttp
from typing import Optional

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker
import matplotlib.font_manager as fm

# ── 日本語フォント設定 ──
_jp_font = None
for name in ['MS Gothic', 'Meiryo', 'Yu Gothic', 'Malgun Gothic']:
    if any(f.name == name for f in fm.fontManager.ttflist):
        _jp_font = name
        break

if _jp_font:
    plt.rcParams['font.family'] = _jp_font
    plt.rcParams['axes.unicode_minus'] = False


# ── 期間プリセット ──
# days: CoinGecko API用, cache_ttl: キャッシュ保持秒数
TIMEFRAMES = {
    "live":  {"days": "1",   "label": "Live (24h)", "cache_ttl": 60},
    "1h":    {"days": "1",   "label": "1 Day",      "cache_ttl": 600},
    "6h":    {"days": "7",   "label": "7 Days",     "cache_ttl": 1800},
    "1d":    {"days": "14",  "label": "14 Days",    "cache_ttl": 3600},
    "7d":    {"days": "30",  "label": "30 Days",    "cache_ttl": 21600},
}

# ── チャート画像キャッシュ ──
_chart_cache: dict[str, tuple[float, bytes]] = {}


async def fetch_coingecko_data(days: str = "1") -> list[tuple[datetime.datetime, float]]:
    """
    CoinGecko APIからLTC/JPY価格データを取得
    Returns: [(datetime, price), ...]
    """
    url = f"https://api.coingecko.com/api/v3/coins/litecoin/market_chart?vs_currency=jpy&days={days}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                prices = data.get("prices", [])
                points = []
                for ts_ms, price in prices:
                    dt = datetime.datetime.fromtimestamp(ts_ms / 1000)
                    points.append((dt, float(price)))
                return points
    except Exception:
        pass
    return []


def generate_chart_sync(data: list[tuple[datetime.datetime, float]], tf_label: str) -> Optional[io.BytesIO]:
    """価格データからチャート画像を生成する（同期関数）"""
    if len(data) < 2:
        return None

    times = [d[0] for d in data]
    prices = [d[1] for d in data]

    current = prices[-1]
    highest = max(prices)
    lowest = min(prices)
    change = prices[-1] - prices[0]
    change_pct = (change / prices[0]) * 100 if prices[0] > 0 else 0

    # ── 描画設定 ──
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 5), dpi=100)

    fig.patch.set_facecolor('#1a1a2e')
    ax.set_facecolor('#16213e')

    # メインライン
    line_color = '#00ff88' if change >= 0 else '#ff4757'
    ax.plot(times, prices, color=line_color, linewidth=2, zorder=3)

    # グラデーション塗り
    ax.fill_between(times, prices, min(prices) * 0.999,
                    alpha=0.15, color=line_color, zorder=2)

    # 現在値マーカー
    ax.scatter([times[-1]], [prices[-1]], color=line_color, s=60, zorder=5,
               edgecolors='white', linewidths=1.5)

    # ── 軸設定 ──
    days_val = int(TIMEFRAMES.get(tf_label, {}).get("days", "1") if isinstance(tf_label, str) else 1)
    if days_val >= 14:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    elif days_val >= 7:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'{x:,.0f}'))
    ax.tick_params(colors='#888888', labelsize=9)
    ax.grid(True, alpha=0.15, color='#444444', linestyle='--')

    for spine in ax.spines.values():
        spine.set_color('#333333')

    # ── タイトル・情報 ──
    arrow = '+' if change >= 0 else ''
    title_color = '#00ff88' if change >= 0 else '#ff4757'

    fig.text(0.12, 0.95, 'LTC / JPY', fontsize=18, fontweight='bold',
             color='white', va='top')
    fig.text(0.12, 0.88,
             f'{current:,.2f} JPY  {arrow}{change:,.2f} ({arrow}{change_pct:.2f}%)',
             fontsize=13, color=title_color, va='top')

    stats_text = f'H: {highest:,.2f}  |  L: {lowest:,.2f}  |  {tf_label}'
    fig.text(0.88, 0.95, stats_text, fontsize=9, color='#888888',
             va='top', ha='right')

    plt.tight_layout(rect=[0, 0, 1, 0.85])

    # ── 画像出力 ──
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight',
                facecolor=fig.get_facecolor(), edgecolor='none')
    plt.close(fig)
    buf.seek(0)
    return buf


async def generate_chart(timeframe: str = "1h") -> Optional[io.BytesIO]:
    """指定期間のチャート画像を生成（キャッシュあり）"""
    tf = TIMEFRAMES.get(timeframe, TIMEFRAMES["1h"])
    cache_ttl = tf["cache_ttl"]

    # キャッシュチェック
    if timeframe in _chart_cache:
        cached_time, cached_bytes = _chart_cache[timeframe]
        if time.time() - cached_time < cache_ttl:
            return io.BytesIO(cached_bytes)

    # APIからデータ取得して描画
    data = await fetch_coingecko_data(days=tf["days"])
    buf = generate_chart_sync(data, tf["label"])

    # キャッシュに保存
    if buf:
        _chart_cache[timeframe] = (time.time(), buf.getvalue())
        buf.seek(0)

    return buf
