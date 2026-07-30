"""Regression: ISSUE-005 — data_retention_days was never enforced.

Found by /qa on 2026-07-30.
Report: .gstack/qa-reports/qa-report-amazon-tracker-2026-07-30.md

``data_retention_days`` defaulted to 90 but nothing in the codebase read it —
grep found exactly one reference, in config.py. Five history tables therefore
grew without bound. At ~250 ASINs scraped daily that is roughly 91k rows per
table per year, on a t2.micro.

A latent startup crash was fixed at the same time: keyword tracking used
``daily_scrape_hour + 1`` with no wrap, so ``DAILY_SCRAPE_HOUR=23`` built
``CronTrigger(hour=24)`` and blew up scheduler setup.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    BSRHistory, Keyword, KeywordRanking, PriceHistory, Product,
    RatingHistory, SellerOffer,
)

pytestmark = pytest.mark.regression

HISTORY_MODELS = (PriceHistory, BSRHistory, RatingHistory, SellerOffer)


async def _seed_history(db, *, old_days: int, old_count: int, fresh_days: int, fresh_count: int):
    """Insert rows on both sides of the retention cutoff. Returns the product id."""
    now = datetime.utcnow()
    product = Product(asin="B0RETEN001", title="retention probe", first_seen=now)
    db.add(product)
    await db.flush()

    keyword = Keyword(keyword="sattu")
    db.add(keyword)
    await db.flush()

    for age_days, count in ((old_days, old_count), (fresh_days, fresh_count)):
        stamp = now - timedelta(days=age_days)
        for i in range(count):
            db.add(PriceHistory(product_id=product.id, price=100 + i, scraped_at=stamp))
            db.add(BSRHistory(product_id=product.id, bsr_rank=1000 + i, scraped_at=stamp))
            db.add(RatingHistory(product_id=product.id, rating=4.5, scraped_at=stamp))
            db.add(SellerOffer(product_id=product.id, seller_name="s", scraped_at=stamp))
            db.add(KeywordRanking(
                keyword_id=keyword.id, product_id=product.id,
                rank_position=i + 1, page_number=1, scraped_at=stamp,
            ))
    await db.commit()
    return product.id


async def _count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar()


# ─── The purge exists and works ──────────────────────────────────────────────

def test_retention_setting_is_actually_read_somewhere():
    """ISSUE-005 in one assertion: the setting was dead config."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    readers = [
        path.name
        for path in (repo_root / "app").rglob("*.py")
        if "data_retention_days" in path.read_text(encoding="utf-8")
        and path.name != "config.py"
    ]
    assert readers, (
        "data_retention_days is referenced only in config.py — it is dead config "
        "and the history tables will grow without bound."
    )


async def test_purge_deletes_rows_older_than_the_window(db):
    """5 rows at 200 days must go; 3 rows at 2 days must stay."""
    from app.scheduler import scheduled_purge_old_history

    await _seed_history(db, old_days=200, old_count=5, fresh_days=2, fresh_count=3)

    for model in HISTORY_MODELS:
        assert await _count(db, model) == 8, f"{model.__tablename__} seed failed"

    await scheduled_purge_old_history()
    db.expire_all()

    for model in (*HISTORY_MODELS, KeywordRanking):
        remaining = await _count(db, model)
        assert remaining == 3, (
            f"{model.__tablename__}: expected 3 fresh rows, found {remaining} — "
            "the retention sweep is not deleting old history"
        )


async def test_purge_keeps_rows_exactly_inside_the_window(db):
    """A row at 89 days must survive a 90-day policy (no off-by-one)."""
    from app.scheduler import scheduled_purge_old_history

    await _seed_history(db, old_days=91, old_count=2, fresh_days=89, fresh_count=4)

    await scheduled_purge_old_history()
    db.expire_all()

    assert await _count(db, PriceHistory) == 4, (
        "rows just inside the retention window were deleted — off-by-one in the cutoff"
    )


async def test_purge_does_not_delete_products_or_invoices(db):
    """Retention applies to history only; the catalogue must survive."""
    from app.models import Invoice
    from app.scheduler import scheduled_purge_old_history

    await _seed_history(db, old_days=400, old_count=3, fresh_days=1, fresh_count=1)
    db.add(Invoice(
        invoice_no="ST/26-27/099", invoice_number=99, shipment_id="OLD",
        date="2024-01-01", total_qty=1, total_amount=100,
        created_at=datetime.utcnow() - timedelta(days=400),
    ))
    await db.commit()

    await scheduled_purge_old_history()
    db.expire_all()

    assert await _count(db, Product) == 1, "the purge deleted a product"
    assert await _count(db, Invoice) == 1, "the purge deleted an invoice — tax records must persist"
    assert await _count(db, Keyword) == 1, "the purge deleted a keyword"


async def test_purge_is_a_noop_when_retention_is_disabled(db, monkeypatch):
    """days <= 0 must mean 'keep everything', not 'delete everything'."""
    import app.scheduler as sch
    from app.scheduler import scheduled_purge_old_history

    await _seed_history(db, old_days=500, old_count=6, fresh_days=1, fresh_count=2)
    monkeypatch.setattr(sch.settings, "data_retention_days", 0)

    await scheduled_purge_old_history()
    db.expire_all()

    assert await _count(db, PriceHistory) == 8, (
        "retention disabled (days=0) still deleted rows — this would destroy history"
    )


async def test_purge_on_an_empty_database_does_not_raise(db):
    from app.scheduler import scheduled_purge_old_history

    await scheduled_purge_old_history()  # must not raise
    assert await _count(db, PriceHistory) == 0


async def test_purge_is_idempotent(db):
    """Running twice must not error or delete anything the first pass kept."""
    from app.scheduler import scheduled_purge_old_history

    await _seed_history(db, old_days=300, old_count=4, fresh_days=3, fresh_count=2)

    await scheduled_purge_old_history()
    db.expire_all()
    after_first = await _count(db, PriceHistory)

    await scheduled_purge_old_history()
    db.expire_all()
    assert await _count(db, PriceHistory) == after_first == 2


# ─── Scheduler wiring ────────────────────────────────────────────────────────

def _registered_jobs(monkeypatch, hour: int, minute: int = 0):
    """Register jobs against a throwaway scheduler and return {id: trigger}."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    import app.scheduler as sch

    throwaway = AsyncIOScheduler()
    monkeypatch.setattr(sch, "scheduler", throwaway)
    monkeypatch.setattr(sch.settings, "scheduler_enabled", True)
    monkeypatch.setattr(sch.settings, "daily_scrape_hour", hour)
    monkeypatch.setattr(sch.settings, "daily_scrape_minute", minute)
    # start() would need a running loop; only registration matters here.
    monkeypatch.setattr(throwaway, "start", lambda *a, **k: None)

    sch.setup_scheduler()
    return {job.id: str(job.trigger) for job in throwaway.get_jobs()}


def test_purge_job_is_registered(monkeypatch):
    jobs = _registered_jobs(monkeypatch, hour=6)
    assert "daily_history_purge" in jobs, (
        "the retention sweep is never scheduled, so it will never run in production"
    )


def test_all_three_daily_jobs_are_registered(monkeypatch):
    jobs = _registered_jobs(monkeypatch, hour=6)
    assert set(jobs) == {"daily_product_scrape", "daily_keyword_track", "daily_history_purge"}


@pytest.mark.parametrize(
    "scrape_hour,expected_keyword_hour,expected_purge_hour",
    [
        (6, 7, 9),
        (22, 23, 1),   # purge wraps past midnight
        (23, 0, 2),    # keyword tracking wraps — this used to build hour=24 and crash
        (0, 1, 3),
    ],
)
def test_derived_hours_wrap_past_midnight(
    monkeypatch, scrape_hour, expected_keyword_hour, expected_purge_hour
):
    """DAILY_SCRAPE_HOUR=23 previously raised on CronTrigger(hour=24)."""
    jobs = _registered_jobs(monkeypatch, hour=scrape_hour)

    assert f"hour='{scrape_hour}'" in jobs["daily_product_scrape"]
    assert f"hour='{expected_keyword_hour}'" in jobs["daily_keyword_track"]
    assert f"hour='{expected_purge_hour}'" in jobs["daily_history_purge"]


def test_scheduler_registers_nothing_when_disabled(monkeypatch):
    """SCHEDULER_ENABLED=false must be honoured — tests rely on it."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    import app.scheduler as sch

    throwaway = AsyncIOScheduler()
    monkeypatch.setattr(sch, "scheduler", throwaway)
    monkeypatch.setattr(sch.settings, "scheduler_enabled", False)

    sch.setup_scheduler()
    assert throwaway.get_jobs() == []
