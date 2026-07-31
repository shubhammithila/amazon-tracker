"""The owner's shipment page: does it talk to endpoints that exist, and safely.

A template is mostly untestable without a browser, so this file deliberately does
not try to test rendering. It tests the two things that have actually broken here
and would break silently again:

**1. Calling an endpoint that no longer exists.** Step 5 moved the plan into the
database and retired ``/shipment/last``, ``/save``, ``/clear``,
``/download-packing-plan`` and ``/download-shipment-file``. The template kept
calling all five. Nothing failed — no test renders JavaScript, and the page loads
fine; it just silently does nothing when you click Save. So every URL the page
fetches is extracted from the file and asserted to be a route the app actually
serves. That is a real end-to-end check with no browser involved.

**2. Re-sorting on the client.** Requirement 3 is "sorted product-wise then
weight-wise on the dashboard *and* in the downloads". Row order therefore has one
home: repository.load_plan_items' ORDER BY. If someone adds ``.sort()`` to this
template to "fix" an ordering complaint, the screen and the four downloads drift
apart again and no other test in the suite notices, because the documents would
still be correct on their own.

Also checks the interpolations are escaped: product names and hold reasons come
from uploaded CSVs, and they are written into the DOM with innerHTML.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "templates" / "shipment.html"

pytestmark = pytest.mark.regression


@pytest.fixture(scope="module")
def source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


# ─── The page must only call endpoints that exist ────────────────────────────

#: Endpoints retired when the plan moved into the database. The template called
#: every one of these after step 5 and nothing complained.
RETIRED = [
    "/shipment/last",
    "/shipment/save",
    "/shipment/clear",
    "/shipment/download-packing-plan",
    "/shipment/download-shipment-file",
]


@pytest.mark.parametrize("path", RETIRED)
def test_the_page_does_not_call_a_retired_endpoint(source, path):
    assert path not in source, (
        f"templates/shipment.html still calls {path}, which was retired when the "
        "plan moved into the database. The button will silently do nothing."
    )


def _referenced_shipment_urls(source: str) -> set[str]:
    """Every /shipment/... URL the page fetches or navigates to.

    Path parameters are normalised to the placeholder FastAPI declares them with,
    so `/shipment/plan/${plan.id}/thresholds` compares equal to the route
    `/shipment/plan/{plan_id}/thresholds`.
    """
    urls = set()
    for raw in re.findall(r"[\"'`](/shipment/[^\"'`\s]*)[\"'`]", source):
        url = raw.split("?")[0]
        # `${...}` interpolations are path params; the names differ from the
        # route's, so normalise both sides to a single marker.
        url = re.sub(r"\$\{[^}]*\}", "{}", url)
        urls.add(url)
    return urls


def _declared_shipment_routes() -> set[str]:
    from app.main import app

    routes = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/shipment"):
            routes.add(re.sub(r"\{[^}]*\}", "{}", path))
    return routes


def test_every_url_the_page_calls_is_a_real_route(source):
    """The regression that hid for a whole step.

    Extracts the URLs from the template and compares them against the app's own
    route table, so a renamed or removed endpoint fails here instead of becoming
    a button that quietly does nothing.
    """
    referenced = _referenced_shipment_urls(source)
    assert referenced, "no /shipment URLs found — the extraction regex is broken"

    declared = _declared_shipment_routes()
    unknown = sorted(referenced - declared)
    assert not unknown, (
        f"templates/shipment.html calls URL(s) the app does not serve: {unknown}\n"
        f"Routes available: {sorted(declared)}"
    )


def test_the_page_uses_the_current_download_routes(source):
    """All four documents must be reachable from the screen.

    Building them and then not linking them is a silent half-delivery of
    requirements 3, 5, 6 and 7.
    """
    for path in (
        "/shipment/download/packing-plan.xlsx",
        "/shipment/download/packing-plan.pdf",
        "/shipment/download/packed.xlsx",
        "/shipment/download/remaining.pdf",
        "/shipment/download/shipment-file.xlsx",
    ):
        assert path in source, f"the page offers no way to download {path}"


@pytest.mark.parametrize("mode", ["remaining", "all", "verified"])
def test_all_three_shipment_file_modes_are_offered(source, mode):
    """The modes give genuinely different quantities; the owner needs all three
    without hand-editing a URL."""
    assert f"mode={mode}" in source


# ─── No client-side sorting: requirement 3 ───────────────────────────────────

def _without_comments(source: str) -> str:
    """The template minus its comments, so prose about sorting is not an offender.

    Three comment syntaxes coexist in this file — Jinja ``{# #}``, HTML
    ``<!-- -->`` and JavaScript ``/* */`` — and the page's own header comment
    explains the no-sorting rule *using the words* ``.sort()``. Scanning the raw
    text therefore fails on the documentation of the rule it is enforcing.

    Comment regions are blanked rather than deleted so line numbers survive for
    the failure message. ``//`` line comments are handled by the caller, because
    stripping them here would eat the rest of any line containing ``https://``.
    """
    def blank(match: re.Match) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    for pattern in (r"\{#.*?#\}", r"<!--.*?-->", r"/\*.*?\*/"):
        source = re.sub(pattern, blank, source, flags=re.S)
    return source


def test_the_page_never_sorts_items_itself(source):
    """Row order has exactly one home, and it is not this file.

    /shipment/active returns items in repository.load_plan_items' ORDER BY, which
    is logic.sort_items, which is what the four documents render. A .sort() here
    would make the screen and the downloads disagree — the exact complaint that
    produced requirement 3.

    Note `.filter()` is fine and is what the search box uses: it preserves order.
    """
    offenders = [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(_without_comments(source).splitlines(), 1)
        if re.search(r"\.sort\s*\(|\.reverse\s*\(|localeCompare", line)
        and not line.strip().startswith("//")
    ]
    assert not offenders, (
        "templates/shipment.html sorts or reverses on the client:\n"
        + "\n".join(f"  {line}" for line in offenders)
        + "\n\nOrder comes from the server (repository.load_plan_items). Sorting "
        "here makes the screen disagree with the four downloads."
    )


# ─── Packed vs shippable must both be visible: requirement 9 ─────────────────

def test_packed_and_shippable_are_both_shown(source):
    """Two separate numbers, not one.

    Held units are packed (the boxes exist, so do not tell the warehouse to pack
    them again) but not shippable (the day is parked). Showing only one of them
    is the subtle bug requirement 9 is about, and it would be invisible until a
    shipment went out short or a day got packed twice.
    """
    assert "Shippable" in source, "the table has no Shippable column"
    assert "Packed" in source, "the table has no Packed column"
    assert "i.shippable" in source and "i.packed" in source, (
        "the page does not read both packed and shippable from the payload"
    )


def test_held_days_are_visibly_marked(source):
    """A held day that looks like any other day gets shipped by accident."""
    assert "badge held" in source or "badge ${st}" in source, (
        "held days carry no visible badge"
    )
    assert "hold_reason" in source, (
        "the page never shows hold_reason, so the owner cannot see WHY a day is held"
    )


def test_release_and_verify_are_both_reachable(source):
    """The threshold suggests; the owner decides. Without a release control the
    system can park stock indefinitely on its own judgement."""
    assert "/verify" in source, "no way to verify a day"
    assert "/release" in source, "no way to force-ship a held day"


# ─── Escaping: CSV-derived strings reach innerHTML ───────────────────────────

def test_untrusted_strings_are_escaped_before_reaching_innerhtml(source):
    """Product names, SKUs and hold reasons come from uploaded CSVs and the DB.

    The table is built with innerHTML, so these have to go through esc(). The
    ASIN matters most: it is interpolated into three inline onchange handlers and
    a data- attribute, where an unescaped quote breaks out of the attribute.
    """
    assert "function esc(" in source, "the template has no escaping helper"
    for expression in ("esc(i.item)", "esc(i.fba_sku)", "esc(i.brand)", "esc(d.hold_reason)"):
        assert expression in source, f"{expression} is interpolated unescaped"
    assert "const asin = esc(i.asin)" in source, (
        "the ASIN is not escaped before being placed in inline event handlers"
    )


def test_raw_item_fields_are_not_interpolated_directly(source):
    """Belt and braces for the check above: the un-escaped forms must not appear.

    Written as an explicit deny-list because `${i.item}` is exactly what someone
    adds back when extending the table, and it reads completely harmless.
    """
    offenders = [
        raw for raw in ("${i.item}", "${i.fba_sku}", "${i.brand}", "${d.hold_reason}")
        if raw in source
    ]
    assert not offenders, (
        f"unescaped CSV-derived interpolation(s) in the table: {offenders} — "
        "wrap them in esc()"
    )


# ─── It still renders, and only for the owner ───────────────────────────────

async def test_the_shipment_page_renders_for_admin(auth_client):
    r = await auth_client.get("/shipment-page")
    assert r.status_code == 200, r.status_code
    assert "Shipment Maker" in r.text


async def test_ops_cannot_open_the_shipment_page(ops_client):
    """It carries projections and purchase-driven numbers, and its buttons are
    admin-only anyway — a page full of 403s reads as a broken app."""
    r = await ops_client.get("/shipment-page")
    assert r.status_code == 403, r.status_code
