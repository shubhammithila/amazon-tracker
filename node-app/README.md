# Amazon Tracker — Node port

React + Node + Express + Postgres + Redis, replacing the Python app one slice at a time.
**The Python app is untouched**: this has its own database, and the seed script opens `tracker.db`
read-only (`mode=ro`) so a bug here cannot write to it.

## Run it

Three terminals, from `node-app/`:

```bash
# 1. Postgres 16 + Redis 7
docker compose up -d

# 2. The API (http://localhost:8001)
cd server && npm install && npm run dev

# 3. The page (http://localhost:5173)
cd web && npm install && npm run dev
```

Then open **http://localhost:5173**, paste a few ASINs, press **Start scrape**.

### First-time setup: schema and seed

Run from the repo root, with the Python venv (these are one-off migration tools, so they live in
Python where the source database is):

```bash
venv/Scripts/python node-app/scripts/gen_schema.py        # DDL from the live SQLite schema
docker exec -i tracker-pg psql -U tracker -d tracker -v ON_ERROR_STOP=1 -f - \
  < node-app/server/src/db/schema.sql
venv/Scripts/python node-app/scripts/seed_from_sqlite.py  # 6,811 rows, verified per table
venv/Scripts/python node-app/scripts/verify_schema.py     # column-by-column comparison
```

## Checks

```bash
cd server
npm test                         # 120 tests
npm run typecheck                # strict + noUncheckedIndexedAccess
npm run lint:dates               # the toISOString / date-string ban, tree-wide
node scripts/mutate-parser.mjs   # 17 mutations, all must be caught
```

`npm test` skips the Redis-backed suites with a warning when Redis is not running, rather than
passing vacuously.

## What is done

| | |
|---|---|
| Schema | 34 tables, **generated** from the live SQLite schema and verified column by column |
| Seed | 6,811 rows, per-table counts asserted, identity sequences reset |
| `ist.ts` | the offset and "today", ported first |
| Scraper | guards + extractors, **agreeing with the Python parser field by field** on real pages |
| Fetch | undici pool, gzip/br decoding, named failure statuses |
| Lock | Redis `SET NX` + TTL + Lua compare-and-delete, replacing a module-level flag |
| Engine | batches of 50, bounded workers, retry rounds, progress in Redis |
| API | `POST /api/scrape`, `GET /api/progress`, `GET /api/results`, `POST /api/stop`, `GET /health` |
| Page | paste ASINs, live progress over WebSocket, results table |

`churn_reports` and `churn_scores` are deliberately absent: 0 rows, referenced only by `models.py`,
and their router and template were already deleted.

## Design notes worth knowing before changing anything

**The differential test is the acceptance bar.** `test/parsers.differential.test.ts` compares this
parser against the Python one on three real saved pages. "The Node tests pass" is never sufficient
for a replacement — a parser that satisfies its own expectations while disagreeing with the original
is exactly the silent failure the Freedom Sale deal badge was.

**It must run both scrapers from the SAME machine.** Amazon serves datacentre IPs a different offer;
measured across six ASINs from a laptop and from EC2, deal status and price move together. Comparing
across hosts produces diffs that are about the network, not the code.

**Dates go through `src/ist.ts`, always.** `toISOString()` is banned tree-wide and enforced by
`scripts/check-dates.mjs`, because it formats through UTC and answers the previous day for 5.5 hours
out of every 24 in IST — which shipped four times in the Python templates, once on a GST invoice
date. A `YYYY-MM-DD` string never reaches `new Date`, which parses it as UTC midnight by spec.

**Three bugs here were found only by running it**, and are commented at their sites:
`import { Pool } from "pg"` typechecks and fails at runtime (pg is CommonJS); undici does not
decompress, so a gzipped 200 looked like a parser fault; and Amazon's bot interstitial is a 200 that
both apps mis-report as a parse error.

## Not done yet

Everything except the scraper: ads, orders, portfolio, projections, shipment, invoices, auth and
permissions. No production deploy and no cutover — `tracker.db` and the Python app stay as they are.

Known limits, stated rather than hidden:

* **Results are persisted after the run, not incrementally.** A crash mid-scrape loses the batch.
  Writing per page is a real improvement and belongs in its own change, with a test that a partial
  run leaves usable rows.
* **`POST /stop` can only stop a run this process started.** The route says so instead of reporting
  success for something that did not happen.
* **The engine's tests fake HTTP.** A fake client answers whatever it is told, which is how the FBA
  packing bug survived 2,203 passing tests and 19/19 mutations in the Python app. Only the
  differential test and a live run prove anything about Amazon.
