"""Reading and writing user accounts. The only place that touches the users table.

Same shape as ``app/shipment/repository.py`` and for the same reason: one module owns
the queries, so the rules about who may exist and in what state cannot be reimplemented
differently by a second caller.

Three rules are enforced here rather than in the router, because a router is a place
someone adds a second endpoint:

* **The last admin cannot be removed or demoted.** Not a nicety — this app has no
  password-reset email and no console. An account nobody can sign into as admin is
  recovered with a sqlite3 shell on the EC2 box, which is a bad evening.
* **Disabling, not deleting.** A departed packer's username is stamped on the packing
  days he submitted. Deleting the row makes that history unattributable.
* **Passwords only ever arrive as plaintext and leave as hashes.** Nothing here returns
  a hash to a caller and nothing accepts one.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import credentials, permissions
from app.models import User

logger = logging.getLogger(__name__)


class UserError(Exception):
    """A refusal the owner should read verbatim. Routers turn these into 400s."""


# ─── Reading ─────────────────────────────────────────────────────────────────

async def get_by_username(db: AsyncSession, username: str) -> User | None:
    """Case-insensitive by construction: usernames are stored normalised."""
    name = credentials.normalise_username(username)
    if not name:
        return None
    result = await db.execute(select(User).where(User.username == name))
    return result.scalar_one_or_none()


async def get(db: AsyncSession, user_id: int) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def load_all(db: AsyncSession) -> list[User]:
    """Every account, admins first then alphabetical.

    Admins first because that is the list the owner scans to answer "who can change
    permissions", which is the question that matters most on this screen.
    """
    result = await db.execute(
        select(User).order_by(User.is_admin.desc(), User.username)
    )
    return list(result.scalars())


async def count_active_admins(db: AsyncSession, *, excluding: int | None = None) -> int:
    """How many enabled admins remain. The guard against locking everyone out."""
    query = select(func.count()).select_from(User).where(
        User.is_admin.is_(True), User.is_active.is_(True)
    )
    if excluding is not None:
        query = query.where(User.id != excluding)
    return int((await db.execute(query)).scalar() or 0)


async def any_users_exist(db: AsyncSession) -> bool:
    """Whether the table has been populated yet.

    Drives one thing only: whether the login page mentions usernames at all. Before the
    owner has created anybody, a username field is a puzzle — the only credential that
    works is the shared password.
    """
    return bool((await db.execute(select(User.id).limit(1))).scalar())


# ─── Authenticating ──────────────────────────────────────────────────────────

async def authenticate(db: AsyncSession, username: str, password: str) -> User | None:
    """The user, or None. Deliberately gives no reason.

    A caller cannot tell "no such user" from "wrong password" from "account disabled",
    because the difference tells someone probing the form which usernames exist.

    Verification runs even when the user is missing or disabled, against a dummy hash.
    Skipping it would make a bad username measurably faster to reject than a bad
    password, which leaks exactly what the single error message is hiding.
    """
    user = await get_by_username(db, username)
    stored = user.password_hash if user else _DUMMY_HASH
    ok = credentials.verify_password(password, stored)

    if user is None or not ok or not user.is_active:
        if user is not None and ok and not user.is_active:
            logger.warning("login refused for disabled account %r", user.username)
        return None

    user.last_login_at = datetime.utcnow()
    user.must_change_password = False
    await db.commit()
    await db.refresh(user)
    return user


#: A real scrypt hash of a random string, computed once at import. Its only job is to
#: make the failure path cost the same as the success path.
_DUMMY_HASH = credentials.hash_password(credentials.generate_token(24))


# ─── Writing ─────────────────────────────────────────────────────────────────

async def create(
    db: AsyncSession,
    *,
    username: str,
    full_name: str = "",
    password: str | None = None,
    areas=None,
    is_admin: bool = False,
    created_by: str = "",
) -> tuple[User, str]:
    """Create an account. Returns (user, plaintext password).

    **The plaintext is returned exactly once and never stored.** This is the only
    moment it exists in readable form; the panel shows it, the owner writes it down or
    sends it, and after that a forgotten password can only be reset, not recovered.
    That is the property that makes the hash worth having.
    """
    name = credentials.normalise_username(username)
    error = credentials.username_error(name)
    if error:
        raise UserError(error)

    if await get_by_username(db, name) is not None:
        raise UserError(f"The username {name!r} is already taken.")

    plaintext = password or credentials.generate_password()
    if len(plaintext) < 8:
        raise UserError("A password must be at least 8 characters.")

    user = User(
        username=name,
        full_name=(full_name or "").strip()[:120],
        password_hash=credentials.hash_password(plaintext),
        permissions=permissions.serialise(areas or []),
        is_admin=bool(is_admin),
        is_active=True,
        created_by=credentials.normalise_username(created_by)[:32],
        # True until they sign in, so the panel can say "has not signed in yet" rather
        # than leaving the owner to wonder whether the credentials ever arrived.
        must_change_password=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("user %r created by %r (admin=%s)", name, created_by, is_admin)
    return user, plaintext


async def set_permissions(
    db: AsyncSession, user_id: int, areas, *, is_admin: bool | None = None
) -> User:
    """Change what a user may reach.

    Refuses to remove the last admin. There is no recovery path in the app for that —
    no reset email, no console — so the only fix would be editing the database by hand
    on the server.
    """
    user = await get(db, user_id)
    if user is None:
        raise UserError("No such user.")

    if is_admin is not None and user.is_admin and not is_admin:
        if await count_active_admins(db, excluding=user.id) == 0:
            raise UserError(
                f"{user.username} is the only administrator left. Make somebody else "
                "an administrator first, or nobody will be able to manage users."
            )
        user.is_admin = False
    elif is_admin:
        user.is_admin = True

    user.permissions = permissions.serialise(areas or [])
    await db.commit()
    await db.refresh(user)
    logger.info("permissions for %r set to %r", user.username, user.permissions)
    return user


async def reset_password(
    db: AsyncSession, user_id: int, password: str | None = None
) -> tuple[User, str]:
    """New password, shown once. Returns (user, plaintext)."""
    user = await get(db, user_id)
    if user is None:
        raise UserError("No such user.")

    plaintext = password or credentials.generate_password()
    if len(plaintext) < 8:
        raise UserError("A password must be at least 8 characters.")

    user.password_hash = credentials.hash_password(plaintext)
    user.must_change_password = True
    await db.commit()
    await db.refresh(user)
    logger.info("password reset for %r", user.username)
    return user, plaintext


async def set_active(db: AsyncSession, user_id: int, active: bool) -> User:
    """Enable or disable an account.

    Disabling is the supported way to remove someone: their username stays attached to
    the packing days they submitted, and re-enabling is one click if they come back.
    """
    user = await get(db, user_id)
    if user is None:
        raise UserError("No such user.")

    if not active and user.is_admin and await count_active_admins(db, excluding=user.id) == 0:
        raise UserError(
            f"{user.username} is the only administrator left. Disabling this account "
            "would lock everybody out of user management."
        )

    user.is_active = bool(active)
    await db.commit()
    await db.refresh(user)
    logger.info("user %r %s", user.username, "enabled" if active else "disabled")
    return user


async def delete(db: AsyncSession, user_id: int) -> None:
    """Really delete. Offered because a mistyped account should not linger for ever.

    Guarded the same way as disabling, and the panel points at disabling first — the
    packing history a real user leaves behind is worth more than a tidy list.
    """
    user = await get(db, user_id)
    if user is None:
        raise UserError("No such user.")
    if user.is_admin and await count_active_admins(db, excluding=user.id) == 0:
        raise UserError(
            f"{user.username} is the only administrator left. Deleting this account "
            "would lock everybody out of user management."
        )
    name = user.username
    await db.delete(user)
    await db.commit()
    logger.info("user %r deleted", name)


def payload(user: User) -> dict:
    """One user as the panel consumes it. **Never includes the hash.**

    Stated as a rule because the obvious `{c.name: getattr(...)}` loop over the model
    would include it, and a hash in a JSON response is a hash in the browser's network
    log and in any error reporter the page ever gains.
    """
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name or "",
        "is_admin": bool(user.is_admin),
        "is_active": bool(user.is_active),
        "areas": sorted(permissions.parse(user.permissions)),
        "preset": permissions.preset_for(user.permissions, is_admin=user.is_admin),
        "describe": permissions.describe(user.permissions, is_admin=user.is_admin),
        "created_by": user.created_by or "",
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "never_signed_in": user.last_login_at is None,
    }
