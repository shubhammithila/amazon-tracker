"""**Every route, signed out, sends you to the login page — and the list is generated.**

Asked for as *"on the overall app level. (when someone is not logged in and opens any page or link)
it should go to the login page"*.

The pages and APIs already did. What did not, and what nobody had looked at, were the four routes
**FastAPI mounts by itself**: ``/docs``, ``/redoc``, ``/docs/oauth2-redirect`` and
``/openapi.json``. The last was the one that mattered — it returned 200 to anyone and enumerated
**109 route paths** with their parameters, request bodies and complete docstrings, including
``/ads/apply``, the only route in this app that spends money, together with the prose describing
its guardrails. ``/docs`` rendered the same thing as a form with an Execute button.

They are now removed (``docs_url=None`` and friends) rather than gated. Removed is stronger: a
docs page that does not exist needs no dependency to protect it, and cannot be re-opened by
loosening one later.

**The route list is enumerated from the app, not typed out here.** A hand-written list of pages is
exactly how ``/docs`` went unnoticed for the life of the project — the earlier
``test_unauthenticated_requests_still_redirect`` checked five paths it had been given and passed
while ``/openapi.json`` served the schema. This walks ``app.routes``, so a route added tomorrow is
covered without anyone remembering to add it.
"""
import re

import httpx
import pytest

from app.main import app

pytestmark = pytest.mark.regression

#: The only routes a signed-out visitor may have, each for a stated reason.
#:
#: * ``/login`` — the form itself. Redirecting it to itself is a loop.
#: * ``/logout`` — must work from any state; it clears a cookie and redirects to /login anyway.
#: * ``/no-access`` — where a signed-IN user with no areas lands. Reachable without a cookie, and
#:   deliberately so: it names no data, and bouncing it to /login is precisely the loop it exists
#:   to break (the natural response to landing back on the form is to try the password again).
#: * ``/health`` — what ``deploy/update-ec2.sh`` polls over HTTP after a restart to decide whether
#:   to roll back. It returns one word and no account data. Gating it would make every deploy
#:   look like a failed one.
#: * ``/static/*`` — the shared stylesheet and scripts. No account data, and the login page needs
#:   them to render.
PUBLIC = {
    ("GET", "/login"),
    ("POST", "/login"),
    ("GET", "/logout"),
    ("GET", "/no-access"),
    ("GET", "/health"),
}


def _probe_paths():
    """Every route in the app as ``(method, path, probe_url)``, path parameters filled in.

    Placeholders become ``1`` — a value that parses as an id or reads as a harmless string. The
    guard runs as a dependency, BEFORE the handler, so no route ever gets far enough to care what
    the value means.
    """
    out = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path or path.startswith("/static"):
            continue
        for method in sorted(getattr(route, "methods", set()) or set()):
            if method in ("HEAD", "OPTIONS"):
                continue
            out.append((method, path, re.sub(r"\{[^}]+\}", "1", path)))
    return out


ROUTES = _probe_paths()


def test_there_are_routes_to_check():
    """A guard on the generated list.

    If `app.routes` ever stops yielding what this expects, every parametrised case below would be
    skipped and the file would report success while checking nothing — the failure mode that makes
    a generated test worse than a hand-written one if it is not itself pinned.
    """
    assert len(ROUTES) > 100, f"only {len(ROUTES)} routes enumerated — the walk is broken"


@pytest.mark.parametrize("method,path,probe", ROUTES, ids=lambda v: str(v))
async def test_every_route_sends_a_signed_out_visitor_to_the_login_page(method, path, probe):
    """The whole request surface, one case per route, with no allow-list to keep up to date."""
    if (method, path) in PUBLIC:
        pytest.skip(f"{method} {path} is deliberately public — see PUBLIC")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        response = await client.request(method, probe)

    assert response.status_code == 303, (
        f"{method} {path} answered {response.status_code} to a request with no session cookie"
    )
    assert response.headers.get("location") == "/login", (
        f"{method} {path} redirected to {response.headers.get('location')!r}, not /login"
    )


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
async def test_the_api_schema_and_docs_are_not_served_at_all(path):
    """**404, not 303 — they are removed rather than protected.**

    `/openapi.json` published a map of all 109 routes to anyone who asked, with request bodies and
    the docstrings explaining what each one does. Fails against the old code, which answered 200.

    A 404 is the assertion because a redirect would mean the routes still exist and are one
    loosened dependency away from being public again.

    **`openapi_url=None` is the load-bearing one, and that is measured.** A mutation restoring
    `docs_url="/docs"` alone survived this test, and checking why showed the mutation was inert:
    FastAPI does not mount the docs UI without a schema to render, so `/docs` stayed 404 anyway.
    Restoring `openapi_url` IS caught. Recorded here so a future reader does not "fix" the
    surviving mutation by adding an assertion for behaviour that cannot occur.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        response = await client.get(path)

    assert response.status_code == 404, (
        f"{path} answered {response.status_code} — the API schema is public"
    )


async def test_an_unknown_path_is_a_404_rather_than_a_redirect():
    """A typo must not be answered with a login page.

    A catch-all redirect would be the lazy way to satisfy "any link goes to login", and it would
    make every mistyped URL look like a session timeout — so a genuinely missing route says so.
    This is the boundary of the requirement, recorded as a decision rather than left to chance.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        for path in ("/portfolio-pag", "/nonexistent", "/ads/not-a-route"):
            response = await client.get(path)
            assert response.status_code == 404, f"{path} -> {response.status_code}"


async def test_the_public_list_is_exactly_what_is_public():
    """The allow-list cannot silently grow.

    `PUBLIC` is a set of exemptions, and an exemption nobody notices is how an authenticated route
    becomes an open one. Asserted both ways: every entry must really be public (so a stale entry
    hiding a now-protected route is caught), and nothing outside it may be.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        for method, path in sorted(PUBLIC):
            response = await client.request(method, path)
            # "Public" means REACHED THE HANDLER, not "returned 200". Three shapes are legitimate:
            #   200 — the login form, /no-access, /health
            #   422 — POST /login with no form body: FastAPI validated and refused, which proves
            #         the request was not intercepted by the auth dependency
            #   303 — /logout, which redirects to /login BY DESIGN after clearing the cookie
            # So the check is "not blocked", and /logout's redirect is named rather than merely
            # tolerated: an accidental 303 from any other entry here would be the auth guard.
            if response.status_code == 303:
                assert path == "/logout", (
                    f"{method} {path} is listed as public but was redirected to "
                    f"{response.headers.get('location')!r} — it is behind the auth guard"
                )
                assert response.headers.get("location") == "/login"
                continue
            assert response.status_code in (200, 422), (
                f"{method} {path} is listed as public but answered {response.status_code}"
            )
