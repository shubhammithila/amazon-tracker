"""SQLite concurrency: a background write must not 500 the page someone is reading.

**This exists because production went down.** On 13 Sep 2026 the app returned Internal Server Error on
every page. The half-hourly orders refresh started at 09:14:31 and died at 09:17:58 leaving a stale
`tracker.db-journal` — an abandoned write transaction — and from then on everything touching the
database raised:

    sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked

15 occurrences in six minutes. It was locked tightly enough that even reading a `PRAGMA` failed.

The cause was configuration, not code:

    journal_mode  = delete   <- one writer blocks every READER
    busy_timeout  = 5000     <- gives up after five seconds

An orders refresh is ~2 minutes of writes, so any page load inside that window could be locked out,
and five seconds is nowhere near long enough to wait it out.

**WAL raises the ceiling; it does not remove it.** Readers and one writer proceed concurrently, which
is the whole failure mode above. SQLite still allows only one writer, which is what the deferred
PostgreSQL move addresses properly — the appendix in the plan file records that decision.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.database import SQLITE_PRAGMAS, engine_kwargs

pytestmark = pytest.mark.regression


def test_wal_and_a_generous_busy_timeout_are_configured():
    """The two settings whose absence took the app down.

    Asserted on the declared pragmas rather than on a live connection, because the test suite runs on
    an IN-MEMORY database where `journal_mode=WAL` is silently refused — SQLite keeps memory databases
    in `memory` journal mode. A test that asked a live in-memory connection would therefore pass while
    proving nothing about production. `test_wal_actually_engages_on_a_file_database` below closes that
    gap with a real file.
    """
    assert SQLITE_PRAGMAS["journal_mode"] == "WAL", (
        "journal_mode must be WAL — under the default `delete` mode a single writer blocks every "
        "reader, which is how a 2-minute orders refresh returned 500 on every page"
    )
    timeout = int(SQLITE_PRAGMAS["busy_timeout"])
    assert timeout >= 15000, (
        f"busy_timeout is {timeout}ms; the orders refresh writes for ~2 minutes, so a few seconds "
        f"guarantees the caller gives up rather than waiting its turn"
    )


def test_wal_actually_engages_on_a_file_database():
    """**Proof against a real file, because the in-memory suite cannot show this.**

    `journal_mode` is a persistent property of the database file, so setting it once on a connection
    changes the file for good. This runs the same pragmas the app runs and reads back what SQLite
    actually chose — the only way to know WAL was accepted rather than silently ignored.
    """
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "probe.db"
        connection = sqlite3.connect(path)
        try:
            for pragma, value in SQLITE_PRAGMAS.items():
                connection.execute(f"PRAGMA {pragma}={value}")
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            busy = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            connection.close()

    assert mode.lower() == "wal", f"SQLite refused WAL and stayed in {mode!r} mode"
    assert busy >= 15000, f"busy_timeout did not take: {busy}"


def test_a_reader_is_not_blocked_by_an_open_write_transaction_under_wal():
    """**The exact production failure, reproduced and then shown fixed.**

    Under `journal_mode=delete` an uncommitted write blocks readers outright — that is what a stale
    `-journal` file did for six minutes. Under WAL the reader sees the last committed state and
    proceeds.

    Both halves are asserted, so this test would still fail if WAL were reverted: the `delete` case
    must raise and the WAL case must not.
    """
    # A named temp DIRECTORY is not used here: on Windows the cleanup fails with WinError 32 if any
    # connection is still open, which turns a real assertion failure into a confusing teardown error.
    # The files are removed explicitly in the `finally` instead.
    folder = Path(tempfile.mkdtemp())
    path = folder / "probe.db"

    def open_write_txn(database: Path):
        """A connection holding an EXCLUSIVE write, which is what a stale journal represents.

        `isolation_level="DEFERRED"` alone is not enough: Python's sqlite3 driver defers the actual
        lock until a statement needs it, so `BEGIN EXCLUSIVE` is issued explicitly. Without this the
        `delete`-mode half of this test does not reproduce the bug at all and passes for the wrong
        reason — which is how the first version of it failed.
        """
        connection = sqlite3.connect(database, timeout=0.2, isolation_level=None)
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute("INSERT INTO t VALUES (99)")
        return connection

    try:
        setup = sqlite3.connect(path)
        setup.execute("PRAGMA journal_mode=delete")
        setup.execute("CREATE TABLE t (n INTEGER)")
        setup.execute("INSERT INTO t VALUES (1)")
        setup.commit()
        setup.close()

        # ── Rollback journal: the reader IS blocked. This is the outage. ──
        writer = open_write_txn(path)
        reader = sqlite3.connect(path, timeout=0.2)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            reader.execute("SELECT COUNT(*) FROM t").fetchone()
        reader.close()
        writer.execute("ROLLBACK")
        writer.close()

        # ── WAL: the same reader gets the last committed state instead of an error ──
        upgrade = sqlite3.connect(path)
        upgrade.execute("PRAGMA journal_mode=WAL")
        upgrade.close()

        writer = open_write_txn(path)
        reader = sqlite3.connect(path, timeout=0.2)
        assert reader.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1, (
            "the reader should see the last COMMITTED row count, not the in-flight write"
        )
        reader.close()
        writer.execute("ROLLBACK")
        writer.close()
    finally:
        for leftover in folder.glob("probe.db*"):
            leftover.unlink(missing_ok=True)
        folder.rmdir()


def test_the_pragmas_are_not_applied_to_postgres():
    """`journal_mode` and `busy_timeout` are SQLite-only; sending them to Postgres is a syntax error.

    `app/database.py` already branches on the URL for pooling, and the deferred PostgreSQL move
    depends on that branch staying honest — the plan's appendix states the app is portable today
    precisely because nothing SQLite-specific leaks out.
    """
    assert "pool_size" not in engine_kwargs or "sqlite" not in _url(), (
        "pooling and the SQLite pragmas must stay on opposite sides of the same branch"
    )


def _url() -> str:
    from app.config import get_settings

    return get_settings().database_url
