"""The async engine and session factory.

**The SQLite pragmas below are here because production went down without them.** On 13 Sep 2026 every
page returned Internal Server Error: the half-hourly orders refresh started at 09:14:31, died at
09:17:58 leaving a stale `tracker.db-journal`, and from then on everything touching the database
raised `sqlite3.OperationalError: database is locked` — 15 times in six minutes, tightly enough that
even reading a `PRAGMA` failed.

The cause was configuration rather than code. SQLite's default rollback journal lets **one writer
block every reader**, and the orders refresh writes for ~2 minutes, so any page load inside that
window could be locked out with only a 5-second timeout to wait it out.

WAL fixes that specific failure and does not remove SQLite's one-writer limit — that is what the
deferred PostgreSQL move addresses, and the plan's appendix records why it was set aside. Nothing here
prejudges it: the pragmas are scoped to SQLite so the Postgres path is unaffected.
"""
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

#: Applied to every new SQLite connection. **Order matters**: `journal_mode` is a persistent property
#: of the database FILE, so it is set once and then read back by the deploy check rather than assumed.
#:
#: * **`journal_mode=WAL`** — readers and one writer proceed concurrently. Under the default `delete`
#:   mode a single uncommitted write blocks every reader, which is precisely how a stale journal file
#:   turned a failed background job into a total outage. This is the load-bearing line.
#: * **`busy_timeout=30000`** — 30 seconds, up from SQLite's 5. When two writers do collide the loser
#:   should WAIT its turn rather than raise; the orders refresh is minutes long, so five seconds
#:   guaranteed a 500 instead of a pause. 30s is comfortably longer than any single statement here and
#:   still short enough that a genuine deadlock surfaces rather than hanging the request for ever.
#: * **`synchronous=NORMAL`** — the standard companion to WAL. `FULL` fsyncs on every commit, which on
#:   a t2.micro's disk is the difference between a 6-second bulk insert and a much slower one; with WAL
#:   the risk it removes is a power loss corrupting the last transaction, not the database. `update-ec2.sh`
#:   backs up before every deploy, which is the real protection.
#:
#: **SQLite-only.** `journal_mode` and `busy_timeout` are not Postgres syntax, so they are attached
#: under the same URL branch as the pooling options below — the deferred Postgres move depends on
#: nothing SQLite-specific leaking out, and `tests/test_database_locking.py` asserts that.
SQLITE_PRAGMAS = {
    "journal_mode": "WAL",
    "busy_timeout": "30000",
    "synchronous": "NORMAL",
}

_IS_SQLITE = "sqlite" in settings.database_url

engine_kwargs = {"echo": False}
if not _IS_SQLITE:
    engine_kwargs["pool_size"] = 5
    engine_kwargs["max_overflow"] = 10

engine = create_async_engine(settings.database_url, **engine_kwargs)

if _IS_SQLITE:

    @event.listens_for(engine.sync_engine, "connect")
    def _apply_sqlite_pragmas(dbapi_connection, _record):
        """Set the pragmas on each new connection.

        A `connect` event rather than a one-off statement at startup, because SQLAlchemy opens
        connections lazily and pools them: `busy_timeout` and `synchronous` are per-CONNECTION, so a
        connection created later would otherwise keep SQLite's defaults and be the one that raises.

        **An in-memory database silently refuses WAL** (SQLite keeps `:memory:` in `memory` journal
        mode), which is exactly what the test suite uses — so this must not fail when that happens.
        The pragmas are applied best-effort and the real file-backed behaviour is verified against a
        temporary file in `tests/test_database_locking.py` and again by the deploy script.
        """
        cursor = dbapi_connection.cursor()
        try:
            for pragma, value in SQLITE_PRAGMAS.items():
                cursor.execute(f"PRAGMA {pragma}={value}")
        finally:
            cursor.close()


async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with async_session() as session:
        yield session
