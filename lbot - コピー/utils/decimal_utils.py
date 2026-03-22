from decimal import Decimal, ROUND_DOWN
from typing import Union

def quantize_ltc(value: Union[int, float, str, Decimal]) -> Decimal:
    """
    数値を Decimal に変換し、LTCの最小単位（satoshi, 小数点以下8桁）で切り捨てる共通関数。
    """
    if isinstance(value, float):
        value = str(value)
    
    return Decimal(str(value)).quantize(Decimal('0.00000000'), rounding=ROUND_DOWN)
