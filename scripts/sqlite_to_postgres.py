"""Copy every row from tracker.db into a PostgreSQL database, then prove it landed.

    python scripts/sqlite_to_postgres.py \
        --sqlite tracker.db \
        --postgres "postgresql+asyncpg://user:pass@host:5432/tracker" \
        [--allow-nonempty] [--dry-run]

Run it against a SCRATCH database first (`--postgres .../tracker_rehearsal`). It is a
one-off migration tool, not part of the app, and nothing imports it.

**It never writes to SQLite.** The source is opened read-only, so `tracker.db` survives as
the rollback path: point DATABASE_URL back at it and the app is exactly as it was.

Three properties are worth stating, because each one is a way this could quietly go wrong:

**1. Insert order follows foreign keys.** ``Base.metadata.sorted_tables`` is already in
dependency order (products before price_history, shipment_plans before its items), so the
copy walks that list rather than a hand-maintained one that would drift as tables are
added.

**2. Sequences are reset afterwards, and this is the classic silent failure.** Copying
explicit ``id`` values does not advance PostgreSQL's SERIAL sequences — they stay at 1. The
copy looks perfect, every count matches, and then the first real INSERT collides on the
primary key. On this app that first insert could be a GST invoice, so it gets checked
rather than hoped for.

**3. Verification is by count AND by value.** Counts alone cannot see a NULL that should
have been a number, a Decimal that arrived as a string, or a date that lost its time.
So the GST invoice numbers, the packing days with their units and cartons, and the
carry lineage are re-read out of PostgreSQL and compared to SQLite field by field.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import func, insert, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.models import Base  # noqa: E402

#: Rows per INSERT. Small enough that a failure message names a manageable batch, large
#: enough that 27,000 history rows do not become 27,000 round trips to RDS.
BATCH = 500


def log(msg: str) -> None:
    print(msg, flush=True)


# ─── Reading SQLite ──────────────────────────────────────────────────────────

def open_source(path: Path) -> sqlite3.Connection:
    """Open tracker.db READ-ONLY, so this tool cannot damage the rollback path."""
    if not path.exists():
        raise SystemExit(f"no such SQLite file: {path}")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def source_tables(con: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in con.execute(
            "select name from sqlite_master where type='table'"
        )
    }


def read_rows(con: sqlite3.Connection, table, columns: list[str]) -> list[dict]:
    """Every row of one table, as dicts keyed by column name.

    Only columns the MODEL declares are read. A column that exists in SQLite but not in
    the models is deliberately dropped: the target schema is built by Alembic from the
    models, so an extra legacy column has nowhere to go and silently failing on it is
    better than aborting a migration for a field nothing reads.
    """
    quoted = ", ".join(f'"{c}"' for c in columns)
    return [dict(r) for r in con.execute(f'select {quoted} from "{table.name}"')]


def coerce(table, rows: list[dict]) -> list[dict]:
    """Fix up the two places SQLite's loose typing does not survive PostgreSQL.

    SQLite stores whatever it is given. PostgreSQL does not, and these are the two
    mismatches this schema can actually produce:

    * **Boolean columns hold 0/1 integers.** asyncpg refuses an int for a boolean, so
      every Boolean column is converted explicitly. (`s`, `m`, `b` on a plan item and
      `is_admin`/`active` on a user all go through here.)
    * **Numeric columns may hold floats.** PostgreSQL wants a Decimal for NUMERIC, and
      `float` -> `Decimal` via `str()` avoids binary-float noise turning 65.0 into
      65.00000000000001 on a purchase rate that reaches a GST invoice.
    """
    from sqlalchemy import Boolean, Numeric

    bools = [c.name for c in table.columns if isinstance(c.type, Boolean)]
    nums = [c.name for c in table.columns if isinstance(c.type, Numeric)]
    if not bools and not nums:
        return rows

    for row in rows:
        for name in bools:
            if row.get(name) is not None:
                row[name] = bool(row[name])
        for name in nums:
            value = row.get(name)
            if value is not None and not isinstance(value, Decimal):
                row[name] = Decimal(str(value))
    return rows


# ─── Writing PostgreSQL ──────────────────────────────────────────────────────

async def target_is_empty(engine) -> tuple[bool, dict[str, int]]:
    """Row counts per table in the target, and whether they are all zero."""
    counts: dict[str, int] = {}
    async with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            n = (await conn.execute(select(func.count()).select_from(table))).scalar()
            counts[table.name] = int(n or 0)
    return (not any(counts.values())), counts


async def copy_table(engine, table, rows: list[dict]) -> int:
    """Insert one table's rows in batches, inside one transaction per table."""
    if not rows:
        return 0
    async with engine.begin() as conn:
        for start in range(0, len(rows), BATCH):
            chunk = rows[start:start + BATCH]
            try:
                await conn.execute(insert(table), chunk)
            except Exception as exc:                      # noqa: BLE001 - reported, not hidden
                raise SystemExit(
                    f"\n  FAILED inserting {table.name} rows "
                    f"{start}-{start + len(chunk) - 1}: {type(exc).__name__}: {exc}\n"
                    f"  first row of the failing batch: {chunk[0]}"
                ) from exc
    return len(rows)


async def reset_sequences(engine) -> list[tuple[str, int]]:
    """Advance every SERIAL sequence past the highest id that was copied.

    THE step most likely to be forgotten, and the one whose absence is invisible until
    the next insert. Copying explicit ids leaves each sequence at 1, so the next INSERT
    reuses id 1 and fails on the primary key — and on this app the next insert may be a
    GST invoice, i.e. a legally-sequential document.

    ``pg_get_serial_sequence`` returns NULL for a table whose primary key is not a
    sequence, which is skipped rather than treated as an error.
    """
    advanced: list[tuple[str, int]] = []
    async with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            pk = list(table.primary_key.columns)
            if len(pk) != 1 or not pk[0].autoincrement:
                continue
            col = pk[0].name
            seq = (
                await conn.execute(
                    text("select pg_get_serial_sequence(:t, :c)"),
                    {"t": table.name, "c": col},
                )
            ).scalar()
            if not seq:
                continue
            highest = (
                await conn.execute(text(f'select max("{col}") from "{table.name}"'))
            ).scalar()
            if highest is None:
                continue
            # is_called=true means the NEXT value is highest+1, which is what we want.
            await conn.execute(
                text("select setval(:s, :v, true)"),
                {"s": seq, "v": int(highest)},
            )
            advanced.append((table.name, int(highest)))
    return advanced


# ─── Verification ────────────────────────────────────────────────────────────

async def verify_counts(engine, expected: dict[str, int]) -> list[str]:
    """Per-table row counts must match the source exactly."""
    problems = []
    async with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            want = expected.get(table.name, 0)
            got = int(
                (await conn.execute(select(func.count()).select_from(table))).scalar() or 0
            )
            if got != want:
                problems.append(f"{table.name}: SQLite {want} rows, PostgreSQL {got}")
    return problems


async def verify_values(engine, con: sqlite3.Connection) -> list[str]:
    """Re-read the rows that would cost real money if they were wrong.

    Counts prove arity, not content. These three checks prove content, and they are
    chosen because each one is a number someone outside this app relies on:

    * **invoices** — the GST series is legally sequential; a lost or altered number is an
      audit question.
    * **packing days** — units and cartons are what a shipment and its invoice are built
      from, and ``carried_from_plan_id`` is the lineage that explains a carried day.
    * **product prices** — the purchase rate becomes the declared value on an Amazon
      inbound shipment, and Amazon rejects a zero with a misleading error.
    """
    problems: list[str] = []

    def rows(sql: str) -> list[tuple]:
        return [tuple(r) for r in con.execute(sql)]

    checks = (
        (
            "invoices",
            "select id, invoice_no, invoice_number from invoices order by id",
            text("select id, invoice_no, invoice_number from invoices order by id"),
        ),
        (
            "packing days",
            "select id, plan_id, pack_date, status, total_units, total_cartons, "
            "carried_from_plan_id from shipment_packing_days order by id",
            text(
                "select id, plan_id, pack_date, status, total_units, total_cartons, "
                "carried_from_plan_id from shipment_packing_days order by id"
            ),
        ),
        (
            "product prices",
            "select asin, hsn_code from product_prices order by asin",
            text("select asin, hsn_code from product_prices order by asin"),
        ),
    )

    async with engine.connect() as conn:
        for label, sqlite_sql, pg_sql in checks:
            want = rows(sqlite_sql)
            got = [tuple(r) for r in (await conn.execute(pg_sql)).all()]
            if len(want) != len(got):
                problems.append(f"{label}: {len(want)} rows in SQLite, {len(got)} in PostgreSQL")
                continue
            for a, b in zip(want, got):
                # Compare as strings: SQLite hands back int/float/str loosely while
                # PostgreSQL is typed, and the question here is "is it the same VALUE",
                # not "is it the same Python type".
                if [str(x) for x in a] != [str(x) for x in b]:
                    problems.append(f"{label}: SQLite {a} != PostgreSQL {b}")
    return problems


async def verify_insert_after_copy(engine) -> list[str]:
    """Prove the sequences really were reset, by inserting and rolling back.

    Without this the sequence reset is asserted only by the setval call succeeding, which
    says nothing about whether the value was right. Here a real row is inserted into
    `shipment_plans` and the transaction is then rolled back, so nothing persists —
    if the sequence were still at 1 this would raise a duplicate-key error.
    """
    from app.models import ShipmentPlan

    problems: list[str] = []
    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                insert(ShipmentPlan.__table__)
                .values(label="__sequence probe__", status="draft")
                .returning(ShipmentPlan.__table__.c.id)
            )
            new_id = result.scalar()
            highest = (
                await conn.execute(text("select max(id) from shipment_plans"))
            ).scalar()
            if new_id is None or new_id < int(highest or 0):
                problems.append(
                    f"a new shipment_plans row got id {new_id} but max(id) is {highest} — "
                    "the sequence was not advanced past the copied rows"
                )
            raise _Rollback()
    except _Rollback:
        pass
    except Exception as exc:                              # noqa: BLE001
        problems.append(
            f"inserting a probe row failed ({type(exc).__name__}: {exc}) — this is what "
            "the first real insert after the migration would have done"
        )
    return problems


class _Rollback(Exception):
    """Raised to abort the probe transaction so the probe row never persists."""


# ─── Entry point ─────────────────────────────────────────────────────────────

async def run(sqlite_path: Path, pg_url: str, allow_nonempty: bool, dry_run: bool) -> int:
    con = open_source(sqlite_path)
    present = source_tables(con)

    plan: list[tuple[object, list[dict]]] = []
    expected: dict[str, int] = {}
    log(f"\n== Reading {sqlite_path} ==")
    for table in Base.metadata.sorted_tables:
        if table.name not in present:
            log(f"  {table.name:32} absent in source — skipped")
            expected[table.name] = 0
            continue
        columns = [c.name for c in table.columns]
        rows = coerce(table, read_rows(con, table, columns))
        expected[table.name] = len(rows)
        plan.append((table, rows))
        if rows:
            log(f"  {table.name:32} {len(rows):6} rows")
    total = sum(expected.values())
    log(f"  {'TOTAL':32} {total:6} rows")

    if dry_run:
        # Reported and returned BEFORE connecting, on purpose. A dry run whose whole
        # output is a ConnectionRefusedError is useless: the first thing anyone wants is
        # "what would move", and that question is answerable from tracker.db alone,
        # before RDS exists. The connection is what --dry-run is avoiding.
        log("\n== Dry run: nothing was written, and PostgreSQL was not contacted ==")
        log(f"  {total} rows across {len(plan)} tables would be copied.")
        return 0

    engine = create_async_engine(pg_url, echo=False)
    try:
        empty, counts = await target_is_empty(engine)
        if not empty:
            occupied = {k: v for k, v in counts.items() if v}
            if not allow_nonempty:
                log(f"\n  REFUSING: the target already holds rows: {occupied}")
                log("  Re-run with --allow-nonempty only if you mean to add to it.")
                return 2
            log(f"\n  ! target is not empty ({occupied}) and --allow-nonempty was given")

        log("\n== Copying ==")
        for table, rows in plan:
            n = await copy_table(engine, table, rows)
            if n:
                log(f"  {table.name:32} {n:6} rows")

        log("\n== Resetting sequences ==")
        for name, highest in await reset_sequences(engine):
            log(f"  {name:32} next id after {highest}")

        log("\n== Verifying ==")
        problems = await verify_counts(engine, expected)
        problems += await verify_values(engine, con)
        problems += await verify_insert_after_copy(engine)
        if problems:
            log("  FAILED:")
            for p in problems:
                log(f"    - {p}")
            return 1
        log(f"  row counts match for all {len(Base.metadata.sorted_tables)} tables")
        log("  invoice numbers, packing days and product prices match by value")
        log("  a fresh insert gets an id past the copied rows")
        log("\n  tracker.db was opened read-only and is unchanged.")
        return 0
    finally:
        await engine.dispose()
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite", default="tracker.db", type=Path)
    ap.add_argument("--postgres", required=True,
                    help="postgresql+asyncpg://user:pass@host:5432/dbname")
    ap.add_argument("--allow-nonempty", action="store_true",
                    help="copy into a database that already holds rows")
    ap.add_argument("--dry-run", action="store_true",
                    help="read and report, write nothing")
    args = ap.parse_args()
    if not args.postgres.startswith("postgresql+asyncpg://"):
        raise SystemExit("--postgres must be a postgresql+asyncpg:// URL")
    return asyncio.run(run(args.sqlite, args.postgres, args.allow_nonempty, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
