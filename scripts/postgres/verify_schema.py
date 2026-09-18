"""Compare the Postgres schema against the SQLite original, COLUMN BY COLUMN.

Run from the repo root:  venv/Scripts/python scripts/postgres/verify_schema.py

Counting tables proves almost nothing. 34 tables and 344 columns can match in total while a
`NUMERIC(12,2)` has become `NUMERIC(10,2)`, a `NOT NULL` has been dropped, or a UNIQUE index that
stops a flaky warehouse phone double-counting a packing save has quietly not been created. Each of
those is invisible until it costs something, and two of them cost real money in this app's history
(`good_rating: 99` passing a finite-float check; the `(day_id, asin)` uniqueness).

So every column is compared on name, type, precision and nullability, and every UNIQUE constraint
is checked to exist. Differences are reported as ALLOWED or UNEXPECTED — the allowed ones are the
deliberate mapping decisions, each with its reason, so a NEW difference cannot hide among them.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
DUMP = HERE / "schema_dump.json"
CONTAINER = "tracker-pg"

#: Deliberate mapping decisions, stated so an UNEXPECTED difference cannot hide among them.
ALLOWED = {
    "DATETIME->timestamp without time zone":
        "the Python app stores naive UTC; TIMESTAMPTZ would shift every value by the session tz",
    "TEXT->jsonb":
        "the six columns that genuinely hold JSON — the one real upgrade in the mapping",
    "INTEGER->integer": "same type, different spelling",
    "TEXT->text": "same type, different spelling",
    "BOOLEAN->boolean": "same type, different spelling",
}


def psql(sql: str) -> list[list[str]]:
    """One query through the container, with a readable failure.

    `check=True` alone raises `CalledProcessError` carrying the whole SQL string, which buries the
    actual cause — "the daemon is not running" reads as a broken script. Reported plainly instead,
    because the most likely reason this fails is that Docker simply is not up.
    """
    result = subprocess.run(
        ["docker", "exec", CONTAINER, "psql", "-U", "tracker", "-d", "tracker",
         "-t", "-A", "-F", "\t", "-c", sql],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        first = detail[0] if detail else "no output"
        raise SystemExit(
            f"Could not query Postgres in container {CONTAINER!r}: {first}\n\n"
            "Start it first, from scripts/postgres/:\n"
            "    docker compose up -d\n"
            "...then apply the schema:\n"
            "    docker exec -i tracker-pg psql -U tracker -d tracker -v ON_ERROR_STOP=1 "
            "-f - < scripts/postgres/schema.sql"
        )
    return [line.split("\t") for line in result.stdout.strip().splitlines() if line.strip()]


def sqlite_type_to_pg(raw: str) -> str:
    raw = (raw or "").upper()
    if raw.startswith("VARCHAR"):
        return "character varying"
    if raw.startswith("NUMERIC"):
        return "numeric"
    return {"INTEGER": "integer", "TEXT": "text", "BOOLEAN": "boolean",
            "DATETIME": "timestamp without time zone"}.get(raw, raw.lower())


def main() -> int:
    schema = json.loads(DUMP.read_text())

    pg_cols: dict[tuple[str, str], dict] = {}
    for table, col, dtype, maxlen, prec, scale, nullable in psql("""
        select table_name, column_name, data_type,
               coalesce(character_maximum_length::text,''),
               coalesce(numeric_precision::text,''),
               coalesce(numeric_scale::text,''), is_nullable
        from information_schema.columns where table_schema='public'
    """):
        pg_cols[(table, col)] = dict(
            type=dtype, maxlen=maxlen, precision=prec, scale=scale,
            nullable=(nullable == "YES"),
        )

    problems: list[str] = []
    allowed_seen: dict[str, int] = {}
    checked = 0

    for table, spec in sorted(schema.items()):
        for col in spec["columns"]:
            key = (table, col["name"])
            if key not in pg_cols:
                problems.append(f"MISSING COLUMN  {table}.{col['name']}")
                continue
            checked += 1
            got = pg_cols[key]
            want_raw = (col["type"] or "").upper()
            want = sqlite_type_to_pg(want_raw)

            if got["type"] != want:
                mapping = f"{want_raw}->{got['type']}"
                if mapping in ALLOWED:
                    allowed_seen[mapping] = allowed_seen.get(mapping, 0) + 1
                else:
                    problems.append(
                        f"TYPE  {table}.{col['name']}: sqlite {want_raw} -> pg {got['type']}"
                    )
                continue

            # VARCHAR length must match exactly. A wider column accepts data the app's own
            # validation would reject, which is how an over-long merchant SKU reaches Amazon.
            if want == "character varying":
                want_len = want_raw[want_raw.index("(") + 1: -1]
                if got["maxlen"] != want_len:
                    problems.append(
                        f"LENGTH  {table}.{col['name']}: sqlite {want_len} -> pg {got['maxlen']}"
                    )

            # NUMERIC precision AND scale. This is the one that hides money bugs.
            if want == "numeric":
                inner = want_raw[want_raw.index("(") + 1: -1].replace(" ", "")
                want_p, want_s = inner.split(",")
                if (got["precision"], got["scale"]) != (want_p, want_s):
                    problems.append(
                        f"PRECISION  {table}.{col['name']}: sqlite ({want_p},{want_s}) -> "
                        f"pg ({got['precision']},{got['scale']})"
                    )

            # NOT NULL. A column that became nullable accepts a row the original refused.
            want_nullable = not col["notnull"]
            if got["nullable"] != want_nullable:
                problems.append(
                    f"NULLABLE  {table}.{col['name']}: sqlite nullable={want_nullable} -> "
                    f"pg nullable={got['nullable']}"
                )

    # UNIQUE constraints carry the app's real invariants — (plan_id, pack_date) and
    # (day_id, asin) are what turn a repeated save from a warehouse phone into an update
    # rather than a double-count.
    pg_unique = {
        (t, tuple(sorted(c.split(","))))
        for t, c in psql("""
            select t.relname,
                   string_agg(a.attname, ',' order by a.attname)
            from pg_index i
            join pg_class t on t.oid = i.indrelid
            join pg_namespace n on n.oid = t.relnamespace
            join pg_attribute a on a.attrelid = t.oid and a.attnum = any(i.indkey)
            where i.indisunique and n.nspname = 'public'
            group by t.relname, i.indexrelid
        """)
    }
    unique_checked = 0
    for table, spec in sorted(schema.items()):
        for idx in spec["indexes"]:
            if not idx["unique"]:
                continue
            unique_checked += 1
            want = (table, tuple(sorted(idx["cols"])))
            if want not in pg_unique:
                problems.append(
                    f"MISSING UNIQUE  {table}({', '.join(idx['cols'])}) — this is an app "
                    "invariant, not a performance hint"
                )

    print(f"compared {checked} columns and {unique_checked} unique constraints "
          f"across {len(schema)} tables\n")
    print("allowed mapping differences:")
    for mapping, count in sorted(allowed_seen.items()):
        print(f"  {count:>4}x  {mapping:<42} {ALLOWED[mapping]}")

    if problems:
        print(f"\n{len(problems)} UNEXPECTED DIFFERENCE(S):")
        for p in problems:
            print("  -", p)
        return 1
    print("\nNo unexpected differences. The Postgres schema matches the original.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
