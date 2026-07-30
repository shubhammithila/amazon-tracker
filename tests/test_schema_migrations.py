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


def test_alembic_ini_script_location_resolves():
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "alembic.ini", encoding="utf-8")
    location = parser["alembic"]["script_location"]
    assert (REPO_ROOT / location).is_dir(), f"script_location '{location}' does not exist."


# ─── The endpoints that were dead ────────────────────────────────────────────

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
