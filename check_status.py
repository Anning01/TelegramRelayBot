import asyncio
import sys
from sqlalchemy import select
from app.database import init_db, AsyncSessionLocal
from app.models import RelayGroup, TargetUser, MessageLog


async def check_status():
    await init_db()
    async with AsyncSessionLocal() as session:
        # Check Groups
        groups = (await session.execute(select(RelayGroup))).scalars().all()
        print(f"\n=== Relay Groups ({len(groups)}) ===")
        for g in groups:
            status = "ACTIVE" if g.is_active else "PAUSED"
            print(f"[{status}] ID: {g.group_id} | Title: {g.title}")

        if not groups:
            print(
                "⚠️  No groups found. Please add the bot to a group and send a message."
            )

        # Check Users
        users = (await session.execute(select(TargetUser))).scalars().all()
        print(f"\n=== Target Users ({len(users)}) ===")
        for u in users:
            status = "ACTIVE" if u.is_active else "INACTIVE"
            print(
                f"[{status}] ID: {u.user_id} | Name: {u.name} | Index: {u.current_index}"
            )

        if not users:
            print("⚠️  No users found. Send /start to the bot in private chat.")

        # Check Logs
        logs_count = (await session.execute(select(MessageLog))).scalars().all()
        print(f"\n=== Total Messages Processed: {len(logs_count)} ===\n")


if __name__ == "__main__":
    # Ensure we can import app
    import os

    sys.path.append(os.getcwd())

    try:
        asyncio.run(check_status())
    except Exception as e:
        print(f"Error: {e}")
