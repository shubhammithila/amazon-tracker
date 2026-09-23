"""One-off: populate the per-day Portfolio store with 90 days of history.

    venv/Scripts/python scripts/backfill_portfolio_daily.py [--days 90] [--dry-run]

Run ONCE after deploying revision `e7b3f0c92a41`. The nightly job keeps the store current from then
on, fetching only the day it is missing — so this exists purely to fill the history the dropped
per-window tables could not be converted into.

**Why the old rows could not be migrated.** A 30-day total carries no information about which day
each sale fell on, so there was nothing to split. Refetching at DAY granularity is the only honest
way to populate these tables, and it is cheap for the economics half: measured, 30 days of DAY rows
is 8,010 rows in 25 seconds — the same cost as the RANGE query it replaces.

**The ads half is the expensive part, and it is stored CHUNK BY CHUNK.** Amazon caps one report at
31 days, so 90 days is three reports at ~15 minutes each. Storing after each chunk means a throttled
third report leaves the first two months' rows on disk rather than discarding 30 minutes of work —
the same reason `ads.fetch_targeting` gained an `on_chunk` callback for the Ads tab, where a
throttled chunk is the expected case rather than the exception.

Safe to re-run: every write is delete-then-insert scoped by day, so a second pass corrects rather
than doubles.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import timedelta
from pathlib import Path

# Importable when run as `python scripts/backfill_portfolio_daily.py` from anywhere, matching
# `scripts/sqlite_to_postgres.py`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import ist  # noqa: E402 - after the sys.path fix above
from app.database import async_session  # noqa: E402
from app.portfolio import ads, economics, repository  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
)
logger = logging.getLogger("backfill")


async def _economics(days: int, *, dry_run: bool) -> int:
    """Fetch and store `days` days of economics at DAY granularity. One query for the whole span."""
    end = ist.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    logger.info("economics: fetching %s..%s at DAY granularity", start, end)

    t0 = time.time()
    asin_rows, sku_rows, window_start, window_end = await economics.fetch_economics(
        start=start.isoformat(), end=end.isoformat(), by_day=True,
    )
    logger.info(
        "economics: %d ASIN row(s) + %d SKU row(s) for %s..%s in %.0fs",
        len(asin_rows), len(sku_rows), window_start, window_end, time.time() - t0,
    )
    if dry_run:
        logger.info("economics: --dry-run, nothing stored")
        return 0

    async with async_session() as db:
        stored = await repository.save_economics_daily(db, asin_rows)
        stored_skus = await repository.save_sku_snapshot(db, sku_rows)
    logger.info("economics: stored %d ASIN row(s), %d SKU row(s)", stored, stored_skus)
    return stored


async def _ads(days: int, *, dry_run: bool) -> int:
    """Fetch and store `days` days of ad figures, **committing after each 31-day chunk.**"""
    end = ist.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    chunks = ads.split_window(start.isoformat(), end.isoformat())
    logger.info(
        "ads: %s..%s needs %d report(s) (Amazon caps one at %d days) — roughly %d minutes",
        start, end, len(chunks), ads.MAX_REPORT_DAYS, 15 * len(chunks),
    )

    total = 0
    for index, (chunk_start, chunk_end) in enumerate(chunks, 1):
        t0 = time.time()
        logger.info("ads: chunk %d/%d %s..%s", index, len(chunks), chunk_start, chunk_end)
        try:
            rows = await ads.fetch_acos(chunk_start, chunk_end, daily=True)
        except ads.AdsNotConfigured:
            logger.warning("ads: credentials are not configured, skipping the ACOS half entirely")
            return total
        except ads.AdsError as exc:
            # Stop rather than continue: the chunks are contiguous, so a later one succeeding after
            # an earlier failure would leave an interior gap — and `range_completeness` would then
            # refuse every range spanning it, which is correct but needlessly wide. Re-run the
            # script; the days already stored are kept.
            logger.warning("ads: chunk %d/%d failed (%s) — stopping, re-run to continue",
                           index, len(chunks), exc)
            return total

        if dry_run:
            logger.info("ads: chunk %d/%d returned %d row(s) in %.0fs, --dry-run so not stored",
                        index, len(chunks), len(rows), time.time() - t0)
            continue

        async with async_session() as db:
            stored = await repository.save_ads_daily(db, rows)
        total += stored
        logger.info("ads: chunk %d/%d stored %d row(s) in %.0fs",
                    index, len(chunks), stored, time.time() - t0)
    return total


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=repository.DAILY_RETENTION_DAYS,
        help="how many days of history to fetch (default: the retention window)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="fetch and report row counts without writing anything",
    )
    args = parser.parse_args()

    if args.days > repository.DAILY_RETENTION_DAYS:
        logger.warning(
            "--days %d is wider than the %d-day retention window, so the oldest days would be "
            "purged by the next refresh. Fetching %d instead.",
            args.days, repository.DAILY_RETENTION_DAYS, repository.DAILY_RETENTION_DAYS,
        )
        args.days = repository.DAILY_RETENTION_DAYS

    async with async_session() as db:
        before = await repository.coverage(db)
    logger.info("before: %s", before)

    await _economics(args.days, dry_run=args.dry_run)
    await _ads(args.days, dry_run=args.dry_run)

    async with async_session() as db:
        after = await repository.coverage(db)
        # The honest check: is the widest range the tab offers actually answerable now?
        end = ist.today() - timedelta(days=1)
        start = end - timedelta(days=args.days - 1)
        done = await repository.range_completeness(db, start.isoformat(), end.isoformat())

    logger.info("after : %s", after)
    if done["complete"]:
        logger.info("the full %d-day range is summable — every sub-range is now instant", args.days)
        return 0

    logger.warning(
        "the %d-day range is still missing %d day(s) (e.g. %s). Re-run to fill them; ranges "
        "inside %s..%s are already instant.",
        args.days, done["missing_count"], ", ".join(done["missing"]),
        after["first"], after["last"],
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
