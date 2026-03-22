import aiohttp
import os
import asyncio
import logging

logger = logging.getLogger(__name__)

MARKET_PRICE_API = os.getenv("MARKET_PRICE_API", "https://api.kraken.com/0/public/Ticker?pair=LTCJPY")

# キャッシュ用変数
_cached_price: float = 0.0
_last_update: float = 0.0

async def fetch_ltc_jpy_price() -> float:
    """Kraken APIから最新のLTC/JPY価格を取得しキャッシュする"""
    global _cached_price, _last_update
    current_time = asyncio.get_event_loop().time()
    
    # 30秒以内の場合はキャッシュを返す
    if current_time - _last_update < 30 and _cached_price > 0:
        return _cached_price

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(MARKET_PRICE_API) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = data.get('result', {})
                    for pair_key, pair_data in result.items():
                        price_str = pair_data.get('c', [0])[0]
                        _cached_price = float(price_str)
                        _last_update = current_time
                        return _cached_price
    except Exception as e:
        logger.error(f"価格取得エラー: {e}")
    
    return _cached_price

async def calculate_ltc_amount(jpy_amount: int, margin_percent: float) -> float:
    """指定されたJPY金額とマージン(%)からLTCの数量を計算する"""
    market_price = await fetch_ltc_jpy_price()
    if market_price <= 0:
        raise ValueError("市場価格が取得できません")
    
    applied_price = market_price * (margin_percent / 100.0)
    return jpy_amount / applied_price
