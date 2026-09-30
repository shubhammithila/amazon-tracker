"""The Portfolio column picker: hide and reorder columns, saved per login.

Asked for as "make every column except the Sales, ad spend, units and weights… hiddenable", then
"also make the columns slidable — which one first, which later, or last". See
docs/superpowers/specs/2026-09-30-portfolio-column-picker-design.md.
"""
import pytest

pytestmark = pytest.mark.regression


def test_users_has_a_nullable_preferences_column():
    """NULL means "never chosen", deliberately distinct from "chose the default": it is what lets a
    later change of default reach everyone who never customised."""
    from app.models import User

    column = User.__table__.c.preferences_json
    assert column.nullable is True
    assert column.server_default is None


from app.portfolio import columns as C

ALL = ["verdict", "sales", "ad_spend", "tacos", "acos", "net_pct",
       "units", "weight_kg", "returns_pct", "rating", "decision"]


def test_the_vocabulary_is_exactly_the_agreed_twelve_columns():
    assert [c["id"] for c in C.COLUMNS] == ["product"] + ALL
    locked = {c["id"] for c in C.COLUMNS if c["locked"]}
    assert locked == {"product", "sales", "ad_spend", "units", "weight_kg"}


def test_the_default_is_todays_screen():
    assert C.DEFAULT_LAYOUT == {"order": ALL, "hidden": ["returns_pct"]}


@pytest.mark.parametrize("junk", [None, "", "[]", 42, [], {"order": "x"}, {"order": [1, 2]}])
def test_anything_malformed_becomes_the_default(junk):
    assert C.normalise_column_layout(junk) == C.DEFAULT_LAYOUT


def test_a_valid_layout_survives_untouched():
    layout = {"order": list(reversed(ALL)), "hidden": ["acos", "rating"]}
    assert C.normalise_column_layout(layout) == layout


def test_unknown_ids_and_duplicates_are_dropped():
    out = C.normalise_column_layout(
        {"order": ["gone", "sales", "sales"] + ALL, "hidden": ["gone", "acos", "acos"]}
    )
    # First occurrence kept: `sales` is the first KNOWN id in the input, so it leads.
    assert out["order"] == ["sales"] + [c for c in ALL if c != "sales"]
    assert out["hidden"] == ["acos"]


def test_product_is_never_stored_in_the_order():
    out = C.normalise_column_layout({"order": ["product"] + ALL, "hidden": []})
    assert "product" not in out["order"]


def test_a_protected_column_cannot_be_hidden():
    out = C.normalise_column_layout(
        {"order": ALL, "hidden": ["sales", "ad_spend", "units", "weight_kg", "product", "acos"]}
    )
    assert out["hidden"] == ["acos"]


def test_a_NEW_column_appears_at_its_default_position_for_an_existing_user():
    """**The rule most likely to be got wrong.** A column added next month is absent from every
    saved layout; dropping it would make it silently never show for anyone who customised. It goes
    after the nearest default-order predecessor the user still has."""
    saved = [c for c in reversed(ALL) if c != "tacos"]      # user reversed everything; no tacos
    out = C.normalise_column_layout({"order": saved, "hidden": []})
    # In the default order tacos follows ad_spend, so it lands right after ad_spend here.
    assert out["order"].index("tacos") == out["order"].index("ad_spend") + 1
    assert sorted(out["order"]) == sorted(ALL)


def test_a_missing_FIRST_column_goes_to_the_front():
    saved = [c for c in ALL if c != "verdict"]
    out = C.normalise_column_layout({"order": list(reversed(saved)), "hidden": []})
    assert out["order"][0] == "verdict"


def test_the_normaliser_is_idempotent():
    layout = {"order": ["rating", "gone", "sales"], "hidden": ["sales", "decision"]}
    once = C.normalise_column_layout(layout)
    assert C.normalise_column_layout(once) == once
