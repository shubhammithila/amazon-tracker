"""Regression: ISSUE-004 — the scheduled scrape wiped a manual scrape's results.

Found by /qa on 2026-07-30.
Report: .gstack/qa-reports/qa-report-amazon-tracker-2026-07-30.md

``app/scraper/engine.py`` exposes a single module-level ``scrape_state`` shared by
the manual ``POST /scrape`` route, the 06:00 APScheduler job, and the
``/ws/progress`` socket. Two defects combined:

  * ``scheduled_product_scrape()`` called ``scrape_state.results.clear()``
    unconditionally — twice. If the cron job overlapped a manual scrape it wiped
    results belonging to the other run, so the dashboard table and the partial
    download emptied mid-flight.
  * the ``if scrape_state.running`` guard was check-then-act with no lock, so
    both callers could pass it and share one state object.

The fix added ``ScrapeState.claim_for_new_run()``, which resets state and marks
it running atomically under an ``asyncio.Lock``.

No network calls happen here: ``run_scrape`` is stubbed or the state object is
driven directly, so these tests are fast and deterministic.
"""
import asyncio

import pytest

from app.scraper.engine import ScrapeAlreadyRunning, ScrapeState, scrape_state

pytestmark = pytest.mark.regression


# ─── The atomic claim ────────────────────────────────────────────────────────

async def test_concurrent_claims_produce_exactly_one_winner():
    """The core invariant. Previously check-then-act let several through."""
    state = ScrapeState()
    results = await asyncio.gather(*[state.claim_for_new_run() for _ in range(16)])

    assert sum(results) == 1, (
        f"{sum(results)} callers claimed the scraper simultaneously; exactly 1 may win"
    )
    assert state.running is True


async def test_claim_is_refused_while_a_run_is_in_flight():
    state = ScrapeState()
    assert await state.claim_for_new_run() is True
    assert await state.claim_for_new_run() is False, "a second run claimed a busy scraper"


async def test_claim_succeeds_again_after_reset():
    state = ScrapeState()
    await state.claim_for_new_run()
    state.reset()
    assert await state.claim_for_new_run() is True


async def test_reset_does_not_replace_the_lock():
    """reset() runs inside claim_for_new_run; rebuilding the lock would break it."""
    state = ScrapeState()
    lock_before = state._claim_lock
    state.reset()
    assert state._claim_lock is lock_before, (
        "reset() replaced the claim lock — concurrent claims would stop being "
        "mutually exclusive"
    )


async def test_claim_clears_results_from_the_previous_run():
    """A new run must start clean, otherwise stale rows show in the dashboard."""
    state = ScrapeState()
    state.results.extend([{"asin": "B0STALE0001"}, {"asin": "B0STALE0002"}])
    state.running = False

    assert await state.claim_for_new_run() is True
    assert state.results == []


async def test_claim_is_serialised_under_load():
    """Interleave claim/reset pairs; the scraper must never be doubly owned."""
    state = ScrapeState()
    owners = []

    async def worker(n):
        if await state.claim_for_new_run():
            owners.append(n)
            await asyncio.sleep(0)  # yield mid-ownership
            state.reset()

    await asyncio.gather(*[worker(i) for i in range(30)])

    # Each winner released before the next could claim, so no two overlapped.
    assert len(owners) == len(set(owners))
    assert state.running is False


# ─── run_scrape refuses to trample an in-flight run ──────────────────────────

async def test_run_scrape_raises_when_already_running(monkeypatch):
    from app.scraper import engine

    scrape_state.reset()
    assert await scrape_state.claim_for_new_run() is True  # simulate a live run

    async def fail_if_called(*args, **kwargs):
        pytest.fail("_run_batch ran despite another scrape being in flight")

    monkeypatch.setattr(engine, "_run_batch", fail_if_called)

    with pytest.raises(ScrapeAlreadyRunning):
        await engine.run_scrape(["B0AAAAAAAA"])


async def test_run_scrape_does_not_clear_the_owning_runs_results(monkeypatch):
    """The exact ISSUE-004 failure: a second caller wiping live results."""
    from app.scraper import engine

    scrape_state.reset()
    await scrape_state.claim_for_new_run()
    scrape_state.results.extend({"asin": f"B0MANUAL{i:03d}"} for i in range(5))

    async def fail_if_called(*args, **kwargs):
        pytest.fail("_run_batch ran despite another scrape being in flight")

    monkeypatch.setattr(engine, "_run_batch", fail_if_called)

    with pytest.raises(ScrapeAlreadyRunning):
        await engine.run_scrape(["B0BBBBBBBB"])

    assert len(scrape_state.results) == 5, (
        "the rejected run cleared the in-flight run's results — ISSUE-004 regressed"
    )
    assert scrape_state.running is True, "the rejected run cleared the running flag"


async def test_run_scrape_clears_running_when_it_finishes(monkeypatch):
    from app.scraper import engine

    scrape_state.reset()

    async def noop_batch(batch, state, on_result):
        return None

    monkeypatch.setattr(engine, "_run_batch", noop_batch)
    await engine.run_scrape(["B0CCCCCCCC"])

    assert scrape_state.running is False
    assert scrape_state.last_scraped_at is not None


async def test_run_scrape_clears_running_even_when_a_batch_raises(monkeypatch):
    """A crashed scrape must not leave the scraper permanently claimed."""
    from app.scraper import engine

    scrape_state.reset()

    async def exploding_batch(batch, state, on_result):
        raise RuntimeError("network died")

    monkeypatch.setattr(engine, "_run_batch", exploding_batch)
    await engine.run_scrape(["B0DDDDDDDD"])

    assert scrape_state.running is False, (
        "running stayed True after a crash — every later scrape would 409 forever"
    )
    assert scrape_state.error is not None


# ─── The scheduler must not touch a run it does not own ──────────────────────

async def test_scheduled_scrape_skips_when_a_manual_run_is_already_visible():
    """The cheap pre-check: an obviously-busy scraper is left alone."""
    import app.scheduler as sch

    scrape_state.reset()
    scrape_state.running = True  # a manual scrape is mid-flight
    scrape_state.results.extend({"asin": f"B0MANUAL{i:03d}"} for i in range(5))

    await sch.scheduled_product_scrape()

    assert len(scrape_state.results) == 5
    assert scrape_state.running is True, "the scheduled job cleared the running flag"


async def test_scheduled_scrape_does_not_clear_results_a_manual_run_added_mid_flight(
    monkeypatch, db
):
    """The headline ISSUE-004 regression, exercising the real interleaving.

    The damaging sequence is NOT "manual first, scheduled second" — the
    pre-check catches that. It is:

      1. the scheduled job passes the pre-check (nothing running) and starts
      2. a manual scrape claims the scraper part-way through and buffers rows
      3. the scheduled job finishes and clears results it no longer owns

    Old code cleared unconditionally in on_complete *and* after run_scrape, so
    the manual run's rows vanished. The fix returns early on
    ScrapeAlreadyRunning and only clears when its own claim succeeded.
    """
    import app.scheduler as sch
    from app.models import Product

    db.add(Product(asin="B0SCHED0002", title="probe", is_active=True))
    await db.commit()

    scrape_state.reset()  # pre-check will pass

    async def manual_run_steals_the_scraper(asins, on_result=None, on_complete=None):
        # Stand in for run_scrape: a manual scrape got the claim first, so the
        # real engine raises before touching state.
        scrape_state.running = True
        scrape_state.results.extend({"asin": f"B0MANUAL{i:03d}"} for i in range(5))
        raise ScrapeAlreadyRunning()

    monkeypatch.setattr(sch, "run_scrape", manual_run_steals_the_scraper)

    await sch.scheduled_product_scrape()

    assert len(scrape_state.results) == 5, (
        "the scheduled job cleared results buffered by the manual scrape that "
        "beat it to the claim — ISSUE-004 has regressed"
    )
    assert scrape_state.running is True, (
        "the scheduled job reset the manual run's running flag"
    )


async def test_scheduled_scrape_does_clear_results_it_owns(monkeypatch, db):
    """The other half: when the run owns the claim, freeing memory is correct."""
    import app.scheduler as sch
    from app.models import Product

    db.add(Product(asin="B0SCHED0003", title="probe", is_active=True))
    await db.commit()

    scrape_state.reset()

    async def successful_run(asins, on_result=None, on_complete=None):
        scrape_state.results.extend({"asin": f"B0OWNED{i:03d}"} for i in range(3))
        if on_complete:
            await on_complete(scrape_state.results)

    monkeypatch.setattr(sch, "run_scrape", successful_run)

    await sch.scheduled_product_scrape()

    assert scrape_state.results == [], (
        "a scheduled run that owned the claim must free its buffer afterwards"
    )


async def test_scheduled_scrape_exits_early_when_a_scrape_is_running(monkeypatch):
    import app.scheduler as sch

    scrape_state.reset()
    scrape_state.running = True

    async def fail_if_called(*args, **kwargs):
        pytest.fail("scheduled job called run_scrape while another scrape was running")

    monkeypatch.setattr(sch, "run_scrape", fail_if_called)
    await sch.scheduled_product_scrape()


async def test_scheduled_scrape_handles_losing_the_race(monkeypatch, db):
    """Pre-check passes, then a manual run claims first — must not raise."""
    import app.scheduler as sch
    from app.models import Product

    db.add(Product(asin="B0SCHED0001", title="probe"))
    await db.commit()

    scrape_state.reset()  # pre-check will pass

    async def steal_the_claim(*args, **kwargs):
        raise ScrapeAlreadyRunning()

    monkeypatch.setattr(sch, "run_scrape", steal_the_claim)

    # Must swallow it: an unhandled exception inside an APScheduler job is
    # logged and swallowed, but it would leave the run half-done.
    await sch.scheduled_product_scrape()


async def test_scheduled_scrape_does_nothing_without_active_products(monkeypatch, db):
    import app.scheduler as sch

    scrape_state.reset()

    async def fail_if_called(*args, **kwargs):
        pytest.fail("run_scrape was called with no active products")

    monkeypatch.setattr(sch, "run_scrape", fail_if_called)
    await sch.scheduled_product_scrape()


# ─── The HTTP route ──────────────────────────────────────────────────────────

async def test_scrape_route_rejects_a_second_request_with_409(auth_client, monkeypatch):
    from app.scraper import engine

    async def never_finishing_batch(batch, state, on_result):
        await asyncio.sleep(30)

    monkeypatch.setattr(engine, "_run_batch", never_finishing_batch)

    first = await auth_client.post("/scrape", json={"asins": "B0AAAAAAAA"})
    assert first.status_code == 200, first.text

    # Let the background task claim the scraper before the second request.
    for _ in range(50):
        if scrape_state.running:
            break
        await asyncio.sleep(0.01)
    assert scrape_state.running, "background scrape task never claimed the scraper"

    second = await auth_client.post("/scrape", json={"asins": "B0BBBBBBBB"})
    assert second.status_code == 409
    assert "already in progress" in second.json()["error"]

    await auth_client.post("/stop")


async def test_stop_clears_running_state(auth_client, monkeypatch):
    from app.scraper import engine

    async def never_finishing_batch(batch, state, on_result):
        await asyncio.sleep(30)

    monkeypatch.setattr(engine, "_run_batch", never_finishing_batch)

    await auth_client.post("/scrape", json={"asins": "B0AAAAAAAA"})
    for _ in range(50):
        if scrape_state.running:
            break
        await asyncio.sleep(0.01)

    r = await auth_client.post("/stop")
    assert r.status_code == 200
    assert r.json()["message"] == "Scrape stopped"


async def test_stop_is_safe_when_nothing_is_running(auth_client):
    r = await auth_client.post("/stop")
    assert r.status_code == 200
    assert r.json()["message"] == "No scrape running"
