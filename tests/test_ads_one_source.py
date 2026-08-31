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
