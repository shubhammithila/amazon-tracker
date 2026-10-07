"""The customer key: a salted HMAC of Amazon's masked buyer email. The email itself is never kept.

Salted so a leaked database cannot be joined against any other list of Amazon relay addresses.
The salt is generated once and stored in `portfolio_settings`; a restored backup restores it too,
so keys stay stable. **Rotating it severs every customer's history** — do not.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PortfolioSettings

SALT_NAME = "customer_key_salt"


def customer_key(email: str | None, salt: str) -> str | None:
    normal = (email or "").strip().lower()
    if not normal:
        return None
    return hmac.new(salt.encode(), normal.encode(), hashlib.sha256).hexdigest()[:32]


async def load_or_create_salt(db: AsyncSession) -> str:
    row = (await db.execute(select(PortfolioSettings).where(PortfolioSettings.name == SALT_NAME))
           ).scalar_one_or_none()
    if row and row.value_json:
        return json.loads(row.value_json)["salt"]
    salt = secrets.token_hex(32)
    db.add(PortfolioSettings(name=SALT_NAME, value_json=json.dumps({"salt": salt}),
                             updated_by="repeat"))
    await db.commit()
    return salt
