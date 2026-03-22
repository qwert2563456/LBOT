import asyncio
import os
from dotenv import load_dotenv
load_dotenv()
import database
from models import Transaction, User
from sqlalchemy import select

async def main():
    print('DB:', os.getenv('DATABASE_URL'))
    async with database.AsyncSessionLocal() as s:
        res = await s.execute(select(Transaction).where(Transaction.tx_type == 'DEPOSIT').order_by(Transaction.id.desc()).limit(5))
        print('--- tx ---')
        for tx in res.scalars():
            print(f'TX: id={tx.id}, conf={tx.confirmations}, user={tx.user_id}')

        res = await s.execute(select(User).where(User.unconfirmed_balance > 0))
        print('--- users ---')
        for u in res.scalars():
            print(f'USER: {u.discord_id}, unconf={u.unconfirmed_balance}')

asyncio.run(main())
