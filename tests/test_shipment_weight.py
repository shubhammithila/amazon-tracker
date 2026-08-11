"""Shipment weight, calculated rather than typed.

Asked for: *"The weight of the shipment should be auto calculated basis the units x
weight of each item. and summed. whether I am uploading csv or it is going through the
shipments tab."*

Both paths go through **one** function, ``logic.shipment_weight``, so an invoice raised
from a CSV and one raised from a plan cannot disagree about the weight of the same boxes.

Three properties matter more than the arithmetic, and each is a way to put a wrong number
on a GST document:

* **It is NET.** Product only — cartons, filler and tape are not in the catalogue, so the
  weighbridge will read higher. The screen says so and the field stays editable; a number
  that silently disagrees with the truck is worse than no number.
* **A line with no pack size is excluded AND counted.** Treating it as 0 kg would make a
  130 kg shipment quietly report 90, and the total would still look complete.
* **A hand-typed weight is never overwritten.** Re-parsing a file must not discard a
  weighbridge figure.
"""
import pytest

from app.shipment import logic

pytestmark = pytest.mark.regression


# ─── The arithmetic ──────────────────────────────────────────────────────────

def test_units_times_pack_size_summed():
    """The example from the brief: 100 x 1kg + 60 x 500g + 2 x 250g = 130.5 kg."""
    result = logic.shipment_weight([
        {"title": "Chana Sattu 1kg", "quantity": 100, "weight": 1.0},
        {"title": "Chana Sattu 500g", "quantity": 60, "weight": 0.5},
        {"title": "Posta 250g", "quantity": 2, "weight": 0.25},
    ])
    assert result["total"] == 130.5
    assert result["counted"] == 3
    assert result["unknown"] == 0


def test_float_noise_is_rounded_away():
    """3 x 0.15 is 0.44999999999999996 in binary floating point.

    Unrounded, that reaches a GST document as a weight with seventeen decimal places.
    Three places keeps a 50 g pack meaningful and hides the artefact.
    """
    assert logic.line_weight(3, 0.15) == 0.45
    assert logic.line_weight(7, 0.05) == 0.35
    assert logic.line_weight(60, 0.5) == 30.0


def test_the_breakdown_names_each_line():
    """Returned as working, not a bare float.

    A weight the owner cannot check is one he either trusts blindly or ignores — and on a
    document that goes to a transporter, he will ignore it. The breakdown is what makes a
    wrong total traceable to a wrong line.
    """
    result = logic.shipment_weight([
        {"title": "Chana Sattu 1kg", "quantity": 10, "weight": 1.0},
    ])
    line = result["lines"][0]
    assert line["title"] == "Chana Sattu 1kg"
    assert line["units"] == 10
    assert line["pack_weight"] == 1.0
    assert line["pack_label"] == "1 kg"      # the same label the printed sheet uses
    assert line["weight"] == 10.0


def test_units_key_is_accepted_as_well_as_quantity():
    """The invoice lines say `quantity`; packing entries say `units`. One function has to
    read both, or the caller has to remember which — and that is where a silent zero
    comes from."""
    assert logic.shipment_weight([{"units": 4, "weight": 0.5}])["total"] == 2.0
    assert logic.shipment_weight([{"quantity": 4, "weight": 0.5}])["total"] == 2.0


# ─── Missing pack sizes must be visible, not silently zero ───────────────────

def test_a_line_with_no_pack_size_is_excluded_and_counted():
    """The important failure mode.

    Treating an unknown pack size as 0 kg makes the total SHORT while still looking like
    a complete answer. Counting them lets the caller say "2 lines are not in this total",
    which is the difference between a wrong number and a partial one.
    """
    result = logic.shipment_weight([
        {"title": "Known", "quantity": 10, "weight": 1.0},
        {"title": "No weight", "quantity": 99, "weight": 0},
        {"title": "Null weight", "quantity": 5, "weight": None},
    ])
    assert result["total"] == 10.0, "an unknown pack size contributed to the total"
    assert result["counted"] == 1
    assert result["unknown"] == 2
    # And the unknown lines are still listed, so the screen can NAME them.
    unnamed = [l for l in result["lines"] if l["weight"] is None]
    assert {l["title"] for l in unnamed} == {"No weight", "Null weight"}


def test_a_zero_quantity_line_is_skipped_entirely():
    """Not "unknown" — there is simply nothing to weigh. Counting it as a missing pack
    size would produce a warning about a line that is not being shipped."""
    result = logic.shipment_weight([
        {"title": "Nothing shipped", "quantity": 0, "weight": 1.0},
        {"title": "Real", "quantity": 2, "weight": 1.0},
    ])
    assert result["total"] == 2.0
    assert result["counted"] == 1
    assert result["unknown"] == 0


@pytest.mark.parametrize("lines", [
    [], None,
    [{"quantity": "abc", "weight": "xyz"}],
    [{"quantity": 5}],                       # no weight key at all
    ["not a dict"],
    [{"quantity": -5, "weight": 1.0}],       # a negative count
    [{"quantity": 5, "weight": -1.0}],       # a negative pack size
])
def test_junk_produces_zero_rather_than_raising(lines):
    """Every value here arrives from a CSV or from JSON a browser assembled. A 500 on the
    invoice screen because one cell was odd would block the whole invoice."""
    result = logic.shipment_weight(lines)
    assert result["total"] == 0.0
    assert isinstance(result["lines"], list)


# ─── The Shipment tab path ───────────────────────────────────────────────────

ASIN = "B0AAA00001"          # conftest.plan_factory: Chana Sattu 1.0 kg, planned 500
SECOND = "B0AAA00002"        # Chana Sattu 0.5 kg, planned 300
MONDAY = "2026-07-30"


async def _packed_and_verified(auth_client, ops_client, entries, cartons=30):
    await ops_client.post(
        f"/shipment/packing/{MONDAY}",
        json={"entries": entries, "cartons": cartons},
    )
    await ops_client.post(f"/shipment/packing/{MONDAY}/submit")
    r = await auth_client.post(f"/shipment/packing/{MONDAY}/verify")
    assert r.status_code == 200, r.text


async def test_the_invoice_payload_carries_the_calculated_weight(
    auth_client, ops_client, plan_factory
):
    """100 x 1kg + 60 x 500g = 130 kg, straight from the packing entries."""
    await plan_factory()
    await _packed_and_verified(auth_client, ops_client, [
        {"asin": ASIN, "units": 100},
        {"asin": SECOND, "units": 60},
    ])

    body = (await auth_client.post(
        "/shipment/invoice-payload", json={"pack_dates": [MONDAY]}
    )).json()

    assert body["weight"]["total"] == 130.0, body["weight"]
    assert body["weight"]["unknown"] == 0


async def test_each_invoice_line_carries_its_own_weight(
    auth_client, ops_client, plan_factory
):
    """Per line as well as in the total. A total nobody can break down is a total nobody
    can check, and the invoice screen shows the working."""
    await plan_factory()
    await _packed_and_verified(auth_client, ops_client, [{"asin": ASIN, "units": 100}])

    body = (await auth_client.post(
        "/shipment/invoice-payload", json={"pack_dates": [MONDAY]}
    )).json()
    line = next(i for i in body["items"] if i["asin"] == ASIN)

    assert line["weight"] == 1.0, "the pack size is missing from the line"
    assert line["line_weight"] == 100.0, "the line total is missing"


async def test_the_weight_covers_every_selected_day(
    auth_client, ops_client, plan_factory
):
    """Combining days combines their weight too — that is the point of combining."""
    await plan_factory()
    for date, units in ((MONDAY, 100), ("2026-07-31", 40)):
        await ops_client.post(
            f"/shipment/packing/{date}",
            json={"entries": [{"asin": ASIN, "units": units}], "cartons": 30},
        )
        await ops_client.post(f"/shipment/packing/{date}/submit")
        await auth_client.post(f"/shipment/packing/{date}/verify")

    body = (await auth_client.post(
        "/shipment/invoice-payload", json={"pack_dates": [MONDAY, "2026-07-31"]}
    )).json()
    assert body["weight"]["total"] == 140.0, body["weight"]


async def test_a_missing_pack_size_is_warned_about_on_the_payload(
    auth_client, ops_client, plan_factory
):
    """The total is short, and saying so is the whole point.

    Without the warning the owner sees a confident number that is missing a line, and the
    only clue would be the figure looking low.
    """
    await plan_factory(items=[
        {"asin": "B0NOWEIGHT", "item": "Mystery Product", "weight": 0,
         "brand": "MF", "fba_sku": "MF-X", "shipment_plan": 100, "deficit": 100},
    ])
    await _packed_and_verified(
        auth_client, ops_client, [{"asin": "B0NOWEIGHT", "units": 50}]
    )

    body = (await auth_client.post(
        "/shipment/invoice-payload", json={"pack_dates": [MONDAY]}
    )).json()

    assert body["weight"]["unknown"] == 1
    assert body["weight"]["total"] == 0.0
    joined = " ".join(body.get("warnings") or [])
    assert "pack size" in joined.lower(), f"no warning about the missing weight: {joined}"


# ─── The CSV upload path ─────────────────────────────────────────────────────

def test_the_csv_parser_looks_up_pack_sizes():
    """The same catalogue the plan uses, so both paths agree about a product's weight."""
    from app.invoice.parser import get_pack_weight

    # B0CWGXYLT6 is a real 0.5 kg product in product_families.json.
    assert get_pack_weight("", "B0CWGXYLT6") == 0.5
    assert get_pack_weight("", "B0NOTREAL00") == 0.0, "an unknown ASIN must not guess"
    assert get_pack_weight("", "") == 0.0


def test_the_csv_parser_totals_the_shipment_weight():
    """A parsed TSV comes back with the same `weight` block the Shipment tab sends, so
    templates/invoice.html has one code path to fill the field from."""
    from app.invoice.parser import parse_shipment_tsv

    tsv = (
        "Shipment ID\tFBA15TEST999\n"
        "Name\tTest-ISK3\n"
        "Ship to\tISK3\n"
        "Total SKUs\t2\n"
        "Total units\t160\n"
        "\n"
        "Merchant SKU\tTitle\tASIN\tFNSKU\tShipped\n"
        "MF-CH-1KG\tChana Sattu 1kg\tB0CWGY2LCP\tX001\t100\n"
        "MF-CH-500G\tChana Sattu 500g\tB0CWGXYLT6\tX002\t60\n"
    )
    parsed = parse_shipment_tsv(tsv)

    assert "weight" in parsed, "the CSV path sends no weight at all"
    # B0CWGY2LCP is 1.5 kg and B0CWGXYLT6 is 0.5 kg in the catalogue.
    expected = 100 * 1.5 + 60 * 0.5
    assert parsed["weight"]["total"] == expected, parsed["weight"]
    assert parsed["weight"]["unknown"] == 0

    line = parsed["items"][0]
    assert line["weight"] == 1.5
    assert line["line_weight"] == 150.0


def test_an_unknown_asin_in_a_csv_is_counted_not_guessed():
    from app.invoice.parser import parse_shipment_tsv

    tsv = (
        "Shipment ID\tFBA15TEST998\n"
        "Ship to\tISK3\n"
        "\n"
        "Merchant SKU\tTitle\tASIN\tFNSKU\tShipped\n"
        "NEW-SKU\tSomething New\tB0BRANDNEW\tX003\t20\n"
    )
    parsed = parse_shipment_tsv(tsv)
    assert parsed["weight"]["total"] == 0.0
    assert parsed["weight"]["unknown"] == 1


# ─── The invoice screen ──────────────────────────────────────────────────────

def _invoice_source() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parent.parent / "templates" / "invoice.html"
    ).read_text(encoding="utf-8")


def test_the_weight_field_is_auto_filled_from_both_paths():
    """One filler, called from the CSV handler and from the Shipment handoff. Two
    separate implementations would drift, and the whole point is that the two paths
    agree."""
    source = _invoice_source()
    assert "function applyShipmentWeight" in source, "no auto-fill at all"
    assert source.count("applyShipmentWeight(data.weight)") == 2, (
        "the weight is filled on only one of the two paths"
    )


def test_a_hand_typed_weight_is_never_overwritten():
    """A weighbridge figure must survive a re-parse.

    Without the latch, re-uploading the file silently replaces the real weight with the
    calculated one, and the owner has no reason to look again.
    """
    source = _invoice_source()
    assert "weightTouched" in source, "nothing tracks that the owner typed a weight"
    assert "if (!weightTouched)" in source, (
        "the auto-fill is not guarded, so it overwrites a typed weight"
    )


def test_a_re_parse_still_says_which_number_is_in_the_field():
    """Found in a browser, with the latch already working.

    The typed 412.5 correctly survived a re-parse — but the tag was blanked at the top
    of the function and only re-set on the auto-fill branch, so the field showed 412.5
    under a note reading "Net product weight ... 390 kg". Two numbers on a GST document's
    screen and a label on neither is how the wrong one gets trusted.
    """
    source = _invoice_source()
    start = source.index("function applyShipmentWeight")
    body = source[start:source.index("async function uploadFile")]

    assert "entered by hand" in body, (
        "after a re-parse nothing says the field holds the owner's own figure"
    )
    # And the note must not assert its calculated total as though it were the field.
    assert "weightTouched ?" in body or "weightTouched?" in body, (
        "the note reads the same whether or not the owner overrode the weight, so it "
        "claims a total that is not the number in the field"
    )


def test_the_field_says_the_weight_is_net():
    """Cartons and packing are not in the catalogue, so the truck reads higher.

    Presenting this as the shipment weight without saying "net" puts a number on a GST
    document that disagrees with the weighbridge for a reason nobody can see.
    """
    source = _invoice_source()
    assert "net" in source.lower(), "the field never says the weight is net"
    assert "Excludes cartons" in source, (
        "nothing tells the owner what the calculation leaves out"
    )


def test_the_field_stays_editable():
    """Auto-filled, not locked. A real gross weight has to be enterable."""
    import re

    source = _invoice_source()
    field = re.search(r'<input[^>]*id="f-weight"[^>]*>', source)
    assert field, "the weight field is gone"
    assert "readonly" not in field.group(0), "the weight field is read-only"
    assert "disabled" not in field.group(0), "the weight field is disabled"


def test_the_breakdown_is_built_with_textcontent():
    """Product titles come from an uploaded file, and the breakdown renders them.

    innerHTML with a title containing markup is the same class of bug as the template
    escaping elsewhere — so every cell is set with textContent.
    """
    source = _invoice_source()
    start = source.index("function applyShipmentWeight")
    body = source[start:source.index("async function uploadFile")]
    assert "td.textContent = text" in body, (
        "the working table interpolates titles instead of using textContent"
    )
    assert ".innerHTML" not in body, (
        "the weight breakdown uses innerHTML with file-derived titles"
    )
