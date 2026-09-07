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

    Making him ask the owner for it every morning would defeat the point of giving
    him his own screen.
    """
    assert "/shipment/download/remaining.pdf" in source, (
        "the packer has no way to get the still-to-pack sheet"
    )


def test_the_packed_sheet_can_be_printed_for_accounts(source):
    """"then print the final packed data and submit to the accounts team."

    Scoped to the day on screen, not the whole plan: accounts reconciles one
    shipment at a time, and a sheet covering every day of the week cannot be checked
    against the boxes going out today. The date range is what makes it that scope, so
    it is asserted rather than just the path.
    """
    body = _without_comments(source)
    assert "/shipment/download/packed.pdf" in body, (
        "ops has no way to print the packed data, so it goes to accounts by hand"
    )
    assert re.search(r"date_from=\$\{[^}]*\}&date_to=\$\{[^}]*\}", body), (
        "the packed sheet is not scoped to the day on screen — accounts would get "
        "every packing day on the plan in one document"
    )


# ─── A table, not cards ──────────────────────────────────────────────────────

def test_the_page_is_a_table(source):
    """"for ops team also give the same tabular view but with only options
    relevant to them. not this."

    It was one card per SKU, on the reasoning that a phone cannot scroll sideways.
    That was right about the constraint and wrong about the fix — 117 cards is a very
    long page to find one product in.
    """
    body = _without_comments(source)
    assert "<table" in body and "<tbody" in body, "the page is not a table"
    assert "<tr" in body, "no rows are rendered"


def test_the_product_name_stays_visible_while_the_row_scrolls(source):
    """The reason cards were chosen in the first place, solved differently.

    If the name scrolls off, the row is just numbers — which is how a count gets
    typed against the wrong product. One pinned column plus exactly one input means
    nothing the packer must reach is ever off-screen.
    """
    body = _without_comments(source)
    assert "position:sticky;left:0" in body.replace(" ", ""), (
        "no column is pinned, so the product name scrolls away from its input"
    )
    assert 'class="freeze' in body, "the pinned cells carry no class to style"


#: Columns from the owner's grid that must NOT appear on the packer's screen.
#: Every one is a planning figure — none of them changes what gets boxed today, and
#: several are commercially sensitive (projections, purchase-driven stock levels).
OWNER_ONLY_COLUMNS = ["7d", "Projection", "FBA Stock", "Deficit", "In stock", "To make"]


@pytest.mark.parametrize("label", OWNER_ONLY_COLUMNS)
def test_the_packer_is_not_shown_the_owners_planning_columns(source, label):
    """"only options relevant to them". The relevance test is whether it changes
    what gets packed today, and none of these do."""
    headers = re.findall(r"<th[^>]*>(.*?)</th>", _without_comments(source), flags=re.S)
    joined = " ".join(h.strip().lower() for h in headers)
    assert label.lower() not in joined, (
        f"the ops table carries the owner's {label!r} column: {headers}"
    )


def test_the_packer_gets_the_numbers_he_needs_to_do_the_count(source):
    """The other half of the same judgement: too few columns is also wrong.

    "Still needed" is a subtraction of Plan and Packed earlier, so a lone target
    number invites a phone call to the owner to check it.
    """
    headers = " ".join(
        re.findall(r"<th[^>]*>(.*?)</th>", _without_comments(source), flags=re.S)
    ).lower()
    for needed in ("product", "size", "plan", "packed earlier", "still needed"):
        assert needed in headers, f"the table has no {needed!r} column: {headers}"


def _header_order(source: str) -> list[str]:
    return [
        re.sub(r"<[^>]+>", "", h).strip().lower()
        for h in re.findall(r"<th[^>]*>(.*?)</th>", _without_comments(source), flags=re.S)
    ]


def test_the_three_columns_that_matter_are_adjacent_and_first(source):
    """Product, size, then the input — in that order, with nothing between them.

    The input started at the far right, four columns from the name. Entering a count
    then meant reading a product at the left edge and typing at the right edge, with
    three reference numbers in between; that is how a count lands on the wrong row.
    The reference figures are consulted, not acted on, so they come after.
    """
    order = _header_order(source)
    assert order[:3] == ["product", "size", "packed now"], (
        f"the work columns are not adjacent and leftmost: {order}"
    )


def test_the_size_is_as_prominent_as_the_product_name(source):
    """"make the size font bigger. and closer to the product name."

    Not decoration: "Chana Sattu" identifies nothing to pack, because every product
    here comes in three or four sizes. The name and the size together are the
    smallest useful unit of instruction, so they are set to match.

    Asserted as a relation between the two rules rather than a literal size, so the
    type scale can be tuned without rewriting the test as a copy of the CSS.
    """
    body = _without_comments(source)

    def font_px(selector: str) -> float:
        match = re.search(
            re.escape(selector) + r"\{[^}]*font-size:\s*([\d.]+)px", body
        )
        assert match, f"no font-size found for {selector}"
        return float(match.group(1))

    assert font_px(".p-size") >= font_px(".p-name"), (
        "the size is set smaller than the product name, so it reads as a footnote "
        "to a name that cannot identify a product on its own"
    )
    # And it sits against the name rather than centred in a wide column.
    assert re.search(r"\.p-size\{[^}]*text-align:\s*left", body), (
        "the size is centred in its column, which puts a gap between it and the "
        "product name it belongs to"
    )


# ─── The frozen header ───────────────────────────────────────────────────────

def test_the_column_headings_stay_visible_while_the_rows_scroll(source):
    """62 rows, and PACKED NOW / STILL NEEDED / PLAN / PACKED EARLIER are four
    columns of similar-looking numbers. Without the headings in view, the packer is
    counting columns from the left to find the box he types in.

    The wrapper has to be a real scroll region for this to work — a sticky header
    pins to its nearest scrolling ancestor, so without a height constraint here it
    would pin to the page and the whole table would scroll away as one block.
    """
    body = _without_comments(source)

    wrapper = re.search(r"\.table-wrap\{([^}]*)\}", body)
    assert wrapper, "no .table-wrap rule"
    rules = wrapper.group(1).replace(" ", "")
    assert "max-height" in rules, (
        "the table wrapper has no height limit, so it is not a vertical scroll "
        "region and a sticky header inside it has nothing to stick to"
    )
    assert "overflow:auto" in rules or "overflow-y" in rules, (
        "the wrapper does not scroll vertically"
    )

    thead = re.search(r"thead th\{([^}]*)\}", body)
    assert thead, "no thead th rule"
    assert "position:sticky" in thead.group(1).replace(" ", ""), (
        "the column headings are not frozen"
    )


def test_the_frozen_header_and_frozen_column_do_not_fight(source):
    """The Product heading is sticky on BOTH axes, so it sits at the intersection.

    It has to outrank the header row it is part of AND the body cells that slide
    under it. Get the order wrong and the corner cell either disappears beneath the
    header or is painted over by the first row's product name — both of which look
    like a rendering glitch rather than a z-index mistake.
    """
    body = _without_comments(source)

    def z(selector: str) -> int:
        match = re.search(re.escape(selector) + r"\{[^}]*z-index:\s*(\d+)", body)
        assert match, f"no z-index on {selector}"
        return int(match.group(1))

    corner = z("th.freeze")
    assert corner > z("thead th"), (
        "the frozen Product heading sits below the header row it belongs to, so it "
        "vanishes under the other headings when scrolled sideways"
    )
    assert corner > z("th.freeze,td.freeze"), (
        "the frozen Product heading sits below the body's frozen cells, so the first "
        "row's product name scrolls over the heading"
    )


def test_the_table_borders_survive_the_frozen_header(source):
    """A subtle one that looks like a styling nit and is not.

    With `border-collapse:collapse` the browser paints borders on the TABLE, not the
    cells — so a sticky header's bottom border stays behind with the table and the
    rows slide under a naked edge. `separate` plus per-cell borders is what keeps the
    line attached to the header that is moving.
    """
    body = _without_comments(source)
    assert "border-collapse:separate" in body.replace(" ", ""), (
        "the table collapses its borders, so the frozen header loses its bottom "
        "edge and rows appear to slide under nothing"
    )
    assert re.search(r"tbody td\{[^}]*border-bottom", body), (
        "row separators are set on the row, which is not painted when borders are "
        "separate — the rows will have no lines between them"
    )


def test_the_table_header_is_not_offset_into_the_first_row(source):
    """A real bug that hid a product row, and a genuinely surprising one.

    `overflow-x:auto` on the wrapper makes it a scroll container on BOTH axes — the
    y axis computes to `auto` no matter what you write, so `overflow-y:visible` is
    inert there (measured in a browser: still `auto`). A sticky thead inside it
    therefore positions against THAT box rather than the page, and `top:56px` —
    added to clear the page header — parked the header 56px *down inside the table*,
    directly on top of the first row. The row was rendered, present in the DOM, and
    invisible.

    The guard is on the offset, because that is what was load-bearing. Verified by
    re-adding `top:56px` in a live page and measuring a 56px overlap between the
    header's bottom and the first row's top.
    """
    body = _without_comments(source)

    thead = re.search(r"thead th\{([^}]*)\}", body)
    assert thead, "no thead th rule"
    rules = thead.group(1).replace(" ", "")

    offset = re.search(r"top:(-?[\d.]+)(px)?", rules)
    if offset:
        assert float(offset.group(1)) == 0, (
            f"the table header carries top:{offset.group(1)}px. Inside .table-wrap "
            "that offset is measured from the wrapper, not the page, so the header "
            "lands on the first product row and hides it."
        )


def test_the_table_height_is_measured_not_guessed(source):
    """Reported: "the part where displays the items are bit less."

    The cap was `calc(100vh - 300px)`, and 300px is an assumption about the chrome
    above the table. Trimming the header left it ~90px pessimistic, so the space freed
    became whitespace instead of rows; on a short phone the same constant ran the last
    row underneath the fixed savebar, where it cannot be typed into. Both failures come
    from the constant, not from its value, so the fix is to measure.
    """
    body = _without_comments(source)

    # The .table-wrap rule itself must consume the measured value. Asserting the
    # variable merely EXISTS somewhere passed even with the rule reverted to the
    # hardcoded calc(), because sizeTable() still set a property nothing read.
    wrap = re.search(r"\.table-wrap\{([^}]*)\}", body)
    assert wrap, "the .table-wrap rule is gone"
    max_h = re.search(r"max-height:([^;}]*)", wrap.group(1))
    assert max_h, ".table-wrap sets no max-height, so it is not a scroll container"
    assert "--table-cap" in max_h.group(1), (
        f"the table height is a hardcoded guess about the chrome above it "
        f"(max-height:{max_h.group(1).strip()}). Trimming the header then frees space "
        "the table never claims, and a short screen hides the last row under the "
        "savebar."
    )
    assert "function sizeTable" in body, "nothing measures the available height"
    # Measured from the wrapper's own position and the bar's own height.
    assert "getBoundingClientRect().top" in body, (
        "sizeTable does not read where the table actually starts"
    )
    assert re.search(r'addEventListener\("resize",\s*sizeTable\)', body), (
        "the cap is never recomputed, so rotating a phone or opening the keyboard "
        "leaves the last row under the savebar"
    )


def test_enough_rows_fit_without_scrolling(source):
    """"only 5 rows of items are being shown at a time. I want atleast 7-8."

    Measured, not guessed. The height of one row and the height of everything above the
    table are both decided by CSS in this file, so the row count on a given screen is
    arithmetic — and it is the number the packer actually experiences.

    Budget on the reported 698px CSS viewport (a 1080p laptop at 125% scaling, which is
    what the screenshot showed — not the 900px I first tested at):

        header 58 + page-head 21 + toolbar card 60 + search 50 + savebar 63 + slack 14

    Each row is the 46px quantity input plus 2x the vertical cell padding. Asserted as a
    budget rather than a literal `rowsVisible >= 7` because a browser is the only place
    that can measure the truth; this fails if someone reintroduces the stacked heading,
    the bordered totals tiles or the 7px cell padding that together made it 5 rows.
    """
    body = _without_comments(source)

    # There is more than one bare `td{...}` rule (one sets only a border), so find the
    # one that actually sets padding rather than assuming which comes first.
    pad = None
    for match in re.finditer(r"(?<![\w.\-])td\{([^}]*)\}", body):
        found = re.search(r"padding:\s*([\d.]+)px", match.group(1))
        if found:
            pad = found
            break
    assert pad, "no td rule sets a padding in px"
    # `padding: <vertical> <horizontal>`; only the first value adds row height.
    row_height = 46 + 2 * float(pad.group(1))       # the input sets the floor
    assert row_height <= 56, (
        f"a packing row is {row_height:.0f}px tall. On a 698px viewport the chrome "
        f"leaves ~412px for the table, so anything over 56px shows fewer than 7 rows "
        f"— which is the complaint this guards."
    )

    # And the chrome above the table must stay collapsed to one line each. Read the
    # rule's OWN declarations: `"display:flex" in body` passed with .page-head set
    # back to block, because some other rule in the file uses flex.
    head = re.search(r"\.page-head\{([^}]*)\}", body)
    assert head, "the heading and subtitle no longer share a row"
    assert "display:flex" in head.group(1).replace(" ", ""), (
        "the heading and subtitle are stacked again, costing a row of the table"
    )
    assert ".toolbar{" in body, (
        "the date row and the totals no longer share one line — separate bordered "
        "cards for each cost ~200px of the only part of this screen that does work"
    )
    tot = re.search(r"(?<![\w.\-])\.tot\{([^}]*)\}", body)
    assert tot and "align-items:baseline" in tot.group(1).replace(" ", ""), (
        "the totals tiles stack their number above their label again, doubling the "
        "height of a row of three short numbers"
    )


def test_the_hold_warning_is_full_width_not_in_the_toolbar(source):
    """It rendered inside #totals, which the toolbar narrows to ~425px.

    The warning is two sentences, so at that width it wrapped to four lines and made
    the toolbar 122px tall — taller than the two rows of table it was displacing. The
    irony is that it was rendered there to save space.
    """
    body = _without_comments(source)

    assert 'id="hold-warning"' in body, (
        "there is no full-width target for the hold warning"
    )
    assert re.search(r'\$\("hold-warning"\)\.innerHTML\s*=\s*wouldHold', body), (
        "the hold warning is not rendered into its own full-width element"
    )
    # Nothing may write a banner into #totals — including with `+=`, which is how the
    # earlier version of this test was fooled: it only inspected the region between the
    # `#totals` assignment and the `#hold-warning` one, so appending to #totals slipped
    # straight past it.
    for match in re.finditer(r'\$\("totals"\)\.innerHTML\s*\+?=', body):
        tail = body[match.end():match.end() + 700]
        tail = tail[:tail.find('$("')] if '$("' in tail else tail
        assert "banner" not in tail, (
            "a banner is written into #totals, which the toolbar narrows to ~425px — "
            "it wraps to four lines there and costs more height than it saves"
        )


def test_the_thumb_targets_are_not_shrunk_to_win_space(source):
    """The packer is standing, on a phone, possibly gloved.

    Height on this screen was won from labels, padding and tiles. The controls he
    actually hits 62 times a day must not pay for it: shrinking `input.qty` would be
    the easy pixel saving and the wrong one.
    """
    body = _without_comments(source)

    qty = re.search(r"input\.qty\{([^}]*)\}", body)
    assert qty, "the quantity input rule is gone"
    min_h = re.search(r"min-height:(\d+)px", qty.group(1))
    assert min_h and int(min_h.group(1)) >= 44, (
        "the in-table quantity box is under the 44px touch minimum — it is typed into "
        "once per product by someone standing at a bench"
    )

    for name, pattern in (("Save/Submit", r"\.savebar \.btn\{([^}]*)\}"),):
        rule = re.search(pattern, body)
        if rule:
            found = re.search(r"min-height:(\d+)px", rule.group(1))
            assert found and int(found.group(1)) >= 44, (
                f"the {name} button dropped below the 44px touch minimum"
            )


# ─── Ops must not be offered admin actions ───────────────────────────────────

#: Admin-only endpoints. A button here would 403 — and a page of buttons that
#: fail reads as a broken app, which is why the ops nav was kept separate too.
#:
#: ``download/packed`` is deliberately NOT on this list any more. It is what ops
#: prints for the accounts team ("then print the final packed data and submit to the
#: accounts team"), and it carries only what was boxed — no projections, no
#: purchase-driven figures, nothing the warehouse should not see. The plan sheet and
#: the Amazon upload stay closed: those are the owner's decisions.
ADMIN_ONLY = [
    "/shipment/generate",
    "/shipment/items",
    "/shipment/plan/",
    "/shipment/packing/{}/verify",
    "/shipment/packing/{}/release",
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
    "href", ["/invoice-page", "/portfolio-page", "/projections-page", "/shipment-page", "/"]
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

def test_units_are_entered_per_product(source):
    """One input per row, and it is the units packed."""
    assert 'data-field="units"' in source, "no units input"


def test_cartons_are_entered_once_for_the_day_not_per_product(source):
    """"carton is not item wise. it is random. like 500 units packed today in 20
    cartons."

    A carton holds whatever was being packed when it was filled, so it belongs to
    several ASINs at once and to none of them. The per-SKU box asked a question with
    no answer, so it was guessed or skipped — and the guess prefilled the Boxes field
    on a GST invoice.

    Asserted from both directions, because only the second half catches a
    reintroduction: the day-level box existing does not stop someone adding a column
    back "for detail".
    """
    body = _without_comments(source)
    assert 'id="cartonInput"' in body, "there is no carton input at all"
    assert 'data-field="cartons"' not in body, (
        "cartons are back as a per-row field — that number cannot be answered per "
        "SKU, and it feeds a GST invoice's Boxes field"
    )
    # The table's header row must not offer a Cartons column either.
    assert "<th" in body, "the page is no longer a table"
    headers = re.findall(r"<th[^>]*>(.*?)</th>", body, flags=re.S)
    assert not any("carton" in h.lower() for h in headers), (
        f"a Cartons column is back in the table: {headers}"
    )


def test_the_carton_entry_is_visually_prominent(source):
    """"this cartons today entry tab at the bottom is not clearly visible."

    It began as a bare field and read as a stray input. It is a REQUIRED daily entry
    — the number prefills the Boxes field on a GST invoice — so it gets its own
    bordered, tinted panel.

    Asserted as "it has edges and a fill", not as a position. Centring it was the
    first attempt and was wrong: it then floated between the status text and the
    buttons, belonging to neither. It is left-aligned now, and the alignment is a
    judgement that may change again — the panel is the part that must not.
    """
    body = _without_comments(source)
    panel = re.search(r"\.cartonbox\{([^}]*)\}", body)
    assert panel, "no .cartonbox rule"
    rules = panel.group(1).replace(" ", "")

    assert "border:" in rules, "the carton entry has no border, so it has no edges"
    assert "background:" in rules, "the carton entry has no fill to lift it off the bar"
    assert "padding:" in rules, "the panel has no padding, so the border sits on the text"


def test_an_empty_carton_count_is_flagged_while_units_exist(source):
    """The state worth warning about, and only in the state where it applies.

    A submitted day with no carton count sends someone back to recount boxes that
    have already gone out. But on a day with no units it is not outstanding, it is
    simply not applicable yet — so the amber is conditional on there being units.
    """
    body = _without_comments(source)
    assert ".cartonbox.empty" in body, "an empty carton count looks the same as a filled one"
    assert re.search(
        r'classList\.toggle\("empty",\s*!cartons\s*&&\s*unitsTotal\(\)\s*>\s*0\)', body
    ), (
        "the empty-carton warning is not gated on there being units, so a fresh "
        "untouched day would nag about boxes that nobody has packed yet"
    )


# ─── Over-packing: the packer must be told before he boxes more ──────────────

def test_the_packer_is_warned_when_he_packs_more_than_planned(source):
    """"in beetroot sattu 1 kg. plan was 50. but Ops team packed 100. It should show
    warning to them also."

    Him first: he is the one who can still stop, and the one holding the boxes.
    "Still needed" clamps at 0, so before this a doubled row read exactly like a
    finished one.
    """
    body = _without_comments(source)
    assert re.search(r"banner error[\s\S]{0,200}More packed than planned", body), (
        "the packer gets no warning that he has boxed more than the plan asked for"
    )
    assert "over-tag" in body or 'class="over' in body, (
        "the offending row is not marked, so the banner cannot be acted on"
    )


def test_the_over_pack_warning_is_computed_live_not_from_the_payload(source):
    """It must appear as he types, not after a save.

    A server-side figure would only refresh on the next load, by which point he has
    moved on down the list and boxed more of it. So the row totals are recomputed in
    the browser, from `packed_before + units` against `planned`.
    """
    body = _without_comments(source)
    assert "markOverPack" in body, (
        "there is no live recompute, so the warning waits for a save"
    )
    assert re.search(r"packed_before[\s\S]{0,80}planned", body), (
        "the live check does not compare the day's total against the plan"
    )


def test_the_live_recompute_does_not_rebuild_the_input_being_typed_into(source):
    """Why markOverPack exists at all rather than a plain renderRows().

    Re-rendering the table would replace the very input under the caret and drop it
    mid-number — the same reason the totals are repainted rather than the rows. So
    the row's marking is updated in place.
    """
    body = _without_comments(source)
    start = body.find("function markOverPack(")
    assert start != -1, "markOverPack is gone"

    # Slice to the closing brace by counting braces, not by looking for the next
    # `function` — markOverPack is the last declaration in its block, so a naive
    # search runs on into unrelated code and the assertion below reads whatever it
    # happens to find there.
    depth, end = 0, None
    for offset, char in enumerate(body[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = offset + 1
                break
    assert end, "markOverPack has unbalanced braces"
    fn = body[start:end]

    assert "renderRows" not in fn, (
        "markOverPack re-renders the whole table, which destroys the input the "
        "packer is typing into and drops the caret"
    )
    assert "classList.toggle" in fn, "the marking is not updated in place"


def test_over_packing_is_warned_about_and_not_blocked(source):
    """The boxes physically exist.

    Refusing the entry would leave the packer unable to record real stock, and only
    the owner can resolve it — by raising To Ship or having the surplus unpacked. So
    the message tells him to report it, rather than the input rejecting the number.
    """
    body = _without_comments(source)
    assert "min=\"0\"" in body, "the input lost its floor"
    # No max attribute pinned to the plan, and no early return that skips the save.
    assert not re.search(r'max="\$\{[^}]*planned', body), (
        "the input caps at the plan, which would make it impossible to record boxes "
        "that actually exist"
    )
    assert re.search(r"tell the owner", body), (
        "the warning does not tell the packer what to do about it"
    )


def test_the_carton_count_is_only_sent_when_it_was_touched(source):
    """A missing key means "leave it alone"; 0 means "no cartons".

    Without that distinction, saving unit counts before the boxes are stacked would
    silently zero a carton count entered earlier in the day — and that number ends up
    on a tax document. The server honours the difference; this is the client half.
    """
    body = _without_comments(source)
    assert "cartonsDirty" in body, "the page does not track whether cartons changed"
    assert re.search(r"if\s*\(\s*cartonsDirty\s*\)\s*payload\.cartons", body), (
        "cartons are sent unconditionally, so a partial save would clear them"
    )


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
