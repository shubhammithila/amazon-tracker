"""Unit tests for app/shipment/logic.py — the shipment rules in one place.

These cover three requested behaviours:

  * "the weekly shipment plan created should be rounded off to the nearest 10"
  * "sorted product wise, then weight wise ... on the dashboard and in the
    downloads as well"
  * "when the packing is less say 20 cartons and 400 units, we do not create a
    shipment, instead we combine it with next day packing"

The rounding tests matter more than they look. The obvious implementation,
`round(n / 10) * 10`, uses banker's rounding and turns 25 into 20 and 5 into 0.
Both would read as a bug on the dashboard, and the 5 case silently drops a line
item entirely.
"""
import json
from pathlib import Path

import pytest

from app.shipment import logic

pytestmark = pytest.mark.regression


# ─── Rounding to the nearest 10 ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, 0),        # nothing to send stays nothing, not 10
        (1, 10),
        (4, 10),
        (5, 10),       # built-in round() gives 0 here
        (9, 10),
        (10, 10),
        (14, 10),
        (15, 20),      # built-in round() gives 20 too, but via a different path
        (24, 20),
        (25, 30),      # built-in round() gives 20 — the headline case
        (26, 30),
        (35, 40),
        (100, 100),
        (437, 440),
        (1234, 1230),
        (1235, 1240),
    ],
)
def test_round_to_10(raw, expected):
    assert logic.round_to_10(raw) == expected


@pytest.mark.parametrize("raw", [-1, -10, -437])
def test_round_to_10_never_returns_negative(raw):
    """A negative deficit means 'already overstocked', which is 0 to ship."""
    assert logic.round_to_10(raw) == 0


@pytest.mark.parametrize("raw", [None, "", "abc", object()])
def test_round_to_10_survives_junk(raw):
    """Values arrive from CSVs; unparseable input must not raise mid-upload."""
    assert logic.round_to_10(raw) == 0


@pytest.mark.parametrize("raw,expected", [(24.4, 20), (24.5, 20), (25.0, 30), ("25", 30)])
def test_round_to_10_handles_floats_and_strings(raw, expected):
    assert logic.round_to_10(raw) == expected


def test_round_to_10_differs_from_naive_round_where_it_matters():
    """Pin the bug this function exists to avoid, so nobody 'simplifies' it."""
    naive = [round(n / 10) * 10 for n in (5, 25)]
    correct = [logic.round_to_10(n) for n in (5, 25)]
    assert naive == [0, 20]
    assert correct == [10, 30]


def test_round_to_step_rejects_a_nonsense_step():
    with pytest.raises(ValueError):
        logic.round_to_step(50, step=0)


def test_round_to_step_supports_other_multiples():
    """The seam for a future 'round to carton multiple' rule."""
    assert logic.round_to_step(437, step=12) == 432   # nearest multiple of 12
    assert logic.round_to_step(6, step=12) == 12      # floored to one carton
    assert logic.round_to_step(0, step=12) == 0


def test_a_real_need_is_never_rounded_away():
    """Decided 2026-07-30: a 1-9 unit need must not vanish from the shipment.

    Strict nearest-10 would send 0 for a need of 4, so a slow-moving SKU goes out
    of stock at Amazon while the plan claims nothing was required.
    """
    for need in range(1, 10):
        assert logic.round_to_10(need) == 10, f"need of {need} was rounded away"
    assert logic.round_to_10(0) == 0, "zero need must stay zero"


# ─── Product-then-weight ordering ────────────────────────────────────────────

def test_sort_orders_by_product_then_weight():
    items = [
        {"item": "Sattu", "weight": 2.0, "asin": "B03"},
        {"item": "Chana", "weight": 5.0, "asin": "B01"},
        {"item": "Sattu", "weight": 0.5, "asin": "B02"},
        {"item": "Chana", "weight": 1.0, "asin": "B04"},
    ]
    assert [(i["item"], i["weight"]) for i in logic.sort_items(items)] == [
        ("Chana", 1.0), ("Chana", 5.0), ("Sattu", 0.5), ("Sattu", 2.0),
    ]


def test_sort_is_case_insensitive_on_product_name():
    """Otherwise 'Jau Sattu' and 'sattu' land in unrelated parts of the list."""
    items = [
        {"item": "sattu", "weight": 1.0, "asin": "B01"},
        {"item": "Chana", "weight": 1.0, "asin": "B02"},
        {"item": "JAU", "weight": 1.0, "asin": "B03"},
    ]
    assert [i["item"] for i in logic.sort_items(items)] == ["Chana", "JAU", "sattu"]


def test_sort_prefers_sort_product_when_present():
    """Persisted rows carry a normalised sort_product; it must win over `item`."""
    items = [
        {"sort_product": "aaa", "item": "zzz visible name", "weight": 1.0, "asin": "B01"},
        {"sort_product": "zzz", "item": "aaa visible name", "weight": 1.0, "asin": "B02"},
    ]
    assert [i["asin"] for i in logic.sort_items(items)] == ["B01", "B02"]


def test_sort_breaks_weight_ties_by_asin_for_determinism():
    items = [
        {"item": "Sattu", "weight": 1.0, "asin": "B0ZZZZZZZZ"},
        {"item": "Sattu", "weight": 1.0, "asin": "B0AAAAAAAA"},
    ]
    assert [i["asin"] for i in logic.sort_items(items)] == ["B0AAAAAAAA", "B0ZZZZZZZZ"]


@pytest.mark.parametrize("bad_weight", [None, "", "not-a-number"])
def test_sort_tolerates_missing_weight(bad_weight):
    items = [
        {"item": "Sattu", "weight": 1.0, "asin": "B01"},
        {"item": "Sattu", "weight": bad_weight, "asin": "B02"},
    ]
    # Unparseable weight sorts as 0, i.e. first — but must not raise.
    assert [i["asin"] for i in logic.sort_items(items)] == ["B02", "B01"]


def test_sort_works_on_objects_not_just_dicts():
    class Row:
        def __init__(self, item, weight, asin):
            self.item, self.weight, self.asin = item, weight, asin
            self.sort_product = None

    rows = [Row("Sattu", 2.0, "B01"), Row("Chana", 1.0, "B02")]
    assert [r.asin for r in logic.sort_items(rows)] == ["B02", "B01"]


def test_sort_is_stable_and_total_over_the_real_catalogue():
    """Sorting the live 205-ASIN catalogue must be deterministic and idempotent."""
    families = json.loads(
        (Path(__file__).resolve().parent.parent / "app" / "invoice" / "product_families.json")
        .read_text(encoding="utf-8")
    )
    items = [
        {"item": info["parent_product"], "weight": info.get("weight"), "asin": asin}
        for asin, info in families.items()
    ]
    once = logic.sort_items(items)
    assert logic.sort_items(once) == once, "sort is not idempotent"
    assert len(once) == len(items), "sort dropped or duplicated rows"

    # Within a product group, weights must be non-decreasing.
    for i in range(1, len(once)):
        prev, cur = once[i - 1], once[i]
        if prev["item"].casefold() == cur["item"].casefold():
            assert float(prev["weight"]) <= float(cur["weight"]), (
                f"weights out of order within {cur['item']}"
            )


# ─── Remaining ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "planned,packed,expected",
    [(100, 0, 100), (100, 40, 60), (100, 100, 0), (100, 140, 0), (0, 0, 0), (0, 50, 0)],
)
def test_remaining_for(planned, packed, expected):
    assert logic.remaining_for(planned, packed) == expected


def test_remaining_never_negative_when_overpacked():
    """The floor sheet should say 'done', not '-40 to pack'."""
    assert logic.remaining_for(100, 140) == 0


# ─── The hold rule (requirement 9) ───────────────────────────────────────────

@pytest.mark.parametrize(
    "cartons,units,expected,why",
    [
        (20, 400, True,  "the example given: short on both counts"),
        (10, 120, True,  "clearly too small"),
        (24, 499, True,  "just under both thresholds"),
        (25, 400, False, "carton threshold met exactly"),
        (20, 500, False, "unit threshold met exactly"),
        (30, 300, False, "heavy 5kg bags: few units but plenty of cartons"),
        (15, 900, False, "small pouches: few cartons but plenty of units"),
        (100, 5000, False, "a full shipment"),
        (0, 0, False,    "an empty day is empty, not held"),
    ],
)
def test_is_held_uses_and_not_or(cartons, units, expected, why):
    assert logic.is_held(cartons, units, 25, 500) is expected, why


def test_is_held_respects_custom_thresholds():
    assert logic.is_held(20, 400, min_cartons=10, min_units=100) is False
    assert logic.is_held(20, 400, min_cartons=50, min_units=1000) is True


def test_hold_reason_names_the_actual_numbers():
    reason = logic.hold_reason(20, 400, 25, 500)
    for fragment in ("20", "400", "25", "500"):
        assert fragment in reason
    assert "combine" in reason.lower()


# ─── packed vs shippable (the crux of requirement 9) ─────────────────────────

def _day(status, entries):
    return {"status": status, "entries": entries}


def test_held_units_count_as_packed_but_not_as_shippable():
    """The distinction that stops the warehouse double-packing held stock."""
    days = [
        _day(logic.STATUS_VERIFIED, [{"asin": "B01", "units": 100, "cartons": 30}]),
        _day(logic.STATUS_HELD, [{"asin": "B01", "units": 40, "cartons": 5}]),
    ]
    assert logic.packed_units_by_asin(days) == {"B01": 140}, "held units must count as packed"
    assert logic.shippable_units_by_asin(days) == {"B01": 100}, "held units must not ship"


def test_open_days_are_packed_but_not_shippable():
    """Ops has not finished the day, so it must not go into a shipment yet."""
    days = [_day(logic.STATUS_OPEN, [{"asin": "B01", "units": 60, "cartons": 4}])]
    assert logic.packed_units_by_asin(days) == {"B01": 60}
    assert logic.shippable_units_by_asin(days) == {}


@pytest.mark.parametrize(
    "status,shippable",
    [
        (logic.STATUS_OPEN, False),
        (logic.STATUS_HELD, False),
        (logic.STATUS_SUBMITTED, True),
        (logic.STATUS_VERIFIED, True),
        (logic.STATUS_SHIPPED, True),
    ],
)
def test_which_statuses_are_shippable(status, shippable):
    days = [_day(status, [{"asin": "B01", "units": 10, "cartons": 1}])]
    assert bool(logic.shippable_units_by_asin(days)) is shippable


def test_releasing_a_held_day_makes_its_units_shippable():
    """Force-ship: flipping held -> submitted is all it takes."""
    day = _day(logic.STATUS_HELD, [{"asin": "B01", "units": 40, "cartons": 5}])
    assert logic.shippable_units_by_asin([day]) == {}
    day["status"] = logic.STATUS_SUBMITTED
    assert logic.shippable_units_by_asin([day]) == {"B01": 40}


def test_two_small_days_combine_into_one_shipment():
    """Requirement 9 end to end: neither day ships alone, together they do."""
    monday = [{"asin": "B01", "units": 400, "cartons": 20}]
    tuesday = [{"asin": "B01", "units": 300, "cartons": 18}]

    assert logic.is_held(20, 400, 25, 500) is True
    assert logic.is_held(18, 300, 25, 500) is True

    # Combined, the same packing clears the thresholds.
    combined_cartons = logic.packed_cartons(monday + tuesday)
    combined_units = logic.packed_units(monday + tuesday)
    assert (combined_cartons, combined_units) == (38, 700)
    assert logic.is_held(combined_cartons, combined_units, 25, 500) is False

    # And once released, the aggregation picks up both days with no extra state.
    days = [_day(logic.STATUS_SUBMITTED, monday), _day(logic.STATUS_SUBMITTED, tuesday)]
    assert logic.shippable_units_by_asin(days) == {"B01": 700}


def test_units_aggregate_per_asin_across_days():
    days = [
        _day(logic.STATUS_VERIFIED, [{"asin": "B01", "units": 10}, {"asin": "B02", "units": 5}]),
        _day(logic.STATUS_VERIFIED, [{"asin": "B01", "units": 7}]),
    ]
    assert logic.shippable_units_by_asin(days) == {"B01": 17, "B02": 5}


def test_entries_without_an_asin_are_ignored():
    days = [_day(logic.STATUS_VERIFIED, [{"asin": "", "units": 99}, {"asin": "B01", "units": 1}])]
    assert logic.shippable_units_by_asin(days) == {"B01": 1}


def test_held_totals_summarises_parked_stock():
    """Surfaced in the UI so a held day cannot become forgotten inventory."""
    days = [
        _day(logic.STATUS_HELD, [{"asin": "B01", "units": 400, "cartons": 20}]),
        _day(logic.STATUS_HELD, [{"asin": "B02", "units": 100, "cartons": 6}]),
        _day(logic.STATUS_VERIFIED, [{"asin": "B03", "units": 900, "cartons": 40}]),
    ]
    assert logic.held_totals(days) == {"days": 2, "units": 500, "cartons": 26}


def test_held_totals_is_zero_when_nothing_is_parked():
    days = [_day(logic.STATUS_VERIFIED, [{"asin": "B01", "units": 10, "cartons": 1}])]
    assert logic.held_totals(days) == {"days": 0, "units": 0, "cartons": 0}


def test_only_verified_days_are_invoiceable():
    """The GST number is gated on the owner's verification, nothing weaker."""
    assert logic.INVOICEABLE_STATUSES == {logic.STATUS_VERIFIED}
    assert logic.STATUS_SUBMITTED not in logic.INVOICEABLE_STATUSES
    assert logic.STATUS_HELD not in logic.INVOICEABLE_STATUSES


def test_packed_helpers_tolerate_missing_fields():
    entries = [{"asin": "B01"}, {"asin": "B02", "units": None, "cartons": None}]
    assert logic.packed_units(entries) == 0
    assert logic.packed_cartons(entries) == 0
