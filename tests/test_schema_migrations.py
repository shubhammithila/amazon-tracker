"""Regression: ISSUE-001 — schema drift silently killed the /products router.

Found by /qa on 2026-07-30.
Report: .gstack/qa-reports/qa-report-amazon-tracker-2026-07-30.md

The model gained ``products.use_by`` but the database never did, so every
/products endpoint returned 500 with ``no such column: products.use_by``.
``create_all()`` cannot add a column to a table that already exists, and three
separate things stopped Alembic from covering for it:

  * ``alembic/script.py.mako`` was missing, so ``alembic revision`` crashed
  * ``.gitignore`` excluded ``alembic/versions/*.py``, so no migration could ship
  * nothing documented that migrations had to run at all

The important test here is ``test_migrations_match_models``: it fails on the
*next* model change that lacks a migration, which is the actual class of bug.
"""
import configparser
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.database import Base

REPO_ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.regression


# ─── The pipeline that was broken ────────────────────────────────────────────

def test_alembic_script_template_exists():
    """`alembic revision` crashes with FileNotFoundError without this file."""
    template = REPO_ROOT / "alembic" / "script.py.mako"
    assert template.is_file(), (
        "alembic/script.py.mako is missing — `alembic revision` will fail and no "
        "migration can be generated for a model change."
    )


def test_migrations_are_not_gitignored():
    """.gitignore used to exclude alembic/versions/*.py.

    A gitignored migration cannot reach production, so the schema change is
    invisible to every deploy — which is how ISSUE-001 shipped.
    """
    patterns = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    offenders = [
        line for line in patterns
        if line.strip().startswith("alembic/versions") and not line.strip().startswith("!")
    ]
    assert not offenders, (
        f".gitignore excludes migrations ({offenders}) — they can never be "
        "committed, so production will never learn about schema changes."
    )


def test_at_least_one_migration_exists():
    versions = REPO_ROOT / "alembic" / "versions"
    revisions = [p for p in versions.glob("*.py") if p.name != "__init__.py"]
    assert revisions, "No Alembic revisions exist; the schema has no migration path."


def test_migration_history_is_linear_and_reaches_a_single_head():
    """Two heads mean `alembic upgrade head` is ambiguous and can half-apply."""
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"Expected exactly one migration head, found {heads}."


# ─── The drift guard: the test that catches the *next* ISSUE-001 ─────────────

def _upgrade_to_head_on(sync_url: str) -> None:
    """Run every migration against a throwaway synchronous SQLite file."""
    from alembic import command

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    # env.py reads the URL from settings, so override it explicitly. The async
    # driver is swapped for the sync one because this runs outside an event loop.
    cfg.set_main_option("sqlalchemy.url", sync_url)
    command.upgrade(cfg, "head")


def test_migrations_match_models(tmp_path, monkeypatch):
    """Applying all migrations must reproduce app/models.py exactly.

    This is the regression guard that matters. If someone adds a column to
    models.py without generating a migration, compare_metadata() reports the
    difference and this fails — instead of a 500 appearing in production weeks
    later, which is exactly how ISSUE-001 reached a live deployment.
    """
    db_file = tmp_path / "migrated.db"
    sync_url = f"sqlite:///{db_file.as_posix()}"

    # env.py builds its own engine from settings; point that at the temp file
    # too so `alembic upgrade` and the comparison below see one database.
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    import app.config
    app.config.get_settings.cache_clear()
    try:
        _upgrade_to_head_on(sync_url)

        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                ctx = MigrationContext.configure(
                    conn, opts={"compare_type": True, "target_metadata": Base.metadata}
                )
                diff = compare_metadata(ctx, Base.metadata)
        finally:
            engine.dispose()
    finally:
        app.config.get_settings.cache_clear()

    # Alembic reports index differences on SQLite that are cosmetic; only
    # table/column drift indicates a real missing migration.
    significant = [
        d for d in diff
        if isinstance(d, tuple) and d and str(d[0]).startswith(
            ("add_table", "remove_table", "add_column", "remove_column", "modify_type")
        )
    ]
    assert not significant, (
        "app/models.py and the Alembic migrations disagree:\n"
        + "\n".join(f"  {d}" for d in significant)
        + "\n\nRun: alembic revision --autogenerate -m 'describe the change'"
    )


def test_products_table_has_use_by_after_migrations(tmp_path, monkeypatch):
    """The exact column whose absence 500'd the /products router."""
    db_file = tmp_path / "usebycheck.db"
    sync_url = f"sqlite:///{db_file.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    import app.config
    app.config.get_settings.cache_clear()
    try:
        _upgrade_to_head_on(sync_url)
        engine = create_engine(sync_url)
        try:
            columns = {c["name"] for c in inspect(engine).get_columns("products")}
        finally:
            engine.dispose()
    finally:
        app.config.get_settings.cache_clear()

    assert "use_by" in columns, (
        "products.use_by is absent after `alembic upgrade head` — this is the "
        "exact drift that made /products, /products/{asin}/history, "
        "/products/{asin}/sellers and /products/download all return 500."
    )


def test_alembic_env_uses_batch_mode_for_sqlite():
    """SQLite has no ALTER COLUMN; without render_as_batch, migrations fail."""
    env = (REPO_ROOT / "alembic" / "env.py").read_text(encoding="utf-8")
    assert env.count("render_as_batch=True") >= 2, (
        "alembic/env.py must set render_as_batch=True in both the online and "
        "offline paths, or any future ALTER/DROP COLUMN migration fails on SQLite."
    )


def test_running_migrations_does_not_disable_application_logging(tmp_path, monkeypatch):
    """An in-process `alembic upgrade` must not switch off `app.*` loggers.

    env.py calls logging.config.fileConfig, which defaults to
    disable_existing_loggers=True and would then disable every logger not named
    in alembic.ini — and alembic.ini names only root, sqlalchemy and alembic.

    This is not hypothetical tidiness. It cost a genuinely confusing failure:
    tests/test_shipment_documents.py's missing-SKU warning test passed on its own
    and failed in the full suite, because this file had run first and killed
    `app.shipment.documents`. A production startup hook that runs migrations
    would silence application logging the same way, and nobody would notice until
    they went looking for a warning that was never emitted.

    Asserted behaviourally rather than by grepping env.py for the keyword, so it
    still holds if the logging setup is restructured.
    """
    import logging

    probe = logging.getLogger("app.shipment.documents")
    assert not probe.disabled, "the probe logger was already disabled before this test"

    db_file = tmp_path / "logcheck.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    import app.config
    app.config.get_settings.cache_clear()
    try:
        _upgrade_to_head_on(f"sqlite:///{db_file.as_posix()}")
    finally:
        app.config.get_settings.cache_clear()

    assert not probe.disabled, (
        "running migrations disabled the app.shipment.documents logger — "
        "alembic/env.py must call fileConfig(..., disable_existing_loggers=False)"
    )

    # Belt and braces: the logger being enabled is only useful if a record
    # actually reaches a handler afterwards.
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Capture()
    probe.addHandler(handler)
    try:
        probe.warning("probe %d", 1)
    finally:
        probe.removeHandler(handler)
    assert records == ["probe 1"], f"logging is broken after migrations: {records}"


def test_alembic_ini_script_location_resolves():
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "alembic.ini", encoding="utf-8")
    location = parser["alembic"]["script_location"]
    assert (REPO_ROOT / location).is_dir(), f"script_location '{location}' does not exist."


# ─── The endpoints that were dead ────────────────────────────────────────────

def test_shipment_tables_and_unique_indexes_after_migrations(tmp_path, monkeypatch):
    """The shipment workflow's concurrency guard lives in the schema.

    idx_packing_days_plan_date and idx_packing_entries_day_asin being UNIQUE is
    what turns an ops double-save into an upsert instead of double-counted
    units. If either index loses its unique flag in a future migration, packing
    totals silently double — so pin it here, at the schema level.
    """
    db_file = tmp_path / "shipcheck.db"
    sync_url = f"sqlite:///{db_file.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    import app.config
    app.config.get_settings.cache_clear()
    try:
        _upgrade_to_head_on(sync_url)
        engine = create_engine(sync_url)
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            expected = {
                "shipment_plans", "shipment_plan_items",
                "shipment_packing_days", "shipment_packing_entries",
            }
            missing = expected - tables
            assert not missing, f"missing shipment tables after migration: {missing}"

            day_indexes = {
                ix["name"]: ix["unique"]
                for ix in inspector.get_indexes("shipment_packing_days")
            }
            entry_indexes = {
                ix["name"]: ix["unique"]
                for ix in inspector.get_indexes("shipment_packing_entries")
            }
        finally:
            engine.dispose()
    finally:
        app.config.get_settings.cache_clear()

    assert day_indexes.get("idx_packing_days_plan_date"), (
        "idx_packing_days_plan_date must be UNIQUE — one packing day per plan per date"
    )
    assert entry_indexes.get("idx_packing_entries_day_asin"), (
        "idx_packing_entries_day_asin must be UNIQUE — a double-save must upsert, "
        "not double-count units"
    )


async def test_products_endpoints_do_not_500(auth_client):
    """All four /products endpoints returned 500 before ISSUE-001 was fixed."""
    for path in ("/products", "/products/download"):
        r = await auth_client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"


async def test_products_list_selects_use_by_column(auth_client, db):
    """Exercises the SELECT that raised `no such column: products.use_by`."""
    from app.models import Product

    db.add(Product(asin="B0TEST0001", title="probe", use_by="Dec 2027"))
    await db.commit()

    r = await auth_client.get("/products")
    assert r.status_code == 200, r.text
    assert [p["asin"] for p in r.json()] == ["B0TEST0001"]


async def test_product_history_and_sellers_reachable(auth_client, db):
    """These 500'd too; a missing ASIN must 404, not blow up."""
    from app.models import Product

    db.add(Product(asin="B0TEST0002", title="probe2", use_by="Jan 2028"))
    await db.commit()

    for suffix in ("history", "sellers"):
        r = await auth_client.get(f"/products/B0TEST0002/{suffix}")
        assert r.status_code == 200, f"{suffix} -> {r.status_code}: {r.text[:200]}"

        missing = await auth_client.get(f"/products/B0NOTREAL1/{suffix}")
        assert missing.status_code == 404, f"absent ASIN {suffix} -> {missing.status_code}"


async def test_product_download_returns_a_real_xlsx(auth_client, db):
    """The Excel export reads p.use_by directly, so it 500'd on the drift."""
    from app.models import Product

    db.add(Product(asin="B0TEST0003", title="probe3", use_by="Feb 2028"))
    await db.commit()

    r = await auth_client.get("/products/download")
    assert r.status_code == 200
    # XLSX files are zip archives — verify the magic bytes, not just the status.
    assert r.content[:2] == b"PK", "download did not return a valid .xlsx"




# ─── The deploy script's baseline detector, RUN rather than grepped ───────────

def _baseline_detector_source() -> str:
    """The Python heredoc out of deploy/update-ec2.sh, so it can be executed.

    Extracted rather than duplicated: a copy in this test would drift from the script and
    then pass while the real deploy failed, which is the whole failure mode being guarded.
    """
    script = (REPO_ROOT / "deploy" / "update-ec2.sh").read_text(encoding="utf-8")
    start = script.index("BASELINE=")
    body = script[script.index("\n", start) + 1:]
    # The heredoc terminator is a line that is exactly PY. Splitting on lines handles
    # CRLF, which a regex on the raw text does not — that mistake made an earlier version
    # of this test silently extract nothing.
    lines = []
    for line in body.splitlines():
        if line.strip() == "PY":
            break
        lines.append(line)
    return "\n".join(lines)


def _detected_baseline(db_path) -> str:
    """Run the real detector against a real database file and return its answer."""
    import subprocess
    import sys

    source = _baseline_detector_source()
    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True, text=True, cwd=str(db_path.parent),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_the_deploy_detector_reports_the_head_for_a_head_schema(tmp_path, monkeypatch):
    """A failed production deploy, and the test that would have prevented it.

    ``deploy/update-ec2.sh`` decides which revision production's schema already matches by
    inspecting columns, then stamps it so `upgrade head` applies only what is genuinely
    new. That guard exists because ``create_all()`` has twice outrun Alembic.

    Its newest branch was still ``users in tables -> 394fc6f28429`` after 7c1a4e9b2d38 had
    shipped. So it inspected a database already at the head, concluded it was one revision
    older, and stamped it **backwards** — and `upgrade head` then re-ran a migration whose
    columns already existed and died on "duplicate column name". The rollback worked and no
    data was lost, but the deploy failed for a reason unrelated to the code in it.

    This RUNS the detector against a fully-migrated database and asserts it says "head".
    Grepping the script for the revision id was not enough: the id also appears in a
    comment, so a substring check passed even with the branch deleted. Verified by
    deleting the branch and watching this fail.
    """
    db = tmp_path / "tracker.db"
    # `.as_posix()` AND the env var both matter: env.py builds its own engine from
    # settings, so without the override the migration runs against the real dev database
    # and this test then inspects an empty temp file — which is how the first version of
    # it reported '' and looked like a detector bug.
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db.as_posix()}")
    import app.config
    app.config.get_settings.cache_clear()
    try:
        _upgrade_to_head_on(f"sqlite:///{db.as_posix()}")
    finally:
        app.config.get_settings.cache_clear()
    assert db.exists(), "the migration did not create the temp database"

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    head = ScriptDirectory.from_config(cfg).get_current_head()

    detected = _detected_baseline(db)
    assert detected == head, (
        f"the deploy script's detector says a head schema is at {detected!r}, but the head "
        f"is {head!r}. It will stamp production BACKWARDS and the deploy will die "
        "re-applying a migration that has already run. Add a branch for the new revision, "
        "newest first, keyed on a column it specifically adds."
    )


def test_the_deploy_detector_reports_nothing_for_an_empty_database(tmp_path):
    """An empty database must migrate from scratch, not be stamped at anything.

    Stamping an empty database would skip every migration and leave the app pointing at
    tables that do not exist.
    """
    import sqlite3

    db = tmp_path / "tracker.db"
    sqlite3.connect(db).close()          # exists, no tables
    assert _detected_baseline(db) == ""
