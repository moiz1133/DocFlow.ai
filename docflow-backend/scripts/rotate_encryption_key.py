"""Key rotation / re-encryption for PHI columns (Phase 7).

Run via `make rotate-key`. Moves every value currently encrypted under
the OLD key version to the NEW one, table by table, column by column
(app.security.phi_columns.PHI_COLUMNS — the same list the Phase 7 data
migration uses). Rows already on the new version are skipped (safe to
re-run/resume); legacy, still-unencrypted plaintext rows are left alone
(that's the data migration's job, not rotation's).

This does not attempt "live" rotation (no online cutover coordination,
no batching/backoff for a huge table) — it is the straightforward,
correct path: decrypt with the old key, re-encrypt with the new one, one
row at a time, inside one transaction per column. For a genuinely large
production table this would want batching; documented here rather than
built, since nothing in this codebase currently has that scale.

Configuration is intentionally NOT part of app.config.Settings: a
rotation needs to hold BOTH the old and new key material at once, which
the long-running app process never should (Settings only ever knows
about the current key/version — see app/security/keys.py). Instead, read
directly from the environment:

    KEY_PROVIDER=local (the only rotation path this script implements —
    see README's "Key rotation" section for the AWS KMS equivalent, which
    is a matter of pointing ROTATE_OLD/NEW_KMS_KEY_ID at two distinct KMS
    keys instead; the KeyProvider interface is the same either way)
    ROTATE_OLD_KEY_VERSION=1
    ROTATE_OLD_LOCAL_ENCRYPTION_KEY=<base64 32-byte key>
    ROTATE_NEW_KEY_VERSION=2
    ROTATE_NEW_LOCAL_ENCRYPTION_KEY=<base64 32-byte key>

Add --dry-run to count what would change without writing anything.
"""

import argparse
import asyncio
import base64
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_admin_sessionmaker
from app.security.encryption import (
    blob_key_version,
    decrypt_value,
    encrypt_value,
    is_encrypted_blob,
)
from app.security.keys import LocalKeyProvider
from app.security.phi_columns import PHI_COLUMNS


def _load_provider() -> tuple[LocalKeyProvider, int, int]:
    try:
        old_version = int(os.environ["ROTATE_OLD_KEY_VERSION"])
        new_version = int(os.environ["ROTATE_NEW_KEY_VERSION"])
        old_key = base64.b64decode(os.environ["ROTATE_OLD_LOCAL_ENCRYPTION_KEY"], validate=True)
        new_key = base64.b64decode(os.environ["ROTATE_NEW_LOCAL_ENCRYPTION_KEY"], validate=True)
    except KeyError as exc:
        print(f"Missing required environment variable: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"Malformed key material: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    if old_version == new_version:
        print("ROTATE_OLD_KEY_VERSION and ROTATE_NEW_KEY_VERSION must differ", file=sys.stderr)
        raise SystemExit(1)

    provider = LocalKeyProvider(
        keys={old_version: old_key, new_version: new_key}, active_version=new_version
    )
    return provider, old_version, new_version


async def _rotate_column(
    db: AsyncSession,
    *,
    table: str,
    column: str,
    provider: LocalKeyProvider,
    old_version: int,
    new_version: int,
    dry_run: bool,
) -> tuple[int, int]:
    # table/column always come from PHI_COLUMNS, never external input.
    rows = (
        await db.execute(text(f'SELECT id, "{column}" FROM {table} WHERE "{column}" IS NOT NULL'))
    ).fetchall()

    rotated = 0
    for row_id, value in rows:
        if not value or not is_encrypted_blob(value) or blob_key_version(value) != old_version:
            continue
        plaintext = decrypt_value(value, key_provider=provider)
        if not dry_run:
            new_blob = encrypt_value(plaintext, key_provider=provider, key_version=new_version)
            await db.execute(
                text(f'UPDATE {table} SET "{column}" = :val WHERE id = :row_id'),
                {"val": new_blob, "row_id": row_id},
            )
        rotated += 1
    return rotated, len(rows)


async def rotate(*, dry_run: bool) -> None:
    provider, old_version, new_version = _load_provider()
    print(
        f"Rotating key version {old_version} -> {new_version}" + (" (dry run)" if dry_run else "")
    )

    sessionmaker = get_admin_sessionmaker()
    total_rotated = 0
    async with sessionmaker() as db, db.begin():
        for table, column in PHI_COLUMNS:
            rotated, seen = await _rotate_column(
                db,
                table=table,
                column=column,
                provider=provider,
                old_version=old_version,
                new_version=new_version,
                dry_run=dry_run,
            )
            total_rotated += rotated
            print(f"  {table}.{column}: {rotated} row(s) rotated (of {seen} non-null)")
        if dry_run:
            await db.rollback()

    print(f"Done. {total_rotated} value(s) {'would be ' if dry_run else ''}rotated.")
    if not dry_run:
        print(
            f"Both version {old_version} and version {new_version} still decrypt correctly "
            "during any transition window — see app/security/keys.py's LocalKeyProvider, "
            "which accepts multiple versions at once for exactly this reason."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Count what would change without writing anything"
    )
    args = parser.parse_args()
    asyncio.run(rotate(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
