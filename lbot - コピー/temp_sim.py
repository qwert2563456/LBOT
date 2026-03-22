import asyncio
import os
from dotenv import load_dotenv
load_dotenv()
import database
from models import Transaction, User
from sqlalchemy import select
from utils.electrum import call_electrum_rpc, fetch_network_height
from decimal import Decimal
from utils.decimal_utils import quantize_ltc

async def main():
    print('Simulating monitor_deposits_loop')
    unspent_data = await call_electrum_rpc('listunspent')
    async with database.AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.deposit_address.isnot(None)))
        address_to_user = {u.deposit_address: u for u in result.scalars()}
        sample_address = next(iter(address_to_user.keys()), None)
        network_height = await fetch_network_height(sample_address) if sample_address else 0
        print(f'Network height: {network_height}')
        
        for utxo in unspent_data:
            address = utxo.get('address')
            txid = utxo.get('tx_hash')
            tx_height = utxo.get('height', 0)
            value_str = utxo.get('value')
            print(f'\\nUTXO: addr={address}, txid={txid[:10]}..., height={tx_height}, val={value_str}')
            matched_user = address_to_user.get(address)
            if not matched_user:
                print('  -> No matched user for address')
                continue
            val_ltc = quantize_ltc(Decimal(str(value_str)))
            confirmations = max(1, network_height - tx_height + 1) if tx_height > 0 else 0
            if tx_height > 0 and network_height <= 0:
                confirmations = 1
            print(f'  -> User matching: {matched_user.discord_id}, calc_conf={confirmations}')
            
            stmt = select(Transaction).where(Transaction.txid == txid, Transaction.user_id == matched_user.discord_id, Transaction.tx_type == 'DEPOSIT')
            existing_tx = (await session.execute(stmt)).scalar_one_or_none()
            if existing_tx:
                print(f'  -> Existing TX found. conf={existing_tx.confirmations}')
            else:
                print('  -> No existing TX')

asyncio.run(main())
