import asyncio
from dotenv import load_dotenv
from utils.electrum import call_electrum_rpc
from database import AsyncSessionLocal
from sqlalchemy import select
from models import Transaction

load_dotenv()

async def main():
    unspent = await call_electrum_rpc('listunspent')
    print('--- listunspent ---')
    if isinstance(unspent, list):
        for u in unspent:
            print(f'addr: {u.get("address")}, txid: {u.get("tx_hash")[:10]}..., height: {u.get("height")}, value: {u.get("value")}')
    print('--- transactions ---')
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(Transaction).where(Transaction.tx_type == 'DEPOSIT').order_by(Transaction.id.desc()).limit(3))
        for tx in res.scalars():
            print(f'txid={tx.txid[:10]}..., conf={tx.confirmations}, user={tx.user_id}, amt={tx.amount_ltc}')
asyncio.run(main())
