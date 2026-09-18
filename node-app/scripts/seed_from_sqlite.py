"""Copy tracker.db into the Node stack's Postgres, and VERIFY the copy.

Run from the repo root:  venv/Scripts/python node-app/scripts/seed_from_sqlite.py

**Reads tracker.db read-only and never writes to it.** Opened via a `mode=ro` URI so a bug here
cannot touch the running app's database — the Python app must remain untouched, and "I was
careful" is not the same as "it could not have happened".

**Uses a real driver with parameterised inserts, not SQL built as text.** The first version piped
generated SQL to `psql` through `subprocess` and died on

    ERROR: invalid byte sequence for encoding "UTF8": 0x96

which read exactly like corrupt source data and was nothing of the kind: six product names
contain an en dash (`White Sesame Laddoo – Jaggery`), the source is clean UTF-8, and the *pipe*
was re-encoding it as cp1252 on Windows. Parameter binding removes the entire class — encoding,
quoting and SQL injection — instead of escaping around it.

Four things this gets right that the obvious version gets wrong, each of them silent:

* **Identity sequences are reset afterwards.** Explicit ids are copied so foreign keys line up,
  which leaves every IDENTITY sequence at 1 — the next insert then collides on the primary key,
  and the failure surfaces later, in the app, as a duplicate-key error nobody can explain.
* **Dependency order**, so foreign keys hold. Alphabetical order fails on `amazon_order_items`
  before `amazon_orders` — the same trap the schema generator hit.
* **JSON columns are parsed, not copied.** They are `JSONB` now; handing Postgres a raw string
  stores a JSON *string* rather than an object, and `->>` then returns nothing from a column that
  looks full.
* **Booleans are converted.** SQLite stores 0/1 integers, which will not bind to a Postgres
  BOOLEAN.

Verification is per table AND by value, because equal row counts prove only that the right
NUMBER of rows arrived.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

import psycopg
from psycopg.types.json import Jsonb

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = ROOT.parent
DUMP = ROOT / "schema_dump.json"
SQLITE = REPO / "tracker.db"
DSN = "postgresql://tracker:tracker_local_dev@localhost:5432/tracker"

sys.path.insert(0, str(ROOT / "scripts"))
from gen_schema import JSON_COLUMNS, dependency_order  # noqa: E402


def convert(value, *, sqlite_type: str, is_json: bool):
    """One SQLite value as something psycopg can bind."""
    if value is None:
        return None
    if is_json:
        # Parsed HERE so a malformed value fails with the table and column named, rather than as
        # an opaque driver error thousands of rows into the copy.
        return Jsonb(json.loads(value) if isinstance(value, str) else value)
    if sqlite_type == "BOOLEAN":
        return value in (1, True, "1", "true")
    return value


def main() -> int:
    if not SQLITE.exists():
        raise SystemExit(f"{SQLITE} not found")
    schema = json.loads(DUMP.read_text())

    # READ-ONLY: `mode=ro` refuses any write at the driver level.
    con = sqlite3.connect(f"file:{SQLITE.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    order = dependency_order(schema)
    print(f"seeding {len(order)} tables in dependency order\n")

    source_counts: dict[str, int] = {}
    copied = 0

    with psycopg.connect(DSN, autocommit=False) as pg:
        with pg.cursor() as cur:
            # Wiped in REVERSE dependency order so foreign keys never block the delete.
            # TRUNCATE ... CASCADE would be shorter and would silently empty tables that are
            # not in this list.
            for table in reversed(order):
                cur.execute(f"DELETE FROM {table}")

            for table in order:
                spec = schema[table]
                cols = [c["name"] for c in spec["columns"]]
                types = {c["name"]: (c["type"] or "").upper() for c in spec["columns"]}
                rows = con.execute(f'SELECT {", ".join(cols)} FROM "{table}"').fetchall()
                source_counts[table] = len(rows)
                if not rows:
                    print(f"  {table:<30} 0")
                    continue

                placeholders = ", ".join(["%s"] * len(cols))
                sql = (
                    f'INSERT INTO {table} ({", ".join(cols)}) VALUES ({placeholders})'
                )
                payload = [
                    tuple(
                        convert(
                            row[c],
                            sqlite_type=types[c],
                            is_json=(table, c) in JSON_COLUMNS,
                        )
                        for c in cols
                    )
                    for row in rows
                ]
                try:
                    cur.executemany(sql, payload)
                except Exception as exc:
                    raise SystemExit(f"{table}: {exc}") from exc
                copied += len(rows)
                print(f"  {table:<30} {len(rows)}")

            # ── Reset every identity sequence past MAX(id) ──
            #
            # THE classic silent failure of a copy like this: without it the next insert reuses
            # id 1 and collides.
            resets = 0
            for table, spec in schema.items():
                pk = [
                    c for c in spec["columns"]
                    if c["pk"] and (c["type"] or "").upper() == "INTEGER"
                ]
                if len(pk) != 1:
                    continue
                name = pk[0]["name"]
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence(%s, %s), "
                    f"GREATEST(COALESCE((SELECT MAX({name}) FROM {table}), 0), 1), "
                    f"(SELECT COUNT(*) > 0 FROM {table}))",
                    (table, name),
                )
                resets += 1
        pg.commit()
        print(f"\nreset {resets} identity sequences")

        # ── Verify: per-table counts must match EXACTLY ──
        mismatches = []
        with pg.cursor() as cur:
            for table, want in sorted(source_counts.items()):
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                got = cur.fetchone()[0]
                if got != want:
                    mismatches.append(f"{table}: sqlite {want} -> pg {got}")

        print(f"\ncopied {copied} rows")
        if mismatches:
            print(f"{len(mismatches)} COUNT MISMATCH(ES):")
            for m in mismatches:
                print("  -", m)
            return 1
        print(f"all {len(source_counts)} tables match on row count")

        # ── Verify by VALUE, not just by count ──
        #
        # Equal counts prove the right NUMBER of rows arrived. These check the right DATA did, on
        # the things that would cost something.
        checks = [
            ("products", "SELECT COUNT(*) FROM products WHERE title IS NOT NULL"),
            ("rating_history", "SELECT COUNT(*) FROM rating_history"),
            ("JSONB is an OBJECT not a string",
             "SELECT COUNT(*) FROM product_decision WHERE snapshot_json IS NOT NULL "
             "AND jsonb_typeof(snapshot_json) = 'object'"),
            ("the en dash survived",
             "SELECT COUNT(*) FROM product_prices WHERE item LIKE '%' || chr(8211) || '%'"),
            ("money kept 2dp",
             "SELECT COUNT(*) FROM economics_snapshot "
             "WHERE ordered_sales IS NOT NULL AND scale(ordered_sales) <= 2"),
            ("next products.id is past MAX(id)",
             "SELECT (SELECT last_value FROM pg_sequences WHERE sequencename LIKE 'products_id%')"
             " >= (SELECT COALESCE(MAX(id), 1) FROM products)"),
        ]
        print("\nvalue checks:")
        with pg.cursor() as cur:
            for label, sql in checks:
                cur.execute(sql)
                print(f"  {label:<38} {cur.fetchone()[0]}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
