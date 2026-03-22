import asyncio
import os
from dotenv import load_dotenv
load_dotenv()
import database
from models import Transaction, User
from sqlalchemy import select
from utils.electrum import call_electrum_rpc

async def main():
    unspent_data = await call_electrum_rpc('listunspent')
    with open('utxo_debug.txt', 'w', encoding='utf-8') as f:
        f.write('--- UTXOs ---\n')
        if isinstance(unspent_data, list):
            for u in unspent_data:
                f.write(f'addr={u.get("address")}, txid={str(u.get("tx_hash"))[:10]}, h={u.get("height")}\n')
        
        f.write('\n--- DB TX ---\n')
        async with database.AsyncSessionLocal() as session:
            res = await session.execute(select(Transaction).where(Transaction.tx_type == 'DEPOSIT').order_by(Transaction.id.desc()).limit(10))
            for tx in res.scalars():
                f.write(f'txid={tx.txid[:10]}, conf={tx.confirmations}, u={tx.user_id}\n')
            f.write('\n--- Users ---\n')
            res = await session.execute(select(User).where(User.unconfirmed_balance > 0))
            for u in res.scalars():
                f.write(f'id={u.discord_id}, unconf={u.unconfirmed_balance}, avail={u.available_balance}, addr={u.deposit_address}\n')

asyncio.run(main())
