import asyncio
import os
from database import AsyncSessionLocal
from models import User
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as session:
        # Find users with extremely small locked balances (likely due to floating point error)
        # e.g., less than 0.0001 LTC. We can even check if it's > 0 to be safe.
        stmt = select(User).where(User.locked_balance > 0, User.locked_balance < 0.00001)
        result = await session.execute(stmt)
        users_to_fix = result.scalars().all()
        
        count = 0
        for user in users_to_fix:
            locked = float(user.locked_balance)
            print(f"Found tiny locked balance for user {user.discord_id}: {locked:.8f} LTC")
            # Refund to available balance
            user.available_balance = round(float(user.available_balance) + locked, 8)
            user.locked_balance = 0.0
            count += 1
            
        if count > 0:
            await session.commit()
            print(f"Successfully refunded tiny locked balances for {count} users.")
        else:
            print("No tiny locked balances found.")

if __name__ == "__main__":
    asyncio.run(main())
