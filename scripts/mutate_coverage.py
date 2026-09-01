"""Mutation harness for the coverage/IST/run-record change. Throwaway; not imported by the app.

Each entry breaks ONE decision and names the test that must catch it. Run rather than reasoned about,
because this feature has already had four mutations survive a green suite — including the original
Sponsored Brands bug, which passed because a fake `fetch_targeting` does not care which grain it was
asked for.

    venv/Scripts/python scripts/mutate_coverage.py
"""
import pathlib
import subprocess
import sys

IST = pathlib.Path("app/ist.py")
REPO = pathlib.Path("app/ads/repository.py")
REFRESH = pathlib.Path("app/ads/refresh.py")
ROUTER = pathlib.Path("app/routers/ads.py")
TEMPLATE = pathlib.Path("templates/ads.html")
SCHED = pathlib.Path("app/scheduler.py")

MUTATIONS = [
    # ── The IST conversion, which is the scheduler bug ──────────────────────
    (
        "utc_hhmm ADDS the offset instead of subtracting, so 08:00 IST fires at 13:30",
        IST,
        "    anchored = datetime(2000, 1, 2, hour, minute, tzinfo=IST)\n"
        "    as_utc = anchored.astimezone(timezone.utc)",
        "    anchored = datetime(2000, 1, 2, hour, minute, tzinfo=timezone.utc)\n"
        "    as_utc = anchored.astimezone(IST)",
        "test_an_ist_wall_clock_time_becomes_the_utc_one_cron_needs",
    ),
    (
        "the scheduler passes the IST hour straight to cron, the original defect",
        SCHED,
        "    ads_utc = ist.utc_hhmm(*ADS_REFRESH_IST)",
        "    ads_utc = ADS_REFRESH_IST",
        "test_the_nightly_jobs_fire_at_the_IST_time_they_claim",
    ),
    (
        "'today' is the server's UTC date again, so the window is a day early before 05:30 IST",
        IST,
        "    return now().date()",
        "    return datetime.utcnow().date()",
        "test_today_is_the_ist_day_not_the_utc_day or test_the_ads_window_asks_ist",
    ),
    (
        "default_window uses date.today() rather than the IST day",
        REFRESH,
        "    end = (today or ist.today()) - timedelta(days=1)",
        "    end = (today or date.today()) - timedelta(days=1)",
        "test_the_ads_window_asks_ist_for_its_yesterday",
    ),
    # ── Per-product completeness ────────────────────────────────────────────
    (
        "completeness judged from each product's SPAN, so an interior gap reads as complete",
        REPO,
        "        absent = [day for day in wanted if day not in held]",
        "        absent = ([] if (held and min(held) <= wanted[0] and max(held) >= wanted[-1])\n"
        "                  else [day for day in wanted if day not in held])",
        "test_an_interior_gap_is_incomplete_even_though_the_span_covers_it",
    ),
    (
        "the product list is hardcoded, so a third ad product is silently ignored",
        REPO,
        "async def daily_products(db: AsyncSession) -> list[str]:",
        "async def daily_products(db: AsyncSession) -> list[str]:\n"
        "    return ['sp', 'sb']\n"
        "def _unused_daily_products(db):",
        "test_a_third_ad_product_needs_no_change_here",
    ),
    (
        "an empty table is vacuously COMPLETE, so an empty window reports Rs 0 as a fact",
        REPO,
        '        "complete": bool(products),',
        '        "complete": True,',
        "test_an_empty_table_is_incomplete_rather_than_vacuously_complete",
    ),
    (
        "missing_count reports the CAPPED length, understating a 59-day gap as 5",
        REPO,
        '            answer["missing_count"][product] = len(absent)',
        '            answer["missing_count"][product] = len(absent[:MISSING_DAYS_SHOWN])',
        "test_the_named_days_are_capped_but_the_count_stays_exact",
    ),
    # ── The screen agreeing with the server ─────────────────────────────────
    (
        "the preset dots read the product-MERGED span again — the reported bug",
        TEMPLATE,
        "    const cached = !!entry.complete;",
        "    const cover = data.daily_coverage || [];\n"
        "    const cached = cover.length === 2 && cover[0] <= entry.start && cover[1] >= entry.end;",
        "test_the_merged_span_gates_nothing_on_screen",
    ),
    # ── SP-only is for looking, not for rules ───────────────────────────────
    (
        "/ads/preview accepts ad_product, letting a rule run blind to 28% of spend",
        ROUTER,
        '    if body.get("ad_product"):',
        '    if False and body.get("ad_product"):',
        "test_preview_refuses_a_single_ad_product_view",
    ),
    (
        "sum_daily ignores the product filter, so the SP-only view silently shows everything",
        REPO,
        "    if ad_product:\n        query = query.where(AdsPerformanceDaily.ad_product == ad_product)",
        "    if False and ad_product:\n"
        "        query = query.where(AdsPerformanceDaily.ad_product == ad_product)",
        "test_the_dashboard_can_show_one_ad_product_and_says_what_it_excludes",
    ),
    # ── The run record ──────────────────────────────────────────────────────
    (
        "a partial night is recorded as done, hiding the throttle",
        REPO,
        '    elif sb_error:\n        status = "partial"',
        '    elif False:\n        status = "partial"',
        "test_a_partial_night_is_recorded_as_partial_with_the_counts_apart",
    ),
    (
        "the dashboard reads only the in-memory state, so a restart erases the reason",
        ROUTER,
        'or ("" if live_refresh.get("running") else stored_refresh.get("sb_error", ""))',
        'or ""',
        "test_the_dashboard_explains_a_partial_night_after_a_restart",
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
