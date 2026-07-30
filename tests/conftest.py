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
