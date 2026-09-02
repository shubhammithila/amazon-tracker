"""The only SQL for the Projections tab. Every Decimal must come back as float — see
app.models.ProjectionRow's docstring and the two prior defects (orders payload datetimes,
raw_kg) this app has already shipped from forgetting that conversion.
"""
import pytest

from app.projections import logic, repository

pytestmark = pytest.mark.asyncio


async def test_save_row_then_load_returns_a_float_not_a_decimal(db):
    saved = await repository.save_row(
        db, "Chana Sattu", {"purchase_rate": 120.0, "daily_rate": 5.5}, source="sheet",
    )
    assert isinstance(saved["purchase_rate"], float)
    assert isinstance(saved["daily_rate"], float)

    rows = await repository.load_rows(db)
    assert rows[0]["purchase_rate"] == 120.0


async def test_save_row_upserts_by_parent_name(db):
    """A repeated save for the same parent updates the one row rather than doubling it — the
    same SELECT-then-UPDATE-or-INSERT idiom `save_raw_stock` uses."""
    await repository.save_row(db, "Chana Sattu", {"purchase_rate": 100.0}, source="sheet")
    await repository.save_row(db, "Chana Sattu", {"purchase_rate": 150.0}, source="sheet")

    rows = await repository.load_rows(db)
    assert len(rows) == 1
    assert rows[0]["purchase_rate"] == 150.0


async def test_a_manual_edit_marks_the_row_manual(db):
    await repository.save_row(db, "Chana Sattu", {"last_month_sale": 999.0}, source="manual")
    rows = await repository.load_rows(db)
    assert rows[0]["sales_source"] == "manual"


async def test_upsert_sheet_rows_skips_a_manually_edited_row(db):
    """The rule the whole 'manual overrides survive a refresh' requirement rests on. A weekly
    recompute must not silently discard a hand-typed correction."""
    await repository.save_row(db, "Chana Sattu", {"last_month_sale": 999.0}, source="manual")
    await repository.save_row(db, "Govind Bhog Rice", {"last_month_sale": 10.0}, source="sheet")

    updated = await repository.upsert_sheet_rows(db, [
        {"parent_product": "Chana Sattu", "last_month_sale": 5.0, "daily_rate": 1.0},
        {"parent_product": "Govind Bhog Rice", "last_month_sale": 20.0, "daily_rate": 2.0},
    ])

    rows = {r["parent_product"]: r for r in await repository.load_rows(db)}
    assert rows["Chana Sattu"]["last_month_sale"] == 999.0, "a manual row was overwritten by a refresh"
    assert rows["Govind Bhog Rice"]["last_month_sale"] == 20.0, "a sheet row was not updated"
    assert updated == 1, "the skipped manual row must not count as updated"


async def test_upsert_sheet_rows_creates_a_new_row_for_a_first_seen_parent(db):
    updated = await repository.upsert_sheet_rows(db, [
        {"parent_product": "Triphala Sattu", "last_month_sale": 3.0, "needs_review": True},
    ])
    assert updated == 1
    rows = await repository.load_rows(db)
    assert rows[0]["parent_product"] == "Triphala Sattu"
    assert rows[0]["needs_review"] is True


async def test_reset_to_sheet_clears_the_manual_flag(db):
    await repository.save_row(db, "Chana Sattu", {"last_month_sale": 999.0}, source="manual")
    result = await repository.reset_to_sheet(db, "Chana Sattu")
    assert result["sales_source"] == "sheet"


async def test_reset_to_sheet_is_none_for_an_unknown_parent(db):
    assert await repository.reset_to_sheet(db, "Nonexistent") is None


# ─── blend settings ────────────────────────────────────────────────────────────


async def test_blend_settings_round_trip(db):
    saved = await repository.save_blend_settings(db, {"seven_day_weight": 0.5})
    assert saved["seven_day_weight"] == 0.5
    loaded = await repository.load_blend_settings(db)
    assert loaded["seven_day_weight"] == 0.5


async def test_blend_settings_reset_by_deleting_the_row(db):
    await repository.save_blend_settings(db, {"seven_day_weight": 0.9})
    reset = await repository.reset_blend_settings(db)
    assert reset == logic.DEFAULT_BLEND


async def test_save_blend_settings_raises_on_an_invalid_value(db):
    with pytest.raises(ValueError, match="seven_day_weight"):
        await repository.save_blend_settings(db, {"seven_day_weight": 99})


async def test_load_blend_settings_defaults_when_never_saved(db):
    assert await repository.load_blend_settings(db) == logic.DEFAULT_BLEND


async def test_global_growth_rate_and_divergence_multiplier_round_trip(db):
    """The two new blend settings use the exact same load/save/reset path as the pre-existing
    seven_day_weight/divergence_pct — no new repository functions needed, since
    load_blend_settings/save_blend_settings already iterate DEFAULT_BLEND generically."""
    defaults = await repository.load_blend_settings(db)
    assert defaults["global_growth_rate"] == 0.3
    assert defaults["divergence_buffer_multiplier"] == 1.5

    saved = await repository.save_blend_settings(
        db, {"global_growth_rate": 0.5, "divergence_buffer_multiplier": 2.0},
    )
    assert saved["global_growth_rate"] == 0.5
    assert saved["divergence_buffer_multiplier"] == 2.0

    reloaded = await repository.load_blend_settings(db)
    assert reloaded["global_growth_rate"] == 0.5
    assert reloaded["divergence_buffer_multiplier"] == 2.0


async def test_divergence_buffer_multiplier_below_one_is_refused(db):
    """The good_rating: 99 lesson, applied here: a multiplier below 1.0 would SHRINK a volatile
    product's buffer, the opposite of what this setting exists to do."""
    with pytest.raises(ValueError, match="divergence_buffer_multiplier"):
        await repository.save_blend_settings(db, {"divergence_buffer_multiplier": 0.5})


# ─── refresh history ───────────────────────────────────────────────────────────


async def test_record_refresh_then_last_refresh_round_trips(db):
    await repository.record_refresh(
        db, window_start="2026-08-02", window_end="2026-08-31", rows_stored=47,
    )
    last = await repository.last_refresh(db)
    assert last["rows_stored"] == 47
    assert last["error"] == ""
    assert isinstance(last["started_at"], str), "a datetime reaching JSON must be pre-serialised"


async def test_last_refresh_is_none_when_never_run(db):
    assert await repository.last_refresh(db) is None


async def test_last_refresh_returns_the_newest_row(db):
    await repository.record_refresh(db, window_start="2026-07-01", window_end="2026-07-30", rows_stored=1)
    await repository.record_refresh(db, window_start="2026-08-02", window_end="2026-08-31", rows_stored=2)
    last = await repository.last_refresh(db)
    assert last["rows_stored"] == 2
