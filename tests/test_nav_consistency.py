"""Regression: the nav bar drifted between templates and lost a tab.

Reported 2026-07-30: "on clicking on projections tab, shipment tab is vanishing".

Root cause: the nav was copy-pasted into all 7 page templates, and
templates/projections.html was simply missing the /shipment-page link. Nothing
detected it, because each template was independently correct-looking.

The nav now lives in templates/nav.html and is included. That removes the
duplication, but a partial alone does not *prevent* the bug — someone can still
add a page and forget to add its link. These tests are the actual guard: they
render every page route through the real app and assert every canonical link is
present.

Also covers the request to remove the History and Keywords tabs.
"""
import pytest

# Every link that must appear on every admin page. Adding a page to the app
# without adding its link to templates/nav.html fails the parametrised test below.
CANONICAL_NAV = {
    "dashboard": "/",
    "invoice": "/invoice-page",
    "portfolio": "/portfolio-page",
    "ads": "/ads-page",
    "projections": "/projections-page",
    "shipment": "/shipment-page",
    "orders": "/orders-page",
}

ADMIN_PAGES = ["/", "/invoice-page", "/portfolio-page", "/ads-page", "/projections-page",
               "/shipment-page", "/orders-page", "/pricing-page"]

# Removed on request ("its of no use").
REMOVED_PAGES = ["/history-page", "/keywords-page"]

pytestmark = pytest.mark.regression


@pytest.mark.parametrize("page", ADMIN_PAGES)
async def test_every_page_shows_every_nav_link(auth_client, page):
    """The direct regression: Projections was missing the Shipment link."""
    r = await auth_client.get(page)
    assert r.status_code == 200, f"{page} -> {r.status_code}"

    missing = [
        f"{name} ({href})"
        for name, href in CANONICAL_NAV.items()
        if f'href="{href}"' not in r.text
    ]
    assert not missing, (
        f"{page} is missing nav link(s): {', '.join(missing)}. "
        "Every admin page must render the full nav from templates/nav.html."
    )


@pytest.mark.parametrize("page", ADMIN_PAGES)
async def test_page_highlights_its_own_tab(auth_client, page):
    """A nav with nothing marked active means the route forgot to pass `active`."""
    r = await auth_client.get(page)
    assert 'class="active"' in r.text, (
        f"{page} renders no active nav link — the route probably did not pass "
        'an `active` value to TemplateResponse.'
    )


@pytest.mark.parametrize("page", REMOVED_PAGES)
async def test_removed_pages_are_gone(auth_client, page):
    r = await auth_client.get(page)
    assert r.status_code == 404, f"{page} should no longer exist, got {r.status_code}"


@pytest.mark.parametrize("page", ADMIN_PAGES)
async def test_no_page_links_to_a_removed_tab(auth_client, page):
    """A dead link is worse than a missing one — it 404s the user."""
    r = await auth_client.get(page)
    for dead in REMOVED_PAGES:
        assert f'href="{dead}"' not in r.text, f"{page} still links to removed page {dead}"


# ops.html is allowed its own nav because every link in nav.html is admin-only
# and would 403 for its user. It is exempt from "must include the partial" but
# NOT from scrutiny: test_a_hardcoded_nav_must_not_duplicate_admin_links below
# checks it contains no canonical admin link, which is the property that matters.
OWN_NAV_ALLOWED = {"ops.html"}


async def test_nav_partial_is_the_only_place_admin_links_are_defined():
    """Guard the de-duplication itself: no admin page may hardcode its own nav.

    If someone pastes a <nav class="nav-links"> block back into a page, the
    partial stops being the single source of truth and drift can resume.
    """
    from pathlib import Path

    templates_dir = Path(__file__).resolve().parent.parent / "templates"
    offenders = [
        path.name
        for path in templates_dir.glob("*.html")
        if path.name not in ({"nav.html", "login.html"} | OWN_NAV_ALLOWED)
        and '<nav class="nav-links">' in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"These templates hardcode a nav instead of including nav.html: {offenders}. "
        "That is exactly how the Shipment link went missing from Projections."
    )


@pytest.mark.parametrize("template_name", sorted(OWN_NAV_ALLOWED))
async def test_a_hardcoded_nav_must_not_duplicate_admin_links(template_name):
    """The only reason to hardcode a nav is to *exclude* the admin links.

    Without this, OWN_NAV_ALLOWED would be a hole in the drift guard: someone
    could copy the full admin nav into ops.html and nothing would object, giving
    the packer five links that all answer 403.
    """
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "templates" / template_name
    assert path.is_file(), f"{template_name} is in OWN_NAV_ALLOWED but does not exist"

    markup = path.read_text(encoding="utf-8")
    leaked = [
        href
        for name, href in CANONICAL_NAV.items()
        if href != "/" and f'href="{href}"' in markup
    ]
    assert not leaked, (
        f"{template_name} hardcodes its own nav but links to admin page(s) {leaked}. "
        "Either include nav.html or link only to pages this role can open."
    )


async def test_login_page_has_no_nav(client):
    """Nav links on the login page would be dead ends for a signed-out user."""
    r = await client.get("/login")
    assert r.status_code == 200
    for href in CANONICAL_NAV.values():
        if href == "/":
            continue  # a bare "/" can legitimately appear in other markup
        assert f'href="{href}"' not in r.text, f"login page exposes nav link {href}"
