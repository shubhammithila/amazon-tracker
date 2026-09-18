# SQLite → PostgreSQL migration tooling

Three scripts and a compose file for the migration described in the plan appendix. **Nothing here
runs as part of the app** — they are one-off tools, and none of them writes to `tracker.db`
(the reads use `mode=ro`, so a bug here cannot touch the running app's database).

## Where these came from

Written during a **Node/React rewrite that was abandoned**. The rewrite was dropped because the
scraper is 3% of a 27,000-line app and the remaining 97% is knowledge paid for in production
incidents — the ads tab that spends real money, the legally-sequential GST invoice series, five
wrong attempts at the orders fetch. Re-risking that had no payoff that Postgres alone does not give.

The schema work was kept because it is the **risky half of the Postgres migration**, already built
and proven against a real Postgres 16.

## Usage

```bash
# 1. Postgres 16 + Redis 7 locally
cd scripts/postgres && docker compose up -d && cd ../..

# 2. Generate the DDL from the LIVE SQLite schema (reflects it fresh every run)
venv/Scripts/python scripts/postgres/gen_schema.py

# 3. Apply it
docker exec -i tracker-pg psql -U tracker -d tracker -v ON_ERROR_STOP=1 \
  -f - < scripts/postgres/schema.sql

# 4. Copy the data, with per-table verification
venv/Scripts/python scripts/postgres/seed_from_sqlite.py

# 5. Compare the result against the original, column by column
venv/Scripts/python scripts/postgres/verify_schema.py
```

`psycopg[binary]==3.2.3` is needed for the seed. It is not in `requirements.txt` because the app
does not use it yet — **do not add it to the production install** before the migration itself, and
remember `update-ec2.sh` never runs `pip install -r requirements.txt` on that box.

## What each script is for

| Script | Does | Verifies |
|---|---|---|
| `gen_schema.py` | 34-table DDL from `PRAGMA table_info` | JSONB column set asserted in BOTH directions |
| `seed_from_sqlite.py` | copies every row, resets identity sequences | per-table counts, plus value checks |
| `verify_schema.py` | compares Postgres against SQLite | every column's type, length, precision, nullability, and every UNIQUE |

Measured on this machine: **34 tables, 344 columns, 39 indexes, 12 FKs, 6 JSONB, 20 unique
constraints, 6,811 rows** — zero unexpected differences.

`verify_schema.py` is proved non-vacuous: widening a money column, dropping the
`(day_id, asin)` UNIQUE, and making `products.asin` nullable are each caught.

## Four things that cost real debugging time

**1. Alphabetical table order fails on Postgres.** `amazon_order_items` sorts before `amazon_orders`
and its foreign key errors with *relation "amazon_orders" does not exist*. SQLite tolerates the
forward reference, which is why the original schema never had to care. `gen_schema.py` does a
topological sort with ties broken alphabetically, so the output is stable.

**2. Generated SQL through a pipe corrupts UTF-8 on Windows.** The first seed died with
*invalid byte sequence for encoding "UTF8": 0x96*, which reads exactly like corrupt source data and
was nothing of the kind: six product names contain an en dash (`White Sesame Laddoo – Jaggery`),
the source is clean, and `subprocess` was re-encoding as cp1252. The seed uses **psycopg with bound
parameters**, which removes the whole class — encoding, quoting, injection — rather than escaping
around it. *"the en dash survived"* is a value check.

**3. Identity sequences must be reset after copying explicit ids.** Otherwise every sequence sits at
1 and the next insert collides on the primary key — and the failure appears later, in the app, as a
duplicate-key error nobody can explain.

**4. A wrong `NUMERIC` precision is invisible until money is computed.** `rating_history.rating` is
`NUMERIC(2,1)`, so the database itself rejects the `good_rating: 99` class of bug. The DDL is
generated rather than transcribed precisely because 344 columns is where a hand-typed slip hides.

## Two decisions worth re-reading before cutover

* **`DATETIME` → `TIMESTAMP`, not `TIMESTAMPTZ`.** The app stores naive UTC via
  `datetime.utcnow()`. `TIMESTAMPTZ` would convert on read using the session timezone and silently
  shift every stored value. `app/ist.py` stays the only thing that decides which day it is.
* **No `ON DELETE` clauses.** SQLAlchemy's `cascade="all, delete-orphan"` is enforced in the ORM,
  and this app's history includes a cascade deleting 400 units of real packed stock. Reproducing
  that at the DDL level would make the same accident unrecoverable and invisible.

## Still to do for an actual migration

See the plan appendix for the full sequence. The parts these scripts do not cover:

* `asyncpg` in `requirements.txt` and a `postgresql+asyncpg://` `DATABASE_URL`
* `deploy/update-ec2.sh` — four `sqlite3.connect` sites, including the baseline detector that has
  already stamped production **backwards** once and failed two deploys
* RDS provisioning (`db.t4g.micro`, `ap-south-1`, security group open only to the EC2 instance,
  **not** publicly accessible)
* A dry run against real data before any cutover, and `tracker.db` kept as the fallback
