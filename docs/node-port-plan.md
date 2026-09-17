# Porting Amazon Tracker to React + Node + Express + Postgres + Redis

**Goal: replace the Python app entirely.** Not coexistence — so every slice must be provably
equivalent before the Python original is retired, and "the Node tests pass" is never sufficient
evidence. The acceptance bar is **agreement with the current app on real data**.

## Decisions taken

| | |
|---|---|
| Language | **TypeScript** |
| Schema | **34 live tables**; `churn_reports`/`churn_scores` dropped (0 rows, no code — router and template already deleted) |
| Database | **Its own**, never shared with the Python app |
| Local infra | **Docker Desktop** — `postgres:16-alpine` + `redis:7-alpine` |
| Seed | This machine's `tracker.db`, **6,812 rows** |
| Location | `amazon-tracker-node/`, a **sibling directory** — the Python app is structurally untouchable, not merely untouched |
| First slice | The scraper (832 lines across 5 modules + 2 routes) |

## Measured facts this plan rests on

- Python app: **27,607 lines**, 64 modules, **2,197 tests**
- Scraper: `engine` 245 · `parsers` 394 · `keyword_tracker` 107 · `stealth` 66 · `http_client` 20
- Its routes: `scrape.py` 247 + `ws.py` 73
- Local DB: 34 tables, 6,812 rows. **Production: 632,899 rows / 171 MB**, mostly ads
- Column types: 108 Integer, 49 DateTime, 25 Text, 24 `Numeric(12,2)`, 16 Boolean, 34 indexes/constraints
- **6 `Text` columns hold JSON** (`fees_json`, `ads_json`, `snapshot_json`, `value_json`,
  `conditions_json`, `invoice_data`) — these become real `JSONB`
- Node v22.16.0 and npm 11.4.2 present; **Docker, psql, redis-cli absent**

## The parser library is settled by measurement

Full detail in `docs/node-port-parser-spike.md`. Three candidates against three real 2.3 MB pages:

| Library | Correct | Speed | Memory |
|---|---|---|---|
| **cheerio** | **15/15** | **274 ms/page** | +39 MB |
| `xpath`+`@xmldom/xmldom` | **0/15** | — | +12 MB |
| `jsdom` | 15/15 | 2,240 ms/page | +511 MB |

`@xmldom/xmldom` dies on real HTML (`tag mismatch: "div" != "br"` — an unclosed `<br>`), which
kills the appealing option of keeping the XPath byte-identical to lxml. `jsdom` is 8× slower and
+511 MB on a **951 MB** box running 10 concurrent workers.

**Two traps the spike exposed, and both would have shipped silently:**

1. **`.text()` on a multi-node cheerio selection concatenates.** On `B0D817HX57` the last-resort
   price selector matches **21** nodes and produced `"294.494.597.35294…"`. Every ported selector
   takes `.first()`.
2. **`detect_unavailable` nulls the price deliberately.** Python returned `null`; the naive
   rewrite returned `₹294.00` — a real price belonging to a *different* product in an ad
   carousel. So **guards are ported before extractors**, inverting the order the file reads in.

## Structure

```
amazon-tracker-node/
  docker-compose.yml         postgres:16-alpine + redis:7-alpine
  server/
    src/
      config.ts              zod-validated env; fails at boot like SECRET_KEY does today
      db/
        schema/*.sql         34 tables
        migrate.ts           node-pg-migrate
        pool.ts              pg.Pool
      ist.ts                 PORTED FIRST — see below
      scraper/
        guards.ts            captcha · dog page · unavailable   <- before extractors
        parsers.ts           extractors, every one .first()
        stealth.ts           UA rotation, delays, headers
        httpClient.ts        undici
        engine.ts            Redis lock + batching
      queue/scrapeQueue.ts   BullMQ
      routes/scrape.ts       /scrape /progress /results /stop
      realtime/progress.ts   ws + Redis pub/sub
    test/                    vitest
  web/                       React + Vite + TS
  scripts/
    seed-from-sqlite.ts      6,812 rows, per-table verified
    differential-scrape.ts   THE acceptance test
```

## `ist.ts` is ported before anything else

`app/ist.py` exists because **the same timezone bug was found seven times** — including a GST
invoice dated before the shipment it billed, and the nightly jobs silently running at 09:20 IST.
The business runs IST; servers run UTC.

JavaScript is *worse* here than Python: `new Date("2026-08-25")` is UTC midnight by spec, and
`toISOString()` is a UTC formatter that already shifted four dates a day early in the current
app's templates. So `ist.ts` lands first, with `today()`, `utcInstant()` and `localDate()`, and
the ban on `toISOString` is a lint rule from day one rather than a test added after the fourth
occurrence.

## What Redis genuinely improves

Not translation — these are limits of the current design:

- **`ScrapeState` is a module-level singleton with an `asyncio.Lock`.** It works only because
  there is exactly one uvicorn process; it cannot survive a restart, and two workers would give
  two independent scrape states. Its own comment records a real bug: the manual and 06:00
  scheduled runs overlapping and clearing each other's results. Redis `SET NX` + TTL makes the
  lock **distributed and restart-surviving**.
- **The 3-round retry loop** is hand-rolled `for round_num in range(...)`. BullMQ expresses it as
  retries with backoff.
- **WebSocket progress** becomes Redis pub/sub, so any process can publish.

## Build sequence

Each step ends with something you can run and check.

| # | Step | Verified by |
|---|---|---|
| 1 | `docker-compose up`, both services reachable | `pg_isready`, `redis-cli ping` |
| 2 | 34-table schema + migrations | every table and all 34 indexes exist |
| 3 | `ist.ts` + the `toISOString` lint rule | unit tests incl. the 18:30Z boundary |
| 4 | Seed from `tracker.db` | **per-table row counts match exactly** |
| 5 | `guards.ts` then `parsers.ts` | the 3 saved pages, field-by-field vs Python |
| 6 | `stealth.ts`, `httpClient.ts` | header/delay shape |
| 7 | `engine.ts` + BullMQ + Redis lock | concurrent-claim test: only one run wins |
| 8 | Express routes + WS progress | drive a real scrape |
| 9 | Minimal React page | you run it locally |
| 10 | **Differential scrape** | same ASINs both apps, every field compared |

## Verification

**Automated**
- Guards before extractors: an unavailable page yields `price: null`, never a carousel price
- Every extractor asserted against the 3 saved real pages
- A cheerio selection with several matches takes `.first()` — asserted at source, since a
  concatenated price like `₹294.00` can look entirely plausible
- Seed script: per-table counts equal, and JSON columns parse as `JSONB`
- Two simultaneous scrape claims: exactly one wins (the bug `claim_for_new_run` documents)
- `ist.utcInstant(2026-09-19) === "2026-09-18T18:30Z"`, and no `toISOString` in any source

**Manual, by you, locally**
`docker compose up` → migrate → seed → open the React page → scrape ~10 ASINs → watch live
progress → compare the table against the Python app's `/results` for the same ASINs.

**The differential test is the gate for retiring anything.** A parser that passes its own tests
and disagrees with the original is exactly the silent failure the deal-badge bug was — and this
spike already produced one such disagreement in 12 fields.

## Out of scope for slice 1

SP-API, ads, invoices, shipment, orders, portfolio, projections, auth/permissions. No production
deploy, no cutover. `tracker.db` and the Python app are never modified.

## Risks, named

- **34 tables is the biggest single step.** Mechanical, but a wrong `Numeric` precision is
  invisible until money is computed. Types are transcribed from `models.py`, not inferred from
  data.
- **Amazon serves datacentre IPs different offers** (CLAUDE.md measured deal/price differing
  between laptop and EC2). So the differential test must run **both scrapers from the same
  machine**, or the comparison is meaningless.
- **`parsers.py` is tuned to today's DOM.** Both apps break together when Amazon changes; that is
  acceptable, but the ported selectors must be re-verified against fresh pages at cutover, not
  trusted from these three saved ones.
- **2,197 tests do not port automatically.** Slice 1 rebuilds only the scraper's share.
