"""The Portfolio routes, the stored snapshot, and the decision record.

Covers the three defects this app has already shipped once each and must not ship again:
Decimal reaching `JSONResponse`, a datetime reaching `JSONResponse`, and an N+1 query pattern
inside a page render.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.models import EconomicsSnapshot, Product, ProductDecision, RatingHistory
from app.portfolio import repository

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"
WINDOW = ("2026-07-28", "2026-08-26")


def _rows():
    return json.loads((FIXTURES / "economics_rows.json").read_text(encoding="utf-8"))


async def _seed_snapshot(db, rows=None):
    stored = await repository.save_snapshot(db, WINDOW[0], WINDOW[1], rows or _rows())
    return stored


async def _seed_rating(db, asin, rating, count, *, days_ago=0):
    product = Product(asin=asin, title=f"title {asin}", first_seen=datetime.utcnow())
    db.add(product)
    await db.flush()
    db.add(RatingHistory(
        product_id=product.id, rating=rating, rating_count=count,
        scraped_at=datetime.utcnow() - timedelta(days=days_ago),
    ))
    await db.commit()
    return product


# ─── The snapshot round trip ─────────────────────────────────────────────────


async def test_the_snapshot_round_trips_through_the_database(db):
    """Stored rows must come back in Amazon's own shape.

    **One input format for `logic.portfolio`, whether the rows arrived from the API a second ago
    or from the database.** A second code path for stored rows is how a cached dashboard starts
    disagreeing with a freshly refreshed one.
    """
    rows = _rows()
    stored = await _seed_snapshot(db, rows)
    assert stored == len(rows)

    back = await repository.load_snapshot(db)
    assert len(back) == len(rows)

    # The nested shape logic.size_row expects.
    sample = back[0]
    assert "childAsin" in sample and "parentAsin" in sample
    assert "orderedProductSales" in sample["sales"]
    assert isinstance(sample["fees"], list)
    assert isinstance(sample["ads"], list)
    assert "total" in sample["netProceeds"]


async def test_the_same_window_upserts_rather_than_doubling_the_portfolio(db):
    """Pressing Refresh twice in a morning must correct the figures, not duplicate them."""
    from sqlalchemy import func, select

    rows = _rows()
    for _ in range(3):
        await _seed_snapshot(db, rows)

    count = (await db.execute(select(func.count()).select_from(EconomicsSnapshot))).scalar()
    assert count == len({r["childAsin"] for r in rows}), (
        f"{count} rows stored for {len(rows)} products — the upsert is inserting"
    )


async def test_no_decimal_survives_the_load(db):
    """**A Decimal reaching JSONResponse is a 500 and a blank screen.**

    This app has shipped that defect twice — datetimes on the orders payload, then `raw_kg` on
    the purchasing view — and both were found in a browser on production. `load_snapshot` casts
    once so every route inherits the fix rather than remembering it.
    """
    await _seed_snapshot(db)
    back = await repository.load_snapshot(db)
    json.dumps(back)            # would raise on a Decimal


async def test_the_latest_window_is_the_newest_END_date(db):
    """Ordered by window_end, not by fetched_at.

    Re-running an OLDER window — a deliberate look at last month, say — would otherwise become
    "the latest" and shift the whole dashboard backwards in time without saying so.
    """
    rows = _rows()[:2]
    await repository.save_snapshot(db, "2026-06-01", "2026-06-30", rows)
    await repository.save_snapshot(db, "2026-07-28", "2026-08-26", rows)
    # Stored last, but covers an older period.
    await repository.save_snapshot(db, "2026-05-01", "2026-05-31", rows)

    assert await repository.latest_window(db) == ("2026-07-28", "2026-08-26")


# ─── Ratings: one query, not one per product ─────────────────────────────────


async def test_the_latest_rating_per_product_comes_back(db):
    """Only the newest scrape per product, not every row in the history."""
    product = await _seed_rating(db, "B0RATE0001", 3.8, 100, days_ago=30)
    db.add(RatingHistory(
        product_id=product.id, rating=4.3, rating_count=180, scraped_at=datetime.utcnow()
    ))
    await db.commit()

    ratings = await repository.load_ratings(db)
    assert ratings["B0RATE0001"]["rating"] == 4.3
    assert ratings["B0RATE0001"]["rating_count"] == 180


async def test_ratings_load_in_ONE_query_regardless_of_catalogue_size(db):
    """**The N+1 the old tab had.**

    It ran two queries per ASIN inside a loop: at 262 products that is 524 round trips to render
    one page, and it grew with the catalogue. Asserted by COUNTING queries rather than by timing,
    so it fails deterministically if someone reintroduces the loop.
    """
    for index in range(12):
        await _seed_rating(db, f"B0RATE{index:04d}", 4.0 + index / 100, 50 + index)

    # Counted by wrapping the session's own execute, which works regardless of how the engine
    # is wired — the previous version reached for `engine.sync_engine` and broke on the test
    # engine, testing the harness rather than the code.
    seen = []
    original = db.execute

    async def counting_execute(statement, *args, **kwargs):
        seen.append(str(statement))
        return await original(statement, *args, **kwargs)

    db.execute = counting_execute
    try:
        ratings = await repository.load_ratings(db)
    finally:
        db.execute = original

    assert len(ratings) == 12
    assert len(seen) == 1, (
        f"{len(seen)} queries to load 12 ratings — the per-ASIN loop is back:\n"
        + "\n".join(q[:120] for q in seen[:4])
    )


async def test_a_rating_is_a_float_not_a_decimal(db):
    """`Numeric(2, 1)` returns Decimal, which JSONResponse cannot serialise."""
    await _seed_rating(db, "B0RATE0001", 4.2, 477)
    ratings = await repository.load_ratings(db)
    assert isinstance(ratings["B0RATE0001"]["rating"], float)
    json.dumps(ratings)


# ─── Decisions ───────────────────────────────────────────────────────────────


async def test_a_decision_is_stored_and_clearing_it_deletes_the_row(db):
    """Absence is the single representation of "not decided".

    A stored empty decision would be a second way to say the same thing, and no stale note may
    sit behind a cleared flag — the same rule the per-order packed tick follows.
    """
    from sqlalchemy import func, select

    await repository.save_decision(
        db, "B0PARENT01", "kill", note="ads off, sell through", decided_by="owner"
    )
    stored = await repository.load_decisions(db)
    assert stored["B0PARENT01"]["decision"] == "kill"
    assert stored["B0PARENT01"]["note"] == "ads off, sell through"
    assert stored["B0PARENT01"]["decided_at"], "no timestamp, so it cannot be reviewed later"

    await repository.save_decision(db, "B0PARENT01", "")
    assert await repository.load_decisions(db) == {}
    count = (await db.execute(select(func.count()).select_from(ProductDecision))).scalar()
    assert count == 0


async def test_re_deciding_replaces_rather_than_appending(db):
    """One standing decision per product, so the dashboard never guesses which row is current."""
    from sqlalchemy import func, select

    for decision in ("watch", "keep", "kill"):
        await repository.save_decision(db, "B0PARENT01", decision, note=decision)

    count = (await db.execute(select(func.count()).select_from(ProductDecision))).scalar()
    assert count == 1
    assert (await repository.load_decisions(db))["B0PARENT01"]["decision"] == "kill"


# ─── The routes ──────────────────────────────────────────────────────────────


async def test_the_portfolio_route_returns_products_and_totals(auth_client, db):
    """One payload behind the screen and the export, so they cannot disagree."""
    await _seed_snapshot(db)
    response = await auth_client.get("/portfolio")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["parents"], "no products in the payload"
    assert body["totals"]["parents"] == len(body["parents"])
    assert "verdicts" in body["totals"]
    assert body["pre_cogs"] is True, (
        "the payload does not flag that margins exclude manufacturing cost, so an export "
        "could present the number without the caveat"
    )
    for parent in body["parents"]:
        assert parent["verdict"]
        assert parent["verdict_reason"]


async def test_the_whole_payload_is_json_safe(auth_client, db):
    """Decimal AND datetime, at the HTTP boundary rather than only in the repository."""
    await _seed_snapshot(db)
    await _seed_rating(db, "B0CWGXYLT6", 4.0, 355)
    await repository.save_decision(db, "B0DR322QYG", "watch", note="seasonal")

    response = await auth_client.get("/portfolio")
    assert response.status_code == 200, response.text
    json.dumps(response.json())


async def test_an_empty_database_renders_rather_than_erroring(auth_client, db):
    """Before the first refresh there is nothing stored, and that must not 500."""
    response = await auth_client.get("/portfolio")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["parents"] == []
    assert body["totals"]["parents"] == 0


async def test_the_decision_route_records_and_clears(auth_client, db):
    """The owner's judgement, saved and revocable."""
    await _seed_snapshot(db)
    response = await auth_client.post(
        "/portfolio/decision",
        json={"parent_asin": "B0DR322QYG", "decision": "kill", "note": "ads off"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["decisions"]["B0DR322QYG"]["decision"] == "kill"

    cleared = await auth_client.post(
        "/portfolio/decision", json={"parent_asin": "B0DR322QYG", "decision": ""}
    )
    assert cleared.status_code == 200, cleared.text
    assert "B0DR322QYG" not in cleared.json()["decisions"]


async def test_a_decision_stores_the_figures_it_was_taken_on(auth_client, db):
    """**Why decisions are kept at all.**

    Revisiting a kill in three months otherwise means trusting memory about what the margin was.
    Being able to ask "I marked this KILL at -56.8%; what is it now?" is the whole reason this
    replaced a bare name in a JSON set.
    """
    await _seed_snapshot(db)
    await auth_client.post(
        "/portfolio/decision",
        json={"parent_asin": "B0DR322QYG", "decision": "kill", "note": "why"},
    )
    stored = await repository.load_decisions(db)
    snapshot = stored["B0DR322QYG"]["snapshot"]
    assert snapshot, "no figures were recorded with the decision"
    assert "verdict" in snapshot and "net_pct" in snapshot and "tacos" in snapshot


async def test_an_unknown_decision_is_refused(auth_client, db):
    """A typo must not create a fourth category the dashboard can neither filter nor count."""
    response = await auth_client.post(
        "/portfolio/decision", json={"parent_asin": "B0DR322QYG", "decision": "delete"}
    )
    assert response.status_code == 400, response.text
    assert "kill" in response.json()["error"]


async def test_a_decision_without_a_product_is_refused(auth_client, db):
    response = await auth_client.post("/portfolio/decision", json={"decision": "kill"})
    assert response.status_code == 400, response.text


async def test_a_malformed_decision_body_is_refused(auth_client, db):
    response = await auth_client.post(
        "/portfolio/decision", content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400, response.text


async def test_the_refresh_status_route_is_json_safe(auth_client, db):
    """The status carries datetimes, and this is the route polled while a refresh runs.

    The orders feature shipped exactly this bug on exactly this kind of path — the one that fires
    when someone is trying to find out what is happening.
    """
    from app.portfolio import refresh

    refresh.reset_state()
    refresh.STATE.update({"running": True, "started_at": datetime.utcnow()})
    try:
        response = await auth_client.get("/portfolio/refresh-status")
        assert response.status_code == 200, response.text
        json.dumps(response.json())
        assert isinstance(response.json()["started_at"], str)
    finally:
        refresh.reset_state()


async def test_a_second_refresh_is_refused_while_one_runs(auth_client, db):
    """Two concurrent Data Kiosk queries would spend two minutes to store the same rows."""
    from app.portfolio import refresh

    refresh.reset_state()
    refresh.STATE.update({"running": True, "started_at": datetime.utcnow()})
    try:
        response = await auth_client.post("/portfolio/refresh")
        assert response.status_code == 409, response.text
        json.dumps(response.json())
    finally:
        refresh.reset_state()


async def test_the_portfolio_downloads_as_a_workbook(auth_client, db):
    """Built through the same aggregation as the screen, so the file cannot disagree."""
    await _seed_snapshot(db)
    response = await auth_client.get("/portfolio/download.xlsx")
    assert response.status_code == 200, response.text
    assert response.content[:2] == b"PK", "not a workbook"
    assert len(response.content) > 1000


async def test_the_export_states_that_margins_are_pre_cogs(auth_client, db):
    """A file leaves the app and gets read without the screen's banner beside it.

    So the caveat travels IN the document — otherwise a spreadsheet showing "+8.8% net" gets
    forwarded to someone who reads it as profit.
    """
    import io

    from openpyxl import load_workbook

    await _seed_snapshot(db)
    response = await auth_client.get("/portfolio/download.xlsx")
    book = load_workbook(io.BytesIO(response.content))
    text = " ".join(
        str(cell.value or "")
        for row in book.active.iter_rows(max_row=6)
        for cell in row
    )
    assert "PRE-COGS" in text.upper(), text[:300]


async def test_the_portfolio_pages_are_gated_on_the_area(client, db):
    """Unauthenticated requests must not reach the figures."""
    for path in ("/portfolio", "/portfolio/refresh-status", "/portfolio/download.xlsx"):
        response = await client.get(path)
        assert response.status_code in (303, 401, 403), f"{path} -> {response.status_code}"


# ─── The refresh job ─────────────────────────────────────────────────────────


async def test_the_refresh_stores_what_it_fetched(monkeypatch, db_schema):
    """The job's whole point: fetch, store, and record that it ran."""
    from app.database import async_session
    from app.portfolio import economics, refresh

    rows = _rows()

    async def fake_fetch(**kwargs):
        on_progress = kwargs.get("on_progress")
        if on_progress:
            on_progress("poll", 1, 2)
        return rows, WINDOW[0], WINDOW[1]

    monkeypatch.setattr(economics, "fetch_economics", fake_fetch)
    refresh.reset_state()
    result = await refresh.run(db_factory=async_session)

    assert result["phase"] == "done", result
    assert result["rows"] == len(rows)
    assert result["percent"] == 100
    assert result["running"] is False

    async with async_session() as session:
        assert len(await repository.load_snapshot(session)) == len(rows)
        last = await repository.last_refresh(session)
        assert last and last["rows_stored"] == len(rows)


async def test_a_failed_refresh_is_recorded_and_does_not_wedge_the_flag(monkeypatch, db_schema):
    """**A crash must not leave `running` True for the life of the process.**

    It would refuse every later refresh, including the nightly one, and the only symptom would be
    a dashboard that quietly stopped updating.
    """
    from app.database import async_session
    from app.portfolio import economics, refresh
    from app.shipment.spapi import SpApiError

    async def fake_fetch(**kwargs):
        raise SpApiError("Amazon said no")

    monkeypatch.setattr(economics, "fetch_economics", fake_fetch)
    refresh.reset_state()
    result = await refresh.run(db_factory=async_session)

    assert result["phase"] == "failed"
    assert "Amazon said no" in result["error"]
    assert result["running"] is False, "the running flag stayed set after a failure"

    async with async_session() as session:
        last = await repository.last_refresh(session)
        assert last and "Amazon said no" in last["error"], (
            "a failed refresh left no record, so a stale dashboard cannot say why"
        )


async def test_an_unexpected_crash_also_clears_the_flag(monkeypatch, db_schema):
    """Not just SpApiError — any exception."""
    from app.database import async_session
    from app.portfolio import economics, refresh

    async def fake_fetch(**kwargs):
        raise ValueError("something nobody predicted")

    monkeypatch.setattr(economics, "fetch_economics", fake_fetch)
    refresh.reset_state()
    result = await refresh.run(db_factory=async_session)

    assert result["running"] is False
    assert result["phase"] == "failed"


async def test_a_cancelled_refresh_also_clears_the_flag(monkeypatch, db_schema):
    """**The hole a mutation found: `CancelledError` is a BaseException.**

    `except Exception` does not catch it, so only the `finally` clears the flag — and this job
    runs as a fire-and-forget `asyncio.create_task`, which is precisely the kind of thing that
    gets cancelled when the event loop shuts down or the task is torn down mid-flight.

    Without `finally`, `running` would stay True for the life of the process and EVERY later
    refresh — including the nightly one — would be silently refused. The only symptom would be a
    dashboard that quietly stopped updating, which is the hardest class of bug to notice here.

    The three exception tests above all passed with `finally` removed, so this is the one that
    actually pins it.
    """
    import asyncio

    from app.database import async_session
    from app.portfolio import economics, refresh

    async def fake_fetch(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(economics, "fetch_economics", fake_fetch)
    refresh.reset_state()
    try:
        await refresh.run(db_factory=async_session)
    except asyncio.CancelledError:
        pass        # re-raised on purpose: cancellation must not be swallowed

    assert refresh.STATE["running"] is False, (
        "a cancelled refresh left `running` set, so every later refresh would be refused "
        "and the dashboard would silently stop updating"
    )
    refresh.reset_state()


async def test_a_concurrent_refresh_is_refused_rather_than_raising(db_schema):
    """The nightly job overlapping a manual one is a no-op, not an error in the log."""
    from app.database import async_session
    from app.portfolio import refresh

    refresh.reset_state()
    refresh.STATE["running"] = True
    try:
        result = await refresh.run(db_factory=async_session)
        assert result["refused"] is True
    finally:
        refresh.reset_state()


def test_the_progress_bar_never_steps_backwards():
    """A bar that jumps back reads as a fault.

    It matters here because the poll phase is a fraction of a CEILING: a query finishing on
    attempt 3 of 40 would otherwise report 15% after the download phase had already reported 85%.
    """
    from app.portfolio import refresh

    refresh.reset_state()
    refresh._progress("poll", 30, 40)
    high = refresh.STATE["percent"]
    refresh._progress("poll", 1, 40)
    assert refresh.STATE["percent"] == high, "the bar went backwards"
    refresh.reset_state()
