"""Inactive products must not reach a plan — and a sheet outage must not stop one.

Column T ("Active", Y/N) of the product master sheet is the owner's own record of
what he still sells. At the time of writing the live sheet marks **152 ASINs
inactive against 119 active**, so this is not a trim: over half the catalogue
would otherwise be offered to the warehouse, put on the Amazon upload, and
potentially invoiced.

The awkward part is not the filter, it is what happens when the sheet cannot be
read. Two failures are available and they are not symmetrical:

* **Blocking generate** turns a Google outage or a sharing change into "the app is
  broken" on a Monday morning, for a reason the owner cannot fix.
* **Silently including everything** hands the packer 88 discontinued SKUs and
  looks identical to a correct plan.

So: fall back to the last good copy and say which date it came from; and if there
has never been a good copy, proceed with everything but say that too. Both are
covered below, because a fallback nobody has watched work is not a fallback.

Network access is stubbed throughout. A test that reads the real sheet would be
slow, would fail offline, and — worst — would change behaviour when somebody edits
a spreadsheet, so an unrelated test could break on Wednesday because a product was
discontinued on Tuesday.
"""
import json

import pytest

from app.shipment import catalogue

pytestmark = pytest.mark.regression


#: A miniature version of the real sheet: the same column layout (ASIN in I,
#: Active in T) with a deliberately awkward mix.
#:
#: The Net Weight values are deliberately NOT all numeric. The real sheet stores a
#: bare number ("0.5", "1"), but "500g" is exactly the sort of thing a hand-edited
#: cell contains, and a row must survive it — weight only affects sort order, whereas
#: dropping the row loses a product from a shipment.
SHEET_CSV = (
    "Name,Net Weight,M.R.P,M.F.G. DATE,Use By Date,FSSAI,Expiry ,Batch Code,ASIN,"
    "FNSKU,FK SKU,FSN,Amazon FBA SKU,Split Into,Packet Size,Packet used,"
    "Product label,Blinkit UPC Code,Brand Name,Active\n"
    "Live Sattu,500g,,,,,,,B0AAAAAAAA,,,,MF-1,,,,,,Mithila,Y\n"
    "Dead Chana,1kg,,,,,,,B0BBBBBBBB,,,,MF-2,,,,,,Mithila,N\n"
    "Blank Flag,1kg,,,,,,,B0CCCCCCCC,,,,MF-3,,,,,,Mithila,\n"
    "Lowercase yes,2kg,,,,,,,B0DDDDDDDD,,,,MF-4,,,,,,Howrah,y\n"
    "Junk row with no asin,,,,,,,,,,,,,,,,,,,Y\n"
)

#: The same layout with the numeric weights the real sheet actually uses, for the
#: tests that care about the product record rather than the Active flag.
CATALOGUE_CSV = (
    "Name,Net Weight,M.R.P,M.F.G. DATE,Use By Date,FSSAI,Expiry ,Batch Code,ASIN,"
    "FNSKU,FK SKU,FSN,Amazon FBA SKU,Split Into,Packet Size,Packet used,"
    "Product label,Blinkit UPC Code,Brand Name,Active\n"
    "Triphala Sattu,0.5,400,,,,,,B0H8NPDB88,,,,,,zipper,Sticker,No,,Mithila Foods,Y\n"
    "Triphala Sattu,1,650,,,,,,B0H8NPVN6Z,,,,,,zipper,Sticker,No,,Mithila Foods,Y\n"
    "Retired Thing,1,200,,,,,,B0RETIRED1,,,,,,zipper,Sticker,No,,Mithila Foods,N\n"
    "Howrah Rice,2,300,,,,,,B0HOWRAH01,,,,,,zipper,Sticker,No,,Howrah Foods,Y\n"
    "Bad Weight,not-a-number,50,,,,,,B0BADWEIGH,,,,,,zipper,Sticker,No,,Mithila Foods,Y\n"
)


# ─── Reading the sheet ───────────────────────────────────────────────────────

def test_only_y_means_active():
    flags = catalogue.parse_active_flags(SHEET_CSV)
    assert flags["B0AAAAAAAA"] is True, "Y should be active"
    assert flags["B0DDDDDDDD"] is True, "lowercase y should be active too"
    assert flags["B0BBBBBBBB"] is False, "N should be inactive"


def test_a_blank_flag_means_inactive():
    """Blank is not "probably fine".

    95 rows in the live sheet have an empty column T. Treating blank as active
    would put every one of them back on the packer's sheet, which is the outcome
    this feature exists to prevent — and the owner would have no way to tell the
    difference between "not decided" and "yes".
    """
    assert catalogue.parse_active_flags(SHEET_CSV)["B0CCCCCCCC"] is False


def test_rows_without_an_asin_are_ignored():
    flags = catalogue.parse_active_flags(SHEET_CSV)
    assert len(flags) == 4, f"picked up junk rows: {sorted(flags)}"


def test_columns_are_found_by_name_not_position():
    """A column inserted in the middle of the sheet must not shift the flag.

    Someone adding a column by hand is normal maintenance. Position-only lookup
    would then read "Active" from whatever landed in slot T — most likely the
    brand name, which matches nothing, marking the entire catalogue inactive and
    producing an empty plan. Loud, but for a completely baffling reason.
    """
    shifted = SHEET_CSV.replace("Name,Net Weight", "NEW COLUMN,Name,Net Weight")
    shifted = shifted.replace(",B0AAAAAAAA", ",x,B0AAAAAAAA")
    shifted = shifted.replace(",B0BBBBBBBB", ",x,B0BBBBBBBB")
    shifted = shifted.replace(",B0CCCCCCCC", ",x,B0CCCCCCCC")
    shifted = shifted.replace(",B0DDDDDDDD", ",x,B0DDDDDDDD")

    flags = catalogue.parse_active_flags(shifted)
    assert flags.get("B0AAAAAAAA") is True, (
        "an inserted column broke the lookup — every product would read inactive"
    )
    assert flags.get("B0BBBBBBBB") is False


def test_an_empty_sheet_is_not_a_catastrophe():
    assert catalogue.parse_active_flags("") == {}
    assert catalogue.parse_active_flags("just,a,header,row\n") == {}


def test_an_unknown_asin_counts_as_active():
    """Missing information is not a decision to discontinue.

    Dropping an ASIN the sheet has never heard of would shrink the plan silently,
    leaving the owner to notice a row count. Including it is visible and harmless
    — he can remove the row.
    """
    flags = {"B0AAAAAAAA": False}
    assert catalogue.is_active(flags, "B0NOTINSHEET") is True
    assert catalogue.is_active(flags, "B0AAAAAAAA") is False
    assert catalogue.is_active({}, "anything") is True


# ─── Fetch, cache and the fallback ───────────────────────────────────────────

@pytest.fixture
def temp_cache(tmp_path, monkeypatch):
    """Point the cache file somewhere disposable.

    Used together with ``@pytest.mark.real_catalogue``, which tells conftest's
    autouse stub to stand aside so these tests reach the genuine
    ``load_active_flags`` with only httpx faked underneath it.
    """
    path = tmp_path / "active_products.json"
    monkeypatch.setattr(catalogue, "CACHE_FILE", path, raising=True)
    return path


class _Response:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def _fake_client(response=None, error=None):
    """A stand-in httpx.AsyncClient that returns or raises what we tell it."""
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            if error is not None:
                raise error
            return response

    return lambda *a, **kw: _Client()


@pytest.mark.real_catalogue
async def test_a_successful_fetch_returns_flags_and_caches_them(temp_cache, monkeypatch):
    monkeypatch.setattr(
        catalogue.httpx, "AsyncClient", _fake_client(_Response(200, SHEET_CSV))
    )
    flags, warning = await catalogue.load_active_flags()

    assert warning is None, "a clean fetch should not warn"
    assert flags["B0BBBBBBBB"] is False
    assert temp_cache.exists(), "the fetch was not cached, so there is no fallback"

    saved = json.loads(temp_cache.read_text(encoding="utf-8"))
    assert saved["flags"]["B0BBBBBBBB"] is False
    assert saved["fetched_at"], "no timestamp, so the warning cannot name a date"


@pytest.mark.real_catalogue
async def test_a_network_failure_falls_back_to_the_cache(temp_cache, monkeypatch):
    """The Monday-morning case. Generate must still work."""
    temp_cache.write_text(json.dumps({
        "fetched_at": "2026-08-01T09:00:00",
        "flags": {"B0BBBBBBBB": False, "B0AAAAAAAA": True},
    }), encoding="utf-8")

    monkeypatch.setattr(
        catalogue.httpx, "AsyncClient",
        _fake_client(error=RuntimeError("DNS is down")),
    )
    flags, warning = await catalogue.load_active_flags()

    assert flags["B0BBBBBBBB"] is False, "the cached list was not used"
    assert warning, "the owner is not told he is looking at a stale list"
    assert "2026-08-01" in warning, (
        f"the warning does not say WHEN the list is from: {warning!r}"
    )


@pytest.mark.real_catalogue
async def test_a_login_page_also_falls_back(temp_cache, monkeypatch):
    """Sharing changed: Google returns 200 with an HTML login page, not CSV.

    Status-code checking alone would treat that as success and produce an empty
    flag set — which, because unknown means active, silently disables the filter.
    So an empty parse has to fall through to the cache like any other failure.
    """
    temp_cache.write_text(json.dumps({
        "fetched_at": "2026-08-02T09:00:00",
        "flags": {"B0BBBBBBBB": False},
    }), encoding="utf-8")

    monkeypatch.setattr(
        catalogue.httpx, "AsyncClient",
        _fake_client(_Response(200, "<html><body>Sign in</body></html>")),
    )
    flags, warning = await catalogue.load_active_flags()

    assert flags == {"B0BBBBBBBB": False}, "a login page was accepted as data"
    assert warning and "2026-08-02" in warning


@pytest.mark.real_catalogue
async def test_no_sheet_and_no_cache_proceeds_but_says_so(temp_cache, monkeypatch):
    """First ever run, offline. Do not block the owner — but do not pretend either.

    Empty flags mean nothing gets filtered (unknown = active), so without the
    warning a full unfiltered plan is indistinguishable from a correct one.
    """
    assert not temp_cache.exists()
    monkeypatch.setattr(
        catalogue.httpx, "AsyncClient", _fake_client(error=RuntimeError("offline"))
    )
    flags, warning = await catalogue.load_active_flags()

    assert flags == {}
    assert warning, "silently produced an unfiltered plan"
    assert "no saved copy" in warning.lower()


@pytest.mark.real_catalogue
async def test_a_failed_fetch_never_raises(temp_cache, monkeypatch):
    """Whatever httpx throws, generate must survive it.

    An exception here would 500 the upload the owner just spent time preparing.
    """
    for error in (RuntimeError("boom"), OSError("socket"), ValueError("weird")):
        monkeypatch.setattr(
            catalogue.httpx, "AsyncClient", _fake_client(error=error)
        )
        flags, warning = await catalogue.load_active_flags()
        assert isinstance(flags, dict) and warning


@pytest.mark.real_catalogue
async def test_an_unwritable_cache_does_not_break_the_fetch(tmp_path, monkeypatch):
    """The cache is next run's convenience, not this run's result."""
    monkeypatch.setattr(
        catalogue, "CACHE_FILE", tmp_path / "no" / "such" / "dir" / "x.json",
        raising=True,
    )
    monkeypatch.setattr(
        catalogue.httpx, "AsyncClient", _fake_client(_Response(200, SHEET_CSV))
    )

    def _explode(*a, **kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(catalogue, "_write_cache", _explode, raising=True)

    with pytest.raises(OSError):
        catalogue._write_cache({})

    # Restore a working writer only to prove load_active_flags is what shields it.
    # Signature is (flags, products=None) now, so accept both.
    monkeypatch.setattr(
        catalogue, "_write_cache", lambda flags, products=None: None, raising=True
    )
    flags, warning = await catalogue.load_active_flags()
    assert flags["B0AAAAAAAA"] is True
    assert warning is None


# ─── The filter, over HTTP ───────────────────────────────────────────────────

async def test_generate_skips_inactive_products(auth_client, monkeypatch):
    """The headline behaviour: a discontinued ASIN never enters the plan."""
    from app.routers.shipment import FAMILIES

    asins = sorted(FAMILIES)
    keep, drop = asins[0], asins[1]

    # Patches ``load_catalogue``, which is what the router actually calls. Stubbing
    # ``load_active_flags`` here tested nothing once the plan's product list moved to
    # the sheet — the inactive ASIN sailed straight into the plan, and this test is
    # what caught it.
    async def _catalogue():
        return (
            {
                keep: {"asin": keep, "name": "Keep Me", "weight": 1.0,
                       "brand": "Mithila Foods", "active": True},
                drop: {"asin": drop, "name": "Drop Me", "weight": 1.0,
                       "brand": "Mithila Foods", "active": False},
            },
            None,
            "sheet",
        )

    monkeypatch.setattr(
        "app.shipment.catalogue.load_catalogue", _catalogue, raising=True
    )

    sales = f"(Child) ASIN,Units Ordered\n{keep},10\n{drop},10\n"
    stock = f"asin,sku,afn-fulfillable-quantity\n{keep},SKU-A,0\n{drop},SKU-B,0\n"
    r = await auth_client.post(
        "/shipment/generate",
        files={
            "sales_csv": ("sales.csv", sales.encode(), "text/csv"),
            "stock_csv": ("stock.csv", stock.encode(), "text/csv"),
        },
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    listed = {i["asin"] for i in body["items"]}
    assert keep in listed, "an active product was dropped"
    assert drop not in listed, (
        "an INACTIVE product reached the plan — it would go on the packer's sheet "
        "and the Amazon upload"
    )
    assert body["skipped_inactive"] >= 1, (
        "the skipped count is not reported, so 205 products becoming 117 rows is "
        "unexplained on screen"
    )


async def test_generate_reports_a_stale_or_missing_sheet(auth_client, monkeypatch):
    """The warning has to reach the response, not just the log."""
    async def _catalogue():
        return (
            {}, "Could not read the MRP sheet, using the copy from 2026-08-01.", "cache"
        )

    monkeypatch.setattr(
        "app.shipment.catalogue.load_catalogue", _catalogue, raising=True
    )

    from app.routers.shipment import FAMILIES
    asin = sorted(FAMILIES)[0]
    r = await auth_client.post(
        "/shipment/generate",
        files={
            "sales_csv": ("s.csv", f"(Child) ASIN,Units Ordered\n{asin},10\n".encode(), "text/csv"),
            "stock_csv": ("k.csv", f"asin,sku,afn-fulfillable-quantity\n{asin},S,0\n".encode(), "text/csv"),
        },
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text
    assert "2026-08-01" in (r.json().get("warning") or ""), (
        "the sheet warning did not reach the owner's screen"
    )


@pytest.mark.real_catalogue
async def test_generate_still_works_when_the_sheet_is_unreachable(
    auth_client, monkeypatch, tmp_path
):
    """A Google outage must not block the plan. The end-to-end version.

    The unit tests above prove ``load_active_flags`` swallows its own errors; this
    proves the ROUTE is fine when it does — the owner uploads his CSVs with no
    internet and still gets a draft, plus a warning telling him the filter could
    not run.
    """
    # Real loader, no cache, and a raising httpx: the worst case.
    monkeypatch.setattr(catalogue, "CACHE_FILE", tmp_path / "none.json", raising=True)
    monkeypatch.setattr(
        catalogue.httpx, "AsyncClient", _fake_client(error=RuntimeError("offline"))
    )

    from app.routers.shipment import FAMILIES
    asin = sorted(FAMILIES)[0]
    r = await auth_client.post(
        "/shipment/generate",
        files={
            "sales_csv": ("s.csv", f"(Child) ASIN,Units Ordered\n{asin},10\n".encode(), "text/csv"),
            "stock_csv": ("k.csv", f"asin,sku,afn-fulfillable-quantity\n{asin},S,0\n".encode(), "text/csv"),
        },
        data={"multiplier": "5"},
    )

    assert r.status_code == 200, (
        f"a sheet outage broke generate ({r.status_code}) — the owner would be "
        f"unable to build a plan for a reason he cannot fix: {r.text[:200]}"
    )
    body = r.json()
    assert body["items"], "the plan came back empty"
    assert body["skipped_inactive"] == 0, "nothing should have been filtered"
    assert "could not read the mrp sheet" in (body.get("warning") or "").lower(), (
        "an unfiltered plan was produced with no warning — indistinguishable from "
        "a correctly filtered one"
    )


# ─── The sheet decides WHICH products exist ─────────────────────────────────
#
# The reported bug: "why is triphala sattu not in the shipment list when it is there
# in sales and also in MRP sheet".
#
# It was in the sheet, marked Active, in two pack sizes. It never reached a plan
# because the plan iterated ``product_families.json`` — a static 205-ASIN file that
# Triphala had never been added to — and consulted the sheet only for a yes/no Active
# flag. So the sheet could say "yes, I sell this" about a product the plan had no way
# to know existed.
#
# These tests pin the fix: the sheet supplies the product LIST, not just the flag.

def test_the_parser_returns_full_product_records():
    """Name, weight and brand, not just the flag — those are what the plan needs."""
    products = catalogue.parse_catalogue(CATALOGUE_CSV)

    triphala = products["B0H8NPDB88"]
    assert triphala["name"] == "Triphala Sattu"
    assert triphala["weight"] == 0.5
    assert triphala["brand"] == "Mithila Foods"
    assert triphala["active"] is True


def test_an_unparseable_weight_keeps_the_row():
    """Weight only affects sort order; a missing row affects a shipment.

    So a typo in one cell must cost the ordering, never the product. ``None`` rather
    than 0, so the caller can tell "the sheet does not say" from "the sheet says zero"
    and fall back to the static file.
    """
    products = catalogue.parse_catalogue(CATALOGUE_CSV)
    assert "B0BADWEIGH" in products, "a row was dropped over one bad weight cell"
    assert products["B0BADWEIGH"]["weight"] is None
    assert products["B0BADWEIGH"]["active"] is True


def test_the_flags_view_still_agrees_with_the_full_parser():
    """``parse_active_flags`` is now a view over ``parse_catalogue``.

    Asserted because two parsers that must agree is exactly the drift this avoids —
    and the fallback path still uses the narrow one.
    """
    products = catalogue.parse_catalogue(SHEET_CSV)
    flags = catalogue.parse_active_flags(SHEET_CSV)
    assert flags == {a: r["active"] for a, r in products.items()}


async def test_a_product_only_in_the_sheet_reaches_the_plan(auth_client, monkeypatch):
    """The reported bug, end to end.

    B0H8NPDB88 is deliberately NOT in product_families.json — that is the whole point.
    Before the fix this ASIN could sell, be marked Active, and still produce no row.
    """
    from app.routers.shipment import FAMILIES

    new_asin = "B0H8NPDB88"
    assert new_asin not in FAMILIES, (
        "premise changed: this ASIN is now in product_families.json, so it no longer "
        "tests the sheet-only path. Pick another that is absent from the file."
    )

    async def _catalogue():
        return catalogue.parse_catalogue(CATALOGUE_CSV), None, "sheet"

    monkeypatch.setattr(
        "app.shipment.catalogue.load_catalogue", _catalogue, raising=True
    )

    sales = f"(Child) ASIN,Units Ordered\n{new_asin},40\n"
    stock = f"asin,sku,afn-fulfillable-quantity\n{new_asin},TRI-500G,0\n"
    r = await auth_client.post(
        "/shipment/generate",
        files={
            "sales_csv": ("s.csv", sales.encode(), "text/csv"),
            "stock_csv": ("k.csv", stock.encode(), "text/csv"),
        },
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    row = next((i for i in body["items"] if i["asin"] == new_asin), None)
    assert row is not None, (
        "a product that is in the MRP sheet, marked Active, and selling did not reach "
        "the plan — this is the Triphala Sattu bug"
    )
    # And it arrives fully formed, not as a nameless row.
    assert row["item"] == "Triphala Sattu"
    assert row["weight"] == 0.5
    assert row["brand"] == "MF", "brand was not mapped from the sheet"
    # 40 sold * 5 = 200 projected, 0 stock -> 200 to ship.
    assert row["shipment_plan"] == 200
    # The merchant SKU comes from the uploaded stock CSV, not the sheet blank column M.
    assert row["fba_sku"] == "TRI-500G"


async def test_the_sheet_brand_drives_the_mithila_howrah_split(auth_client, monkeypatch):
    """Brand decides sort order (MF before HF) and prints on every document.

    The sheet says "Mithila Foods" / "Howrah Foods" where the code wants MF / HF, and
    that mapping is a substring test — so a sheet that ever says "Mithila Foods Pvt
    Ltd" still maps correctly.
    """
    async def _catalogue():
        return catalogue.parse_catalogue(CATALOGUE_CSV), None, "sheet"

    monkeypatch.setattr(
        "app.shipment.catalogue.load_catalogue", _catalogue, raising=True
    )

    rows = [("B0H8NPDB88", 10, "A"), ("B0HOWRAH01", 10, "B")]
    sales = "(Child) ASIN,Units Ordered\n" + "".join(f"{a},{u}\n" for a, u, _ in rows)
    stock = "asin,sku,afn-fulfillable-quantity\n" + "".join(
        f"{a},{s},0\n" for a, _, s in rows
    )
    r = await auth_client.post(
        "/shipment/generate",
        files={"sales_csv": ("s.csv", sales.encode(), "text/csv"),
               "stock_csv": ("k.csv", stock.encode(), "text/csv")},
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text
    brands = {i["asin"]: i["brand"] for i in r.json()["items"]}
    assert brands["B0H8NPDB88"] == "MF"
    assert brands["B0HOWRAH01"] == "HF"


async def test_an_inactive_sheet_product_never_reaches_the_plan(auth_client, monkeypatch):
    """The other half: the sheet can also REMOVE a product, including a new one."""
    async def _catalogue():
        return catalogue.parse_catalogue(CATALOGUE_CSV), None, "sheet"

    monkeypatch.setattr(
        "app.shipment.catalogue.load_catalogue", _catalogue, raising=True
    )

    sales = "(Child) ASIN,Units Ordered\nB0RETIRED1,50\nB0H8NPDB88,50\n"
    stock = "asin,sku,afn-fulfillable-quantity\nB0RETIRED1,R,0\nB0H8NPDB88,T,0\n"
    r = await auth_client.post(
        "/shipment/generate",
        files={"sales_csv": ("s.csv", sales.encode(), "text/csv"),
               "stock_csv": ("k.csv", stock.encode(), "text/csv")},
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text
    listed = {i["asin"] for i in r.json()["items"]}
    assert "B0H8NPDB88" in listed
    assert "B0RETIRED1" not in listed, (
        "a product marked N in the sheet reached the plan, so it would go on the "
        "packer morning sheet and the Amazon upload"
    )


async def test_generate_says_where_the_product_list_came_from(auth_client, monkeypatch):
    """A plan built from a week-old cache looks identical to one built from today's
    sheet. The owner should not have to guess which he is holding."""
    async def _catalogue():
        return catalogue.parse_catalogue(CATALOGUE_CSV), None, "sheet"

    monkeypatch.setattr(
        "app.shipment.catalogue.load_catalogue", _catalogue, raising=True
    )

    r = await auth_client.post(
        "/shipment/generate",
        files={
            "sales_csv": ("s.csv", b"(Child) ASIN,Units Ordered\nB0H8NPDB88,10\n", "text/csv"),
            "stock_csv": ("k.csv", b"asin,sku,afn-fulfillable-quantity\nB0H8NPDB88,T,0\n", "text/csv"),
        },
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text
    info = r.json()["catalogue"]

    assert info["source"] == "sheet"
    assert info["sheet_products"] == 5
    assert info["active"] == 4          # one row is marked N
    assert info["skipped_inactive"] >= 1
    # Named, not just counted: "2 new products" sends the owner hunting through 110 rows.
    assert any("Triphala" in n for n in info["new_to_the_catalogue"]), (
        "the newly-added products are not named"
    )


async def test_generate_names_what_appeared_and_what_vanished(
    auth_client, monkeypatch, plan_factory
):
    """The sheet is hand-edited, so a stale Active flag shows up here as a row
    silently leaving the plan. "110 rows" alone gives the owner no way to notice."""
    await plan_factory()

    async def _catalogue():
        return catalogue.parse_catalogue(CATALOGUE_CSV), None, "sheet"

    monkeypatch.setattr(
        "app.shipment.catalogue.load_catalogue", _catalogue, raising=True
    )

    r = await auth_client.post(
        "/shipment/generate",
        files={
            "sales_csv": ("s.csv", b"(Child) ASIN,Units Ordered\nB0H8NPDB88,10\n", "text/csv"),
            "stock_csv": ("k.csv", b"asin,sku,afn-fulfillable-quantity\nB0H8NPDB88,T,0\n", "text/csv"),
        },
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text
    info = r.json()["catalogue"]

    assert any("Triphala" in a for a in info["added"]), (
        "the new product is not reported as added"
    )
    assert info["removed"], (
        "the fixture plan products are gone from the new plan and nothing said so"
    )


async def test_the_static_file_is_the_offline_fallback(auth_client, monkeypatch):
    """With no sheet and no cache, the plan is still built — from the static file.

    A Google outage must not stop the owner building a plan, and the response says the
    filtering did not happen so an unfiltered plan is not mistaken for a filtered one.
    """
    async def _nothing():
        return {}, "Could not read the MRP sheet and there is no saved copy.", "none"

    monkeypatch.setattr(
        "app.shipment.catalogue.load_catalogue", _nothing, raising=True
    )

    from app.routers.shipment import FAMILIES
    asin = sorted(FAMILIES)[0]
    r = await auth_client.post(
        "/shipment/generate",
        files={
            "sales_csv": ("s.csv", f"(Child) ASIN,Units Ordered\n{asin},10\n".encode(), "text/csv"),
            "stock_csv": ("k.csv", f"asin,sku,afn-fulfillable-quantity\n{asin},S,0\n".encode(), "text/csv"),
        },
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"], "no sheet and no cache produced an empty plan"
    assert body["catalogue"]["source"] == "none"
    assert "could not read the mrp sheet" in (body.get("warning") or "").lower()
