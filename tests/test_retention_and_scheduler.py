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


def test_all_scheduled_jobs_are_registered(monkeypatch):
    """An exact set, so a job added without a test is caught here.

    Named as a set rather than an "in" check because a job that silently stops being
    registered never runs in production and nothing complains — the failure the purge job
    already had once.
    """
    jobs = _registered_jobs(monkeypatch, hour=6)
    assert set(jobs) == {
        "daily_product_scrape", "daily_keyword_track", "daily_history_purge",
        "order_refresh",
    }


def test_the_order_refresh_runs_every_thirty_minutes(monkeypatch):
    """Asked for: "every 30 mins is fine + manual refresh button".

    An interval rather than a cron, because "since the last run" is what matters for a
    poll, not a wall-clock time. Asserted on the emitted trigger so a changed constant
    fails here rather than only in production.
    """
    from app import scheduler as sched

    jobs = _registered_jobs(monkeypatch, hour=6)
    assert "order_refresh" in jobs, (
        "the order refresh is never scheduled, so the Orders tab would only ever update "
        "when someone pressed the button"
    )
    assert sched.ORDER_REFRESH_MINUTES == 30
    assert "0:30:00" in jobs["order_refresh"], jobs["order_refresh"]


def test_the_routine_order_refresh_pages_past_the_measured_backlog():
    """One page is 100 orders and the measured actionable backlog is 371.

    Amazon caps a page at 100 and returns NextToken when more exists, so too low a cap makes
    the packer's sheet quietly incomplete — and it does so worst when the warehouse is
    busiest, which is when the sheet matters most.
    """
    from app import scheduler as sched

    assert sched.ORDER_REFRESH_PAGES >= 4, (
        "the measured actionable set is 371 orders in 4 pages, so a lower cap truncates "
        "today's picking sheet"
    )


def test_the_routine_window_asks_only_for_today():
    """**This assertion has now been wrong in BOTH directions, which is the lesson.**

    v1 required `<= 30` days, reasoning that a short window keeps a date-bounded fetch cheap.
    Wrong: `getOrders` pages oldest-first, so a short window meant 6 pages of `Delivered` while
    371 orders waited unseen.

    v2 required `>= 60` days, reasoning that `EasyShipShipmentStatuses` bounds the answer so the
    window is free. True only while that filter was `PendingSchedule,PendingPickUp` — statuses
    with almost no history.

    v3, here: adding `PickedUp` to the filter was necessary (without it, 99 of one day's 194
    orders were never fetched), and `PickedUp` has MONTHS of history. Measured on 2026-08-26 with
    the 90-day window: 800 rows, 8 pages, ~3 minutes, and **0** orders due today.

    The window and the filter are ONE budget, not two independent knobs. Whichever is widened,
    the other must narrow. Asserted as "the routine window is today" rather than a day count,
    because that is the actual requirement — the dispatch screen only shows today.
    """
    from app import scheduler as sched
    from app.orders import spapi_orders

    assert sched.ORDER_REFRESH_DAYS == spapi_orders.TODAY_ONLY, (
        "the routine refresh does not ask for today only; with the collected statuses in the "
        "filter, a wider window spends its whole budget paging orders that are long gone"
    )
    # And the sentinel must resolve to the START of the business day in IST.
    since = spapi_orders._since(sched.ORDER_REFRESH_DAYS)
    assert since.endswith("18:30:00Z"), (
        f"midnight IST is 18:30Z the previous day; got {since}"
    )


def test_the_manual_button_looks_further_back_than_the_routine_job():
    """Two windows, two jobs. The button is the catch-up, so it must be wider.

    Equal windows would make the button pointless; a 90-day button would make it feel broken,
    which is what it did — three minutes to fetch nothing due today.
    """
    from app.routers import orders as orders_router
    from app.orders import spapi_orders

    assert orders_router.BACKFILL_DAYS >= 2, "the manual refresh is not a catch-up at all"
    assert orders_router.BACKFILL_DAYS <= 7, (
        "a wide manual window spends minutes paging oldest-first through collected orders; "
        "use the one-off backfill for genuine history repair"
    )
    assert orders_router.BACKFILL_DAYS != spapi_orders.TODAY_ONLY, (
        "the button asks for exactly what the half-hourly job already did, so pressing it "
        "cannot recover a straggler"
    )


async def test_the_order_refresh_skips_when_spapi_is_not_configured(
    monkeypatch, no_spapi_credentials
):
    """The app must work without Amazon credentials.

    Without this guard the log fills with an auth failure every 30 minutes on any
    deployment that has no SP-API keys — which is every fresh install.
    """
    from app import scheduler as sched
    from app.orders import refresh

    called = []

    async def should_not_run(*a, **k):
        called.append(True)
        return {}

    monkeypatch.setattr(refresh, "run", should_not_run)
    await sched.scheduled_order_refresh()
    assert not called, "the refresh ran with no credentials configured"


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


# ─── The orders job has its own flag ─────────────────────────────────────────
#
# Production runs SCHEDULER_ENABLED=false, so nothing scheduled runs there. Turning it on to
# get the orders refresh would also wake the 06:00 product scrape (10 async workers), the 07:30
# keyword track and the 09:15 purge — on a 951 MB box with no swap that has already OOM-killed
# a pip install. Waking three dormant jobs as a side effect of a UI change is not a decision
# the Orders feature gets to make.


def _jobs_with_flags(monkeypatch, *, scheduler_enabled: bool, order_refresh_enabled: bool):
    """Register jobs with both flags set explicitly. Returns {job_id: trigger}.

    A separate helper from `_registered_jobs`, which hardcodes `scheduler_enabled=True` and
    therefore cannot express the case that matters here.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    import app.scheduler as sch

    throwaway = AsyncIOScheduler()
    monkeypatch.setattr(sch, "scheduler", throwaway)
    monkeypatch.setattr(sch.settings, "scheduler_enabled", scheduler_enabled)
    monkeypatch.setattr(sch.settings, "order_refresh_enabled", order_refresh_enabled)
    monkeypatch.setattr(sch.settings, "daily_scrape_hour", 6)
    monkeypatch.setattr(sch.settings, "daily_scrape_minute", 0)
    monkeypatch.setattr(throwaway, "start", lambda *a, **k: None)

    sch.setup_scheduler()
    return {job.id: str(job.trigger) for job in throwaway.get_jobs()}


def test_the_orders_job_runs_on_its_own_flag_without_waking_the_others(monkeypatch):
    """Production's exact configuration: master off, orders on.

    Asserted on the registered job ids rather than on the flag, because the bug this guards is a
    refactor moving `if not settings.scheduler_enabled: return` back to the top of
    setup_scheduler — which reads as tidy and silently stops the orders refresh.
    """
    jobs = _jobs_with_flags(monkeypatch, scheduler_enabled=False, order_refresh_enabled=True)
    assert "order_refresh" in jobs, (
        "the orders refresh does not run with only its own flag set, so production would have "
        "to enable every dormant job to get it"
    )
    for dormant in ("daily_product_scrape", "daily_keyword_track", "daily_history_purge"):
        assert dormant not in jobs, (
            f"{dormant} woke up as a side effect of enabling the orders refresh"
        )


def test_the_master_flag_alone_still_registers_the_orders_job(monkeypatch):
    """The flags are OR'd, so no existing installation loses its refresh.

    `scheduler_enabled` defaults to True, so a fresh install must keep working without anyone
    learning about a second flag.
    """
    jobs = _jobs_with_flags(monkeypatch, scheduler_enabled=True, order_refresh_enabled=False)
    assert "order_refresh" in jobs
    assert "daily_product_scrape" in jobs, "the master flag stopped registering its own jobs"


def test_both_flags_off_registers_nothing(monkeypatch):
    """A deployment that wants no background work must get none."""
    jobs = _jobs_with_flags(monkeypatch, scheduler_enabled=False, order_refresh_enabled=False)
    assert jobs == {}, f"jobs registered with every flag off: {sorted(jobs)}"


def test_the_order_refresh_flag_defaults_to_off():
    """Opt-in, so deploying this code changes no running system's behaviour by itself.

    Note the deliberate asymmetry: `scheduler_enabled` defaults to True, so a fresh install
    already gets the orders job through the OR. This flag is only load-bearing where the master
    flag has been explicitly turned off — which is exactly production.
    """
    from app.config import Settings

    assert Settings().order_refresh_enabled is False
