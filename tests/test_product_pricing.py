"""The Products tab: purchase rate, HSN and GST per product.

Asked for: *"lets add a products tab separately where I can update the pricing. Jitne
products ka pricing hai abhi wo daal do. and rest ka blank chor do."*

The reason this exists at all is a real failure. Creating an Amazon shipment refused with
*"2 product(s) have no purchase rate: ABC Sattu 500g, Pea Isolate Sattu 500g"* — and there
was nowhere in the app to fix it. The prices lived in ``pricing_data.json``, a file tracked
in git, so the only remedy was editing a file and redeploying.

Two properties carry most of the weight here:

* **A missing price is NULL, never 0.** Amazon rejects an inbound shipment whose declared
  value is zero, and it does so with *"We encountered an internal error. Please try
  again."* — which reads as a fault on their side and is not one. Verified against the live
  account: the identical call with a real amount succeeded immediately.
* **Unpriced ACTIVE products sort first.** 72 products have no price but only 8 are still
  sold. Sorting by "unpriced" alone buries those 8 among 64 discontinued lines.
"""
import pytest

from app import products

pytestmark = pytest.mark.regression


# ─── The table, and why NULL is not 0 ────────────────────────────────────────

async def test_a_missing_price_is_null_not_zero(auth_client, db):
    """The property Amazon's error message hides.

    A declared value of 0 is refused by Amazon with "We encountered an internal error",
    which looks transient and is not. So "not priced yet" must be a distinguishable state,
    not a number that can be sent.
    """
    row = await products.upsert(db, "B0NEWPROD1")
    assert row.purchase_rate is None, "a new product was given a zero price"
    assert await products.rate_for(db, "B0NEWPROD1") == 0.0, (
        "rate_for must report 0 for an unpriced product, which is what the shipment "
        "guard checks — but the stored value stays NULL"
    )


async def test_a_zero_price_is_refused_over_http(auth_client):
    """Refused with the reason, not silently coerced.

    A 0 stored here would reach Amazon as a declared value and be rejected with a message
    that sends the owner looking for a fault on Amazon's side.
    """
    r = await auth_client.post(
        "/products-pricing/save", json={"asin": "B0TEST00001", "purchase_rate": 0}
    )
    assert r.status_code == 400
    error = r.json()["error"]
    assert "more than 0" in error
    assert "declared value" in error, "the error does not explain why zero is refused"


async def test_a_price_can_be_cleared(auth_client, db):
    """An empty string clears it; that is different from 0.

    A rate typed by mistake has to be removable, and the way back is a blank field rather
    than a zero — which would be stored and then rejected by Amazon.
    """
    await products.upsert(db, "B0CLEARME1", purchase_rate=50)
    assert await products.rate_for(db, "B0CLEARME1") == 50.0

    r = await auth_client.post(
        "/products-pricing/save", json={"asin": "B0CLEARME1", "purchase_rate": ""}
    )
    assert r.status_code == 200
    assert r.json()["purchase_rate"] is None
    products.invalidate()
    assert await products.rate_for(db, "B0CLEARME1") == 0.0


async def test_saving_one_field_leaves_the_others_alone(auth_client, db):
    """The screen saves one cell at a time.

    A full-row payload would let a blank cell elsewhere overwrite a value that is already
    correct — so an omitted field means "leave alone", and this is what proves it.
    """
    await products.upsert(db, "B0PARTIAL1", purchase_rate=80, hsn_code="1106", gst_rate=5)

    r = await auth_client.post(
        "/products-pricing/save", json={"asin": "B0PARTIAL1", "gst_rate": 12}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["gst_rate"] == 12
    assert body["purchase_rate"] == 80.0, "the price was wiped by a GST-only save"
    assert body["hsn_code"] == "1106", "the HSN was wiped by a GST-only save"


@pytest.mark.parametrize("hsn", ["11", "12345", "abcd", "1106x"])
async def test_an_invalid_hsn_is_refused(auth_client, hsn):
    """HSN codes are 4, 6 or 8 digits.

    A typo reaches both a GST document and an Amazon inbound declaration, so it is checked
    rather than trusted.
    """
    r = await auth_client.post(
        "/products-pricing/save", json={"asin": "B0HSNTEST1", "hsn_code": hsn}
    )
    assert r.status_code == 400, f"{hsn!r} was accepted"
    assert "HSN" in r.json()["error"]


@pytest.mark.parametrize("hsn", ["1106", "110610", "11061000"])
async def test_valid_hsn_lengths_are_accepted(auth_client, hsn):
    r = await auth_client.post(
        "/products-pricing/save", json={"asin": "B0HSNOK0001", "hsn_code": hsn}
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("gst", [-1, 101, "abc"])
async def test_an_impossible_gst_rate_is_refused(auth_client, gst):
    r = await auth_client.post(
        "/products-pricing/save", json={"asin": "B0GSTTEST1", "gst_rate": gst}
    )
    assert r.status_code == 400, f"{gst!r} was accepted"


async def test_hsn_and_gst_default_to_1106_at_5(db):
    """Every F2D product is HSN 1106 at 5%, so that is the default rather than an error —
    but it is stored per product so a non-food line can differ."""
    tax = await products.tax_for(db, "B0UNKNOWN01")
    assert tax["hsn_code"] == "1106"
    assert tax["gst_rate"] == 5.0


# ─── The list is the CATALOGUE, not the price table ──────────────────────────

@pytest.fixture
def fake_catalogue(monkeypatch):
    """Three products, one of them discontinued.

    The suite's autouse fixture returns an EMPTY catalogue so no test hits the live Google
    Sheet. That is right for the shipment tests, but this screen's whole job is to list
    products — so the ones that matter are supplied explicitly rather than depending on
    whatever the sheet happens to hold today.
    """
    async def _catalogue():
        return (
            {
                "B0PRICED001": {"asin": "B0PRICED001", "name": "Chana Sattu",
                                "weight": 1.0, "brand": "Mithila Foods", "active": True},
                "B0BLANK0001": {"asin": "B0BLANK0001", "name": "Kulthi Sattu",
                                "weight": 0.5, "brand": "Mithila Foods", "active": True},
                "B0OLD000001": {"asin": "B0OLD000001", "name": "Ajinomoto",
                                "weight": 0.2, "brand": "Mithila Foods", "active": False},
            },
            None,
            "sheet",
        )

    monkeypatch.setattr(
        "app.shipment.catalogue.load_catalogue", _catalogue, raising=True
    )
    return _catalogue


async def test_the_list_includes_products_that_have_no_price(
    auth_client, db, fake_catalogue
):
    """The point of the screen is to find the blanks.

    A list built from the price table would show only what is already priced, which is
    exactly backwards — the blanks are what the owner came to fill.
    """
    await products.upsert(db, "B0PRICED001", purchase_rate=100)

    body = (await auth_client.get("/products-pricing")).json()
    by_asin = {r["asin"]: r for r in body["products"]}

    assert body["total"] == 3
    assert by_asin["B0PRICED001"]["purchase_rate"] == 100.0
    # The unpriced ones are LISTED, not omitted.
    assert "B0BLANK0001" in by_asin
    assert by_asin["B0BLANK0001"]["purchase_rate"] is None
    assert by_asin["B0OLD000001"]["purchase_rate"] is None
    # Every row carries the editable fields, priced or not.
    for row in body["products"]:
        assert "hsn_code" in row and "gst_rate" in row and "active" in row


async def test_a_priced_product_the_catalogue_dropped_is_still_listed(
    auth_client, db, fake_catalogue
):
    """An ASIN can leave the MRP sheet while its price stays meaningful.

    Silently dropping it would look like data loss, so it is listed and tagged instead.
    """
    await products.upsert(db, "B0GONE00001", purchase_rate=75)

    body = (await auth_client.get("/products-pricing")).json()
    row = next((r for r in body["products"] if r["asin"] == "B0GONE00001"), None)
    assert row is not None, "a priced ASIN missing from the catalogue was dropped"
    assert row["in_catalogue"] is False
    assert row["purchase_rate"] == 75.0


async def test_unpriced_active_products_sort_first(auth_client, db, fake_catalogue):
    """The ordering IS the feature, and it needs a REAL mix to prove it.

    On the live data 72 products have no price and only 8 are still sold. "Unpriced first"
    alone would put 64 discontinued lines above the 8 that can actually refuse a shipment.

    An earlier version of this test asserted only that the sort classes were non-decreasing
    — and SURVIVED a mutation that removed the active-first rule, because the stub
    catalogue happened to list the active product first anyway. So the fixture now supplies
    an inactive unpriced product BEFORE an active one alphabetically ("Ajinomoto" before
    "Kulthi Sattu"), which means only the real rule can produce the expected order.
    """
    await products.upsert(db, "B0PRICED001", purchase_rate=100)

    rows = (await auth_client.get("/products-pricing")).json()["products"]
    order = [r["asin"] for r in rows]

    # Kulthi (unpriced, ACTIVE) must beat Ajinomoto (unpriced, inactive) even though
    # Ajinomoto sorts first by name — and both must beat the priced one.
    assert order.index("B0BLANK0001") < order.index("B0OLD000001"), (
        "an unpriced INACTIVE product sorts above an unpriced active one, so the 8 that "
        "matter get buried among the 64 discontinued ones"
    )
    assert order.index("B0OLD000001") < order.index("B0PRICED001"), (
        "a priced product sorts above an unpriced one"
    )


async def test_the_active_missing_count_is_reported_separately(
    auth_client, db, fake_catalogue
):
    """"8 active without a price" is actionable; "72 without a price" is not.

    A discontinued product with no price can never reach a shipment, so conflating the two
    turns an eight-item job into a list nobody finishes.

    Asserted as exact numbers against the fixture. The earlier `<=` version was true of
    both the right answer and the wrong one, and survived a mutation that made the two
    counts identical.
    """
    await products.upsert(db, "B0PRICED001", purchase_rate=100)

    body = (await auth_client.get("/products-pricing")).json()
    # Two unpriced (Kulthi active, Ajinomoto inactive), of which ONE is active.
    assert body["missing_price"] == 2, body["missing_price"]
    assert body["missing_price_active"] == 1, (
        f"missing_price_active is {body['missing_price_active']}, but only one unpriced "
        "product is still sold — the count the owner acts on must exclude discontinued ones"
    )


# ─── The chain: a price typed here unblocks a shipment ───────────────────────

async def test_a_price_typed_here_reaches_the_shipment_flow(auth_client, plan_factory):
    """The failure that prompted this screen, end to end.

    "2 product(s) have no purchase rate: ABC Sattu 500g, Pea Isolate Sattu 500g" — with no
    way to fix it in the app. Now there is, and this proves the fix takes effect without a
    restart or a redeploy.
    """
    await plan_factory(items=[
        {"asin": "B0NOPRICE1", "item": "Mystery Product", "weight": 0.5, "brand": "MF",
         "fba_sku": "MYSTERY-500G", "shipment_plan": 100, "deficit": 100},
    ])
    await auth_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0NOPRICE1", "units": 10}], "cartons": 5},
    )
    await auth_client.post("/shipment/packing/2026-07-30/submit")
    await auth_client.post("/shipment/packing/2026-07-30/verify")

    # Before: the create route refuses, naming the product.
    r = await auth_client.post(
        "/shipment/amazon-shipment/create",
        json={"pack_dates": ["2026-07-30"], "fc_code": "ISK3"},
    )
    assert r.status_code == 400
    assert "purchase rate" in r.json()["error"], r.json()["error"]
    assert "Mystery Product" in r.json()["error"]

    # Fix it on the Products tab.
    saved = await auth_client.post(
        "/products-pricing/save",
        json={"asin": "B0NOPRICE1", "purchase_rate": 42.5},
    )
    assert saved.status_code == 200

    # After: the rate is visible to the shipment flow immediately — no restart, and the
    # cache was invalidated by the write rather than waiting for a TTL.
    r2 = await auth_client.post(
        "/shipment/amazon-shipment/create",
        json={"pack_dates": ["2026-07-30"], "fc_code": "ISK3"},
    )
    assert "purchase rate" not in (r2.json().get("error") or ""), (
        "the price typed on the Products tab did not reach the shipment flow"
    )


async def test_the_edit_takes_effect_without_a_restart(auth_client, db):
    """The cache is invalidated on write, not on a timer.

    A price the owner just typed has to apply to the next shipment attempt — a TTL would
    mean "it did not work" for however long the window was.
    """
    await products.upsert(db, "B0CACHE0001", purchase_rate=10)
    assert await products.rate_for(db, "B0CACHE0001") == 10.0

    await auth_client.post(
        "/products-pricing/save", json={"asin": "B0CACHE0001", "purchase_rate": 99}
    )
    assert await products.rate_for(db, "B0CACHE0001") == 99.0, (
        "a saved price was not visible until the cache expired"
    )


# ─── Access ──────────────────────────────────────────────────────────────────

async def test_pricing_is_admin_only(ops_client):
    """A purchase rate is the cost side of the business. The Accounts preset withholds
    purchase costs for the same reason, so this cannot be an area grant."""
    for method, path in (("get", "/products-pricing"),
                         ("post", "/products-pricing/save")):
        r = await getattr(ops_client, method)(
            path, **({"json": {"asin": "B0X"}} if method == "post" else {})
        )
        assert r.status_code in (401, 403), f"{path} -> {r.status_code}"


async def test_the_page_is_admin_only(ops_client):
    r = await ops_client.get("/pricing-page")
    assert r.status_code in (302, 303, 401, 403), r.status_code


# ─── The screen ──────────────────────────────────────────────────────────────

def _pricing_source() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parent.parent / "templates" / "pricing.html"
    ).read_text(encoding="utf-8")


def test_the_missing_badge_has_one_implementation():
    """It had two, and they disagreed.

    The save handler recomputed the badge inline and reported "71 without a price",
    dropping the active/total distinction the moment a price was typed. Found in a browser.
    Two copies of a rule is two chances to disagree.
    """
    source = _pricing_source()
    assert "function renderMissingBadge" in source
    # Exactly one place builds the text.
    assert source.count("active without a price") == 1, (
        "the badge text is built in more than one place, which is how the two copies "
        "started disagreeing"
    )


def test_the_price_input_is_saved_on_change_not_on_every_keystroke():
    """Typing "65" on `input` would save 6 and then 65, and 6 is a real price briefly
    stored against a real product."""
    source = _pricing_source()
    assert 'addEventListener("change"' in source
    assert 'addEventListener("input", async' not in source, (
        "the price field saves on every keystroke, so an intermediate value is stored"
    )


def test_product_names_are_escaped():
    """They come from the MRP sheet, which is a spreadsheet anyone can type into, and they
    are rendered with innerHTML."""
    source = _pricing_source()
    assert "function esc(" in source
    assert "esc(p.item)" in source, "the product name is interpolated unescaped"
    assert "esc(p.asin)" in source
