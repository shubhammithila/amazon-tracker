"""Pure rules for the Projections tab. No database, no network.

The parent-grouping unit is the MRP sheet's own product `name`, UNMERGED — never
`product_families.json`, and never Portfolio's `family_label()`. That function collapses flavour
variants (Cheese & Cream Chana, Nimbu Pudina Chana, Peri Peri Chana...) into one shared display
name for a rollup; `product_families.json` itself keeps them as separate parents, because
different flavours are different recipes and different purchase decisions. Folding them here
would be wrong for exactly the reason `family_label` is right for Portfolio's screen.
"""
import pytest

from app.projections import logic


# ─── normalize_name ───────────────────────────────────────────────────────────


def test_normalize_name_ignores_case_space_and_hyphen():
    """`projection_defaults.json` spells "Govind Bhog Rice" with a space; other data sources
    may not agree on spacing or case, so matching must be forgiving about exactly these three
    things and nothing else — a genuine spelling difference (Gobindobhog vs Govind Bhog) must
    still NOT match, or two different products merge into one row."""
    assert logic.normalize_name("Govind Bhog Rice") == logic.normalize_name("govind-bhog rice")
    assert logic.normalize_name("Chana Sattu") == logic.normalize_name("CHANA SATTU")
    assert logic.normalize_name("Govind Bhog Rice") != logic.normalize_name("Gobindobhog Rice")


def test_normalize_name_handles_empty_and_none():
    assert logic.normalize_name("") == ""
    assert logic.normalize_name(None) == ""


# ─── match_defaults ───────────────────────────────────────────────────────────


def test_match_defaults_finds_a_normalized_match():
    defaults = {"Govind Bhog Rice": {"purchase_rate": 75.0}}
    assert logic.match_defaults("govind-bhog rice", defaults) == {"purchase_rate": 75.0}


def test_match_defaults_returns_none_for_a_genuinely_new_parent():
    """Triphala Sattu-shaped: active in the sheet, no entry anywhere in
    projection_defaults.json under any spelling. The caller (build_parent_config) is what
    turns this None into Global Defaults + needs_review — this function must not guess."""
    defaults = {"Govind Bhog Rice": {"purchase_rate": 75.0}}
    assert logic.match_defaults("Triphala Sattu", defaults) is None


# ─── group_active_by_name ─────────────────────────────────────────────────────


def _sheet_row(asin, name, weight, active=True, brand="Mithila Foods"):
    return {"asin": asin, "name": name, "weight": weight, "brand": brand, "active": active}


def test_group_active_by_name_excludes_inactive_asins():
    catalogue = {
        "B0ACTIVE01": _sheet_row("B0ACTIVE01", "Chana Sattu", 0.5),
        "B0DEAD0001": _sheet_row("B0DEAD0001", "Kasundi", 0.3, active=False),
    }
    groups = logic.group_active_by_name(catalogue)
    assert list(groups) == ["Chana Sattu"]


def test_group_active_by_name_keeps_flavours_as_separate_parents():
    """The measured case: Cheese & Cream Roasted Chana and Peri Peri Roasted Chana are two
    ASINs of the SAME flavour-suffix pattern but must stay two separate groups — this function
    does no flavour merging at all, by design."""
    catalogue = {
        "B0CHEESE01": _sheet_row("B0CHEESE01", "Cheese & Cream Roasted Chana", 0.2),
        "B0PERI0001": _sheet_row("B0PERI0001", "Peri Peri Roasted Chana", 0.2),
    }
    groups = logic.group_active_by_name(catalogue)
    assert set(groups) == {"Cheese & Cream Roasted Chana", "Peri Peri Roasted Chana"}


def test_group_active_by_name_collects_every_pack_size_asin():
    catalogue = {
        "B0SIZE0001": _sheet_row("B0SIZE0001", "Chana Sattu", 0.5),
        "B0SIZE0002": _sheet_row("B0SIZE0002", "Chana Sattu", 1.0),
        "B0SIZE0003": _sheet_row("B0SIZE0003", "Chana Sattu", 2.0),
    }
    groups = logic.group_active_by_name(catalogue)
    assert sorted(groups["Chana Sattu"]["asins"]) == ["B0SIZE0001", "B0SIZE0002", "B0SIZE0003"]
    assert groups["Chana Sattu"]["weights"] == {
        "B0SIZE0001": 0.5, "B0SIZE0002": 1.0, "B0SIZE0003": 2.0,
    }


def test_group_active_by_name_is_empty_for_an_empty_catalogue():
    assert logic.group_active_by_name({}) == {}


# ─── build_parent_config ──────────────────────────────────────────────────────

GLOBAL_DEFAULTS = {
    "growth_rate": 0.3, "seasonal_impact": 1.0, "supplier_to_wh": 5, "packing": 2,
    "wh_to_ixd": 10, "ixd_to_fba": 5, "wh_buffer_days": 10.0,
}


def test_build_parent_config_uses_matched_defaults():
    defaults = {"Chana Sattu": {"purchase_rate": 120.0, "supplier_to_wh": 5, "packing": 2,
                                 "wh_to_ixd": 10, "ixd_to_fba": 5, "wh_buffer_days": 10.0,
                                 "seasonal_impact": 1.5, "growth_rate": 0.3, "brand": "Mithila Foods"}}
    config = logic.build_parent_config("Chana Sattu", {}, defaults, GLOBAL_DEFAULTS)
    assert config["purchase_rate"] == 120.0
    assert config["needs_review"] is False


def test_build_parent_config_flags_needs_review_with_global_defaults():
    """The Triphala Sattu case: no match anywhere, so it gets Global Defaults and is flagged —
    never hidden, per the owner's explicit decision that a live product must never be invisible
    because a static file has not heard of it."""
    config = logic.build_parent_config("Triphala Sattu", {}, {}, GLOBAL_DEFAULTS)
    assert config["needs_review"] is True
    assert config["purchase_rate"] == 0
    assert config["seasonal_impact"] == GLOBAL_DEFAULTS["seasonal_impact"]
    assert config["growth_rate"] == GLOBAL_DEFAULTS["growth_rate"]
    assert config["wh_buffer_days"] == GLOBAL_DEFAULTS["wh_buffer_days"]


# ─── calculate_projections: the blended rate must actually be used ────────────


def test_calculate_projections_forecasts_from_the_blended_rate_for_a_sheet_row():
    """**The bug this test exists to catch: the weekly blend must not be silently discarded.**

    A `sheet`-sourced row already carries `daily_rate` from `blended_daily_rate` — computed once
    a week from real units_ordered data. Re-deriving `daily_rate` from `last_month_sale *
    seasonal * (1 + growth)` here, as the pre-existing formula did unconditionally, would make
    the entire 7d/30d blend (the whole reason this feature exists) invisible on screen: the
    number the weekly job computes and the number the forecast displays would disagree.
    """
    products = [{
        "parent_product": "Chana Sattu", "sales_source": "sheet",
        "daily_rate": 14.0,               # the blended rate, as Task 6's refresh job would store it
        "last_month_sale": 300.0,          # 30-day total kg — NOT what the forecast should use
        "seasonal_impact": 2.0, "growth_rate": 5.0,   # deliberately extreme, to prove they are IGNORED
        "purchase_rate": 0, "supplier_to_wh": 0, "packing": 0, "wh_to_ixd": 0, "ixd_to_fba": 0,
        "wh_buffer_days": 0, "current_fba_stock": 0, "current_wh_stock": 0,
    }]
    result = logic.calculate_projections(products)[0]
    assert result["daily_rate"] == 14.0, "the blended rate was overwritten"
    assert result["monthly_forecast"] == 420.0, "monthly_forecast must be daily_rate * 30"


def test_calculate_projections_falls_back_to_last_month_sale_for_a_manual_row():
    """A `manual` row never went through the weekly job and has no blended rate to read — it
    must keep using the original seasonal/growth formula, unchanged from before this feature."""
    products = [{
        "parent_product": "Chana Sattu", "sales_source": "manual",
        "daily_rate": 0, "last_month_sale": 300.0,
        "seasonal_impact": 1.5, "growth_rate": 0.2,
        "purchase_rate": 0, "supplier_to_wh": 0, "packing": 0, "wh_to_ixd": 0, "ixd_to_fba": 0,
        "wh_buffer_days": 0, "current_fba_stock": 0, "current_wh_stock": 0,
    }]
    result = logic.calculate_projections(products)[0]
    # 300 * 1.5 * 1.2 = 540
    assert result["monthly_forecast"] == 540.0
    assert result["daily_rate"] == pytest.approx(18.0)


def test_calculate_projections_falls_back_for_a_sheet_row_never_yet_refreshed():
    """A brand-new parent (`build_current_rows` just created it) is `sales_source="sheet"` but
    has never been through the weekly job, so `daily_rate` is 0/None — it must fall back to the
    same formula a manual row uses, not silently forecast zero."""
    products = [{
        "parent_product": "Triphala Sattu", "sales_source": "sheet",
        "daily_rate": 0, "last_month_sale": 30.0,
        "seasonal_impact": 1.0, "growth_rate": 0.3,
        "purchase_rate": 0, "supplier_to_wh": 0, "packing": 0, "wh_to_ixd": 0, "ixd_to_fba": 0,
        "wh_buffer_days": 0, "current_fba_stock": 0, "current_wh_stock": 0,
    }]
    result = logic.calculate_projections(products)[0]
    assert result["monthly_forecast"] == pytest.approx(39.0)  # 30 * 1.0 * 1.3
