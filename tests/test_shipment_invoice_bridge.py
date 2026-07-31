"""Verified packing → invoice payload. The one path that touches the GST series.

Requirement 8: "when the data daily is entered and is verified by me, the
operations team can directly generate invoice using this if they want to."

Everything here exists because of one asymmetry. A GST invoice number is a
legally-sequential series: issuing one wrongly cannot be undone by deleting a
row, and a gap in the sequence is a question you answer during an audit. So this
endpoint's refusals matter more than its successes, and the refusals are what
most of this file tests.

Two invariants above all:

* **Nothing here allocates an invoice number.** ``/shipment/invoice-payload``
  builds a *payload*. ``POST /invoice/save`` is untouched and remains the only
  writer of the series — its own 26 tests in tests/test_invoice_save.py keep
  guarding it, and ``test_the_bridge_creates_no_invoice_row`` asserts from this
  side that a rejected request left the table alone.
* **"Verified by me" is a gate, not a label.** Any day short of `verified` is a
  400. "All but one day approved" is not approval.

The aggregation being per-ASIN across dates is not a convenience either — it is
how requirement 9's combined held days become one invoice.
"""
import pytest

from app.models import Invoice
from app.shipment import logic, repository

pytestmark = pytest.mark.regression

ASIN = "B0AAA00001"
SECOND = "B0AAA00002"

MONDAY = "2026-07-30"
TUESDAY = "2026-07-31"


async def _packed_and_verified(auth_client, ops_client, pack_date, entries):
    """Record, submit and verify a day. Returns the verify response."""
    r = await ops_client.post(
        f"/shipment/packing/{pack_date}", json={"entries": entries}
    )
    assert r.status_code == 200, r.text
    r = await ops_client.post(f"/shipment/packing/{pack_date}/submit")
    assert r.status_code == 200, r.text
    r = await auth_client.post(f"/shipment/packing/{pack_date}/verify")
    assert r.status_code == 200, r.text
    return r


async def _payload(client, *pack_dates):
    return await client.post(
        "/shipment/invoice-payload", json={"pack_dates": list(pack_dates)}
    )


# ─── The happy path, and the shape the invoice screen needs ──────────────────

async def test_a_verified_day_becomes_an_invoice_payload(
    auth_client, ops_client, plan_factory
):
    """Requirement 8's headline: verified packing turns into invoice lines."""
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 600, "cartons": 30}],
    )

    r = await _payload(auth_client, MONDAY)
    assert r.status_code == 200, r.text
    body = r.json()

    assert len(body["items"]) == 1
    line = body["items"][0]
    assert line["asin"] == ASIN
    assert line["quantity"] == 600
    assert line["hsn_code"], "no HSN code — the invoice cannot be raised without one"
    assert line["gst_rate"], "no GST rate on the line"


async def test_ops_may_build_the_payload(auth_client, ops_client, plan_factory):
    """"the operations team can directly generate invoice using this if they want to."

    Deliberately open to ops, unlike verify. Building the payload is not the
    privileged act — verifying was, and it has already happened by this point.
    """
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 600, "cartons": 30}],
    )

    r = await _payload(ops_client, MONDAY)
    assert r.status_code == 200, r.text
    assert r.json()["items"], "ops got an empty payload"


async def test_the_payload_keys_match_what_the_invoice_screen_consumes(
    auth_client, ops_client, plan_factory
):
    """A superset of parse_shipment_tsv's line shape.

    templates/invoice.html reads these exact keys out of whatever produced the
    payload. If the bridge omits one, populateItems renders "undefined" into a tax
    document — which looks like a typo rather than a bug and could plausibly be
    saved that way.
    """
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 600, "cartons": 30}],
    )

    body = (await _payload(auth_client, MONDAY)).json()

    required = {
        "sku", "title", "short_title", "asin", "fnsku",
        "quantity", "hsn_code", "gst_rate", "rate", "unit",
    }
    missing = required - set(body["items"][0])
    assert not missing, (
        f"invoice line is missing key(s) the invoice page reads: {sorted(missing)} "
        "— those render as 'undefined' on a GST document"
    )
    assert "metadata" in body, "invoice.html reads data.metadata"
    assert body["metadata"]["supplier_gstin"], "no supplier GSTIN in the metadata"


async def test_amazon_only_fields_are_left_blank_not_guessed(
    auth_client, ops_client, plan_factory
):
    """The FC and shipment id come from Amazon AFTER the shipment exists.

    A plausible-looking guess here would put the wrong FC — and therefore the
    wrong recipient GSTIN and place of supply — on a tax document. Blank is
    visibly incomplete; wrong is not.
    """
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 600, "cartons": 30}],
    )

    meta = (await _payload(auth_client, MONDAY)).json()["metadata"]
    assert meta["shipment_id"] == "", "a shipment id was invented"
    assert meta["ship_to"] == "", "an FC was guessed"
    assert meta["recipient_gstin"] == "", (
        "a recipient GSTIN was guessed — that is the wrong state's GST number on "
        "an invoice if the guess is wrong"
    )


async def test_the_cartons_ops_counted_prefill_the_boxes_field(
    auth_client, ops_client, plan_factory
):
    """Requirement 7's concrete payoff, and the reason cartons are entered daily.

    "boxes/carton packed is also a thing which needs to be entered daily by the
    operations manager. so that I can take that data and the units packed and
    create shipment." Without this the count is re-done by hand at invoice time,
    and the daily entry was pointless.
    """
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 600, "cartons": 30}],
    )

    assert (await _payload(auth_client, MONDAY)).json()["boxes"] == 30


async def test_lines_come_out_in_the_canonical_order(
    auth_client, ops_client, plan_factory
):
    """Product-then-weight, the same order as the screen and the four downloads.

    The owner checks the invoice against the packed sheet. Two documents listing
    the same SKUs in different orders makes that a hunt instead of a read.
    """
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [
            {"asin": ASIN, "units": 300, "cartons": 20},
            {"asin": SECOND, "units": 300, "cartons": 15},
        ],
    )

    body = (await _payload(auth_client, MONDAY)).json()
    got = [line["asin"] for line in body["items"]]

    # plan_factory: both are "Chana Sattu", 1.0 kg and 0.5 kg. Weight ascending.
    assert got == [SECOND, ASIN], (
        f"invoice lines are not in product-then-weight order: {got}"
    )


@pytest.fixture
def priced_asin():
    """A real ASIN that has a purchase rate in the master pricing.

    Needed because plan_factory's ASINs are invented, so every line it produces
    has rate 0 — a bridge that never called get_purchase_rate at all would pass
    every other test in this file unnoticed.
    """
    from app.invoice.parser import PRICING

    for key, value in PRICING.items():
        if key.startswith("B0") and len(key) == 10 and value:
            return key, float(value)
    pytest.skip("pricing_data.json has no ASIN-keyed entry to test against")


async def test_the_purchase_rate_is_actually_looked_up(
    auth_client, ops_client, plan_factory, priced_asin
):
    """The rate must come from the master pricing, not be left at zero.

    Reusing the invoice module's own get_purchase_rate matters beyond saving
    code: a rate correction made through the invoice screen has to apply here
    too, or the two paths would quietly price the same SKU differently.
    """
    asin, expected = priced_asin
    await plan_factory(items=[{
        "asin": asin, "item": "Priced Product", "weight": 1.0,
        "brand": "MF", "fba_sku": "", "shipment_plan": 500, "deficit": 480,
    }])
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": asin, "units": 600, "cartons": 30}],
    )

    line = (await _payload(auth_client, MONDAY)).json()["items"][0]
    assert line["rate"] == expected, (
        f"rate for {asin} came through as {line['rate']}, expected {expected} "
        "from pricing_data.json — the master pricing is not being consulted"
    )


async def test_a_missing_purchase_rate_is_reported_not_silently_zero(
    auth_client, ops_client, plan_factory
):
    """A blank rate makes the taxable value zero, and the invoice would still save.

    /invoice/save validates that items exist, not that they are priced. So a SKU
    absent from the master pricing has to be called out here, or a zero-value line
    goes onto a GST document and consumes a number in the series.
    """
    await plan_factory(items=[{
        "asin": "B0ZZZ99999", "item": "Unpriced Thing", "weight": 1.0,
        "brand": "MF", "fba_sku": "NO-SUCH-SKU-IN-PRICING",
        "shipment_plan": 500, "deficit": 480,
    }])
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": "B0ZZZ99999", "units": 600, "cartons": 30}],
    )

    body = (await _payload(auth_client, MONDAY)).json()
    assert body["items"][0]["rate"] in (0, 0.0), "premise changed: the SKU is priced"
    assert body["warnings"], (
        "a line with no purchase rate produced no warning — it would go onto the "
        "invoice at zero and still save"
    )
    assert any("rate" in w for w in body["warnings"])


# ─── Combining days: requirement 9 meeting requirement 8 ─────────────────────

async def test_two_days_aggregate_into_one_invoice_line_per_sku(
    auth_client, ops_client, plan_factory
):
    """Where "combine it with next day packing and then create a shipment" lands.

    Two dates, one SKU, one invoice line carrying the sum. Per-day lines would
    put the same product on an invoice twice, which is not how the document is
    read or checked.
    """
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 400, "cartons": 20}],
    )
    await _packed_and_verified(
        auth_client, ops_client, TUESDAY,
        [{"asin": ASIN, "units": 200, "cartons": 10}],
    )

    body = (await _payload(auth_client, MONDAY, TUESDAY)).json()
    lines = [line for line in body["items"] if line["asin"] == ASIN]
    assert len(lines) == 1, f"{ASIN} appears on {len(lines)} lines, expected one"
    assert lines[0]["quantity"] == 600, "the two days did not add up"
    assert body["boxes"] == 30, "cartons did not sum across the days"


async def test_only_the_selected_days_are_invoiced(
    auth_client, ops_client, plan_factory
):
    """Selecting Monday must not quietly invoice Tuesday as well.

    Both days are verified and both are invoiceable, so nothing would complain —
    the extra units would simply be on the document.
    """
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 400, "cartons": 20}],
    )
    await _packed_and_verified(
        auth_client, ops_client, TUESDAY,
        [{"asin": ASIN, "units": 200, "cartons": 10}],
    )

    body = (await _payload(auth_client, MONDAY)).json()
    assert body["items"][0]["quantity"] == 400, (
        "an unselected day's units were included — the invoice would overstate "
        "the shipment"
    )
    assert body["boxes"] == 20


async def test_a_repeated_date_does_not_double_the_quantity(
    auth_client, ops_client, plan_factory
):
    """The same date twice in one request is a UI slip, not an instruction to double.

    Worth knowing *why* this holds, because it is not the de-duplication in the
    request parsing — mutation-checked: removing that leaves this test green. The
    real protection is structural. Days are aggregated by filtering
    load_days_with_entries on date membership, and that returns exactly one row
    per date, so a date listed twice contributes once no matter what.

    Kept anyway. It pins the *behaviour* rather than the mechanism, so if the
    aggregation is ever rewritten into a per-date loop — which is the natural
    shape and would double — this fails.
    """
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 400, "cartons": 20}],
    )

    body = (await _payload(auth_client, MONDAY, MONDAY)).json()
    assert body["items"][0]["quantity"] == 400, "a duplicated date doubled the units"
    assert body["boxes"] == 20, "a duplicated date doubled the carton count"


# ─── The refusals, which is where the GST risk actually lives ────────────────

@pytest.mark.parametrize("stop_at", ["open", "submitted", "held"])
async def test_an_unverified_day_is_refused(
    auth_client, ops_client, plan_factory, stop_at
):
    """"verified by me" is the gate. Nothing weaker opens it.

    Parametrised over every status short of verified, because a check written as
    `!= "open"` or `in ("submitted", "verified")` passes the obvious test and
    lets one of the others through.
    """
    await plan_factory()
    entries = (
        [{"asin": ASIN, "units": 400, "cartons": 20}]  # small -> held on submit
        if stop_at == "held"
        else [{"asin": ASIN, "units": 600, "cartons": 30}]
    )
    r = await ops_client.post(f"/shipment/packing/{MONDAY}", json={"entries": entries})
    assert r.status_code == 200, r.text
    if stop_at != "open":
        r = await ops_client.post(f"/shipment/packing/{MONDAY}/submit")
        assert r.json()["status"] == stop_at, r.json()

    r = await _payload(auth_client, MONDAY)
    assert r.status_code == 400, f"a {stop_at} day was accepted for invoicing: {r.text}"
    assert MONDAY in r.json()["error"], "the error does not say which day is the problem"


async def test_one_unverified_day_blocks_the_whole_request(
    auth_client, ops_client, plan_factory
):
    """Partial approval is not approval.

    The dangerous alternative is silently invoicing only the verified subset: the
    request succeeds, the document is short, and nobody notices until the units
    are reconciled against Amazon.
    """
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 400, "cartons": 20}],
    )
    # Tuesday is submitted but NOT verified.
    await ops_client.post(
        f"/shipment/packing/{TUESDAY}",
        json={"entries": [{"asin": ASIN, "units": 600, "cartons": 30}]},
    )
    await ops_client.post(f"/shipment/packing/{TUESDAY}/submit")

    r = await _payload(auth_client, MONDAY, TUESDAY)
    assert r.status_code == 400, (
        "a mixed selection was accepted — the invoice would silently cover only "
        "the verified day"
    )
    assert TUESDAY in r.json()["error"]


async def test_the_bridge_creates_no_invoice_row(
    auth_client, ops_client, plan_factory, count_rows
):
    """The invariant that matters most, asserted at the table.

    Neither a successful payload nor a rejected one may write to `invoices`. Only
    POST /invoice/save allocates a number, and a number consumed by a request the
    owner never completed is a permanent gap in a legally-sequential series.
    """
    await plan_factory()
    before = await count_rows(Invoice)

    # A rejected request.
    await ops_client.post(
        f"/shipment/packing/{MONDAY}",
        json={"entries": [{"asin": ASIN, "units": 600, "cartons": 30}]},
    )
    r = await _payload(auth_client, MONDAY)
    assert r.status_code == 400, r.text
    assert await count_rows(Invoice) == before, "a rejected request created an invoice"

    # And a successful one.
    await ops_client.post(f"/shipment/packing/{MONDAY}/submit")
    await auth_client.post(f"/shipment/packing/{MONDAY}/verify")
    r = await _payload(auth_client, MONDAY)
    assert r.status_code == 200, r.text
    assert await count_rows(Invoice) == before, (
        "building a payload allocated an invoice number — only /invoice/save may "
        "touch the GST series"
    )


async def test_a_day_that_was_never_packed_is_a_404(auth_client, plan_factory):
    await plan_factory()
    r = await _payload(auth_client, "2026-01-01")
    assert r.status_code == 404, r.text


@pytest.mark.parametrize("dates", [[], ["not-a-date"], ["2026-13-45"]])
async def test_a_nonsense_request_is_rejected(auth_client, plan_factory, dates):
    """Rejected rather than coerced. A silently-adjusted date invoices the wrong day."""
    await plan_factory()
    r = await auth_client.post("/shipment/invoice-payload", json={"pack_dates": dates})
    assert r.status_code == 400, r.text


async def test_no_active_plan_is_a_404_not_a_500(auth_client):
    r = await _payload(auth_client, MONDAY)
    assert r.status_code == 404, r.text


# ─── Attaching the invoice, and the double-invoice guard ─────────────────────

async def test_attaching_an_invoice_marks_the_days_shipped(
    auth_client, ops_client, plan_factory, read_committed
):
    """The bookkeeping that closes the loop.

    Until the invoice id is recorded, nothing in the app knows those boxes are
    spoken for — which is exactly the state the 409 below depends on.
    """
    plan = await plan_factory()
    plan_id = plan.id
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 600, "cartons": 30}],
    )

    r = await auth_client.post(
        "/shipment/attach-invoice",
        json={"pack_dates": [MONDAY], "invoice_id": 4242},
    )
    assert r.status_code == 200, r.text

    day = await read_committed(repository.get_day, plan_id, MONDAY)
    assert day.status == logic.STATUS_SHIPPED
    assert day.invoice_id == 4242


async def test_an_invoiced_day_cannot_be_invoiced_again(
    auth_client, ops_client, plan_factory
):
    """Two GST documents against one set of boxes is a tax problem, not a UI one."""
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 600, "cartons": 30}],
    )
    await auth_client.post(
        "/shipment/attach-invoice",
        json={"pack_dates": [MONDAY], "invoice_id": 4242},
    )

    r = await _payload(auth_client, MONDAY)
    assert r.status_code == 409, f"the same day was invoiced twice: {r.text}"
    assert "4242" in r.json()["error"], (
        "the error does not name the existing invoice, so the owner cannot go and "
        "look at what already covers these boxes"
    )


async def test_the_already_invoiced_error_wins_over_not_verified(
    auth_client, ops_client, plan_factory
):
    """Ordering, and it is not cosmetic.

    A shipped day is not `verified` either, so both checks match. Reporting "not
    verified yet" would send the owner hunting for a verify button on a day that
    is finished and invoiced; "already on invoice #N" tells him what happened.
    """
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 600, "cartons": 30}],
    )
    await auth_client.post(
        "/shipment/attach-invoice",
        json={"pack_dates": [MONDAY], "invoice_id": 4242},
    )

    r = await _payload(auth_client, MONDAY)
    assert r.status_code == 409, r.text
    assert "verified" not in r.json()["error"].lower(), (
        "a shipped day reports as unverified — misleading, since verifying it "
        "again is not the fix"
    )


async def test_re_attaching_the_same_invoice_is_harmless(
    auth_client, ops_client, plan_factory
):
    """Idempotent, because this call is a separate round trip from /invoice/save.

    If the response is lost the frontend retries, and a retry must not 409 on
    work it did itself.
    """
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 600, "cartons": 30}],
    )
    for _ in range(2):
        r = await auth_client.post(
            "/shipment/attach-invoice",
            json={"pack_dates": [MONDAY], "invoice_id": 4242},
        )
        assert r.status_code == 200, r.text
    assert r.json()["already_attached"] == [MONDAY]


async def test_attaching_a_different_invoice_is_refused(
    auth_client, ops_client, plan_factory, read_committed
):
    """Not idempotency: this is the double-invoice case wearing the same shape."""
    plan = await plan_factory()
    plan_id = plan.id
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 600, "cartons": 30}],
    )
    await auth_client.post(
        "/shipment/attach-invoice",
        json={"pack_dates": [MONDAY], "invoice_id": 4242},
    )

    r = await auth_client.post(
        "/shipment/attach-invoice",
        json={"pack_dates": [MONDAY], "invoice_id": 9999},
    )
    assert r.status_code == 409, r.text

    day = await read_committed(repository.get_day, plan_id, MONDAY)
    assert day.invoice_id == 4242, "the second invoice overwrote the first despite the 409"


async def test_attach_needs_an_invoice_id(auth_client, ops_client, plan_factory):
    """Without one the day would be marked shipped against nothing.

    That is worse than failing: the units leave the "to invoice" list and no
    document covers them.
    """
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 600, "cartons": 30}],
    )
    r = await auth_client.post(
        "/shipment/attach-invoice", json={"pack_dates": [MONDAY]}
    )
    assert r.status_code == 400, r.text


async def test_shipped_units_still_count_as_packed_and_shippable(
    auth_client, ops_client, plan_factory
):
    """A shipped day must not fall out of the totals.

    `shipped` is in SHIPPABLE_STATUSES on purpose. If invoicing removed the units
    from `packed`, the plan would report them as still to pack and the warehouse
    would pack the same order twice.
    """
    await plan_factory()
    await _packed_and_verified(
        auth_client, ops_client, MONDAY,
        [{"asin": ASIN, "units": 600, "cartons": 30}],
    )
    await auth_client.post(
        "/shipment/attach-invoice",
        json={"pack_dates": [MONDAY], "invoice_id": 4242},
    )

    r = await auth_client.get("/shipment/active")
    item = next(i for i in r.json()["items"] if i["asin"] == ASIN)
    assert item["packed"] == 600, (
        "shipped units stopped counting as packed — the plan would ask for them "
        "to be packed again"
    )
    assert item["shippable"] == 600, "shipped units stopped counting as shippable"
