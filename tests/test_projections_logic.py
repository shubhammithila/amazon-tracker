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


# ─── sales_kg_by_parent ────────────────────────────────────────────────────────


def _econ_row(asin, units_ordered, net_units=None):
    """One economics_snapshot row in Amazon's own nested shape — the same shape
    `app.portfolio.economics.fetch_economics` returns and `app.portfolio.repository.load_snapshot`
    reconstructs from storage, so this fixture is honest about what the real caller passes."""
    return {
        "childAsin": asin,
        "sales": {
            "unitsOrdered": units_ordered,
            "netUnitsSold": net_units if net_units is not None else units_ordered,
        },
    }


def test_sales_kg_by_parent_sums_units_ordered_times_weight():
    groups = {"Chana Sattu": {"asins": ["B01"], "weights": {"B01": 0.5}, "brand": ""}}
    rows = [_econ_row("B01", units_ordered=100)]
    result = logic.sales_kg_by_parent(rows, groups)
    assert result == {"Chana Sattu": 50.0}


def test_sales_kg_by_parent_sums_multiple_pack_sizes_into_one_parent():
    groups = {"Chana Sattu": {"asins": ["B01", "B02"], "weights": {"B01": 0.5, "B02": 1.0},
                              "brand": ""}}
    rows = [_econ_row("B01", units_ordered=100), _econ_row("B02", units_ordered=40)]
    result = logic.sales_kg_by_parent(rows, groups)
    assert result == {"Chana Sattu": 90.0}  # 100*0.5 + 40*1.0


def test_sales_kg_by_parent_ignores_net_units_and_never_goes_negative():
    """Measured cause: net_units went negative (-1) on 2 ASINs in a real refund-heavy 7-day
    window. units_ordered is the demand signal; a returns problem is not lower demand, and a
    negative daily rate would produce a negative purchase quantity."""
    groups = {"Chana Sattu": {"asins": ["B01"], "weights": {"B01": 1.0}, "brand": ""}}
    rows = [_econ_row("B01", units_ordered=5, net_units=-1)]
    result = logic.sales_kg_by_parent(rows, groups)
    assert result == {"Chana Sattu": 5.0}, "net_units leaked into the sales figure"


def test_sales_kg_by_parent_ignores_an_asin_outside_the_active_groups():
    """A row for a discontinued or unknown ASIN must not silently create a phantom parent."""
    groups = {"Chana Sattu": {"asins": ["B01"], "weights": {"B01": 1.0}, "brand": ""}}
    rows = [_econ_row("B01", units_ordered=10), _econ_row("B99UNKNOWN", units_ordered=999)]
    result = logic.sales_kg_by_parent(rows, groups)
    assert result == {"Chana Sattu": 10.0}


def test_sales_kg_by_parent_is_empty_for_no_rows():
    groups = {"Chana Sattu": {"asins": ["B01"], "weights": {"B01": 1.0}, "brand": ""}}
    assert logic.sales_kg_by_parent([], groups) == {}


# ─── blended_daily_rate ────────────────────────────────────────────────────────


def test_blended_daily_rate_weights_seven_and_thirty_day():
    """0.4 * (7d/7) + 0.6 * (30d/30), the default weight — verified against real account
    figures: Bangla Moori-shaped (7d rate above 30d) and Miniket-shaped (7d rate below) both
    move in the direction the blend implies, not toward zero."""
    rate, diverged = logic.blended_daily_rate(kg_30d=300.0, kg_7d=70.0, weight=0.4)
    # 30d/day = 10.0, 7d/day = 10.0 -> exact agreement, no divergence
    assert rate == 10.0
    assert diverged is False


def test_blended_daily_rate_responds_to_a_spike():
    # 30d/day = 10.0, 7d/day = 20.0 (2x) -> blended = 0.4*20 + 0.6*10 = 14.0
    rate, diverged = logic.blended_daily_rate(kg_30d=300.0, kg_7d=140.0, weight=0.4)
    assert rate == 14.0
    assert diverged is True, "a 2x week-over-month move must be flagged against the default 30% threshold"


def test_blended_daily_rate_takes_an_explicit_divergence_threshold():
    """The threshold is a PARAMETER, not a hardcoded constant, because it is a saved, editable
    setting (`DEFAULT_BLEND['divergence_pct']`) — the refresh job (Task 6) reads it from storage
    and must be able to pass a value other than the default."""
    # 30d/day = 10.0, 7d/day = 11.0 -> 10% move: not diverged at the default 30% threshold...
    _, diverged_default = logic.blended_daily_rate(kg_30d=300.0, kg_7d=77.0, weight=0.4)
    assert diverged_default is False
    # ...but IS diverged against a tight 5% threshold, passed explicitly.
    _, diverged_tight = logic.blended_daily_rate(
        kg_30d=300.0, kg_7d=77.0, weight=0.4, divergence_fraction=0.05,
    )
    assert diverged_tight is True


def test_blended_daily_rate_falls_back_to_thirty_day_when_seven_day_is_missing():
    """kg_7d=None means no 7-day snapshot exists yet for this parent — NOT a zero-sales week.
    Falling back entirely (rather than blending toward zero) is the whole point: 4 of 47
    currently-selling parents on the real account had 30-day sales but no stored 7-day window
    at all when this was measured, and treating that as a zero would have cut their forecasts
    40% on no evidence.
    """
    rate, diverged = logic.blended_daily_rate(kg_30d=300.0, kg_7d=None, weight=0.4)
    assert rate == 10.0  # 300/30, the 30-day rate alone
    assert diverged is False, "a missing window is not evidence of divergence"


def test_blended_daily_rate_DOES_blend_a_genuine_zero_sales_week():
    """The other half of the same distinction: a REAL zero-sales week (the window exists, it
    says 0) is data, and IS blended at the normal weight — collapsing 'no data' and 'zero
    data' into the same behaviour is the mutation this test exists to catch.
    """
    rate, diverged = logic.blended_daily_rate(kg_30d=300.0, kg_7d=0.0, weight=0.4)
    # 30d/day = 10.0, 7d/day = 0.0 -> blended = 0.4*0 + 0.6*10 = 6.0
    assert rate == 6.0
    assert diverged is True


def test_blended_daily_rate_handles_a_dead_parent():
    rate, diverged = logic.blended_daily_rate(kg_30d=0.0, kg_7d=0.0, weight=0.4)
    assert rate == 0.0
    assert diverged is False, "0 vs 0 is agreement, not divergence"


# ─── blend settings: range-checked on read and write ──────────────────────────


def test_default_blend_weight_and_threshold():
    assert logic.DEFAULT_BLEND == {"seven_day_weight": 0.4, "divergence_pct": 30.0}


def test_blend_setting_error_refuses_an_unknown_key():
    assert logic.blend_setting_error("bogus", 5) is not None


def test_blend_setting_error_refuses_an_out_of_range_weight():
    """The good_rating: 99 lesson — a weight of 99 would mean 'ignore the 30-day figure
    entirely and pretend last week is the only history that exists', silently."""
    assert logic.blend_setting_error("seven_day_weight", 99) is not None
    assert logic.blend_setting_error("seven_day_weight", 0.4) is None


def test_blend_setting_error_refuses_a_non_numeric_value():
    assert logic.blend_setting_error("seven_day_weight", "lots") is not None


def test_blend_or_default_merges_over_the_defaults():
    merged = logic.blend_or_default({"seven_day_weight": 0.5})
    assert merged == {"seven_day_weight": 0.5, "divergence_pct": 30.0}


def test_blend_or_default_discards_an_invalid_stored_value():
    """Validated on READ, not only on write — a value already in the database, or edited by
    hand, must not keep weakening the setting with nothing on screen explaining why."""
    merged = logic.blend_or_default({"seven_day_weight": 500})
    assert merged["seven_day_weight"] == logic.DEFAULT_BLEND["seven_day_weight"]
