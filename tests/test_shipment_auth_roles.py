"""Role separation for the shipment workflow: owner (admin) vs operations (ops).

Requested 2026-07-30: "for the operations employee, we should have a separate
tab" plus "verified by me" gating invoice generation. That needs a second login,
which means the single-password cookie scheme grows a role field.

The single most important invariant in this file:

    A legacy cookie — the exact payload {"authenticated": True} with no role —
    MUST still be treated as admin.

Every live browser session and every existing test fixture carries that payload.
If absent-role ever stops meaning admin, the owner is locked out of his own app
the moment this deploys. The ops cookie explicitly carries role="ops", so it can
only ever be *less* privileged than a legacy session, never more.

These tests are written BEFORE the role implementation (step 4 is flagged
highest-risk in the plan) so a mistake fails loudly here instead of in
production.
"""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.auth import SESSION_COOKIE, serializer

pytestmark = pytest.mark.regression

ADMIN_PAGES = ["/", "/invoice-page", "/churn-page", "/projections-page", "/shipment-page"]


@pytest.fixture
def ops_cookie():
    """A validly signed session cookie carrying the ops role."""
    return serializer.dumps({"authenticated": True, "role": "ops"})


@pytest_asyncio.fixture
async def ops_client(db_schema, ops_cookie):
    """HTTP client authenticated as the operations employee."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac:
        ac.cookies.set(SESSION_COOKIE, ops_cookie)
        yield ac


# ─── The load-bearing invariant: legacy cookies stay admin ───────────────────

async def test_legacy_cookie_without_role_is_admin(auth_client):
    """auth_client's cookie is the pre-roles payload {"authenticated": True}.

    It must keep full admin access — this is what stops the deploy from locking
    the owner out of his own app.
    """
    for page in ADMIN_PAGES:
        r = await auth_client.get(page)
        assert r.status_code == 200, (
            f"legacy admin session lost access to {page} ({r.status_code}) — "
            "absent role must mean admin"
        )


def test_get_current_role_treats_absent_role_as_admin():
    from unittest.mock import Mock

    from app.routers.auth import ROLE_ADMIN, get_current_role

    legacy = serializer.dumps({"authenticated": True})
    request = Mock(cookies={SESSION_COOKIE: legacy})
    assert get_current_role(request) == ROLE_ADMIN


def test_get_current_role_reads_the_ops_role(ops_cookie):
    from unittest.mock import Mock

    from app.routers.auth import ROLE_OPS, get_current_role

    request = Mock(cookies={SESSION_COOKIE: ops_cookie})
    assert get_current_role(request) == ROLE_OPS


@pytest.mark.parametrize(
    "token",
    [
        None,
        "garbage",
        "",
    ],
)
def test_get_current_role_rejects_invalid_tokens(token):
    from unittest.mock import Mock

    from app.routers.auth import get_current_role

    cookies = {} if token is None else {SESSION_COOKIE: token}
    request = Mock(cookies=cookies)
    assert get_current_role(request) is None


def test_a_forged_role_in_an_unsigned_cookie_is_rejected():
    """Role escalation must require the signing key, not just editing a cookie."""
    import base64
    import json
    from unittest.mock import Mock

    from app.routers.auth import get_current_role

    fake = base64.urlsafe_b64encode(
        json.dumps({"authenticated": True, "role": "admin"}).encode()
    ).decode()
    request = Mock(cookies={SESSION_COOKIE: fake})
    assert get_current_role(request) is None


# ─── Login routing ───────────────────────────────────────────────────────────

async def test_admin_password_logs_in_as_admin(client):
    r = await client.post("/login", data={"password": "test-password"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert SESSION_COOKIE in r.cookies

    data = serializer.loads(r.cookies[SESSION_COOKIE])
    assert data.get("authenticated") is True
    assert data.get("role", "admin") in (None, "admin") or data["role"] == "admin"


async def test_ops_password_logs_in_as_ops_and_lands_on_ops_page(client):
    r = await client.post("/login", data={"password": "test-ops-password"})
    assert r.status_code == 303
    assert r.headers["location"] == "/ops-page"

    data = serializer.loads(r.cookies[SESSION_COOKIE])
    assert data == {"authenticated": True, "role": "ops"}


async def test_wrong_password_still_fails(client):
    r = await client.post("/login", data={"password": "neither-password"})
    assert r.status_code == 200  # re-renders login with error
    assert "Invalid password" in r.text
    assert SESSION_COOKIE not in r.cookies


async def test_admin_password_wins_when_both_passwords_are_identical(client, monkeypatch):
    """If someone sets OPS_PASSWORD equal to APP_PASSWORD, the owner must not be
    silently demoted to ops. Admin is checked first."""
    from app.routers import auth as auth_module

    monkeypatch.setattr(auth_module.settings, "ops_password", "test-password")
    r = await client.post("/login", data={"password": "test-password"})
    assert r.status_code == 303
    data = serializer.loads(r.cookies[SESSION_COOKIE])
    assert data.get("role") != "ops", "identical passwords demoted the owner to ops"


# ─── Page access by role ─────────────────────────────────────────────────────

@pytest.mark.parametrize("page", ADMIN_PAGES)
async def test_ops_cannot_open_admin_pages(ops_client, page):
    r = await ops_client.get(page)
    assert r.status_code == 403, (
        f"ops opened admin page {page} ({r.status_code}) — plan editing and "
        "invoicing must stay owner-only"
    )


async def test_ops_can_open_the_ops_page(ops_client):
    r = await ops_client.get("/ops-page")
    assert r.status_code == 200, r.status_code


async def test_admin_can_open_the_ops_page_too(auth_client):
    """The owner supervises packing, so the ops screen is not closed to him."""
    r = await auth_client.get("/ops-page")
    assert r.status_code == 200


async def test_signed_out_user_is_redirected_from_the_ops_page(client):
    r = await client.get("/ops-page")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_ops_page_shows_no_admin_nav_links(ops_client):
    """The ops screen must not advertise pages its user cannot open."""
    r = await ops_client.get("/ops-page")
    for admin_href in ("/invoice-page", "/churn-page", "/projections-page", "/shipment-page"):
        assert f'href="{admin_href}"' not in r.text, (
            f"ops page links to admin page {admin_href}"
        )


# ─── Behaviour unchanged for everything that existed before roles ────────────

async def test_unauthenticated_requests_still_redirect(client):
    for page in ADMIN_PAGES:
        r = await client.get(page)
        assert r.status_code == 303, f"{page} -> {r.status_code}"


async def test_existing_api_routes_still_work_for_legacy_sessions(auth_client):
    """Spot-check the require_auth API surface with a role-less cookie."""
    for path in ("/products", "/keywords", "/invoice/next-number", "/progress"):
        r = await auth_client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"


async def test_logout_still_clears_the_session(auth_client):
    r = await auth_client.get("/logout")
    assert r.status_code == 303
    # The cookie is deleted via a set-cookie with empty value / immediate expiry.
    assert 'session_token="";' in r.headers.get("set-cookie", "") or (
        "session_token=;" in r.headers.get("set-cookie", "")
    )
