"""Dev seed: one practice + one owner user, synthetic values only.

Run via `make seed`. Refuses to run unless PHI_MODE=synthetic, since this
script writes obviously-fake but structurally PHI-shaped data (an email,
a name) and must never be pointed at anything else.
"""

import asyncio
import sys
import uuid

from sqlalchemy import select

from app.config import get_settings
from app.db.session import get_sessionmaker, set_tenant
from app.models import Practice, User
from app.models.enums import PracticeStatus, UserRole

SEED_PRACTICE_NAME = "Seed Family Practice"
SEED_OWNER_EMAIL = "owner@seed.docflow.test"


async def seed() -> None:
    settings = get_settings()
    if settings.PHI_MODE != "synthetic":
        print("Refusing to seed: PHI_MODE must be 'synthetic'.", file=sys.stderr)
        raise SystemExit(1)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        existing = await session.execute(
            select(Practice).where(Practice.name == SEED_PRACTICE_NAME)
        )
        if existing.scalar_one_or_none() is not None:
            print("Seed data already present; nothing to do.")
            return

        practice_id = uuid.uuid4()
        practice = Practice(
            id=practice_id,
            name=SEED_PRACTICE_NAME,
            specialty="primary_care",
            status=PracticeStatus.active,
        )
        session.add(practice)
        await session.flush()

        # RLS's WITH CHECK requires the transaction's tenant GUC to match
        # the row being inserted, so set it once the practice (and its id)
        # exist.
        await set_tenant(session, practice_id)

        owner = User(
            practice_id=practice_id,
            email=SEED_OWNER_EMAIL,
            hashed_password=None,
            full_name="Seed Owner",
            role=UserRole.owner,
            is_active=True,
            mfa_enabled=False,
        )
        session.add(owner)

    print(f"Seeded practice {practice_id} and owner user {SEED_OWNER_EMAIL}.")


if __name__ == "__main__":
    asyncio.run(seed())
