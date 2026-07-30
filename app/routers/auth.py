from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer

from app.config import get_settings

router = APIRouter()
settings = get_settings()
templates = Jinja2Templates(directory="templates")
serializer = URLSafeTimedSerializer(settings.secret_key)

SESSION_COOKIE = "session_token"
SESSION_MAX_AGE = 86400 * 7

# Two humans use this app: the owner (admin — plans shipments, verifies packing,
# generates GST invoices) and the operations employee (ops — records what was
# packed each day). Ops was added later, which drives one load-bearing rule:
#
#   A cookie WITHOUT a role field is an admin cookie.
#
# Every session issued before roles existed carries exactly
# {"authenticated": True}. Treating absent-role as admin means those sessions
# (and every test fixture) keep working, and privilege can only ever be
# *reduced* by the new explicit role="ops" — never silently escalated, because
# forging a role requires the itsdangerous signing key.
ROLE_ADMIN = "admin"
ROLE_OPS = "ops"


class RedirectException(Exception):
    """Not signed in — handled in app.main with a 303 to /login."""


class ForbiddenException(Exception):
    """Signed in, but the role does not allow this — handled with a 403.

    Distinct from RedirectException on purpose: bouncing an authenticated ops
    user to the login page would look like a broken session. A 403 tells their
    JS (and them) 'this part is admin-only'.
    """


def get_current_role(request: Request) -> str | None:
    """The session's role, or None when not authenticated.

    `request` may equally be a WebSocket — cookies live on the shared
    HTTPConnection base class (see /ws/progress).
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("authenticated"):
        return None
    return data.get("role") or ROLE_ADMIN


def get_current_user(request: Request) -> bool:
    """Kept with its original bool signature for /ws/progress.

    The WebSocket cannot use a Depends() that raises, because a refusal there has
    to be an explicit close(code=1008) after accept — so it needs the plain
    boolean form. Every HTTP route uses require_auth / require_admin instead.
    """
    return get_current_role(request) is not None


def require_auth(request: Request):
    """Any signed-in session (admin or ops). Behaviour identical to pre-roles."""
    if get_current_role(request) is None:
        raise RedirectException()
    return True


def require_admin(request: Request) -> str:
    """Owner-only: plan editing, verification, invoicing, downloads of record."""
    role = get_current_role(request)
    if role is None:
        raise RedirectException()
    if role != ROLE_ADMIN:
        raise ForbiddenException()
    return role


def require_ops_or_admin(request: Request) -> str:
    """Packing entry and the ops screen. Admin supervises, so both roles pass."""
    role = get_current_role(request)
    if role is None:
        raise RedirectException()
    return role


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(request: Request, password: str = Form(...)):
    # Admin is checked FIRST. If someone sets OPS_PASSWORD to the same value as
    # APP_PASSWORD, the owner must not be silently demoted to ops.
    if password == settings.app_password:
        payload, destination = {"authenticated": True}, "/"
    elif settings.ops_password and password == settings.ops_password:
        payload, destination = {"authenticated": True, "role": ROLE_OPS}, "/ops-page"
    else:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid password"}
        )

    token = serializer.dumps(payload)
    response = RedirectResponse(url=destination, status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax"
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
