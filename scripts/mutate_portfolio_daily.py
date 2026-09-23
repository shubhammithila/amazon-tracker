"""Mutation harness for the per-day Portfolio store.

Run: ``venv/Scripts/python scripts/mutate_portfolio_daily.py``

Every mutation MUST be caught. The stakes: this change moves the money figures the kill/scale
decisions are read from, and **every dangerous failure here is silent**. A day summed twice, a day
dropped, a gap summed over, the MSKU grain leaking into the totals — each produces a plausible
number on a dashboard whose whole purpose is deciding which products to stop selling.

Several mutations RESTORE the old behaviour rather than breaking something new, because the old
behaviour passed 2,287 tests.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REPO = "app/portfolio/repository.py"
ECON = "app/portfolio/economics.py"
ADS = "app/portfolio/ads.py"
REFRESH = "app/portfolio/refresh.py"
ROUTER = "app/routers/portfolio.py"
TEMPLATE = "templates/portfolio.html"
DEPLOY = "deploy/update-ec2.sh"
SCHED = "app/scheduler.py"

#: (file, find, replace, why this would be a real bug)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ── Completeness: the guard against summing short ──
    (
        REPO,
        '        "complete": bool(wanted) and not absent,',
        '        "complete": bool(held),',
        "completeness is decided from what is HELD rather than what is WANTED, so any range answers "
        "once a single day exists — test_an_INTERIOR_gap_makes_the_range_incomplete",
    ),
    (
        REPO,
        "    absent = [day for day in wanted if day not in held]",
        "    absent = [day for day in (wanted[:1] + wanted[-1:]) if day not in held]",
        "**completeness from the ENDPOINTS instead of every day** — an interior gap becomes "
        "invisible and the sum silently understates sales, which is the Ads tab's span defect "
        "— test_an_INTERIOR_gap_makes_the_range_incomplete",
    ),
    (
        REPO,
        '        "complete": bool(wanted) and not absent,',
        '        "complete": not absent,',
        "an EMPTY store reports every range as complete (vacuous truth), so the dashboard renders "
        "zeros as though they were measured — test_an_empty_store_is_incomplete_rather_than_complete",
    ),
    (
        REPO,
        '        "missing_count": len(absent),',
        '        "missing_count": len(absent[:MISSING_DAYS_SHOWN]),',
        "the count is capped like the list, so 'missing 5 days' is reported when 29 are missing "
        "— test_the_missing_list_is_capped_but_the_count_is_EXACT",
    ),
    (
        REPO,
        "        select(table.day).where(table.seller_sku == ASIN_GRAIN).distinct()",
        "        select(table.day).distinct()",
        "a day holding ONLY per-SKU rows counts as held, so summing it drops every product Amazon "
        "could not attribute to one SKU "
        "— test_a_day_holding_ONLY_sku_rows_does_not_count_as_held",
    ),

    # ── The two grains must not destroy or inflate each other ──
    (
        REPO,
        "    await db.execute(\n"
        "        delete(EconomicsDaily).where(EconomicsDaily.day.in_(sorted(days)), grain_filter)\n"
        "    )",
        "    await db.execute(\n"
        "        delete(EconomicsDaily).where(EconomicsDaily.day.in_(sorted(days)))\n"
        "    )",
        "**the delete loses its GRAIN scope**, so storing the MSKU rows destroys the ASIN totals "
        "written moments earlier — the shape that would have wiped 72% of the ads spend "
        "— test_storing_the_MSKU_grain_does_not_delete_the_ASIN_grain",
    ),
    (
        REPO,
        "    await db.execute(\n"
        "        delete(EconomicsDaily).where(EconomicsDaily.day.in_(sorted(days)), grain_filter)\n"
        "    )",
        "    await db.execute(delete(EconomicsDaily).where(grain_filter))",
        "the delete loses its DAY scope, so the nightly one-day run wipes the other 89 days "
        "— test_refetching_one_day_leaves_the_other_days_alone",
    ),
    (
        REPO,
        "    grain_filter = (\n"
        "        EconomicsDaily.seller_sku != ASIN_GRAIN\n"
        "        if sku_grain\n"
        "        else EconomicsDaily.seller_sku == ASIN_GRAIN\n"
        "    )\n"
        "    rows = (",
        "    grain_filter = EconomicsDaily.id.is_not(None)\n"
        "    rows = (",
        "**the MSKU rows leak into the totals**, roughly doubling every figure on the dashboard "
        "— test_the_MSKU_rows_never_reach_a_total",
    ),
    (
        REPO,
        'ASIN_GRAIN = ""',
        "ASIN_GRAIN = None",
        "the authoritative grain is NULL again, and SQLite treats NULLs as DISTINCT in a unique "
        "index — the same (day, asin) can be stored twice, doubling that day "
        "— test_the_same_day_twice_is_an_UPDATE_not_a_second_row",
    ),

    # ── The summing arithmetic ──
    (
        REPO,
        '        bucket["ordered_sales"] += _float(row.ordered_sales)',
        '        bucket["ordered_sales"] = _float(row.ordered_sales)',
        "sales ASSIGN rather than accumulate, so a range reports only its last day — a "
        "plausible-looking number that is 1/30th of the truth "
        "— test_the_days_sum_to_the_window_figure",
    ),
    (
        REPO,
        "                bucket[target][name] = bucket[target].get(name, 0.0) + _float(amount)",
        "                bucket[target][name] = _float(amount)",
        "fees assign rather than accumulate across days "
        "— test_fees_merge_by_NAME_across_days_not_by_position",
    ),
    (
        REPO,
        "            for money in (\"cost\", \"attributed_sales\"):\n"
        "                bucket[money] = round(bucket[money] + figures[money], 2)",
        "            for money in (\"cost\", \"attributed_sales\"):\n"
        "                bucket[money] = figures[money]",
        "ad cost assigns rather than accumulates, keeping only the last day's spend — which is what "
        "the per-window code could safely do and this cannot "
        "— test_ad_days_sum_and_roll_up_to_the_asin",
    ),
    (
        REPO,
        "        day = str(raw.get(\"startDate\") or \"\")[:10]\n        if not day:\n            continue",
        "        day = str(raw.get(\"startDate\") or \"\")[:10] or \"1970-01-01\"",
        "a dateless economics row is filed under a GUESSED day, putting one day's sales into another "
        "— test_the_days_sum_to_the_window_figure",
    ),
    (
        REPO,
        "        if not day or not asin:\n            continue",
        "        if not asin:\n            continue\n        day = day or \"1970-01-01\"",
        "a dateless ADS row is filed under a guessed day — and Amazon really does return those when "
        "DAILY is requested without the date column "
        "— test_an_ad_row_with_no_day_is_SKIPPED_not_defaulted",
    ),

    # ── Retention ──
    (
        REPO,
        "DAILY_RETENTION_DAYS = 90",
        "DAILY_RETENTION_DAYS = 30",
        "retention is narrower than the widest range the tab offers, so a 90d window is "
        "unanswerable by construction "
        "— test_the_retention_window_is_at_least_the_widest_range_offered",
    ),
    (
        REPO,
        "        result = await db.execute(delete(table).where(table.day < cutoff))",
        "        result = await db.execute(delete(table).where(table.day < \"1970-01-01\"))",
        "the purge deletes nothing, so the tables grow without bound — the `ads_performance` "
        "problem repeating — test_purge_keeps_the_retention_window_and_drops_older_days",
    ),
    (
        REFRESH,
        "        try:\n"
        "            async with db_factory() as db:\n"
        "                await repository.purge_daily(db)",
        "        try:\n"
        "            if False:\n"
        "                await repository.purge_daily(None)",
        "the retention sweep never runs "
        "— test_purge_keeps_the_retention_window_and_drops_older_days",
    ),

    # ── The incremental nightly path ──
    (
        REFRESH,
        "    missing = sorted(day for day in wanted if day not in held)",
        "    missing = sorted(wanted)",
        "the nightly run refetches the whole window every night — ~45 minutes instead of ~15, on a "
        "box where the Ads job already runs for an hour "
        "— test_the_nightly_run_is_a_NO_OP_when_the_day_is_already_held",
    ),
    (
        REFRESH,
        "    end_day = (today or ist.today()) - timedelta(days=1)",
        "    end_day = (today or ist.today())",
        "the nightly run asks for TODAY, whose figures are still settling — an ad charge lands "
        "hours after its sale, so every product would read at a punishing TACOS each morning "
        "— test_a_long_gap_is_BOUNDED_rather_than_fetched_all_at_once",
    ),
    (
        REFRESH,
        "MAX_BACKFILL_DAYS = 7",
        "MAX_BACKFILL_DAYS = 90",
        "a long gap is fetched in ONE run: three ads reports, ~45 minutes, holding the nightly job "
        "open — test_the_backfill_cap_stays_inside_amazons_report_limit",
    ),
    (
        SCHED,
        "    result = await portfolio_refresh.run_incremental()",
        "    result = await portfolio_refresh.run()",
        "the scheduler refetches a whole 30-day window nightly instead of the missing day "
        "— test_the_nightly_portfolio_job_is_incremental",
    ),

    # ── The fetchers ──
    (
        ECON,
        '       "date_grain": "DAY" if by_day else "RANGE"}',
        '       "date_grain": "RANGE"}',
        "the economics query asks for RANGE, so every row carries the window as its date and the "
        "whole span lands on one day — test_the_economics_query_asks_for_DAY",
    ),
    (
        ADS,
        '            "columns": list(REPORT_COLUMNS) + (["date"] if daily else []),',
        '            "columns": list(REPORT_COLUMNS),',
        "**DAILY without the `date` column** — Amazon accepts it, the rows carry no date, and every "
        "one is skipped: the refresh reports success and stores nothing "
        "— test_the_ads_report_asks_for_DAILY_and_the_date_column_TOGETHER",
    ),
    (
        ADS,
        "        acc = merged[(day, asin, sku) if daily else (asin, sku)]",
        "        acc = merged[(asin, sku)]",
        "the ads aggregation drops the DAY, collapsing a 30-day report into window figures — the "
        "rows still look right and every sub-range is wrong "
        "— test_the_ads_aggregation_keys_on_the_DAY_under_daily",
    ),
    (
        ADS,
        "        if daily and not day:\n            continue",
        "        if False:\n            continue",
        "a dateless row survives aggregation and is then filed under an empty day "
        "— test_the_ads_aggregation_keys_on_the_DAY_under_daily",
    ),

    # ── The route and the screen: ONE computation of "is this instant" ──
    (
        ROUTER,
        '    result["completeness"] = completeness',
        '    result["completeness"] = None',
        "the server stops telling the screen whether the range is answerable, so the screen cannot "
        "name the missing days or offer the fetch "
        "— test_the_server_says_whether_the_shown_range_is_answerable",
    ),
    (
        TEMPLATE,
        "  const done = data.completeness || {};",
        "  const done = {};",
        "the screen ignores the server's answer and shows no fetch button for a range with a gap "
        "— test_the_screen_holds_no_SECOND_copy_of_the_instant_rule",
    ),
    (
        ROUTER,
        "    summable = bool(completeness and completeness[\"complete\"])",
        "    summable = True",
        "**an incomplete range is SUMMED SHORT** — a 90-day range over 40 stored days renders a "
        "total 50 days light and labels it 90 days. Found by driving the real screen, not by any "
        "mutation: every other one here attacks the completeness calculation, and this was a "
        "missing consumer of its answer "
        "— test_an_INCOMPLETE_range_renders_NOTHING_rather_than_a_short_sum",
    ),
    (
        ROUTER,
        "    summable = bool(completeness and completeness[\"complete\"])",
        "    summable = False",
        "every range refuses, so the tab is permanently empty — the other half of the gate "
        "— test_a_COMPLETE_range_does_render",
    ),

    # ── The deploy ──
    (
        DEPLOY,
        'elif "economics_daily" in tables:\n    print("e7b3f0c92a41")',
        'elif False:\n    print("e7b3f0c92a41")',
        "the baseline detector cannot see the new revision, so it stamps production BACKWARDS and "
        "the deploy dies on an existing table — test_the_deploy_detector_reports_the_head",
    ),
    (
        DEPLOY,
        '        "economics_daily", "ads_daily",',
        "",
        "the new tables are not required, so a deploy that skipped the migration passes and the app "
        "500s on every Portfolio request — test_the_migration_drops_both_per_window_tables",
    ),
]


def run_tests() -> bool:
    proc = subprocess.run(
        [
            str(ROOT / "venv/Scripts/python"),
            "-m",
            "pytest",
            "-q",
            "-x",
            "-p",
            "no:randomly",
            "tests/test_portfolio_daily.py",
            "tests/test_portfolio_api.py",
            "tests/test_portfolio_ads.py",
            "tests/test_projections_refresh.py",
            "tests/test_schema_migrations.py",
            "tests/test_retention_and_scheduler.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def main() -> int:
    survivors: list[str] = []
    for index, (rel, find, replace, why) in enumerate(MUTATIONS, 1):
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        label = f"[{index:2}/{len(MUTATIONS)}]"

        if find not in original:
            print(f"{label} SKIP  target text not found in {rel}")
            print(f"          looked for: {find[:78]!r}")
            survivors.append(f"{rel}: target not found — {why}")
            continue

        backup = tempfile.mktemp(suffix=".bak")
        shutil.copy2(path, backup)
        try:
            path.write_text(original.replace(find, replace, 1), encoding="utf-8")
            if run_tests():
                print(f"{label} SURVIVED  {why}")
                survivors.append(f"{rel}: {why}")
            else:
                print(f"{label} caught    {why}")
        finally:
            shutil.copy2(backup, path)
            Path(backup).unlink(missing_ok=True)

    print()
    if survivors:
        print(f"{len(survivors)} MUTATION(S) SURVIVED — a test is missing:")
        for item in survivors:
            print("  -", item)
        return 1
    print(f"All {len(MUTATIONS)} mutations caught.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
