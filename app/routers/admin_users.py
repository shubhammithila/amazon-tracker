"""The Users panel: create accounts, set what each may see, reset passwords.

Every route here is administrator-only, and that is checked against the DATABASE rather
than the session cookie (``require_user_admin``). The difference matters: revoking
somebody's admin must take effect on their next request, not whenever their week-long
cookie happens to expire. Otherwise "I removed his access" is untrue for up to a week.

**A generated password is returned exactly once**, in the response to the request that
created or reset it. It is never stored in readable form and there is no endpoint that
can retrieve it later — a forgotten password is reset, not recovered. That is the whole
point of storing a hash, and it is worth stating because "just show it again" is an
obvious-looking feature request.
"""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import credentials, permissions, users as users_repo
from app.config import get_settings
from app.database import get_db
from app.routers.auth import require_user_admin

router = APIRouter(prefix="/admin/users")
logger = logging.getLogger(__name__)


def _error(message: str, status: int = 400):
    return JSONResponse({"error": message}, status_code=status)


@router.get("")
async def list_users(
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(require_user_admin),
):
    """Every account, plus the vocabulary the panel needs to render itself.

    `areas` and `presets` are sent rather than duplicated in the template, so adding an
    area to app/permissions.py makes it appear on this screen with no HTML change — and
    cannot appear with a different label or in a different order than the server uses.
    """
    settings = get_settings()
    rows = await users_repo.load_all(db)
    return JSONResponse(
        {
            "users": [users_repo.payload(u) for u in rows],
            "areas": [
                {"key": key, "label": label, "help": help_text}
                for key, label, help_text in permissions.AREAS
            ],
            "presets": [
                {
                    "key": key,
                    "label": permissions.PRESET_LABELS[key],
                    "areas": sorted(areas),
                }
                for key, areas in permissions.PRESETS.items()
            ],
            "implied": {k: sorted(v) for k, v in permissions.IMPLIED.items()},
            # So the panel can warn that a shared password still grants full admin.
            # Without this the owner would reasonably assume named accounts replaced it.
            "shared_password_active": bool(settings.app_password),
            "shared_ops_password_active": bool(settings.ops_password),
            "actor": actor,
        }
    )


@router.post("")
async def create_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(require_user_admin),
):
    """Create an account.

    Body: ``{"full_name": ..., "username": ..., "areas": [...], "is_admin": bool,
    "password": optional}``

    Omitting `username` derives one from the full name; omitting `password` generates a
    readable one. Both are returned so the panel can show what to hand over.
    """
    body = await request.json()

    full_name = str(body.get("full_name") or "").strip()
    username = str(body.get("username") or "").strip()
    if not username:
        existing = {u.username for u in await users_repo.load_all(db)}
        username = credentials.suggest_username(full_name or "user", existing)

    areas = body.get("areas") or []
    if isinstance(areas, str):
        areas = [areas]

    # A preset is expanded here rather than trusted from the client, so the stored grant
    # always matches what app/permissions.py says the preset means.
    preset = str(body.get("preset") or "").strip().lower()
    if preset and preset in permissions.PRESETS:
        areas = sorted(permissions.PRESETS[preset])

    password = body.get("password") or None
    if password is not None:
        password = str(password)

    try:
        user, plaintext = await users_repo.create(
            db,
            username=username,
            full_name=full_name,
            password=password,
            areas=areas,
            is_admin=bool(body.get("is_admin")),
            created_by=actor,
        )
    except users_repo.UserError as e:
        return _error(str(e))

    return JSONResponse(
        {
            "status": "created",
            "user": users_repo.payload(user),
            # Shown once, then unrecoverable. The panel says so on screen.
            "password": plaintext,
        },
        status_code=201,
    )


@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(require_user_admin),
):
    """Change permissions, admin flag, or enabled state.

    Body: any of ``{"areas": [...], "preset": ..., "is_admin": bool, "is_active": bool}``
    """
    body = await request.json()

    try:
        if "is_active" in body:
            user = await users_repo.set_active(db, user_id, bool(body["is_active"]))
            return JSONResponse({"status": "saved", "user": users_repo.payload(user)})

        target = await users_repo.get(db, user_id)
        if target is None:
            return _error("No such user.", 404)

        areas = body.get("areas")
        preset = str(body.get("preset") or "").strip().lower()
        if preset and preset in permissions.PRESETS:
            areas = sorted(permissions.PRESETS[preset])
        if areas is None:
            areas = sorted(permissions.parse(target.permissions))

        user = await users_repo.set_permissions(
            db,
            user_id,
            areas,
            is_admin=bool(body["is_admin"]) if "is_admin" in body else None,
        )
    except users_repo.UserError as e:
        # 409, not 400: the request was well-formed and refused because of the state of
        # the data (the last administrator). A 400 would read as "you sent it wrong".
        return _error(str(e), 409)

    return JSONResponse({"status": "saved", "user": users_repo.payload(user)})


@router.post("/{user_id}/password")
async def reset_user_password(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(require_user_admin),
):
    """Set a new password. Body: ``{"password": optional}``. Returns it once."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    password = body.get("password") or None
    try:
        user, plaintext = await users_repo.reset_password(
            db, user_id, str(password) if password else None
        )
    except users_repo.UserError as e:
        return _error(str(e))

    logger.info("password for %r reset by %r", user.username, actor)
    return JSONResponse(
        {"status": "reset", "user": users_repo.payload(user), "password": plaintext}
    )


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(require_user_admin),
):
    """Delete an account permanently.

    The panel offers Disable first: a real user's name is stamped on the packing days
    they submitted, and deleting the row makes that history unattributable.
    """
    try:
        await users_repo.delete(db, user_id)
    except users_repo.UserError as e:
        return _error(str(e), 409)
    return JSONResponse({"status": "deleted"})


@router.post("/suggest")
async def suggest(
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(require_user_admin),
):
    """A free username and a fresh password, for the create form.

    Server-side so the panel cannot invent a username that fails validation, and so
    password generation uses ``secrets`` rather than ``Math.random()`` — which is not a
    cryptographic source and would make generated passwords predictable.
    """
    body = await request.json()
    full_name = str(body.get("full_name") or "").strip()
    existing = {u.username for u in await users_repo.load_all(db)}
    return JSONResponse(
        {
            "username": credentials.suggest_username(full_name or "user", existing),
            "password": credentials.generate_password(),
        }
    )
