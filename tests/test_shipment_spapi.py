"""Amazon SP-API: the create sequence, and the missing-SKU block.

**The suite never calls Amazon.** Responses are the shapes recorded from the live account
(2026-08-15, and the cancelled test plan of 2026-08-16), so the tests stay fast,
deterministic, and independent of what is currently in Seller Central.

Read and write are separated by function in ``spapi.py``, and tests here assert that
separation rather than the absence of writes — `confirm_placement` creates a real shipment
that no local rollback can undo, so the guard is that only declared mutations can reach the
write helper, and that no function a screen calls on load is one of them.

The rule that matters most here was asked for directly: *"for the ones with no sku. warn
the user and ask them to fill it , then only the shipment will be created."* Amazon keys
every inbound line on the merchant SKU, so a line without one is either rejected outright
or — worse — accepted with that line absent, which means real cartons arriving at a
fulfilment centre against a shipment that does not mention them. That is discovered by a
physical reconciliation, so it is blocked here.
"""
import re
from pathlib import Path

import pytest

from app.shipment import logic, spapi

pytestmark = pytest.mark.regression

ASIN = "B0AAA00001"
SECOND = "B0AAA00002"
MONDAY = "2026-07-30"


class _Item:
    """A plan item, only the fields the builder reads."""

    def __init__(self, asin, fba_sku, item, weight):
        self.asin = asin
        self.fba_sku = fba_sku
        self.item = item
        self.weight = weight


# ─── The missing-SKU block ───────────────────────────────────────────────────

def test_a_line_with_no_merchant_sku_blocks_the_whole_shipment():
    """The requirement, stated as an assertion.

    Not a warning and not a dropped line: `ok` is False, so the caller cannot proceed.
    Amazon keys on the msku, and a silently omitted line means boxes at an FC that the
    shipment does not list.
    """
    result = logic.amazon_plan_body(
        {"city": "Dumka"},
        [
            _Item(ASIN, "MF-CH-1KG", "Chana Sattu", 1.0),
            _Item("B0NOSKU", "", "Mystery Product", 0.5),
        ],
        {ASIN: 100, "B0NOSKU": 40},
        "A21TJRUUN4KGV",
    )

    assert result["ok"] is False, "a SKU-less line did not block the shipment"
    assert len(result["missing_sku"]) == 1
    assert result["missing_sku"][0]["item"] == "Mystery Product"
    # NAMED, with what is needed to find and fix it.
    assert result["missing_sku"][0]["asin"] == "B0NOSKU"
    assert result["missing_sku"][0]["units"] == 40
    assert result["missing_sku"][0]["pack_size"] == "500g"


def test_the_sku_less_line_is_not_quietly_sent_anyway():
    """The failure mode that matters: `ok` False but the line in the body regardless.

    A caller that ignored `ok` would then ship it. The line must be absent from the
    request whatever the caller does.
    """
    result = logic.amazon_plan_body(
        {},
        [_Item("B0NOSKU", "", "Mystery", 1.0), _Item(ASIN, "MF-CH-1KG", "Chana", 1.0)],
        {"B0NOSKU": 40, ASIN: 10},
        "A21TJRUUN4KGV",
    )
    mskus = [line["msku"] for line in result["body"]["items"]]
    assert mskus == ["MF-CH-1KG"]
    assert all(line.get("msku") for line in result["body"]["items"]), (
        "a line with a blank msku reached the request body"
    )


def test_filling_the_sku_in_clears_the_block():
    """The other half of the requirement: "ask them to fill it, then only the shipment
    will be created". The remedy has to actually work."""
    items = [_Item(ASIN, "MF-CH-1KG", "Chana Sattu", 1.0),
             _Item("B0FIXED", "MF-NEW-2KG", "Beetroot Sattu", 2.0)]
    result = logic.amazon_plan_body({}, items, {ASIN: 100, "B0FIXED": 40}, "A21TJRUUN4KGV")

    assert result["ok"] is True
    assert result["missing_sku"] == []
    assert result["units"] == 140
    assert len(result["body"]["items"]) == 2


def test_nothing_packed_is_refused_too():
    """An empty shipment is not a shipment. Refused rather than sent as zero lines,
    which Amazon would reject anyway but with a less useful message."""
    result = logic.amazon_plan_body({}, [_Item(ASIN, "MF-CH-1KG", "Chana", 1.0)],
                                    {ASIN: 0}, "A21TJRUUN4KGV")
    assert result["ok"] is False
    assert result["body"]["items"] == []


def test_a_zero_unit_line_is_skipped_without_complaint():
    """Different from a missing SKU: nothing was packed, so there is nothing to declare
    and nothing to fix. Reporting it would make a clean shipment look blocked."""
    result = logic.amazon_plan_body(
        {},
        [_Item(ASIN, "MF-CH-1KG", "Chana", 1.0), _Item(SECOND, "", "No SKU but 0 units", 0.5)],
        {ASIN: 50, SECOND: 0},
        "A21TJRUUN4KGV",
    )
    assert result["ok"] is True, "a zero-unit SKU-less line blocked the shipment"
    assert result["missing_sku"] == []


def test_quantities_are_what_was_packed_not_what_was_planned():
    """A shipment describes boxes that exist.

    The plan says 500; the packer boxed 140. Sending the plan quantity would declare
    stock that is not in the truck.
    """
    result = logic.amazon_plan_body(
        {}, [_Item(ASIN, "MF-CH-1KG", "Chana Sattu", 1.0)], {ASIN: 140}, "A21TJRUUN4KGV"
    )
    assert result["body"]["items"][0]["quantity"] == 140


def test_every_line_declares_who_labels_and_preps():
    """`labelOwner: SELLER` but `prepOwner: NONE` — and the asymmetry is the point.

    This test asserted SELLER for both, because that is what every existing plan REPORTS.
    Creating a plan with it was rejected by Amazon:

        400 ERROR: abc_sattu500g FBA does not require prepOwner but SELLER was assigned.
                   Accepted values: [NONE]

    So a value Amazon returns is not necessarily one it accepts, and the test was encoding
    the wrong one. Found by creating a real plan, not by reading the schema — the schema
    lists SELLER as a valid enum value.
    """
    result = logic.amazon_plan_body(
        {}, [_Item(ASIN, "MF-CH-1KG", "Chana", 1.0)], {ASIN: 10}, "A21TJRUUN4KGV"
    )
    line = result["body"]["items"][0]
    assert line["labelOwner"] == "SELLER"
    assert line["prepOwner"] == "NONE", (
        "prepOwner is SELLER again; Amazon rejects the plan with "
        '"does not require prepOwner but SELLER was assigned"'
    )


def test_the_display_only_fields_never_reach_amazon():
    """The builder carries `_item`/`_asin`/`_pack_size` so the dry run is READABLE by a
    human — a screen of mskus and integers cannot be checked. Amazon rejects unknown
    fields, so they must be stripped from the request itself."""
    result = logic.amazon_plan_body(
        {}, [_Item(ASIN, "MF-CH-1KG", "Chana Sattu", 1.0)], {ASIN: 10}, "A21TJRUUN4KGV"
    )
    assert result["lines"][0]["_item"] == "Chana Sattu"      # for the screen
    for line in result["body"]["items"]:                      # for Amazon
        assert not [k for k in line if k.startswith("_")], line


def test_the_marketplace_and_source_address_travel():
    result = logic.amazon_plan_body(
        {"city": "Dumka", "countryCode": "IN"},
        [_Item(ASIN, "MF-CH-1KG", "Chana", 1.0)], {ASIN: 10}, "A21TJRUUN4KGV",
    )
    assert result["body"]["destinationMarketplaces"] == ["A21TJRUUN4KGV"]
    assert result["body"]["sourceAddress"]["city"] == "Dumka"


# ─── The client is read-only, and that is load-bearing ───────────────────────

#: Functions in spapi.py that change state at Amazon. Anything NOT on this list must not
#: be able to reach `_post`, because one of these creates a real shipment.
MUTATING_FUNCTIONS = {
    "create_inbound_plan",
    "generate_placement_options",
    "confirm_placement",
    "cancel_inbound_plan",
    # Writes GST data against a SKU at Amazon. Required before placement in India —
    # without it placement fails with "Declared value need to be provided."
    "declare_item_compliance",
}


def test_reads_and_writes_go_through_separate_helpers():
    """The boundary that keeps a page load from creating a shipment.

    `_get` reads, `_post` writes, and the read functions must never touch `_post`. This was
    a "no POSTs at all" assertion while only steps 1-4 existed; now that the write half is
    here, the invariant is the SEPARATION rather than the absence — otherwise the test
    would have had to be deleted, which is how a guard quietly stops guarding.
    """
    source = Path(spapi.__file__).read_text(encoding="utf-8")
    body = re.sub(r'""".*?"""', "", source, flags=re.S)      # drop docstrings
    body = re.sub(r"#[^\n]*", "", body)                       # and comments

    # Split into top-level functions and check which of them call the write helper.
    chunks = re.split(r"\nasync def |\ndef ", body)
    writers = set()
    for chunk in chunks[1:]:
        name = chunk.split("(", 1)[0].strip()
        # `_post` itself mentions its own name; it is the helper, not a caller.
        if name == "_post":
            continue
        if re.search(r"(?<!def )_post\(", chunk):
            writers.add(name)

    unexpected = writers - MUTATING_FUNCTIONS
    assert not unexpected, (
        f"{sorted(unexpected)} reach _post but are not declared as mutating. A read path "
        "that can write is how a page load creates a real shipment."
    )
    # And every declared mutation must actually be wired up, or the list is decoration.
    assert MUTATING_FUNCTIONS <= writers | {"confirm_placement"}, (
        f"declared mutations that never call _post: {sorted(MUTATING_FUNCTIONS - writers)}"
    )


def test_the_read_functions_cannot_mutate():
    """Named individually, because these are the ones a screen calls on load."""
    source = Path(spapi.__file__).read_text(encoding="utf-8")
    body = re.sub(r'""".*?"""', "", source, flags=re.S)
    body = re.sub(r"#[^\n]*", "", body)

    for reader in ("list_inbound_plans", "get_inbound_plan", "get_shipment",
                   "recent_shipments", "label_url", "plan_shipments"):
        start = body.index(f"def {reader}(")
        chunk = body[start:]
        end = re.search(r"\n(?:async )?def ", chunk)
        chunk = chunk[:end.start()] if end else chunk
        assert "_post(" not in chunk, f"{reader} can mutate state at Amazon"


def test_the_commit_point_is_the_only_confirmation():
    """`confirm_placement` is the irreversible one, and nothing else may confirm.

    After it, FBA ids exist, Seller Central shows a working shipment, and a placement fee
    is incurred. No database rollback undoes that.
    """
    source = Path(spapi.__file__).read_text(encoding="utf-8")
    confirmations = re.findall(r'"[^"]*placementOptions/[^"]*confirmation[^"]*"', source)
    assert len(confirmations) == 1, (
        f"{len(confirmations)} places build a placement-confirmation URL; there must be "
        "exactly one so the commit point has a single caller"
    )
    start = source.index("async def confirm_placement")
    assert "COMMIT POINT" in source[start:start + 700], (
        "confirm_placement no longer says it is the irreversible step"
    )


def test_missing_credentials_are_a_state_not_an_error():
    """The app ran without SP-API for its whole life and must keep doing so.

    A distinct exception type so the screen can say "not set up" rather than showing a
    failure — and so a missing credential cannot 500 the Shipment page.
    """
    assert issubclass(spapi.SpApiNotConfigured, spapi.SpApiError)
    message = str(spapi.SpApiNotConfigured())
    assert "SP_API_CLIENT_ID" in message, "the message does not say what to set"


def test_only_label_formats_that_work_are_offered():
    """Measured against the live account: A4_2 and Letter_2 are refused for a
    non-partnered ("Other" carrier) shipment, which is every shipment this business
    sends. Offering an option that always errors reads as a broken app."""
    assert "PackageLabel_Thermal" in spapi.LABEL_PAGE_TYPES
    assert "PackageLabel_A4_4" in spapi.LABEL_PAGE_TYPES
    assert "PackageLabel_Plain_Paper" in spapi.LABEL_PAGE_TYPES
    assert "PackageLabel_A4_2" not in spapi.LABEL_PAGE_TYPES
    assert "PackageLabel_Letter_2" not in spapi.LABEL_PAGE_TYPES


def test_an_unknown_destination_is_not_treated_as_known():
    """`AMAZON_OPTIMIZED` may carry an empty address and warehouse id.

    The destination state decides which of the 15 GSTINs applies, so "Amazon did not say"
    must never be rendered as a blank state on a tax document.
    """
    known = spapi.AmazonShipment(
        inbound_plan_id="wf1", shipment_id="sh1", confirmation_id="FBA15X",
        warehouse_id="ISK3", state="MAHARASHTRA", destination_type="AMAZON_WAREHOUSE",
    )
    optimized = spapi.AmazonShipment(
        inbound_plan_id="wf2", shipment_id="sh2", confirmation_id="FBA15Y",
        destination_type="AMAZON_OPTIMIZED",
    )
    assert known.destination_known is True
    assert optimized.destination_known is False
    assert optimized.as_dict()["destination_known"] is False


def test_a_shipment_is_flattened_from_amazons_shape():
    """Built from the payload recorded live, so a change in Amazon's shape is caught
    here rather than by a blank field on an invoice."""
    payload = {
        "shipmentConfirmationId": "FBA15M59XQFZ",
        "status": "IN_TRANSIT",
        "name": "FBA STA (14/08/2026 08:15)-ISK3",
        "destination": {
            "destinationType": "AMAZON_WAREHOUSE",
            "warehouseId": "ISK3",
            "address": {"city": "BHIWANDI", "stateOrProvinceCode": "MAHARASHTRA",
                        "postalCode": "421302"},
        },
    }
    shipment = spapi._shipment_from_payload("wf835", "shcc455", payload)
    assert shipment.confirmation_id == "FBA15M59XQFZ"
    assert shipment.warehouse_id == "ISK3"
    assert shipment.state == "MAHARASHTRA"
    assert shipment.city == "BHIWANDI"
    assert shipment.destination_known is True


def test_only_error_severity_problems_count_as_failure():
    """Async operations answer 200 and report failure inside `operationProblems`.

    A 200 is therefore not success — this codebase has been bitten by silent failures
    repeatedly. WARNINGs are not failures, and treating them as such would block
    shipments Amazon accepted.
    """
    payload = {"operationProblems": [
        {"severity": "WARNING", "message": "a note"},
        {"severity": "ERROR", "message": "the real problem"},
        {"severity": "error", "message": "lowercase from Amazon"},
    ]}
    problems = spapi.operation_problems(payload)
    assert len(problems) == 2, "severity matching is case-sensitive or missed an error"
    assert spapi.operation_problems({}) == []
    assert spapi.operation_problems({"operationProblems": None}) == []


def test_the_token_cache_refuses_a_stale_token():
    """Tokens last an hour and are refreshed a minute early, so a request about to be
    made cannot be the one that discovers the expiry — that failure would surface as an
    opaque 403 in the middle of a sequence."""
    import time

    fresh = spapi._Token(value="t", expires_at=time.time() + 3600)
    expiring = spapi._Token(value="t", expires_at=time.time() + 30)
    empty = spapi._Token()
    assert fresh.usable is True
    assert expiring.usable is False, "a token expiring within the margin was reused"
    assert empty.usable is False


# ─── The endpoints ───────────────────────────────────────────────────────────

async def test_the_shipment_lookup_says_when_it_is_not_configured(auth_client):
    """200 with `configured: false`, never a 500.

    The test suite has no credentials, which is exactly the state a fresh install is in.
    """
    r = await auth_client.get("/shipment/amazon-shipments")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["shipments"] == []
    assert "SP_API_CLIENT_ID" in body["message"]


async def test_the_lookup_is_admin_only(ops_client):
    """Ops has no use for the owner's Amazon shipment data, and the plan sheet and
    Amazon upload are already closed to them for the same reason."""
    r = await ops_client.get("/shipment/amazon-shipments")
    assert r.status_code in (401, 403), r.status_code


async def test_the_preview_is_admin_only(ops_client):
    r = await ops_client.post(
        "/shipment/amazon-shipment-preview", json={"pack_dates": [MONDAY]}
    )
    assert r.status_code in (401, 403), r.status_code


async def _verify_day(auth_client, date, entries, cartons=30):
    await auth_client.post(
        f"/shipment/packing/{date}", json={"entries": entries, "cartons": cartons}
    )
    await auth_client.post(f"/shipment/packing/{date}/submit")
    r = await auth_client.post(f"/shipment/packing/{date}/verify")
    assert r.status_code == 200, r.text


async def test_the_preview_builds_the_lines_for_the_chosen_days(auth_client, plan_factory):
    await plan_factory()
    await _verify_day(auth_client, MONDAY, [{"asin": ASIN, "units": 100}])

    body = (await auth_client.post(
        "/shipment/amazon-shipment-preview", json={"pack_dates": [MONDAY]}
    )).json()

    assert body["ok"] is True, body["blockers"]
    assert body["units"] == 100
    assert body["request_body"]["items"] == [
        {"msku": "MF-CH-1KG", "quantity": 100,
         "labelOwner": "SELLER", "prepOwner": "NONE"}
    ]
    # And the human-readable copy for the screen.
    assert body["lines"][0]["_item"] == "Chana Sattu"


async def test_the_preview_refuses_and_names_a_product_with_no_sku(
    auth_client, plan_factory
):
    """The whole requirement, over HTTP.

    The blocker must name the product, not just count them: "1 product has no SKU" leaves
    the owner scanning 81 rows.
    """
    await plan_factory(items=[
        {"asin": "B0NOSKU", "item": "Mystery Product", "weight": 1.0, "brand": "MF",
         "fba_sku": "", "shipment_plan": 100, "deficit": 100},
    ])
    await _verify_day(auth_client, MONDAY, [{"asin": "B0NOSKU", "units": 60}])

    body = (await auth_client.post(
        "/shipment/amazon-shipment-preview", json={"pack_dates": [MONDAY]}
    )).json()

    assert body["ok"] is False
    joined = " ".join(body["blockers"])
    assert "Mystery Product" in joined, f"the product is not named: {joined}"
    assert "merchant SKU" in joined
    assert body["request_body"]["items"] == [], (
        "the SKU-less line reached the request body anyway"
    )


async def test_the_preview_refuses_an_unverified_day(auth_client, plan_factory):
    """Same gate as the invoice: this creates the shipment those boxes travel in, so
    unapproved counts must not reach Amazon."""
    await plan_factory()
    await auth_client.post(
        f"/shipment/packing/{MONDAY}",
        json={"entries": [{"asin": ASIN, "units": 100}], "cartons": 30},
    )
    await auth_client.post(f"/shipment/packing/{MONDAY}/submit")   # not verified

    r = await auth_client.post(
        "/shipment/amazon-shipment-preview", json={"pack_dates": [MONDAY]}
    )
    assert r.status_code == 400
    assert "not verified" in r.json()["error"].lower()


async def test_the_preview_aggregates_one_asin_across_days(auth_client, plan_factory):
    """One SKU packed on two days is ONE inbound line. Two lines for the same msku is
    what Amazon rejects."""
    await plan_factory()
    await _verify_day(auth_client, MONDAY, [{"asin": ASIN, "units": 100}])
    await _verify_day(auth_client, "2026-07-31", [{"asin": ASIN, "units": 40}])

    body = (await auth_client.post(
        "/shipment/amazon-shipment-preview",
        json={"pack_dates": [MONDAY, "2026-07-31"]},
    )).json()

    items = body["request_body"]["items"]
    assert len(items) == 1, f"{len(items)} lines for one msku"
    assert items[0]["quantity"] == 140


@pytest.mark.parametrize("payload,expected", [
    ({}, 400),                                   # no dates
    ({"pack_dates": []}, 400),
    ({"pack_dates": ["13-08-2026"]}, 400),       # wrong format
    ({"pack_dates": ["2026-12-25"]}, 404),       # nothing packed
])
async def test_the_preview_rejects_bad_input(auth_client, plan_factory, payload, expected):
    """It is reachable directly, so it must refuse rather than build a plausible
    shipment from nothing."""
    await plan_factory()
    r = await auth_client.post("/shipment/amazon-shipment-preview", json=payload)
    assert r.status_code == expected, f"{payload} -> {r.status_code}"


# ─── The screen ──────────────────────────────────────────────────────────────

def _shipment_source() -> str:
    return (
        Path(__file__).resolve().parent.parent / "templates" / "shipment.html"
    ).read_text(encoding="utf-8")


def test_the_screen_offers_both_read_only_lookups():
    source = _shipment_source()
    assert "lookupAmazonShipments" in source, "no way to fetch shipments from Amazon"
    assert "previewAmazonShipment" in source, "no dry run before creating a shipment"
    assert "/shipment/amazon-shipments" in source
    assert "/shipment/amazon-shipment-preview" in source


def test_amazons_destination_overrides_the_picked_fc():
    """Amazon decides where the boxes go; the owner's pick was a request.

    Silently keeping his choice when Amazon shipped elsewhere puts the wrong state's
    GSTIN on a tax document, and the change is called out on screen rather than made
    quietly.
    """
    source = _shipment_source()
    start = source.index("function useAmazonShipment")
    body = source[start:start + 1600]
    assert "lastFc = shipment.warehouse_id" in body, (
        "the destination Amazon reported does not replace the picked FC"
    )
    assert "Destination changed" in body, (
        "an FC change is applied silently, so the owner never learns Amazon sent the "
        "boxes somewhere else"
    )


def test_the_amazon_panel_is_built_with_textcontent():
    """Product titles and Amazon's own error messages both come from outside this app,
    and one of them ends up on a GST document."""
    source = _shipment_source()
    start = source.index("function amazonLine")
    body = source[start:source.index("function useAmazonShipment")]
    assert "textContent" in body
    assert ".innerHTML" not in body, (
        "the Amazon panel interpolates outside data with innerHTML"
    )


# ─── Ordering and connection reuse ───────────────────────────────────────────

def test_plans_are_sorted_newest_first():
    """Amazon returned the 10 live plans NOT in date order.

    Measured: the raw response had 2026-08-04 above 2026-08-07. A picker that trusted
    Amazon's order would offer a July shipment first, and choosing the wrong shipment puts
    the wrong FBA id and destination state on a GST invoice.
    """
    source = Path(spapi.__file__).read_text(encoding="utf-8")
    assert re.search(
        r'plans\.sort\(\s*key=lambda p: str\(p\.get\("createdAt"\).*reverse=True',
        source,
    ), "the plan list is not sorted newest-first"


def test_one_http_client_is_shared_across_the_lookup():
    """The performance fix, and it is not obvious.

    Sequentially the lookup took 21s; parallelising it made it WORSE (36s). The cost was
    not waiting, it was a fresh TCP+TLS handshake per call — a new AsyncClient every
    request. One shared client took it to 10.4s. A regression here would be felt as "the
    button hangs" rather than as a failure, so it is pinned.
    """
    source = Path(spapi.__file__).read_text(encoding="utf-8")
    start = source.index("async def recent_shipments")
    body = source[start:]

    assert "async with httpx.AsyncClient" in body, (
        "recent_shipments no longer opens one client for the whole lookup"
    )
    # Every inner call must be handed that client, or it silently opens its own.
    for call in ("list_inbound_plans(client=client)",
                 "get_inbound_plan(plan_id, client=client)",
                 "get_shipment(plan_id, shipment_id, client=client)"):
        assert call in body, f"{call} does not reuse the shared connection"


def test_only_the_needed_plan_details_are_fetched():
    """Fetching detail for all 10 plans when the caller asked for 3 cost ~7 wasted round
    trips at 2 requests/second. The slice is what took limit=5 from 11.8s to 7.6s."""
    source = Path(spapi.__file__).read_text(encoding="utf-8")
    start = source.index("async def recent_shipments")
    body = source[start:]
    assert "][:limit]" in body, (
        "every plan's detail is fetched regardless of how many shipments were asked for"
    )


def test_the_concurrency_respects_amazons_rate_limit():
    """2 requests/second is documented for these operations. Going higher would spend
    the burst allowance on a convenience lookup and make the picker fail intermittently
    with a 429 — worse than being a few seconds slow."""
    assert spapi._CONCURRENCY == 2, (
        f"concurrency is {spapi._CONCURRENCY}; Amazon documents 2 requests/second here"
    )


def _shipment_router_source() -> str:
    """app/routers/shipment.py, comments intact.

    Ordering assertions below use `.index()` on real code positions, which is the point:
    several of these guards are about WHEN something happens, not whether it exists.
    """
    from app.routers import shipment as router_module

    return Path(router_module.__file__).read_text(encoding="utf-8")


# ─── Creating the shipment: the guards, and the traps live testing exposed ────
#
# Every fact asserted here was learned by creating real (then cancelled) inbound plans
# against the live Amazon.in account on 2026-08-16. None of it is in the documentation, and
# two of the four would have been impossible to guess.


async def test_create_needs_an_fc(auth_client, plan_factory):
    """The destination is not optional here.

    Unlike the invoice, where a blank FC is a field the owner fills in later, a shipment
    without a destination cannot be created at all — and the FC also decides the GSTIN.
    """
    await plan_factory()
    await _verify_day(auth_client, MONDAY, [{"asin": ASIN, "units": 10}])
    r = await auth_client.post(
        "/shipment/amazon-shipment/create", json={"pack_dates": [MONDAY]}
    )
    assert r.status_code == 400
    assert "FC" in r.json()["error"]


async def test_create_refuses_an_unknown_fc(auth_client, plan_factory):
    await plan_factory()
    await _verify_day(auth_client, MONDAY, [{"asin": ASIN, "units": 10}])
    r = await auth_client.post(
        "/shipment/amazon-shipment/create",
        json={"pack_dates": [MONDAY], "fc_code": "ISK33"},
    )
    assert r.status_code == 400
    assert "ISK33" in r.json()["error"]


async def test_create_is_admin_only(ops_client):
    """Creating a shipment is the owner's decision, like the plan sheet and the Amazon
    upload. It also spends money if a placement fee ever applies."""
    r = await ops_client.post(
        "/shipment/amazon-shipment/create",
        json={"pack_dates": [MONDAY], "fc_code": "ISK3"},
    )
    assert r.status_code in (401, 403)


async def test_confirm_and_cancel_are_admin_only(ops_client):
    for path, body in (
        ("/shipment/amazon-shipment/confirm",
         {"inbound_plan_id": "wf1", "placement_option_id": "pl1"}),
        ("/shipment/amazon-shipment/cancel", {"inbound_plan_id": "wf1"}),
    ):
        r = await ops_client.post(path, json=body)
        assert r.status_code in (401, 403), f"{path} -> {r.status_code}"


async def test_create_says_when_amazon_is_not_set_up(auth_client, plan_factory):
    """The test suite has no credentials, which is also a fresh install's state. A 400
    that names the missing keys, never a 500."""
    await plan_factory()
    await _verify_day(auth_client, MONDAY, [{"asin": ASIN, "units": 10}])
    r = await auth_client.post(
        "/shipment/amazon-shipment/create",
        json={"pack_dates": [MONDAY], "fc_code": "ISK3"},
    )
    assert r.status_code == 400
    assert "SP_API" in r.json()["error"]


async def test_confirm_requires_both_ids(auth_client):
    """A confirm with a missing option id would be a 4xx from Amazon after a round trip;
    refusing locally keeps the error legible."""
    for body in ({"inbound_plan_id": "wf1"}, {"placement_option_id": "pl1"}, {}):
        r = await auth_client.post("/shipment/amazon-shipment/confirm", json=body)
        assert r.status_code == 400, body


# ─── The traps, asserted so they cannot come back ────────────────────────────

def test_a_zero_declared_value_is_never_sent():
    """The most misleading error of the whole build.

    With no purchase rate on file the route sent `declaredValue: 0`, and Amazon answered
    *"We encountered an internal error. Please try again."* — which reads as a transient
    fault and is not one. The identical call with a real amount succeeded immediately.

    So a missing rate is refused BEFORE the plan is created, and the guard lives in the
    route where the value is computed.
    """
    source = _shipment_router_source()
    assert "missing_rate" in source, "nothing checks for a missing purchase rate"
    # It must be refused before anything exists at Amazon, or a data problem leaves an
    # orphan plan in Seller Central.
    rate_check = source.index("missing_rate = [")
    create_call = source.index("plan_id = await spapi.create_inbound_plan")
    assert rate_check < create_call, (
        "the purchase-rate check runs AFTER the plan is created, so a missing rate leaves "
        "an orphan plan at Amazon that someone has to cancel by hand"
    )


def test_compliance_is_declared_before_placement():
    """India enforces HSN and declared value at PLACEMENT, not at creation.

    The plan created fine and then placement failed with "ERROR: Declared value need to be
    provided." Amazon already held those values for the SKUs tested and still refused until
    they were re-declared, so it is sent for every line every time.
    """
    source = _shipment_router_source()
    declare = source.index("declare_item_compliance")
    placement = source.index("generate_placement_options")
    assert declare < placement, (
        "placement is requested before the GST details are declared, which fails with "
        '"Declared value need to be provided"'
    )


def test_the_compliance_body_is_flat_not_a_list():
    """`{"complianceDetails": [...]}` — the shape the GET RETURNS — is rejected:

        400 3 validation errors detected: Value '' at 'request.msku' failed to satisfy
            constraint: Member must have length greater than or equal to 1

    One SKU per call, `{"msku": ..., "taxDetails": {...}}`. Another case of Amazon not
    accepting the shape it returns.
    """
    source = Path(spapi.__file__).read_text(encoding="utf-8")
    start = source.index("async def declare_item_compliance")
    body = source[start:start + 1400]
    assert '"msku": msku' in body, "the compliance body is not flat"
    assert "complianceDetails" not in body, (
        "the compliance body wraps the SKU in a list again, which Amazon rejects"
    )


def test_the_plan_id_is_persisted_before_placement_is_confirmed():
    """The irreversibility rule.

    A plan confirmed at Amazon with no local record is invisible here and entirely real
    there — and unlike the invoice-attach window, a shipment cannot be reconciled by
    re-running anything. So the id is written the moment it exists.
    """
    source = _shipment_router_source()
    persist = source.index("attach_inbound_plan")
    placement = source.index("generate_placement_options")
    assert persist < placement, (
        "the inbound plan id is stored after placement, so a crash in between loses track "
        "of a plan that exists at Amazon"
    )


async def test_days_already_sent_to_amazon_are_refused(auth_client, plan_factory, db):
    """Two shipments for one set of cartons means the FC expects twice what is on the
    truck — the same class of mistake as invoicing a day twice, which is already a 409.

    Exercised over HTTP rather than grepped. The grep version SURVIVED a mutation that
    replaced the check with an empty list: the message was still in the file, so the string
    was still found, while the guard did nothing.
    """
    from sqlalchemy import select
    from app.models import ShipmentPackingDay

    await plan_factory()
    await _verify_day(auth_client, MONDAY, [{"asin": ASIN, "units": 10}])

    # Pretend this day already went to Amazon.
    day = (await db.execute(
        select(ShipmentPackingDay).where(ShipmentPackingDay.pack_date == MONDAY)
    )).scalar_one()
    day.shipment_confirmation_id = "FBA15EXISTING"
    await db.commit()

    r = await auth_client.post(
        "/shipment/amazon-shipment/create",
        json={"pack_dates": [MONDAY], "fc_code": "ISK3"},
    )
    assert r.status_code == 409, (
        f"a day already sent to Amazon was accepted again ({r.status_code})"
    )
    error = r.json()["error"]
    assert "FBA15EXISTING" in error, f"the existing shipment is not named: {error}"


def test_cancel_will_not_forget_a_confirmed_shipment():
    """Cancelling must not detach a shipment Amazon is expecting.

    `clear_inbound_plan` skips any day carrying a `shipment_confirmation_id`: those boxes
    are real to Amazon, and forgetting them locally would let the same cartons be sent
    again.
    """
    from app.shipment import repository

    source = Path(repository.__file__).read_text(encoding="utf-8")
    start = source.index("async def clear_inbound_plan")
    body = source[start:start + 1400]
    assert "if day.shipment_confirmation_id:" in body and "continue" in body, (
        "clear_inbound_plan clears confirmed days too, which would let the same boxes be "
        "shipped twice"
    )


def test_the_destination_recorded_is_amazons_not_the_request():
    """The FC asked for and the FC used can differ, and the destination state decides
    which of the 15 GSTINs the invoice must use. So what is stored is Amazon's answer."""
    source = _shipment_router_source()
    start = source.index("async def confirm_amazon_shipment")
    body = source[start:start + 4000]
    assert "warehouse_id=first.warehouse_id" in body, (
        "the requested FC is stored instead of the one Amazon actually chose"
    )
    assert "state=first.state" in body


def test_labels_are_returned_as_a_url_not_proxied():
    """Amazon's label link is short-lived and signed. Streaming it through this app would
    add a timeout to a download that works directly, for no benefit."""
    source = _shipment_router_source()
    start = source.index("async def amazon_shipment_labels")
    body = source[start:start + 1600]
    assert '"url": url' in body
    assert "StreamingResponse" not in body
