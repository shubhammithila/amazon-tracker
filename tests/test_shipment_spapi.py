"""Amazon SP-API: the read-only steps, and the missing-SKU block.

Steps 1-4 of ``docs/sp-api-create-shipment-plan.md``. **Nothing here creates, confirms or
modifies anything at Amazon**, and one test asserts exactly that by grepping the client
for mutating verbs — because the difference between this and the next step is a real
shipment that no failed transaction can undo.

The suite never calls Amazon. Responses are the shapes recorded from the live account on
2026-08-15, so the tests stay fast, deterministic, and independent of what is currently in
Seller Central.

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
    """Both were SELLER on every line of every live plan, at zero prep fee.

    Sent explicitly rather than relying on Amazon's default, because label ownership
    decides who pays and a default is Amazon's to change.
    """
    result = logic.amazon_plan_body(
        {}, [_Item(ASIN, "MF-CH-1KG", "Chana", 1.0)], {ASIN: 10}, "A21TJRUUN4KGV"
    )
    line = result["body"]["items"][0]
    assert line["labelOwner"] == "SELLER"
    assert line["prepOwner"] == "SELLER"


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

def test_the_spapi_client_makes_no_mutating_calls():
    """Steps 1-4 must not be able to create a shipment.

    A confirmed inbound plan is a real shipment at Amazon that no rollback can undo. The
    guard is structural rather than a comment: the client has one request helper and it is
    a GET, so nothing here can POST by accident.
    """
    source = (Path(spapi.__file__)).read_text(encoding="utf-8")
    body = re.sub(r'""".*?"""', "", source, flags=re.S)      # drop docstrings
    body = re.sub(r"#[^\n]*", "", body)                       # and comments

    assert "client.post" in body, "the LWA token exchange is a POST and must still exist"
    # ...but the only POST allowed is to Amazon's token endpoint.
    posts = re.findall(r"\w+\.post\(\s*([^,\n]+)", body)
    for target in posts:
        assert "LWA_TOKEN_URL" in target, (
            f"a POST to {target.strip()} — steps 1-4 must not mutate anything at Amazon"
        )
    for verb in (".put(", ".patch(", ".delete("):
        assert verb not in body, f"the client uses {verb}, which can only be a mutation"


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
         "labelOwner": "SELLER", "prepOwner": "SELLER"}
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
