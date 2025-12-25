"""Create or promote an EduHub operator account.

    EDUHUB_ADMIN_PASSWORD=... python -m app.scripts.create_platform_admin you@company.com "Your Name"

The password is read from the environment (or prompted) so it never lands in shell history.
"""

import asyncio
import getpass
import os
import sys

from app.core.db import get_engine, get_sessionmaker
from app.services import user_service


async def main(email: str, full_name: str, password: str) -> None:
    async with get_sessionmaker()() as db:
        user = await user_service.create_platform_admin(db, email, password, full_name)
        print(f"Platform admin ready: {user.email}")
    await get_engine().dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    email = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else "EduHub Admin"
    password = os.environ.get("EDUHUB_ADMIN_PASSWORD") or getpass.getpass("Password: ")
    if len(password) < 12:
        raise SystemExit("Use at least 12 characters for a platform admin password")
    asyncio.run(main(email, name, password))
