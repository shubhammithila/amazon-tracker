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
    """Three documents in two formats each, plus the Amazon upload.

    Building a document and then not linking it is a silent half-delivery, and
    offering only one format of a document that has two is the same thing in
    miniature — so both formats of all three are required here.
    """
    body = _without_comments(source)

    # plan and remaining are linked as literal URLs.
    for name in ("plan", "remaining"):
        for fmt in ("xlsx", "pdf"):
            path = f"/shipment/download/{name}.{fmt}"
            assert path in body, f"the page offers no way to download {path}"

    # packed is built by dlPacked(), which appends the optional date range, so the
    # literal path is never in the source. Assert the pieces instead: the helper
    # exists, both formats call it, and it targets the packed route.
    assert "/shipment/download/packed." in body, "dlPacked does not target packed"
    for fmt in ("xlsx", "pdf"):
        assert f"dlPacked('{fmt}')" in body, f"no {fmt} button for the packed data"

    assert "/shipment/download/shipment-file.xlsx" in body, (
        "the Amazon upload file is not linked"
    )


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


# ─── The per-day columns ─────────────────────────────────────────────────────

def test_the_day_column_does_not_read_a_per_entry_carton_field(source):
    """The "100/undefined" bug, and its shape is worth remembering.

    The day columns rendered `${e.units}/${e.cartons}` per ASIN. When cartons moved
    to the day (a carton holds whatever was being packed, so it belongs to no single
    SKU) that field stopped existing — and JavaScript does not complain about reading
    a missing property, it prints `undefined`. Every day cell read "100/undefined".

    So: no `.cartons` read off a packing ENTRY anywhere in this file. The day's own
    `total_cartons` is fine, and is what the day cards show.
    """
    body = _without_comments(source)
    offenders = [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(body.splitlines(), 1)
        # e.cartons / entry.cartons / x.cartons are per-entry reads. d.total_cartons
        # and carryOver.cartons are day-level and legitimate.
        if re.search(r"\b(e|entry|x)\.cartons\b", line)
    ]
    assert not offenders, (
        "a per-entry carton field is being read again:\n" + "\n".join(offenders)
        + "\n\nCartons live on the DAY (total_cartons). Reading them off an entry "
        "prints 'undefined', which is exactly what shipped."
    )


def test_the_day_column_says_what_its_number_is(source):
    """It used to promise "u / c" for a pair that no longer exists, which is how the
    undefined went unnoticed: the heading agreed with the bug."""
    body = _without_comments(source)
    assert "u / c" not in body, (
        'the day column still promises "u / c" — there is no per-SKU carton figure'
    )
    assert "dayLabel(d.pack_date)" in body, (
        "the day heading is not built through dayLabel"
    )


def test_the_day_heading_names_the_month(source):
    """Asked for: "keep it 10th aug, 11th Aug like this."

    It was `pack_date.slice(5)` — "08-10" — genuinely ambiguous to anyone who reads
    dates day-first, and everyone in this business does. A named month cannot be
    misread as the 8th of October.
    """
    body = _without_comments(source)
    assert "pack_date.slice(5)" not in body, (
        "the day heading is back to a numeric month, which reads as either 8 Oct or "
        "10 Aug depending on the reader"
    )
    assert '"Aug"' in body or "'Aug'" in body, (
        "there is no month-name table, so the heading cannot say Aug"
    )


def test_the_date_is_not_parsed_with_the_date_constructor(source):
    """`new Date("2026-08-10")` is UTC midnight, then rendered in local time.

    Harmless in IST, off by one in any negative-offset zone — the same class of bug
    the packing-date picker already avoids by building its default from local parts.
    Splitting the ISO string cannot drift.
    """
    body = _without_comments(source)
    start = body.find("function dayLabel(")
    assert start != -1, "dayLabel is gone"
    label = body[start:]
    label = label[:label.find("\nfunction ", 10)]
    assert "new Date" not in label, (
        "dayLabel parses with new Date(), which shifts the day by one in "
        "negative-offset timezones"
    )
    assert ".split(" in label, "dayLabel does not split the ISO string"


# ─── Over-packing must be visible to the owner ───────────────────────────────

def test_the_owner_is_warned_when_more_was_packed_than_planned(source):
    """"in beetroot sattu 1 kg. plan was 50. but Ops team packed 100... and in the
    shipment dashboard warning to me also."

    `To pack` clamps at 0, so before this a doubled row read exactly like a finished
    one. The invoice bridge bills what was PACKED, so the surplus reaches a GST
    document — hence an error banner and not a quiet tint.
    """
    body = _without_comments(source)
    assert "over_packed" in body, (
        "the page never reads over_packed, so a row packed to double the plan looks "
        "identical to one that is exactly finished"
    )
    assert re.search(r"banner error[\s\S]{0,200}More packed than planned", body), (
        "the over-pack is not raised as an error banner"
    )


def test_the_over_packed_rows_can_be_found_in_the_table(source):
    """A banner naming products is only actionable if they can be located.

    205 rows, and the two that matter must be findable without searching for them.
    """
    body = _without_comments(source)
    assert "over-packed-row" in body, "over-packed rows carry no class to tint them"
    assert "over-packed" in body, "the Packed cell is not marked"


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


def test_the_combined_backlog_is_reported_not_just_the_individual_holds(source):
    """Requirement 9's second half needs a place on the screen.

    "N days on hold" does not answer the question the owner actually has, which
    is whether those days have now added up to a shipment. Each day is correctly
    held on its own merits, so nothing about a single day can tell him — and
    without this he adds the held columns up by hand every morning, or forgets
    to and the stock sits.

    Asserted as a branch on `carryOver.clears`, not merely as the words being
    present somewhere. That distinction was found by mutation, not foresight:
    disabling the whole banner with `if(held.days && false)` left every keyword
    in the file — they survive in the other branch — and the first, grep-only
    version of this test passed happily on a page that had stopped reporting.
    """
    assert "carry_over" in source, (
        "the page never reads carry_over, so the owner is not told when the held "
        "days combine into a shippable total"
    )
    body = _without_comments(source)
    assert re.search(r"if\s*\(\s*carryOver\s*&&\s*carryOver\.days\s*\)", body), (
        "the held-days banner is not gated on carryOver — the page is back to "
        "reading held_totals, which cannot say whether the backlog now ships"
    )
    assert re.search(r"if\s*\(\s*carryOver\.clears\s*\)", body), (
        "nothing branches on carryOver.clears, so a backlog that has become a "
        "shipment reads exactly like one that has not"
    )
    assert "shortfall_cartons" in body and "shortfall_units" in body, (
        "the page cannot say how far short the backlog still is, only that it is"
    )


def test_stock_stranded_by_a_new_plan_is_reported(source):
    """The silent-disappearance case, and it must not be a toast.

    /shipment/active returns only the active plan, so a day held on Saturday
    drops off every screen the moment Monday's plan is generated. Those boxes are
    physically in the warehouse. The new plan has no held days of its own, so
    this warning cannot be derived from the payload during a later render — it
    has to be kept separately, which is what `rollover` is for.
    """
    assert "abandoned_holds" in source, (
        "the page ignores abandoned_holds — generating a new plan would silently "
        "hide packed stock that is still on the warehouse floor"
    )
    assert "rollover" in source, (
        "the warning is not held outside the payload, so renderBanners would drop "
        "it immediately: the new plan has no held days to derive it from"
    )


# ─── The In-stock column must actually drive a number ────────────────────────

def test_editing_any_field_recomputes_the_derived_cells(source):
    """The reported bug: "when i am changing the available. Left to pack is not
    changing."

    `edit()` used to recompute only inside an `if(field === "shipment_plan")`
    branch, so typing into the In-stock box updated the in-memory row and then
    repainted nothing. The number was real and the screen never showed it.
    """
    body = _without_comments(source)
    edit = body[body.find("function edit("):]
    edit = edit[:edit.find("\nfunction ", 10)]

    assert 'if(field === "shipment_plan")' not in edit, (
        "the recompute is gated on the field again — editing In stock will not "
        "move any number on screen, which is the bug that was reported"
    )
    assert "to_source" in edit, "edit() never recomputes the To-make figure"
    assert "item.remaining" in edit, "edit() never recomputes the To-pack figure"


def test_the_derived_cells_are_addressed_by_class_not_position(source):
    """`tr.lastElementChild` broke the moment a column was appended.

    It silently updated whichever cell happened to be last — so the fix for one
    column would have corrupted the other. Class-based lookup cannot drift.
    """
    body = _without_comments(source)
    assert "lastElementChild" not in body, (
        "a derived cell is addressed by position; appending a column would then "
        "repaint the wrong number with no error"
    )
    assert "cell-topack" in body and "cell-tomake" in body, (
        "the derived cells carry no stable class for edit() to find"
    )


def test_both_derived_numbers_are_shown(source):
    """Two questions, two columns.

    "To pack" ignores warehouse stock because stock on a shelf is not in a
    carton; "To make" subtracts it. One column cannot answer both, and the
    packer's number is the one that must not shrink.
    """
    assert "To pack" in source, "no column for what still needs boxing"
    assert "To make" in source, "no column for what still needs producing"
    assert "In stock" in source, (
        'the column is still labelled "Avl" — the packer and the owner both have '
        "to guess what it means"
    )


# ─── The frozen identity block ───────────────────────────────────────────────

#: The frozen columns, in order, with the width each is pinned to. A sticky `left`
#: offset must equal the SUM of the widths before it — that is the whole invariant,
#: and it is the one thing about this feature that cannot be eyeballed from the CSS.
FROZEN_BLOCK = [
    ("freeze-pick", 30),
    ("freeze-name", 190),
    ("freeze-wt", 42),
    ("freeze-s", 28),      # width comes from .col-smb
    ("freeze-m", 28),
    ("freeze-b", 28),
    ("freeze-brand", 58),
]


def _rule_for(body: str, cls: str) -> str | None:
    """Declarations of the rule whose selector list contains ``.cls``.

    Selectors here are grouped (``th.freeze-pick, td.freeze-pick{...}``), so a plain
    ``\\.cls\\s*\\{`` never matches — it looks for the class immediately before the
    brace and finds the last selector in the group instead.
    """
    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", body):
        selector, declarations = match.group(1), match.group(2)
        if re.search(r"\.%s\b" % re.escape(cls), selector):
            return declarations.replace(" ", "").replace("\n", "")
    return None


def test_the_frozen_offsets_match_the_cumulative_widths(source):
    """The requirement: "freeze the first column. which has item, wt, s m b etc."

    Seven columns are pinned, so each one's `left` has to be the sum of the widths
    before it. Get one wrong and the failure is visual, not functional: too large and
    the column overlaps its neighbour, too small and a transparent stripe opens that
    the scrolling numbers slide through underneath. Both look like a rendering glitch
    rather than a wrong number, so nothing else in the suite would object.

    Measured in a browser at 1280px: offsets 0/30/220/262/290/318/346 against widths
    30/190/42/28/28/28/58.
    """
    body = _without_comments(source)

    expected = 0
    for cls, width in FROZEN_BLOCK:
        declarations = _rule_for(body, cls)
        assert declarations is not None, (
            f"no CSS rule for .{cls} — that column is not frozen"
        )

        # `px` is optional: a zero offset is legitimately written `left:0`.
        found = re.search(r"left:(-?[\d.]+)(?:px)?[;}]?", declarations)
        assert found, f".{cls} is in the frozen block but sets no left offset"
        assert float(found.group(1)) == expected, (
            f".{cls} is pinned at left:{found.group(1)}px but the columns before it "
            f"are {expected}px wide. Too large overlaps the previous column; too "
            f"small leaves a gap the scrolling cells show through."
        )
        expected += width


@pytest.mark.parametrize("cls,width", FROZEN_BLOCK)
def test_each_frozen_column_has_a_pinned_width(source, cls, width):
    """A frozen column may not size itself from its content.

    Item rendered at 222.9px against a 190px `min-width`, because the longest product
    name decided it. Every offset after it is then wrong by 33px — and it would shift
    again on a plan with different products, so the table would be correct on one
    catalogue and broken on the next.
    """
    body = _without_comments(source)
    if cls in ("freeze-s", "freeze-m", "freeze-b"):
        rule = re.search(r"thead th\.col-smb\{([^}]*)\}", body)
        assert rule and f"width:{width}px" in rule.group(1).replace(" ", ""), (
            "the S/M/B columns take their width from .col-smb; it no longer sets one"
        )
        return

    decl = _rule_for(body, cls)
    assert decl is not None, f"no rule for .{cls}"
    assert f"width:{width}px" in decl, f".{cls} does not pin width:{width}px"
    assert f"max-width:{width}px" in decl, (
        f".{cls} sets no max-width, so a long value can still stretch it and push "
        "every offset after it out of alignment"
    )


def test_the_frozen_cells_are_opaque_and_layered(source):
    """Transparent frozen cells let the scrolling numbers show through them.

    The pinned cells overlap the scrolling ones, so they need a background of their
    own and a z-index above the body. Without both, the effect is unreadable rather
    than merely ugly — two numbers in the same place.
    """
    body = _without_comments(source)
    rule = re.search(r"th\.freeze,\s*td\.freeze\{([^}]*)\}", body)
    assert rule, "the frozen cells have no shared rule"
    decl = rule.group(1).replace(" ", "")
    assert "position:sticky" in decl, "the frozen cells are not sticky at all"
    assert "background:" in decl, (
        "the frozen cells are transparent — the scrolling columns will show through"
    )
    assert "z-index" in decl, "the frozen cells sit under the cells they overlap"


def test_the_row_state_tints_reach_the_frozen_cells(source):
    """The frozen cells carry their own opaque background, which hides the row tint.

    So hover, dirty, selected, excluded and over-packed each have to be repeated on
    `td.freeze` — otherwise the row you are pointing at is highlighted everywhere
    except the part holding the product name.
    """
    body = _without_comments(source)
    for state in ("tr:hover", "tr.dirty", "tr.selected", "tr.excluded",
                  "tr.over-packed-row"):
        assert re.search(re.escape(state) + r"\s+td\.freeze\{", body), (
            f"{state} does not repaint the frozen cells, so the tint stops at the "
            "frozen/scrolling join"
        )


# ─── The invoice bridge: requirement 8 ───────────────────────────────────────

def test_the_invoice_bridge_is_reachable_from_a_verified_day(source):
    """"the operations team can directly generate invoice using this if they want to."

    Only from a verified day. The server refuses anything less, and a control that
    always errors reads as a broken app rather than as a rule being enforced.
    """
    body = _without_comments(source)
    assert "/shipment/invoice-payload" in body, "no way to start an invoice"
    assert "invoiceSelectedDays" in body, "the day cards offer no invoice action"
    assert re.search(r'd\.status\s*===\s*"verified"', body), (
        "the invoice control is not gated on a verified day — it would be offered "
        "on days the server will refuse"
    )


def test_the_owner_chooses_which_days_go_on_one_invoice(source):
    """"give option where I can select multiple days packed and create invoice together."

    The old button took EVERY verified day silently — combining was automatic and
    choosing was impossible. Two verified days that should have been two shipments would
    land on one GST invoice with no way to say otherwise.
    """
    body = _without_comments(source)
    assert "invoicePick" in body, "there is no per-day selection at all"
    assert 'data-pick=' in body, "verified days carry no tick box"
    assert re.search(r"invoicePick\.has\(d\.pack_date\)", body), (
        "the tick state is not read back per day, so the boxes cannot reflect the "
        "selection after a re-render"
    )
    # And the request must send the CHOSEN days, not all of them.
    assert re.search(r"pack_dates:\s*chosen", body), (
        "the invoice request does not send the selected subset — it is back to "
        "invoicing every verified day whatever the owner ticked"
    )


def test_a_stale_tick_cannot_reach_an_invoice(source):
    """A day that stops being verified must drop out of the selection.

    Re-verifying, or generating a new plan, changes the day list under a selection the
    owner made minutes ago. A tick that survived would either be refused by the server
    (confusing) or, worse, put an unintended day on a GST document.
    """
    body = _without_comments(source)
    assert "stillValid" in body, (
        "the selection is never reconciled against the current verified days"
    )
    assert re.search(r"invoicePick\s*=\s*new Set\(\)", body), (
        "the selection is not cleared on load, so a tick from a previous plan persists"
    )


def test_leaving_a_verified_day_out_is_called_out(source):
    """Excluding a day is legitimate; doing it by accident is expensive.

    Each invoice spends a number from a legally-sequential GST series, so a day left
    behind needs its own invoice and its own number. The confirm says so rather than
    letting the owner discover it afterwards.
    """
    body = _without_comments(source)
    assert "separate invoice" in body, (
        "nothing warns that unticked verified days will need their own invoice"
    )


def test_the_handoff_goes_through_sessionstorage_not_the_url(source):
    """A URL is shared, logged and bookmarked; this payload is invoice data.

    It is also large enough to hit URL limits, so a query string would truncate
    rather than fail cleanly — a silently short invoice.
    """
    body = _without_comments(source)
    assert "sessionStorage.setItem" in body, "the payload is not stashed for the invoice page"
    assert "shipmentInvoicePayload" in body, (
        "the handoff key does not match the one templates/invoice.html reads"
    )
    assert "/invoice-page" in body, "nothing navigates to the invoice screen"


def test_all_verified_days_are_invoiced_together(source):
    """Where requirements 8 and 9 meet, and it must not become one-invoice-per-day.

    Two days that were individually too small were combined precisely so they
    would be ONE shipment. Two invoices would undo that, and each one spends a
    number from a legally-sequential GST series.
    """
    body = _without_comments(source)
    assert re.search(r'days\.filter\([^)]*status\s*===\s*"verified"', body), (
        "invoiceDay does not gather every verified day — combined days would be "
        "split across separate invoices"
    )


def test_an_already_invoiced_day_offers_no_second_invoice(source):
    """Two GST documents against one set of boxes is a tax problem."""
    body = _without_comments(source)
    assert "invoice_id" in body, (
        "the page never reads invoice_id, so it cannot tell an invoiced day apart "
        "from one still waiting"
    )


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
