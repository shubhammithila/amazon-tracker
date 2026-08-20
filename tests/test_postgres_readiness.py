"""Can this schema and these queries run on PostgreSQL as well as SQLite?

The app is moving from SQLite to RDS PostgreSQL. The suite deliberately stays on
in-memory SQLite — it is fast and needs no running database — so nothing else in these
1092 tests would notice a Postgres-only problem until a deploy.

These tests close that gap **without requiring a Postgres server**: SQLAlchemy can compile
DDL and queries against a dialect without connecting to anything, which catches the class
of error that actually bites (a type or construct one dialect cannot express). What it
cannot catch is runtime behaviour — strict typing, transaction isolation, `Decimal` vs
`float` on the way back out. Those are covered by the copy script's value checks and by
the pre-cutover dry run against real RDS, and that division is deliberate rather than an
oversight.

**`tracker.db` remains the rollback path**, so "works on SQLite" is not a legacy concern
to be dropped once Postgres is live — both dialects have to keep working.
"""
import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import Base

pytestmark = pytest.mark.regression

DIALECTS = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}

#: Every table in the metadata, by name, so a failure names the table rather than an index.
TABLES = sorted(t.name for t in Base.metadata.sorted_tables)


def test_the_schema_has_the_tables_we_expect():
    """A guard on the guard below: parametrising over an empty list passes silently.

    If `Base.metadata` were somehow not populated at import time, every dialect test would
    be skipped with a green tick and the whole file would prove nothing.
    """
    assert len(TABLES) >= 17, f"only {len(TABLES)} tables found: {TABLES}"
    for expected in (
        "invoices", "shipment_plans", "shipment_packing_days",
        "shipment_packing_entries", "shipment_plan_items", "product_prices", "users",
    ):
        assert expected in TABLES, f"{expected} missing from metadata"


@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
@pytest.mark.parametrize("table_name", TABLES)
def test_every_table_compiles_for_both_dialects(table_name, dialect_name):
    """CREATE TABLE must be expressible in both dialects.

    This is what catches a column type that only one backend has. SQLite is loosely
    typed and forgiving; Postgres is not, so a type that SQLite silently accepts can fail
    at deploy time on a box with real data on it.
    """
    table = Base.metadata.tables[table_name]
    ddl = str(CreateTable(table).compile(dialect=DIALECTS[dialect_name]))
    assert table_name in ddl


@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
def test_every_index_compiles_for_both_dialects(dialect_name):
    """Including the UNIQUE ones, which are load-bearing rather than decorative.

    `idx_packing_days_plan_date` (plan_id, pack_date) is what turns a repeated save from a
    flaky warehouse phone into an update instead of a double-count, and
    `carry_days_to_plan` relies on it to refuse a collision. An index that failed to
    create on Postgres would remove a real safety property, silently.
    """
    dialect = DIALECTS[dialect_name]
    seen = 0
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            str(CreateIndex(index).compile(dialect=dialect))
            seen += 1
    assert seen >= 5, f"only {seen} indexes compiled — metadata looks incomplete"


@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
def test_the_unique_packing_day_index_is_unique_on_both(dialect_name):
    """Asserted on the emitted DDL, not on the Python object.

    `index.unique` being True proves what the model says; the compiled statement proves
    what the database will actually be told. Those are different claims, and only the
    second one protects the double-count.
    """
    from app.models import ShipmentPackingDay

    index = next(
        i for i in ShipmentPackingDay.__table__.indexes
        if i.name == "idx_packing_days_plan_date"
    )
    ddl = str(CreateIndex(index).compile(dialect=DIALECTS[dialect_name]))
    assert "UNIQUE" in ddl.upper(), (
        f"the (plan_id, pack_date) index is not UNIQUE on {dialect_name}; a repeated "
        "save from the warehouse would double-count units"
    )


@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
def test_the_plan_item_select_compiles_for_both_dialects(dialect_name):
    """The single SELECT of plan items — the one ORDER BY the whole app depends on.

    `repository.load_plan_items` is the only place plan items are read, and its ORDER BY
    (brand → category → product → weight → ASIN) is what stops the screen and the five
    downloads disagreeing about row order. It uses an OUTER JOIN and COALESCE over a
    joined column, which is the most complex construct in the codebase, so it is the query
    most worth compiling against both backends.
    """
    from sqlalchemy import func, select

    from app.models import ProductCategory, ShipmentPlanItem
    from app.shipment import logic

    priority = func.coalesce(ProductCategory.priority, logic.DEFAULT_CATEGORY)
    query = (
        select(ShipmentPlanItem, priority.label("category_rank"))
        .outerjoin(
            ProductCategory,
            ProductCategory.product_key == ShipmentPlanItem.sort_product,
        )
        .where(ShipmentPlanItem.plan_id == 1)
        .where(ShipmentPlanItem.excluded_at.is_(None))
        .order_by(
            ShipmentPlanItem.brand_rank,
            priority,
            ShipmentPlanItem.sort_product,
            ShipmentPlanItem.weight,
            ShipmentPlanItem.asin,
        )
    )
    sql = str(query.compile(dialect=DIALECTS[dialect_name]))
    assert "LEFT OUTER JOIN" in sql.upper()
    assert "ORDER BY" in sql.upper()


@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
def test_the_plan_summary_aggregate_compiles_for_both_dialects(dialect_name):
    """`list_plans`' subquery-plus-outer-join, which powers the history screen.

    It aggregates in SQL rather than per plan, and uses an OUTER join so a plan with no
    packing days still appears — a freshly generated draft has none, and an inner join
    would hide it. Worth compiling on both, because this is the only aggregate subquery
    in the codebase.
    """
    from sqlalchemy import func, select

    from app.models import ShipmentPackingDay, ShipmentPlan

    totals = (
        select(
            ShipmentPackingDay.plan_id.label("plan_id"),
            func.count(ShipmentPackingDay.id).label("days"),
            func.coalesce(func.sum(ShipmentPackingDay.total_units), 0).label("units"),
        )
        .group_by(ShipmentPackingDay.plan_id)
        .subquery()
    )
    query = (
        select(ShipmentPlan, totals.c.days, totals.c.units)
        .outerjoin(totals, totals.c.plan_id == ShipmentPlan.id)
        .order_by(ShipmentPlan.id.desc())
    )
    sql = str(query.compile(dialect=DIALECTS[dialect_name])).upper()
    assert "LEFT OUTER JOIN" in sql
    assert "COALESCE" in sql


def test_the_postgres_driver_is_installed_and_the_url_parses():
    """asyncpg must be importable, and the production URL shape must resolve to it.

    Checked because the failure mode is a deploy-time ModuleNotFoundError on a box with
    real data on it, at the moment the app is being restarted — the worst time to discover
    a missing dependency.
    """
    import asyncpg  # noqa: F401
    from sqlalchemy.engine import make_url

    url = make_url("postgresql+asyncpg://u:p@example.invalid:5432/tracker")
    assert url.get_backend_name() == "postgresql"
    assert url.get_driver_name() == "asyncpg"
    # And the SQLite rollback path still resolves, because tracker.db stays as the way back.
    sqlite_url = make_url("sqlite+aiosqlite:///./tracker.db")
    assert sqlite_url.get_driver_name() == "aiosqlite"


def test_asyncpg_is_pinned_in_requirements():
    """A driver that works locally and is absent from requirements.txt is a failed deploy.

    `deploy/update-ec2.sh` installs only what is missing, reading this file — so an
    unpinned driver is simply never installed on the server.
    """
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "asyncpg==" in text, "asyncpg is not pinned in requirements.txt"
    assert "aiosqlite==" in text, (
        "aiosqlite was removed, but tracker.db is the documented rollback path and the "
        "test suite runs on SQLite"
    )
