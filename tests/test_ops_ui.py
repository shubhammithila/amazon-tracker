"""The packer's screen: does it call real endpoints, and can he only do his job?

Same approach as tests/test_shipment_admin_ui.py, for the same reason — no test
renders JavaScript, so a page can call a dead endpoint and still look perfectly
healthy. That is exactly how the admin template spent a whole step silently
doing nothing when you clicked Save.

What this file pins:

* Every ``/shipment/...`` URL the page fetches is a route the app serves.
* The page never re-sorts. The packer works down a printed morning PDF and down
  this screen at the same time; if the screen's order differed from the paper's
  he would hunt for every line. Both come from repository.load_plan_items.
* It offers no admin action. The only writes ops may perform are packing rows —
  that write separation is what lets two people work at once without locking.
* CSV-derived strings are escaped before reaching innerHTML, and rows are
  addressed by index rather than by an escaped ASIN. That second one is not
  theoretical tidiness: looking a row up by its *escaped* ASIN silently drops
  the count for any ASIN containing ``&``, with no error shown.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "templates" / "ops.html"

pytestmark = pytest.mark.regression


@pytest.fixture(scope="module")
def source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _without_comments(source: str) -> str:
    """The template minus its comments, so prose is never mistaken for code.

    The page's own header comment explains the no-sorting rule using the words
    ``.sort()``, so scanning the raw text would fail on the documentation of the
    rule being enforced. Regions are blanked rather than removed to keep line
    numbers usable in failure messages.
    """
    def blank(match: re.Match) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    for pattern in (r"\{#.*?#\}", r"<!--.*?-->", r"/\*.*?\*/"):
        source = re.sub(pattern, blank, source, flags=re.S)
    return source


# ─── Only real endpoints ─────────────────────────────────────────────────────

def _referenced_shipment_urls(source: str) -> set[str]:
    """Every /shipment/... URL the page fetches or navigates to.

    ``${...}`` interpolations are path params whose names differ from the
    route's, so both sides normalise to a single marker.
    """
    urls = set()
    for raw in re.findall(r"[\"'`](/shipment/[^\"'`\s]*)[\"'`]", source):
        url = raw.split("?")[0]
        url = re.sub(r"\$\{[^}]*\}", "{}", url)
        urls.add(url)
    return urls


def _declared_shipment_routes() -> set[str]:
    """Every /shipment route the app serves, plus the concrete forms of any
    format-parameterised ones.

    ``/download/plan.{fmt}`` is one route serving two real URLs. Normalising the
    parameter to ``{}`` would compare "/download/plan.{}" against the literal
    "/download/plan.xlsx" a template actually calls, and this guard would fail on
    working links — so the concrete variants are expanded here instead.
    """
    from app.main import app

    routes = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/shipment"):
            continue
        if path.endswith(".{fmt}"):
            stem = path[: -len(".{fmt}")]
            routes.update({f"{stem}.xlsx", f"{stem}.pdf"})
        routes.add(re.sub(r"\{[^}]*\}", "{}", path))
    return routes


def test_every_url_the_page_calls_is_a_real_route(source):
    referenced = _referenced_shipment_urls(source)
    assert referenced, "no /shipment URLs found — the extraction regex is broken"

    unknown = sorted(referenced - _declared_shipment_routes())
    assert not unknown, (
        f"templates/ops.html calls URL(s) the app does not serve: {unknown}\n"
        f"Routes available: {sorted(_declared_shipment_routes())}"
    )


def test_the_page_can_read_save_and_submit_a_day(source):
    """The three things the packer's whole job needs.

    Requirement 5 is only delivered if he can load the day, record units and
    cartons against it, and close it. A page missing any one of these looks
    finished and is not.
    """
    urls = _referenced_shipment_urls(source)
    assert "/shipment/packing/{}" in urls, "cannot read or save a day's packing"
    assert "/shipment/packing/{}/submit" in urls, "no way to submit the day"


def test_the_morning_pdf_is_reachable_from_the_page(source):
    """Requirement 5's "next morning he can download the remaining ones in a pdf".

    It is the one document ops is allowed to pull, and making him ask the owner
    for it every morning would defeat the point of giving him his own screen.
    """
    assert "/shipment/download/remaining.pdf" in source, (
        "the packer has no way to get the still-to-pack sheet"
    )


# ─── Ops must not be offered admin actions ───────────────────────────────────

#: Admin-only endpoints. A button here would 403 — and a page of buttons that
#: fail reads as a broken app, which is why the ops nav was kept separate too.
ADMIN_ONLY = [
    "/shipment/generate",
    "/shipment/items",
    "/shipment/plan/",
    "/shipment/packing/{}/verify",
    "/shipment/packing/{}/release",
    "/shipment/download/packed.xlsx",
    "/shipment/download/packed.pdf",
    "/shipment/download/plan.xlsx",
    "/shipment/download/plan.pdf",
    "/shipment/download/shipment-file.xlsx",
]


@pytest.mark.parametrize("path", ADMIN_ONLY)
def test_the_ops_page_offers_no_admin_action(source, path):
    normalised = {re.sub(r"\$\{[^}]*\}", "{}", u) for u in
                  re.findall(r"[\"'`](/shipment/[^\"'`\s]*)[\"'`]", source)}
    hits = [u for u in normalised if u.startswith(path)]
    assert not hits, (
        f"templates/ops.html references admin-only {path} ({hits}) — ops would "
        "get a 403, and the owner's numbers must not be offered to the warehouse"
    )


@pytest.mark.parametrize(
    "href", ["/invoice-page", "/churn-page", "/projections-page", "/shipment-page", "/"]
)
def test_the_ops_page_links_to_no_admin_page(source, href):
    """Also asserted over HTTP in test_shipment_auth_roles.py; pinned here too
    because the failure mode is someone adding nav.html to this file for
    consistency, and that is a template-level mistake."""
    assert f'href="{href}"' not in source, f"ops page links to admin page {href}"


# ─── No client-side sorting ──────────────────────────────────────────────────

def test_the_page_never_sorts_rows_itself(source):
    """The screen order and the printed PDF order must be the same order.

    Both come from repository.load_plan_items' ORDER BY. If this page sorted
    differently the packer would be reading down paper that does not match his
    phone, which is worse than either order alone.
    """
    offenders = [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(_without_comments(source).splitlines(), 1)
        if re.search(r"\.sort\s*\(|\.reverse\s*\(|localeCompare", line)
        and not line.strip().startswith("//")
    ]
    assert not offenders, (
        "templates/ops.html sorts or reverses on the client:\n"
        + "\n".join(f"  {line}" for line in offenders)
        + "\n\nOrder comes from the server, and the morning PDF prints in that "
        "same order. Sorting here makes the phone disagree with the paper."
    )


# ─── The packing entry surface ───────────────────────────────────────────────

def test_both_units_and_cartons_can_be_entered(source):
    """Requirement 7: cartons are entered daily, alongside units.

    Cartons prefill the invoice's Boxes field, so a units-only screen quietly
    removes the entire point of that requirement.
    """
    assert 'data-field="units"' in source, "no units input"
    assert 'data-field="cartons"' in source, "no cartons input"


def test_rows_are_addressed_by_index_not_by_escaped_asin(source):
    """Guards a silent-data-loss bug, not a style preference.

    An earlier draft passed the *escaped* ASIN into an inline handler and then
    looked the row up with `rows.find(i => i.asin === asin)`. esc() turns `&`
    into `&amp;`, so for any such ASIN the lookup misses, the assignment never
    happens, and the packer's count is discarded with no error on screen. Using
    the integer index removes the class of bug and needs no inline handler.

    Scanned with comments blanked, for the same reason the sort guard is: a
    comment *explaining* this rule must not be able to break it. That happened —
    a comment quoting the banned pattern to say why it is banned failed the test,
    which teaches the next person to delete the explanation rather than keep the
    rule.
    """
    body = _without_comments(source)

    assert 'data-index="${index}"' in body, (
        "inputs no longer carry data-index — rows must not be addressed by an "
        "escaped ASIN, which silently drops counts for ASINs containing '&'"
    )
    assert "oninput=" not in body, (
        "an inline oninput handler is back; interpolating a CSV-derived string "
        "into an attribute is what data-index exists to avoid"
    )
    assert "i.asin ===" not in body, (
        "a row is being looked up by ASIN equality again — if that ASIN came "
        "from esc() the comparison silently fails and the count is lost"
    )


def test_a_dropped_entry_is_explained_to_the_packer(source):
    """The owner removed a row while it was still on this phone.

    The server drops those entries rather than storing units against a row that
    is on no plan, no document and no invoice — see the 409 in
    tests/test_shipment_exclusion.py. But the server refusing quietly is only half
    a fix: if this screen ignored the `dropped` list, the packer would watch his
    count vanish on the next refresh with no explanation, which is exactly the
    silent data loss the guard was built to prevent.

    So the message must name the items, and the list must reload so the removed
    rows stop inviting him to type the same count again.
    """
    body = _without_comments(source)
    assert "data.dropped" in body or "dropped" in body, (
        "the save response's `dropped` list is ignored — the packer's count would "
        "disappear on the next refresh with nothing on screen explaining why"
    )
    assert "banner warn" in body, "a dropped entry produces no visible warning"


def test_the_empty_state_accounts_for_an_unreleased_draft(source):
    """"No plan exists" became wrong once drafts arrived.

    The owner can be sitting on a finished-looking plan this screen cannot see, so
    telling the packer no plan exists sends him to ask for something that is
    already there. The message names the actual missing step instead.
    """
    body = _without_comments(source)
    assert "released" in body.lower(), (
        "the empty state still claims no plan exists, which is misleading while a "
        "draft is waiting to be finalised"
    )
    assert "Finalise" in body, (
        "the packer is not told which action unblocks him, so he cannot ask for it "
        "by name"
    )


def test_the_hold_threshold_is_shown_before_submitting(source):
    """Requirement 9 from the packer's side.

    He should see that the day is below the minimum *before* he submits, not
    discover it afterwards. The client mirrors logic.is_held's AND rule as a
    hint; the server still decides.
    """
    assert "min_cartons" in source and "min_units" in source, (
        "the page never reads the thresholds, so it cannot warn about a hold"
    )
    assert "wouldHold" in source, "no pre-submit warning that the day will be held"


def test_a_locked_day_cannot_be_edited(source):
    """A verified or shipped day may already be on a GST invoice.

    The server 409s on the write regardless; disabling the inputs is so the
    packer finds out before he types fifty numbers rather than after.
    """
    assert "isLocked" in source, "no notion of a locked day"
    assert '"verified"' in source and '"shipped"' in source, (
        "isLocked does not cover both verified and shipped"
    )


def test_unsaved_work_is_not_silently_lost(source):
    """Warehouse wifi drops, and counting is slow, manual work.

    Autosave-per-keystroke was rejected for exactly this reason: a failed
    autosave mid-list loses an hour of counting with no sign it happened.
    """
    assert "beforeunload" in source, "leaving with unsaved counts asks nothing"
    assert "dirty" in source, "the page does not track which rows are unsaved"


# ─── Escaping ────────────────────────────────────────────────────────────────

def test_untrusted_strings_are_escaped_before_reaching_innerhtml(source):
    """Product names, SKUs and ASINs come from an uploaded CSV."""
    assert "function esc(" in source, "the template has no escaping helper"
    for expression in ("esc(i.item)", "esc(i.fba_sku)", "esc(i.asin)"):
        assert expression in source, f"{expression} is interpolated unescaped"


def test_raw_fields_are_not_interpolated_directly(source):
    """Deny-list counterpart: `${i.item}` is what someone adds back when
    extending a card, and it reads completely harmless."""
    offenders = [
        raw
        for raw in ("${i.item}", "${i.fba_sku}", "${i.asin}", "${data.hold_reason}")
        if raw in source
    ]
    assert not offenders, (
        f"unescaped CSV-derived interpolation(s): {offenders} — wrap them in esc()"
    )


# ─── It renders, for both roles ──────────────────────────────────────────────

async def test_the_ops_page_renders_for_ops(ops_client):
    r = await ops_client.get("/ops-page")
    assert r.status_code == 200, r.status_code
    assert "Daily Packing" in r.text


async def test_the_owner_can_open_the_ops_page_too(auth_client):
    """The owner supervises packing, so this screen is not closed to him."""
    r = await auth_client.get("/ops-page")
    assert r.status_code == 200, r.status_code
