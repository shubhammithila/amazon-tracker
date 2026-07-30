"""Shared pytest fixtures for the Amazon Tracker test suite.

Two constraints drive the layout here:

1. ``app/database.py`` builds the engine at *import* time from
   ``get_settings()``, which is ``@lru_cache``d. So the test database URL has to
   be in the environment before anything under ``app.`` is imported — hence the
   os.environ writes at module top, above the app imports.

2. ``app/main.py`` and ``app/routers/auth.py`` mount ``static/`` and
   ``templates/`` by relative path, so pytest must run with the repo root as the
   working directory. ``_chdir_to_repo_root`` enforces that instead of letting
   it fail confusingly later.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Run from the repo root so Jinja2Templates(directory="templates") resolves.
os.chdir(REPO_ROOT)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Point the app at an in-memory database and away from the developer's .env
# BEFORE importing app modules. A shared-cache in-memory URL keeps every
# connection in one database while staying off disk, so a test run can never
# touch tracker.db.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///file:testdb?mode=memory&cache=shared&uri=true"
os.environ["APP_PASSWORD"] = "test-password"
os.environ["OPS_PASSWORD"] = "test-ops-password"
os.environ["SECRET_KEY"] = "test-secret-key-not-used-in-production"
os.environ["SCHEDULER_ENABLED"] = "false"  # never start cron jobs during tests
os.environ["DATA_RETENTION_DAYS"] = "90"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import Base, engine, async_session  # noqa: E402
from app.main import app  # noqa: E402
from app.routers.auth import SESSION_COOKIE, serializer  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def db_schema():
    """Create every table, hand control to the test, then drop them all.

    Per-test rather than per-session: several tests assert on absolute row
    counts, and leaking rows between them would make failures depend on
    execution order.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db(db_schema):
    """An AsyncSession for arranging fixtures and asserting on stored rows."""
    async with async_session() as session:
        yield session


@pytest_asyncio.fixture
async def read_committed():
    """Read committed state in a brand-new session. Use this for assertions.

    Why this exists rather than just reusing the ``db`` fixture: ``db`` holds its
    own read transaction and its own identity map. When a test drives the app over
    HTTP, the app commits in a *different* session — so a later read through ``db``
    can still return the pre-request values and the assertion fails (or, worse,
    passes) for a reason that has nothing to do with the code under test.

    ``expire_all()`` and ``rollback()`` are not the fix. They mark the ORM objects
    expired, and then merely *touching* one of their attributes has to go back to
    the database; outside an ``await`` async SQLAlchemy raises MissingGreenlet.

    So: arrange with ``db``, assert through this. The callable receives a fresh
    session as its first argument::

        items = await read_committed(repository.load_plan_items, plan.id)
    """
    async def _read(fn, *args, **kwargs):
        async with async_session() as session:
            return await fn(session, *args, **kwargs)

    return _read


@pytest_asyncio.fixture
async def count_rows():
    """Count committed rows of a model, in a fresh session.

    ``await count_rows(ShipmentPackingDay, plan_id=7)``. Keyword arguments become
    equality filters. Same reasoning as ``read_committed`` — several tests assert
    "exactly one row exists", which is only meaningful against committed state.
    """
    from sqlalchemy import func as sa_func
    from sqlalchemy import select as sa_select

    async def _count(model, **filters):
        stmt = sa_select(sa_func.count()).select_from(model)
        for column, value in filters.items():
            stmt = stmt.where(getattr(model, column) == value)
        async with async_session() as session:
            result = await session.execute(stmt)
            return int(result.scalar() or 0)

    return _count


@pytest_asyncio.fixture
async def client(db_schema):
    """Unauthenticated HTTP client.

    follow_redirects stays False on purpose: the auth boundary is expressed as a
    303 to /login, and following it would turn every rejection into a 200.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac:
        yield ac


@pytest.fixture
def session_cookie():
    """A validly signed session cookie, minted the same way /login does."""
    return serializer.dumps({"authenticated": True})


@pytest_asyncio.fixture
async def auth_client(db_schema, session_cookie):
    """HTTP client carrying a valid session cookie."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac:
        ac.cookies.set(SESSION_COOKIE, session_cookie)
        yield ac


@pytest.fixture
def ops_cookie():
    """A validly signed cookie carrying the ops role.

    Contrast with ``session_cookie``, which is the pre-roles payload and must
    therefore still resolve to admin.
    """
    return serializer.dumps({"authenticated": True, "role": "ops"})


@pytest_asyncio.fixture
async def ops_client(db_schema, ops_cookie):
    """HTTP client authenticated as the operations employee."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as ac:
        ac.cookies.set(SESSION_COOKIE, ops_cookie)
        yield ac


@pytest_asyncio.fixture
async def plan_factory(db):
    """Create an active shipment plan with items, via the real repository.

    Goes through ``repository.create_plan`` rather than inserting rows directly
    so the tests exercise the same write path the app uses — including the
    casefolded ``sort_product`` that the canonical ORDER BY depends on. A test
    that hand-built rows could pass while the real code stored them wrongly.

    Weights and names are chosen so product-then-weight ordering is observable,
    and so that a *case-sensitive* sort produces a visibly different order.

    That second property needs care. SQLite's default collation is binary, so
    every capital letter sorts before every lowercase one. A lowercase name that
    is also last alphabetically — 'jau sattu' here — therefore lands in the same
    position under both collations and proves nothing. The lowercase name has to
    be one that belongs FIRST: 'aloe vera juice' sorts first when casefolded, but
    last under binary collation, so dropping the casefold reorders the list and
    the ordering tests fail. Verified by mutation: pointing the ORDER BY at the
    raw column makes tests/test_shipment_plan_db.py fail.
    """
    from app.shipment import repository

    DEFAULT_ITEMS = [
        {"asin": "B0AAA00001", "item": "Chana Sattu", "weight": 1.0,
         "brand": "MF", "fba_sku": "MF-CH-1KG", "shipment_plan": 500, "deficit": 480},
        {"asin": "B0AAA00002", "item": "Chana Sattu", "weight": 0.5,
         "brand": "MF", "fba_sku": "MF-CH-500G", "shipment_plan": 300, "deficit": 290},
        {"asin": "B0BBB00001", "item": "jau sattu", "weight": 1.0,
         "brand": "MF", "fba_sku": "MF-JAU-1KG", "shipment_plan": 200, "deficit": 150},
        # Lowercase on purpose, and first alphabetically — see the docstring.
        {"asin": "B0CCC00001", "item": "aloe vera juice", "weight": 2.0,
         "brand": "HF", "fba_sku": "HF-ALOE-2L", "shipment_plan": 0, "deficit": -50},
    ]

    async def _make(items=None, multiplier=5.0, min_cartons=25, min_units=500):
        return await repository.create_plan(
            db,
            items if items is not None else [dict(i) for i in DEFAULT_ITEMS],
            multiplier=multiplier,
            min_cartons=min_cartons,
            min_units=min_units,
        )

    return _make


@pytest.fixture
def valid_invoice_payload():
    """The minimum payload /invoice/save should accept.

    Kept in one place so the validation tests can subtract fields from a known
    good baseline rather than each guessing at what "valid" means.
    """
    return {
        "details": {
            "shipment_id": "FBA15TEST001",
            "date": "2026-07-30",
            "fc_code": "ISK3",
            "place_of_supply": "Maharashtra",
            "transporter": "VRL Logistics",
        },
        "supplier": {"gstin": "20AAFCF9848M1Z7"},
        "recipient": {"gstin": "27AAFCF9848M1ZT"},
        "items": [
            {"description": "Chana Sattu 1kg", "quantity": 10, "rate": 100.0, "gst_rate": 5},
            {"description": "Jau Sattu 500g", "quantity": 4, "rate": 60.0, "gst_rate": 5},
        ],
    }


@pytest_asyncio.fixture(autouse=True)
async def reset_scrape_state():
    """Reset the module-level scrape state around every test.

    ``app/scraper/engine.py`` exposes a single global ``scrape_state`` shared by
    the manual route, the scheduler and the WebSocket. Without this, a test that
    leaves ``running=True`` makes later tests fail for the wrong reason.
    """
    from app.scraper.engine import scrape_state

    scrape_state.reset()
    yield
    scrape_state.reset()
