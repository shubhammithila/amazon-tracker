"""The invariants that were violated while every existing test passed.

Reported as "22-28 is showing 4.44 lakh spend and 22-29 is showing 3.3 lakh" — a superset reporting
LESS than its subset. Measured cause: `refresh.run` writes Sponsored Brands to the per-window table
only, so any window summed from daily rows drops SB entirely. Rs 1,26,328 vanished, and a bid rule
on such a window found 743 changes with 0 SB rows where the stored window found 1,005 with 296.

These tests assert INVARIANTS rather than descriptions. "SB is stored daily" would pass again the
next time a product is added; "a superset can never report less than its subset" would not.
"""
import pytest

from app.ads import repository

pytestmark = pytest.mark.regression


def _report_row(entity_id, day, *, spend, product="sp", match_type="EXACT", bid=10.0):
    """One DAILY report row in the shape Amazon returns.

    `cost` and `sales7d`/`sales` differ per ad product — `logic.metrics_for` reads both — so the
    keys here mirror what the real report carries for that product.
    """
    row = {
        "keywordId": entity_id,
        "date": day,
        "campaignId": "C1",
        "adGroupId": "A1",
        "keyword": f"kw-{entity_id}",
        "matchType": match_type,
        "keywordBid": bid,
        "impressions": 100,
        "clicks": 10,
        "cost": spend,
    }
    if product == "sp":
        row["sales7d"] = spend * 2
        row["purchases7d"] = 1
    else:
        row["sales"] = spend * 2
        row["purchases"] = 1
    return row


async def _no_sleep(_seconds):
    """Skip the 20-second report poll interval in tests."""
    return None


async def test_storing_sb_for_a_day_does_not_delete_that_days_sp_rows(db):
    """**`save_daily` deleted by DAY alone, and that would have destroyed the SP data.**

    It is delete-then-bulk-insert — the deliberate 62x deviation from the house upsert — so a second
    call for the same days wipes the first product's rows. Storing SB after SP would have left
    SB-only days: the reported bug inverted, and worse, because SP is 72% of spend.

    The function's own docstring already claims this property one dimension down ("scoped per DAY so
    refetching a 7-day window cannot disturb the other 23 days"); until now only one product ever
    reached it.
    """
    await repository.save_daily(db, [_report_row("SP1", "2026-08-22", spend=100.0)],
                                ad_product="sp")
    await repository.save_daily(db, [_report_row("SB1", "2026-08-22", spend=50.0, product="sb")],
                                ad_product="sb")

    rows = await repository.sum_daily(db, "2026-08-22", "2026-08-22")
    by_product = {r["ad_product"]: r for r in rows}
    assert "sp" in by_product, "storing SB deleted the SP rows for the same day"
    assert "sb" in by_product, "the SB rows were not stored"
    assert by_product["sp"]["spend"] == 100.0
    assert by_product["sb"]["spend"] == 50.0


async def test_refetching_one_product_replaces_only_its_own_rows(db):
    """The other half: a re-fetch must still REPLACE, not accumulate.

    Delete-then-insert exists because a day's rows are wholly superseded by a refetch. Scoping by
    product must not turn that into an append, or a second nightly run would double the day's spend.
    """
    await repository.save_daily(db, [_report_row("SP1", "2026-08-22", spend=100.0)],
                                ad_product="sp")
    await repository.save_daily(db, [_report_row("SP1", "2026-08-22", spend=140.0)],
                                ad_product="sp")

    rows = await repository.sum_daily(db, "2026-08-22", "2026-08-22")
    assert len(rows) == 1, f"the refetch accumulated instead of replacing: {rows}"
    assert rows[0]["spend"] == 140.0


async def test_a_superset_window_never_reports_less_than_its_subset(db):
    """**The reported bug, as an invariant.**

    22-28 showed Rs 4,44,550 and 22-29 showed Rs 3,34,300 — adding a day REDUCED the total, because
    the first was a stored window (SP + SB) and the second was summed from daily rows (SP only).

    Stated as monotonicity rather than as "SB is stored daily": a test phrased the second way would
    pass again the next time a third ad product is added.
    """
    for day, spend in (("2026-08-22", 100.0), ("2026-08-23", 120.0)):
        await repository.save_daily(db, [_report_row("SP1", day, spend=spend)], ad_product="sp")
        await repository.save_daily(
            db, [_report_row("SB1", day, spend=spend / 2, product="sb")], ad_product="sb")

    subset = await repository.sum_daily(db, "2026-08-22", "2026-08-22")
    superset = await repository.sum_daily(db, "2026-08-22", "2026-08-23")

    subset_spend = sum(r["spend"] for r in subset)
    superset_spend = sum(r["spend"] for r in superset)
    assert superset_spend >= subset_spend, (
        f"a superset window reported LESS ({superset_spend}) than its subset ({subset_spend})"
    )
    assert superset_spend == pytest.approx(330.0), superset_spend


async def test_every_derived_window_carries_the_sponsored_brands_spend(db):
    """Rs 1,26,328 was 28% of the account and read as zero.

    Asserted on the summed rows because that is the one path left after this task — there is no
    longer a second path that could be the one that happens to be right.
    """
    await repository.save_daily(db, [_report_row("SP1", "2026-08-22", spend=318222.0)],
                                ad_product="sp")
    await repository.save_daily(
        db, [_report_row("SB1", "2026-08-22", spend=126328.0, product="sb")], ad_product="sb")

    rows = await repository.sum_daily(db, "2026-08-22", "2026-08-22")
    sb = [r for r in rows if r["ad_product"] == "sb"]
    assert sb, "the Sponsored Brands rows are missing from a derived window"
    assert sum(r["spend"] for r in sb) == pytest.approx(126328.0)
    assert sum(r["spend"] for r in rows) == pytest.approx(444550.0)


async def test_a_range_with_an_interior_gap_declines_to_answer(db):
    """**This is what makes a partial nightly scrape safe rather than silently wrong.**

    A 60-day scrape is four reports and `sbTargeting` throttles for hours, so a missing chunk is the
    expected case. `daily_range_complete` already checks EVERY day rather than the endpoints — a
    missing Tuesday must make the window refuse to answer, because a sum that is quietly short is
    what a bid rule would then act on.

    Pinned here because Task 3 depends on it and nothing else asserts the interior case: a test using
    only the endpoints would pass against a version that checks just `min` and `max`.
    """
    for day in ("2026-08-22", "2026-08-24"):          # 23rd deliberately absent
        await repository.save_daily(db, [_report_row("SP1", day, spend=10.0)], ad_product="sp")

    assert await repository.daily_range_complete(db, "2026-08-22", "2026-08-22") is True
    assert await repository.daily_range_complete(db, "2026-08-22", "2026-08-24") is False, (
        "a range with a missing interior day claimed to be complete, so its sum would be short"
    )


async def test_a_failed_chunk_keeps_the_days_already_stored(db, monkeypatch):
    """**A 60-day scrape is 4 reports; losing the night because the last one failed is not tolerable.**

    Amazon caps a report at 31 days, so 60 days is 2 SP chunks + 2 SB chunks — and `sbTargeting` has
    been measured returning 429 after 15 minutes of complete idleness. So a chunk failing is the
    expected case, not the exceptional one, and each must commit as it lands.

    Asserted through `on_chunk` because that is the mechanism: `fetch_targeting` used to accumulate
    every chunk and return once, so a failure in the last one discarded up to 40 minutes of work.
    """
    from app.ads import reports
    from app.portfolio.ads import AdsError

    stored: list[str] = []

    async def fake_one_report(client, start, end, **kwargs):
        if start >= "2026-08-01":
            raise AdsError("Amazon throttled this report (429).")
        return [_report_row("SP1", start, spend=10.0)]

    monkeypatch.setattr(reports, "_one_report", fake_one_report)

    async def on_chunk(rows, chunk_start, chunk_end):
        await repository.save_daily(db, rows, ad_product="sp")
        stored.append(chunk_start)

    with pytest.raises(AdsError):
        await reports.fetch_targeting(
            "2026-07-02", "2026-08-30", daily=True, on_chunk=on_chunk, sleep=_no_sleep,
        )

    assert stored, "the first chunk was not committed before the second failed"
    rows = await repository.sum_daily(db, "2026-07-02", "2026-07-02")
    assert rows, "the successfully fetched chunk was discarded when a later one failed"


def test_the_window_grain_table_is_gone():
    """One source of truth, enforced structurally.

    While two tables answered the same question, WHICH ONE you got depended on whether somebody had
    fetched that exact range — and they disagreed by 28%. Deleting the model is what makes a
    regression impossible rather than merely unlikely.
    """
    import app.models as models

    assert not hasattr(models, "AdsPerformance"), (
        "the per-window table still exists, so two paths can answer the same question again"
    )
    assert hasattr(models, "AdsPerformanceDaily")


# ─── The four mutations that survived the first pass ─────────────────────────
#
# Every test above passed while these four defects were reintroduced, including **the original bug**.
# That is the same failure the whole change is about — a test asserting a conclusion rather than the
# reason for it — so each of these names the mechanism instead.


def test_the_refresh_stores_sponsored_brands_at_the_DAILY_grain():
    """**The original bug, and nothing pinned it.**

    A mutation removing `daily=True` from the SB fetch — restoring exactly the code that made
    Rs 1,26,328 vanish — passed the entire suite. Nothing could catch it at runtime, because a fake
    `fetch_targeting` in a test does not care which grain was asked for, and the real one is never
    called. So this is asserted as SOURCE: the SB fetch must ask for daily rows and store them with
    `save_daily`, never `save_performance`.
    """
    from pathlib import Path

    source = (Path(__file__).parent.parent / "app" / "ads" / "refresh.py").read_text(
        encoding="utf-8")
    sb = source[source.index("# ── Sponsored Brands performance ──"):]
    assert 'ad_product="sb", daily=True' in sb, (
        "the Sponsored Brands report is fetched at the WINDOW grain — the exact defect that made "
        "Rs 1,26,328 of spend vanish from every window nobody had fetched exactly"
    )
    assert 'save_daily(chunk_db, chunk_rows, ad_product="sb")' in sb, (
        "the SB rows are not written to the daily table"
    )
    assert "save_performance" not in source, (
        "the window-grain write is back, so two grains can disagree again"
    )


async def test_a_day_held_for_only_one_product_is_not_complete(db):
    """**The product-blind `daily_range_complete`, which re-broke the bug by a new route.**

    A 60-day night is four reports and any can throttle. If the SP chunk fails while SB lands, those
    days exist with SB rows ONLY — and a range that calls itself complete then sums 28% of the spend
    and a bid rule previews against it.

    The earlier interior-gap test could not catch this: every day IS present, just not for every
    product. Mutating the check back to product-blindness passed the whole suite.
    """
    # Two days of SP, but only ONE of them also has SB.
    for day in ("2026-08-22", "2026-08-23"):
        await repository.save_daily(db, [_report_row("SP1", day, spend=100.0)], ad_product="sp")
    await repository.save_daily(
        db, [_report_row("SB1", "2026-08-22", spend=50.0, product="sb")], ad_product="sb")

    assert await repository.daily_range_complete(db, "2026-08-22", "2026-08-22") is True
    assert await repository.daily_range_complete(db, "2026-08-22", "2026-08-23") is False, (
        "a range whose second day has no Sponsored Brands rows claimed to be complete — summing it "
        "would report Sponsored Products only, which is the original 28% error"
    )


async def test_two_products_sharing_an_entity_id_stay_separate_rows(db):
    """**A colliding id would be merged, relabelled SP, and its bid written to the wrong API.**

    `sum_daily` groups by `(entity_id, ad_product)`. Grouped by id alone the two rows merge and the
    product comes from `max(ad_product)` — and `max('sb','sp')` is `'sp'`, so `logic.writer_for` would
    route a live Sponsored Brands bid change to `/sp/keywords`.

    Measured: 0 collisions across 29,360 ids today, which is luck rather than a guarantee — so the
    test constructs one, because the consequence is a bid written to the wrong endpoint.
    """
    shared = "999888777"
    await repository.save_daily(db, [_report_row(shared, "2026-08-22", spend=100.0)],
                                ad_product="sp")
    await repository.save_daily(
        db, [_report_row(shared, "2026-08-22", spend=40.0, product="sb")], ad_product="sb")

    rows = await repository.sum_daily(db, "2026-08-22", "2026-08-22")
    assert len(rows) == 2, (
        f"the two products' rows were merged into {len(rows)} — one product's bid would be written "
        f"to the other's API"
    )
    by_product = {r["ad_product"]: r for r in rows}
    assert by_product["sp"]["spend"] == 100.0
    assert by_product["sb"]["spend"] == 40.0
    # And each keeps its own writer, which is the thing that decides the endpoint.
    assert by_product["sp"]["writer"] == "keyword"
    assert by_product["sb"]["writer"] == "sb_keyword"
