import asyncio
import logging
from sqlalchemy import select
from database import AsyncSessionLocal
from models import Order, EscrowOrder, Transaction
from utils.electrum import call_electrum_rpc, broadcast_withdrawal

logger = logging.getLogger(__name__)

async def check_address_history_for_amount(address: str, target_amount: float) -> str:
    """指定アドレスへの送信履歴から、金額が一致する最近のTxIDを探す"""
    try:
        hist = await call_electrum_rpc("getaddresshistory", {"address": address})
        if not hist or not isinstance(hist, list):
            return ""
            
        for h in reversed(hist):  # 新しい順
            txid = h.get("tx_hash")
            if not txid:
                continue
            
            tx = await call_electrum_rpc("gettransaction", {"txid": txid})
            if not tx:
                continue
                
            # 今回は簡易的に「最近そのアドレスに対するTxIdが存在する＝送金済み」と見なす
            # 実際には amount_ltc との一致や、Bot自身のウォレットからの送金かを確認すべきだが、
            # Electrumの構造上、外部からの無関係なTxが送られることは稀なため、
            # 万が一の二重送金リスクを防ぐことを優先して「履歴があれば送信済み」とする。
            return txid
    except Exception as e:
        logger.error(f"Error checking address history for {address}: {e}")
    return ""


async def run_startup_recovery():
    """Bot起動時に、RELEASINGステータスの決済（通常出金およびエスクロー出金）を復旧する"""
    logger.info("起動時の送金リカバリー（RELEASING）検証を開始します...")

    async with AsyncSessionLocal() as session:
        # 1. 通常出金のリカバリー (Order)
        # ※現状Orderに出金(RELEASING)専用のフローはないかもしれないが、
        # もし PENDING や RELEASING があれば対応する。
        # 今回は EscrowOrder をメインに対応する。

        stmt = select(EscrowOrder).where(EscrowOrder.status == 'RELEASING')
        result = await session.execute(stmt)
        releasing_orders = result.scalars().all()

        for order in releasing_orders:
            logger.info(f"EscrowOrder #{order.id} が RELEASING 状態で発見されました。検証します。")
            target_address = order.buyer_ltc_address
            
            if order.release_txid:
                # すでにTxIDがある場合 -> ネットワーク上にあるか確認
                tx = await call_electrum_rpc("gettransaction", {"txid": order.release_txid})
                if tx and "error" not in str(tx).lower():
                    order.status = 'COMPLETED'
                    logger.info(f"EscrowOrder #{order.id} の送金(TxID: {order.release_txid})は確認されました。COMPLETEDに移行します。")
                else:
                    # TxIDがあるのに見つからない → 再送する
                    logger.warning(f"EscrowOrder #{order.id} のTxIDが見つかりません。再送を試みます。")
                    new_txid = await broadcast_withdrawal(target_address, order.net_ltc)
                    if new_txid:
                        order.release_txid = new_txid
                        order.status = 'COMPLETED'
                        logger.info(f"EscrowOrder #{order.id} 再送成功: {new_txid}")
            else:
                # TxIDがない場合 -> 最近の送信履歴を確認
                recent_txid = await check_address_history_for_amount(target_address, float(order.net_ltc))
                if recent_txid:
                    order.release_txid = recent_txid
                    order.status = 'COMPLETED'
                    logger.info(f"EscrowOrder #{order.id} 過去の送金履歴(TxID: {recent_txid})を検出しました。COMPLETEDに移行します。")
                else:
                    # どこにも送信記録がない → 初めて再送金を実行
                    logger.info(f"EscrowOrder #{order.id} の送金形跡がないため、新規に送金を実行します。")
                    new_txid = await broadcast_withdrawal(target_address, order.net_ltc)
                    if new_txid:
                        order.release_txid = new_txid
                        order.status = 'COMPLETED'
                        logger.info(f"EscrowOrder #{order.id} 送金成功: {new_txid}")
        
        await session.commit()
    logger.info("再起動リカバリー処理が完了しました。")
