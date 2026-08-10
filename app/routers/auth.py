"""Signing in, and deciding what the signed-in person may reach.

Three ways in, and the ordering between them is load-bearing:

1. **A named user** from the ``users`` table — username + password. This is the one the
   owner creates from the Users panel, and the only one with per-area permissions.
2. **APP_PASSWORD**, password only, full admin. Kept deliberately (see below).
3. **OPS_PASSWORD**, password only, packing area only.

**Why the shared passwords stay.** Deleting them in the same change that adds the users
table would mean that if anything at all is wrong with that table on the EC2 box — the
migration not run, a typo in a permission string — nobody can sign in, on an app with no
password-reset email and no admin console. They are the recovery path. The Users panel
warns while APP_PASSWORD is still usable so it does not become permanent by accident.

**A cookie without a role or a user id is an admin cookie.** Every session issued before
roles existed is exactly ``{"authenticated": True}``, and this rule is what keeps those
sessions and every test fixture working. Privilege can only ever be *reduced* by an
explicit role or uid, never silently escalated, because forging either requires the
itsdangerous signing key.

The permission check is a **DB read per request** for named users. That is a deliberate
cost: the alternative is baking the grant into the cookie, and then revoking somebody's
access would not take effect until their week-long session expired. Removing access has
to be immediate — that is the entire point of the feature.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from app import permissions, users as users_repo
from app.config import get_settings
from app.database import get_db

router = APIRouter()
settings = get_settings()
templates = Jinja2Templates(directory="templates")
serializer = URLSafeTimedSerializer(settings.secret_key)

SESSION_COOKIE = "session_token"
SESSION_MAX_AGE = 86400 * 7

ROLE_ADMIN = "admin"
ROLE_OPS = "ops"


class RedirectException(Exception):
    """Not signed in — handled in app.main with a 303 to /login."""


class ForbiddenException(Exception):
    """Signed in, but not allowed here — handled with a 403.

    Distinct from RedirectException on purpose: bouncing an authenticated user to the
    login page would look like a broken session. A 403 tells them (and their JS) that
    this part is not theirs.
    """


# ─── Reading the session ─────────────────────────────────────────────────────

def _session(request: Request) -> dict | None:
    """The signed cookie payload, or None. Never raises on a bad cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("authenticated"):
        return None
    return data


def get_current_role(request: Request) -> str | None:
    """The session's coarse role, or None when not authenticated.

    Kept with its original name and meaning because ~40 pre-existing routes depend on
    it. A named user resolves to `admin` or `ops` here purely so those routes keep
    behaving; the fine-grained answer comes from ``require_area``.

    `request` may equally be a WebSocket — cookies live on the shared HTTPConnection
    base class (see /ws/progress).
    """
    data = _session(request)
    if data is None:
        return None
    return data.get("role") or ROLE_ADMIN


def get_current_username(request: Request) -> str | None:
    """The named user's username, or None for a shared-password session."""
    data = _session(request)
    return (data or {}).get("username")


def get_current_user(request: Request) -> bool:
    """Kept with its original bool signature for /ws/progress.

    The WebSocket cannot use a Depends() that raises, because a refusal there has to be
    an explicit close(code=1008) after accept — so it needs the plain boolean form.
    """
    return get_current_role(request) is not None


# ─── The coarse guards, unchanged in behaviour ───────────────────────────────

def require_auth(request: Request):
    """Any signed-in session. Behaviour identical to pre-roles."""
    if get_current_role(request) is None:
        raise RedirectException()
    return True


def require_admin(request: Request) -> str:
    """Owner-only: plan editing, verification, invoicing, documents of record."""
    role = get_current_role(request)
    if role is None:
        raise RedirectException()
    if role != ROLE_ADMIN:
        raise ForbiddenException()
    return role


def require_ops_or_admin(request: Request) -> str:
    """Packing entry and the ops screen. Admin supervises, so both roles pass.

    Cookie-only by design, and that is now a deliberately narrow exception: this is the
    guard on the ~11 packing API routes, and it must keep working when the users table is
    missing or a migration has not run. The warehouse depends on those daily.

    It does NOT notice a disabled account — see ``require_packing`` below, which is what
    the ops PAGE uses.
    """
    role = get_current_role(request)
    if role is None:
        raise RedirectException()
    return role


async def require_packing(
    request: Request, db: AsyncSession = Depends(get_db)
) -> str:
    """The ops PAGE: signed in, not disabled, and granted the packing area.

    Exists because of a bug found on production, by testing rather than by reading: a
    named account was disabled while signed in, and ``/ops-page`` still returned 200. The
    reason is that ``require_ops_or_admin`` reads only the cookie, so a revoked or
    disabled user kept the screen for up to a week — while every other page cut them off
    immediately.

    A shared-password session (no username) is still accepted on the cookie alone, so
    OPS_PASSWORD keeps working even if the users table is missing. Only NAMED accounts are
    re-checked against the database, which is exactly where the stale-session risk lives.
    """
    data = _session(request)
    if data is None:
        raise RedirectException()

    if not data.get("username"):
        return data.get("role") or ROLE_ADMIN  # shared password: unchanged behaviour

    grant, is_admin = await resolve_grant(request, db)
    if grant is None and not is_admin:
        # Account disabled or deleted mid-session. Sending them to /login is right: their
        # session is genuinely over, not merely insufficient.
        raise RedirectException()
    if not permissions.has(grant, permissions.PACKING, is_admin=is_admin):
        raise ForbiddenException()
    return ROLE_ADMIN if is_admin else ROLE_OPS


# ─── The fine-grained guard ──────────────────────────────────────────────────

async def resolve_grant(request: Request, db: AsyncSession) -> tuple[str | None, bool]:
    """(permissions string, is_admin) for this session.

    Shared-password sessions are mapped onto grants rather than special-cased at every
    call site: APP_PASSWORD is admin (everything), OPS_PASSWORD is the packing area. So
    ``require_area`` has exactly one code path.

    A named user whose account was disabled or deleted mid-session gets None, which
    ends the session at the next request. That immediacy is why this reads the database
    rather than trusting the cookie.
    """
    data = _session(request)
    if data is None:
        return None, False

    username = data.get("username")
    if username:
        user = await users_repo.get_by_username(db, username)
        if user is None or not user.is_active:
            return None, False
        return user.permissions, bool(user.is_admin)

    # Shared-password session.
    if (data.get("role") or ROLE_ADMIN) == ROLE_ADMIN:
        return permissions.serialise(permissions.AREA_KEYS), True
    return permissions.serialise([permissions.PACKING]), False


class Grant:
    """What the current session may reach. Handed to page templates.

    Returned by ``require_area`` rather than a bare string so the nav can render only
    the tabs this person can actually open, WITHOUT a second database read per page.
    Drawing a link that answers 403 is how an app earns "it's broken" — the user cannot
    tell a permission boundary from a bug.

    ``.areas`` is the expanded set, so a template asks ``"invoice" in areas`` and never
    has to know that `shipment` implies `packing`.
    """

    __slots__ = ("areas", "is_admin", "username")

    def __init__(self, areas: frozenset[str], is_admin: bool, username: str | None):
        self.areas = areas
        self.is_admin = is_admin
        self.username = username

    def __contains__(self, area: str) -> bool:
        return self.is_admin or area in self.areas


def require_area(area: str):
    """Dependency factory: allow only sessions granted `area`.

    Used on the PAGE routes, which is where a permission is meaningful to the person
    affected — being handed a tab you cannot use is the confusing outcome. The ~40
    pre-existing API routes keep ``require_auth``; that gap is written up in CLAUDE.md
    as a known and accepted limitation rather than silently left.
    """
    async def _guard(request: Request, db: AsyncSession = Depends(get_db)) -> Grant:
        grant, is_admin = await resolve_grant(request, db)
        if grant is None and not is_admin:
            raise RedirectException()
        if not permissions.has(grant, area, is_admin=is_admin):
            raise ForbiddenException()
        return Grant(
            areas=frozenset(permissions.AREA_KEYS) if is_admin
                  else permissions.parse(grant),
            is_admin=is_admin,
            username=get_current_username(request),
        )

    return _guard


async def require_admin_grant(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Grant:
    """Administrator-only, returning the full Grant so a page can render its nav.

    Same check as ``require_user_admin`` — this one exists because the Users PAGE needs
    the grant object, and passing None instead left the page without a Users tab of its
    own. That is how the panel became reachable only by typing the URL.
    """
    grant, is_admin = await resolve_grant(request, db)
    if grant is None and not is_admin:
        raise RedirectException()
    if not is_admin:
        raise ForbiddenException()
    return Grant(
        areas=frozenset(permissions.AREA_KEYS),
        is_admin=True,
        username=get_current_username(request),
    )


async def require_user_admin(
    request: Request, db: AsyncSession = Depends(get_db)
) -> str:
    """Only an administrator may manage accounts. Returns the acting username.

    Separate from ``require_admin`` because that one trusts the cookie's role alone. For
    the routes that hand out permissions, the flag is re-read from the database — so
    revoking somebody's admin takes effect on their very next request rather than when
    their week-long session happens to expire.
    """
    grant, is_admin = await resolve_grant(request, db)
    if grant is None and not is_admin:
        raise RedirectException()
    if not is_admin:
        raise ForbiddenException()
    return get_current_username(request) or ROLE_ADMIN


# ─── Login ───────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    """The form. Shows a username field only once accounts exist.

    Before then it would be a puzzle: the only credential that works is the shared
    password, and an empty username box invites the owner to invent one.
    """
    try:
        named = await users_repo.any_users_exist(db)
    except Exception:
        # The users table may not exist yet — an old database, or a deploy where the
        # migration has not run. The shared password must still get you in, so this
        # falls back to the password-only form rather than 500ing the login page.
        named = False
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "show_username": named}
    )


def _issue(payload: dict, destination: str) -> RedirectResponse:
    response = RedirectResponse(url=destination, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        serializer.dumps(payload),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


def _landing(grant: str | None, is_admin: bool) -> str:
    """Where to send someone after login: the first area they can actually use.

    Sending everyone to `/` would show the packer a 403 as his first impression of the
    app, which reads as broken rather than as a rule.
    """
    if is_admin or permissions.has(grant, permissions.DASHBOARD):
        return "/"
    for area, path in (
        (permissions.SHIPMENT, "/shipment-page"),
        (permissions.INVOICE, "/invoice-page"),
        (permissions.PORTFOLIO, "/churn-page"),
        (permissions.PROJECTIONS, "/projections-page"),
        (permissions.PACKING, "/ops-page"),
    ):
        if permissions.has(grant, area):
            return path
    return "/no-access"


@router.post("/login")
async def login(
    request: Request,
    password: str = Form(...),
    username: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Named user first, then the shared passwords.

    Named first so that creating a user called after a shared password's owner does not
    get shadowed by it. Between the two shared passwords, APP_PASSWORD is checked first:
    if someone sets OPS_PASSWORD to the same value, the owner must not be silently
    demoted to ops.
    """
    async def _reject():
        try:
            named = await users_repo.any_users_exist(db)
        except Exception:
            named = False
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                # One message for every failure. Distinguishing "no such user" from
                # "wrong password" tells someone probing the form which usernames are
                # real.
                "error": "Those details did not work. Check and try again.",
                "show_username": named,
            },
            status_code=401,
        )

    if username.strip():
        try:
            user = await users_repo.authenticate(db, username, password)
        except Exception:
            user = None  # users table missing — fall through to the shared passwords
        if user is not None:
            return _issue(
                {
                    "authenticated": True,
                    "username": user.username,
                    # The coarse role still travels, so the ~40 routes on require_auth
                    # and require_admin keep working without 40 edits.
                    "role": ROLE_ADMIN if user.is_admin else ROLE_OPS,
                },
                _landing(user.permissions, user.is_admin),
            )
        return await _reject()

    if password == settings.app_password:
        return _issue({"authenticated": True}, "/")
    if settings.ops_password and password == settings.ops_password:
        return _issue({"authenticated": True, "role": ROLE_OPS}, "/ops-page")
    return await _reject()


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/whoami")
async def whoami(request: Request, db: AsyncSession = Depends(get_db)):
    """Who am I and what may I see. Drives the nav.

    The nav renders from this rather than from a template variable so a tab the user
    cannot open is never drawn. It is a convenience and never the enforcement — every
    page re-checks server-side.
    """
    grant, is_admin = await resolve_grant(request, db)
    if grant is None and not is_admin:
        raise RedirectException()
    return JSONResponse(
        {
            "username": get_current_username(request),
            "is_admin": is_admin,
            "areas": sorted(permissions.parse(grant)) if not is_admin
                     else sorted(permissions.AREA_KEYS),
            # True while a shared password can still sign somebody in as full admin.
            # Surfaced so the Users panel can say so, rather than the owner assuming
            # named accounts replaced it.
            "shared_password_active": bool(settings.app_password),
        }
    )
