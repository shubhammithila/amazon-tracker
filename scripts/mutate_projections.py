"""Mutation harness for the Projections live-data change. Throwaway; not imported by the app.

Each entry breaks ONE decision this feature makes and names the test that must catch it.

    venv/Scripts/python scripts/mutate_projections.py
"""
import pathlib
import subprocess
import sys

LOGIC = pathlib.Path("app/projections/logic.py")
REPO = pathlib.Path("app/projections/repository.py")
REFRESH = pathlib.Path("app/projections/refresh.py")
ROUTER = pathlib.Path("app/routers/projections.py")

MUTATIONS = [
    (
        "normalize_name stops stripping hyphens, so a plausible sheet spelling stops matching",
        LOGIC,
        r'return re.sub(r"[\s-]+", "", name).casefold()',
        r'return re.sub(r"[\s]+", "", name).casefold()',
        "test_normalize_name_ignores_case_space_and_hyphen",
    ),
    (
        "group_active_by_name stops excluding inactive ASINs",
        LOGIC,
        "        if not row.get(\"active\"):\n            continue",
        "        if False:\n            continue",
        "test_group_active_by_name_excludes_inactive_asins",
    ),
    (
        "sales_kg_by_parent reads net_units instead of units_ordered",
        LOGIC,
        'units = int((row.get("sales") or {}).get("unitsOrdered") or 0)',
        'units = int((row.get("sales") or {}).get("netUnitsSold") or 0)',
        "test_sales_kg_by_parent_ignores_net_units_and_never_goes_negative",
    ),
    (
        "blended_daily_rate treats a missing 7-day window as a zero-sales week",
        LOGIC,
        "    if kg_7d is None:\n        return round(rate_30, 2), False",
        "    if kg_7d is None:\n        kg_7d = 0.0",
        "test_blended_daily_rate_falls_back_to_thirty_day_when_seven_day_is_missing",
    ),
    (
        "blend_or_default stops validating a stored value on read",
        LOGIC,
        "        if blend_setting_error(key, value) is None:\n            merged[key] = float(value)",
        "        merged[key] = float(value)",
        "test_blend_or_default_discards_an_invalid_stored_value",
    ),
    (
        "upsert_sheet_rows stops skipping a manually-edited row",
        REPO,
        '        if current is not None and current.sales_source == "manual":\n            continue',
        "        pass",
        "test_upsert_sheet_rows_skips_a_manually_edited_row",
    ),
    (
        "hidden_parent_names stops sorting, so the note's order is arbitrary",
        LOGIC,
        "    return sorted(stored_names - set(live_groups))",
        "    return list(stored_names - set(live_groups))",
        "test_hidden_parent_names_are_stored_parents_absent_from_the_live_groups",
    ),
    (
        "the refresh job stops recording a failed run",
        REFRESH,
        '        await repository.record_refresh(\n'
        '            db, window_start=None, window_end=None, rows_stored=0, error=str(exc),\n'
        '            started_at=started,\n'
        '        )\n'
        '        return {"rows_stored": 0, "error": str(exc), "window_start": None, "window_end": None}',
        '        return {"rows_stored": 0, "error": str(exc), "window_start": None, "window_end": None}',
        "test_run_records_a_failed_fetch_without_touching_existing_rows",
    ),
    (
        "/projections/calculate stops marking a saved row as manual",
        ROUTER,
        '        }, source="manual")',
        '        }, source="sheet")',
        "test_calculate_marks_every_saved_row_manual",
    ),
    (
        "calculate_projections stops applying growth/seasonal to an already-blended row",
        LOGIC,
        "        daily_rate = demand_rate * seasonal * growth_multiplier",
        "        daily_rate = demand_rate",
        "test_calculate_projections_applies_seasonality_and_growth_to_a_sheet_row",
    ),
    (
        "ideal_wh_stock drops the supplier lead time from the WH reorder trigger",
        LOGIC,
        "        ideal_wh = round(demand_rate * (s2w + effective_wh_buffer) * seasonal * growth_multiplier, 1)",
        "        ideal_wh = round(demand_rate * effective_wh_buffer * seasonal * growth_multiplier, 1)",
        "test_ideal_wh_stock_includes_the_supplier_lead_time",
    ),
    (
        "the divergence buffer multiplier stops being conditional on the diverged flag",
        LOGIC,
        '        effective_wh_buffer = wh_buffer * (divergence_buffer_multiplier if p.get("diverged") else 1.0)',
        "        effective_wh_buffer = wh_buffer * divergence_buffer_multiplier",
        "test_ideal_wh_stock_does_not_widen_the_buffer_for_a_calm_row",
    ),
    (
        "load_rows stops excluding a removed row by default",
        REPO,
        "    query = select(ProjectionRow)\n    if not include_excluded:\n        query = query.where(ProjectionRow.excluded_at.is_(None))",
        "    query = select(ProjectionRow)",
        "test_load_rows_excludes_by_default",
    ),
    (
        "the reorder export stops filtering to a positive reorder level",
        ROUTER,
        '    filtered = [p for p in products if (p.get("ideal_wh_stock") or 0) > 0]',
        "    filtered = list(products)",
        "test_download_reorder_xlsx_only_includes_positive_reorder_levels",
    ),
]


def run(expression):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-k", expression, "tests"],
        capture_output=True, text=True,
    )
    last = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return result.returncode, last


def main():
    survivors = []
    for label, path, old, new, test_name in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"SKIP      {label}\n          target text not found in {path}")
            survivors.append((label, f"target text not found in {path}"))
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            code, summary = run(test_name)
        finally:
            path.write_text(original, encoding="utf-8")
        if code == 0:
            print(f"SURVIVED  {label}\n          {test_name} still passes -> {summary}")
            survivors.append((label, test_name))
        else:
            print(f"caught    {label}\n          {summary}")

    print()
    if survivors:
        print(f"{len(survivors)} SURVIVOR(S):")
        for label, detail in survivors:
            print(f"  - {label} ({detail})")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
