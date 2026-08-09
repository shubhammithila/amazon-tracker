# Amazon Tracker v2 — Project Memory

## Project Overview
Complete rebuild of Amazon product tracker + FBA invoice generator. FastAPI + httpx + lxml replacing Flask + Playwright.

## How to Run
- Double-click `C:\Users\LENOVO\Desktop\Start Amazon Tracker.bat`
- Or manually: `cd` to project dir, `.\venv\Scripts\activate`, `uvicorn app.main:app --reload --port 8000`
- URL: http://localhost:8000
- Tests: `venv/Scripts/python -m pytest -q` (569 tests; random order by default)

### Two logins
| Env var | Role | Gets |
|---|---|---|
| `APP_PASSWORD` (`admin123`) | admin | everything |
| `OPS_PASSWORD` | ops | `/ops-page`, daily packing, the morning PDF |

`APP_PASSWORD` is checked **first**, so setting `OPS_PASSWORD` to the same value
cannot demote you. A session cookie with no role at all means admin — that is
deliberate, because the old cookie was exactly `{"authenticated": True}` and
every existing session and test would otherwise break.

## Project Location
- Working dir: `C:\Users\LENOVO\Desktop\Claude\Amazon Tracker\.claude\worktrees\stoic-allen-bb3a55`
- Launch file: `.claude/launch.json` (use `preview_start` with name `tracker`)

---

## Architecture
- **Backend**: FastAPI (async) + SQLAlchemy async + SQLite (local) / PostgreSQL (prod)
- **Scraping**: httpx async + lxml XPath (no browser needed)
- **Frontend**: Vanilla JS + Chart.js, WebSocket for live progress
- **Scheduling**: APScheduler for daily auto-scrapes
- **Deployment target**: AWS EC2 t2.micro + RDS PostgreSQL (free tier)

## Key Files
```
app/
├── main.py              # FastAPI app entry point
├── config.py            # Settings (pydantic-settings, .env)
├── database.py          # Async SQLAlchemy engine
├── models.py            # DB models (Products, PriceHistory, BSRHistory, RatingHistory, SellerOffers, Keywords, KeywordRankings, ScrapeJobs, Invoices, ShipmentPlan/PlanItem/PackingDay/PackingEntry)
├── scheduler.py         # APScheduler (daily scrape at 06:00, keywords at 07:30)
├── utils.py             # Date parsing helpers
├── routers/
│   ├── auth.py          # Login/logout, get_current_role, require_admin / require_ops_or_admin
│   ├── scrape.py        # /scrape, /progress, /results, /stop, /fetch-sheet
│   ├── products.py      # /products, /products/{asin}/history, /products/download
│   ├── keywords.py      # /keywords, /keywords/track, /keywords/{id}/rankings (no UI tab; the scheduler job still uses it)
│   ├── ws.py            # WebSocket /ws/progress
│   ├── shipment.py      # Plan lifecycle, daily packing, 5 downloads, invoice bridge
│   └── invoice.py       # /invoice/parse-shipment, /invoice/generate-excel, /invoice/generate-pdf, /invoice/save, /invoice/next-number
├── scraper/
│   ├── engine.py        # Async scrape orchestrator (queue, semaphore, retry)
│   ├── http_client.py   # httpx client with stealth headers
│   ├── parsers.py       # lxml XPath extractors (title, price, BSR, rating, seller, fulfillment, deal, use_by)
│   ├── keyword_tracker.py  # Search result rank tracking
│   └── stealth.py       # User agent rotation, delays, headers
├── shipment/
│   ├── logic.py         # ALL shipment rules: rounding, sorting, hold, carry-over, packed vs shippable
│   ├── repository.py    # The only SELECT of plan items (one ORDER BY); write-separated upserts
│   └── documents.py     # The four documents, each returning io.BytesIO
└── invoice/
    ├── company_data.py  # F2D Tech GSTINs, supplier info, priority FC addresses, transporters
    ├── hsn_codes.py     # HSN code master (default 1106 @ 5% for all food products)
    ├── parser.py        # Parse Amazon FBA shipment TSV files
    ├── generator.py     # Generate Excel + PDF invoices (reportlab)
    ├── fc_addresses.json    # 93 Amazon FC addresses (from official Excel)
    ├── pricing_data.json    # 410 SKU/ASIN → purchase rate mappings
    ├── product_families.json # 205 ASINs → parent product + brand + weight (drives plan sorting)
    └── hsn_master.json      # Verified HSN codes (auto-saved after each invoice)
```

### Tabs
Dashboard, Invoice, Portfolio, Projections, Shipment — and `/ops-page` for the
warehouse, which is not in the nav because every link in it is admin-only.

Colour lives in **`static/theme.css`** and nowhere else — one shared light theme
for all seven pages. `tests/test_theme.py` fails on any template that re-declares
`:root`, hardcodes a hex/rgba colour, or forgets the stylesheet link, and it
computes WCAG contrast for every foreground/background pair rather than trusting
how they look. `app/shipment/documents.py` and `app/invoice/generator.py` keep
their dark header bands: those are printed documents, where dark-on-white is the
accounting convention.

The nav lives in `templates/nav.html` and is `include`d, not inherited. It used
to be copy-pasted into all 7 templates and had drifted: `projections.html` was
simply missing the Shipment link, which is why that tab vanished when you opened
Projections. `tests/test_nav_consistency.py` is what stops it recurring — the
partial alone would not.

History and Keywords were removed as requested. `app/routers/keywords.py`, the
models and the scheduler job all stay: only the tabs were unwanted, and
`tests/test_retention_and_scheduler.py` asserts `daily_keyword_track` exists.

---

## Company Data (F2D Tech Private Limited)

### Supplier Info
- **Name**: F2D TECH PRIVATE LIMITED
- **Address**: C/O Dinesh Prasad Sah, New Babu Para, Near Dadi Shyam Mandir, Dumka, Jharkhand 814101
- **Primary GSTIN** (Jharkhand): 20AAFCF9848M1Z7
- **Phone**: 7870034414

### GSTINs by State
| State | GSTIN |
|-------|-------|
| Assam | 18AAFCF9848M1ZS |
| Bihar | 10AAFCF9848M1Z8 |
| Delhi | 07AAFCF9848M1ZV |
| Gujarat | 24AAFCF9848M1ZZ |
| Haryana | 06AAFCF9848M1ZX |
| Jharkhand | 20AAFCF9848M1Z7 |
| Karnataka | 29AAFCF9848M1ZP |
| Maharashtra | 27AAFCF9848M1ZT |
| Odisha | 21AAFCF9848M1Z5 |
| Punjab | 03AAFCF9848M1Z3 |
| Rajasthan | 08AAFCF9848M1ZT |
| Tamil Nadu | 33AAFCF9848M1Z0 |
| Telangana | 36AAFCF9848M1ZU |
| Uttar Pradesh | 09AAFCF9848M1ZR |
| West Bengal | 19AAFCF9848M1ZQ |

### Priority FC Addresses (most used)
- **ISK3** (Maharashtra): Amazon Seller Services Private Limited, Royal Warehousing and Logistics LLP, Survey Number 45, Hissa No.4A, Village Pise Village, Aamne Post, BHIWANDI, MAHARASHTRA 421302, IN
- **BLR4** (Karnataka): Amazon Seller Services Private Limited, Plot No. 12 P2, Hitech, Defence and Aerospace Park, Devanahalli, BENGALURU, KARNATAKA 562149, IN
- **DED3** (Haryana): ASSPL - Haryana, Block J2, Farukhnagar Logistics Parks, LLP, Village- Farrukhnagar, Tehsil- Farrukhanagar, Gurgaon, HARYANA 122506, IN

### Transporters
- All Cargo Logistics
- VRL Logistics

---

## Invoice System

### Invoice Number Format
- Format: `ST/YY-YY/NNN` (e.g., ST/26-27/028)
- Financial year: April to March
- Last known invoice: #027
- Auto-increments, but user can edit

### HSN Codes
- **Default for all F2D food products**: HSN 1106 (flour/meal/powder of legumes & cereals) at 5% GST
- Verified from existing invoices and GST portal (https://services.gst.gov.in/services/searchhsnsac)
- HSN codes saved to `hsn_master.json` after each invoice finalization — never looked up again
- GST portal requires CAPTCHA so no programmatic lookup possible

### Pricing Source
- Mithila Foods master: `C:\Users\LENOVO\Desktop\bms data\F2D tech pvt ltd\Mithila Foods\Master Pricing Packing.xlsx` (sheet: FULL MASTER, column: Purchase)
- Howrah Foods master: `C:\Users\LENOVO\Desktop\bms data\F2D tech pvt ltd\Howrah Foods\Master Pricing.xlsx` (column: Purchase)
- Both loaded into `pricing_data.json` (410 entries, keyed by FBA SKU and ASIN)

### FC Address Source
- Official Amazon file: `C:\Users\LENOVO\Desktop\FC_address_and_POC_details._CB792038618_.xlsx`
- 93 FC addresses loaded into `fc_addresses.json`
- Priority addresses (BLR4, DED3, ISK3) hardcoded in `company_data.py` with exact text

---

## Shipment System

Two people work here at once: the owner plans, the warehouse packs. That is the
constraint everything else follows from.

### Why the plan is in the database
It used to be one JSON blob at repo root, overwritten wholesale by
`POST /shipment/save`. With two roles writing, whoever saved last silently
destroyed the other's work. The fix is **write separation, not locking**: the
owner writes plan rows, ops writes packing rows, and no function writes both.
There is no shared record to clobber, so no version column is needed. The two
UNIQUE indexes — `(plan_id, pack_date)` and `(day_id, asin)` — turn a repeated
save from a flaky warehouse phone into an update rather than a double-count.

`tests/test_shipment_plan_db.py` has the clobber tests in both interleavings.

### Plan lifecycle: draft → active → closed
A generated plan starts as **`draft`**, visible only to the owner. He removes
rows, fixes quantities and fills missing SKUs, then `POST /plan/{id}/finalise`
promotes it and closes the previous plan. Until then the warehouse keeps packing
the old one.

`repository.get_active_plan()` matches `active` **and nothing else**, and that
omission is load-bearing: it is what makes all eleven pre-existing packing and
download endpoints draft-blind without a single edit to any of them. Widen it and
the warehouse starts packing plans the owner has not finished.

> **`create_plan` must never call `close_active_plans()`.** It used to. With
> drafts that means uploading a CSV instantly closes the plan being packed — the
> packer's screen empties mid-shift with nothing to explain it, while the
> replacement sits invisible in draft. The close belongs in `finalise_plan`, which
> is the moment the owner actually decides.
> `test_generate_does_not_disturb_the_plan_being_packed` guards it.

### Removing rows
`excluded_at` is stamped, not deleted, so a mis-click is one click back.
`load_plan_items(..., include_excluded=False)` filters by default — forgetting the
flag then hides a row (cosmetic) rather than leaking a removed SKU onto the
packer's sheet, an Amazon upload or a GST invoice. All five downloads inherit it
through that one function.

**Excluding a row that already has boxes packed is refused (409).**
`logic.packed_units_by_asin` aggregates packing entries by ASIN and never consults
plan items, while the invoice bridge builds its lines *from* plan items — so an
excluded-but-packed row means real boxes ship with no GST line against them. The
error names the units and dates and points at `To Ship = 0` instead. The same
guard's other half: `POST /packing/{date}` drops entries for rows excluded since
the packer's phone loaded, and reports them, rather than creating the orphan.

### One place for each rule
- **Sorting** happens once, in `repository.load_plan_items()`' ORDER BY — the
  only SELECT of plan items anywhere. The dashboard, the ops screen and all four
  downloads render that order as-is. Requirement 3 came from the screen and the
  Excel disagreeing; a client-side `.sort()` would recreate it, so both templates
  are grepped for one.

  Order is **brand → category → product → weight → ASIN**: Mithila Foods before
  Howrah Foods, then P1 Sattu · P2 Chana · P3 Flours · P4 Rice · P5 Seeds · P6
  Rest. Product sits *above* weight so every size of one product is contiguous and
  the packer visits one location once.

  `brand_rank` is persisted because `'MF'`/`'HF'` cannot order alphabetically —
  H sorts before M. Category is **JOINed from `product_categories`**, never stored
  on the row: re-classifying a product then needs no row rewrite and cannot leave
  a plan silently mis-sorted against a stale key. `logic.sort_key` reads the rank
  `load_plan_items` attaches rather than re-deriving it, or an owner's override
  would apply in SQL and be ignored in Python.

  Keyword defaults come from `logic.category_for`, and **the rule order is the
  rule**: nine of the 74 real product names match several keywords, and three
  change bucket depending on which is tested first — "Bangla Chana Sattu" is a
  sattu, "Rice Atta" is a flour, "chana dal badi" is a chana. Every default is
  overridable per product in the app.

- **Rounding** happens once, in `POST /shipment/generate`, and is persisted. Use
  `logic.round_to_10`, never `round(n/10)*10` — the builtin does banker's
  rounding and turns **25 into 20** and **5 into 0**. A real need also never
  rounds away to nothing: 4 units becomes 10, because a stockout on a slow SKU
  costs the Buy Box.

### Two "left to do" numbers, not one
| Function | Answers | Subtracts stock on hand? |
|---|---|---|
| `logic.remaining_for` | what the packer must **box** | no |
| `logic.still_to_source` | what the owner must **make** | yes |

`remaining_for` deliberately does not accept an `available` argument, and a test
asserts its *signature*: stock finished on a shelf is not in a carton, so if the
packer's number subtracted it he would box 410 when the plan needs 610 and the
shipment would go out short. Found by mutation — adding an optional parameter
passed every value-based test because no caller was passing it.

(The `available` column existed and fed nothing at all from the first Shipment
build until this was fixed. Typing into it changed no number anywhere.)

### Day lifecycle
`open` → `submitted` → `verified` → `shipped`, with `held` off to one side.

A day is `held` only when cartons **AND** units are both below the minimum
(default 25 / 500). AND, not OR: these products run from 500 g pouches to 5 kg
bags, so 30 cartons of heavy bags ships on cartons alone and 900 units of
pouches ships on units alone.

**`packed` and `shippable` are two different numbers and must stay that way.**
Held units are packed — the boxes exist, and telling the floor to pack them
again would double the order — but not shippable until the day is released or
verified. Collapsing them into one number is the subtle bug this feature is
built around.

### Units are per SKU. Cartons are per DAY.
Everything else in this feature is per-SKU; this one thing is not, and the
asymmetry is deliberate. "carton is not item wise. it is random. like 500 units
packed today in 20 cartons." A carton is filled with whatever is being packed at
the time, so a mixed box belongs to several ASINs and to none of them.

It used to be `ShipmentPackingEntry.cartons`, summed onto the day. That asked the
packer a question with no answer, so he guessed or skipped it — and the guess
prefilled the **Boxes field on a GST invoice**. It is now entered directly on
`ShipmentPackingDay.total_cartons`, and `logic.day_cartons` reads it off the day.
`_recompute_day_units` must never touch it: recomputing from the entries would
zero the count on every save, silently.

`POST /packing/{date}` treats a **missing** `cartons` key as "leave it alone" and
**0** as "no cartons". The packer counts boxes last, so a units-only save posted
before then must not wipe a count he entered earlier.

The packed sheet reports cartons **per day, in its heading** — not as a column. A
per-row carton figure would put a guess in front of the accounts team as though it
were a measurement.

### Carry-over
`is_held` judges one day in isolation, which is right. `logic.carry_over` judges
the accumulated held days together, which is the other half of what was asked:
"we combine it with next day packing and then create a shipment." Without it
Monday is held, Tuesday is held, together they are a shipment, and nothing says
so — held stock accumulates until someone happens to add the columns up.

It is a prompt, never an action. Releasing stays the owner's decision, since a
big backlog may still be worth holding for a fuller truck.

`POST /shipment/generate` also warns when the plan it is replacing still has
held days: `/shipment/active` only ever returns the active plan, so those boxes
would otherwise drop off every screen while still sitting in the warehouse.

### Documents
Three working documents in both formats, plus Amazon's own file:

| Route | Who | What |
|---|---|---|
| `download/plan.{xlsx,pdf}` | admin | what to pack this week |
| `download/packed.{xlsx,pdf}?date_from=&date_to=` | ops + admin | what was boxed; ops prints this for accounts |
| `download/remaining.{xlsx,pdf}` | ops + admin | the morning clipboard sheet |
| `download/shipment-file.xlsx?mode=` | admin | Amazon upload; `remaining\|all\|verified` |

The first three share ONE column layout, asked for verbatim: `S · M · B · Brand ·
ASIN · Merchant SKU · Product · Pack Size · <quantity>`. Only the quantity column
differs. All go through `_document_rows()`, so a download cannot disagree with the
screen about order or any computed number — one code path produces both.

`packed` is open to ops because printing it for accounts is the packer's job. The
plan sheet and the Amazon upload are not: those carry projections and are the
owner's decisions.

**The PDFs build every cell as a `Paragraph`, never a bare string.** reportlab
draws a plain string at full width and lets it run over the gridline into the next
column, so a 48-character merchant SKU printed on top of the product name on most
rows of a real plan — with the whole suite green, because the bytes were a valid
PDF of a plausible size. Column widths are *measured* from the rows being rendered
(`_pdf_column_widths`); two earlier hardcoded attempts were each wrong about data
they could not see. `tests/test_shipment_documents.py` asserts table HEIGHT for
this, since an overflowing cell is the same height as a fitting one — which is
exactly why the bug was invisible.

Product, pack size and quantity are set large and bold; ASIN and merchant SKU
small and grey. Those three cells are what decide what gets packed.

### Invoice bridge
`POST /shipment/invoice-payload` turns **verified** days into the payload
`templates/invoice.html` already consumes, aggregating units per ASIN across the
selected dates (which is how combined held days become one invoice) and summing the
selected days' `total_cartons` to prefill Boxes. It refuses anything unverified with
a 400 and anything already invoiced with a 409.

It does **not** allocate an invoice number. `POST /invoice/save` remains the only
writer of the GST series. See "Known gaps" below for the one window this leaves.

---

## Scraper Details

### ASIN Validation
- Only accepts ASINs starting with `B0` (10 chars total)
- Rejects FNSKUs (start with X) and other codes

### Scrape Settings (defaults)
- Concurrency: 10 async workers
- Delay: 1.5-3.5s random between requests
- Retry rounds: 3
- Timeout: 15s per request
- Scheduled daily at 06:00

### Data Extracted per ASIN
- Title, Price (₹), Rating, Rating Count, BSR (rank + category), Seller, Fulfillment (FBA/FBM/Easy Ship), Deal status, Use By date

---

## Deployment

### Local (Windows)
- Python 3.11+ with venv
- SQLite database (`tracker.db`)
- `Start Amazon Tracker.bat` on desktop

### Production (AWS Free Tier)
- EC2 t2.micro + RDS PostgreSQL
- Caddy for HTTPS reverse proxy
- systemd service for auto-restart
- Setup script: `deploy/setup-ec2.sh`
- For PostgreSQL: add `asyncpg` to requirements, set `DATABASE_URL` env var

> **Before this deploys:** run `alembic upgrade head` on EC2. Production is still
> on the old schema, so the four shipment tables do not exist there yet. Also set
> `OPS_PASSWORD` in the environment, or the warehouse login will not work.

---

## Dependencies
```
fastapi, uvicorn[standard], sqlalchemy[asyncio], aiosqlite, alembic,
pydantic-settings, httpx, lxml, pandas, openpyxl, apscheduler,
python-multipart, itsdangerous, jinja2, aiofiles, reportlab
```

## Known gaps (deliberate, not oversights)

### The invoice attach is a second request, so there is a window
`POST /invoice/save` allocates the legally-sequential GST number and is left
strictly untouched — the 26 tests in `tests/test_invoice_save.py` guard that
sequence. Marking the packing days `shipped` is therefore a separate call,
`POST /shipment/attach-invoice`, made by the browser right after a successful
save.

If the browser dies between the two, the invoice exists while the days still
read `verified` with no `invoice_id`. The app then believes those boxes are
un-invoiced, and the double-invoice 409 in `/shipment/invoice-payload` cannot
help — nothing was recorded for it to fire on.

Accepted because that failure is recoverable (retry the attach) and is shown to
the owner rather than swallowed, whereas the alternative coupling is not
recoverable: a bug in this bookkeeping rolling back a committed invoice would
burn a number out of the GST series, and a gap in the series is a question you
answer during an audit.

Closing it properly means `/invoice/save` writing the attachment in its own
transaction — a change to the route whose tests protect the sequence, so it
wants its own commit and its own careful pass.

### `cookies.txt` is in git history with a real session token
Committed once in `23f59db`, now untracked and gitignored — but removing it from
history needs a force-push, so the token in it should be treated as exposed. The
session secret it was signed with is worth rotating.

### An ops user could still `curl` a read-only API
`require_auth` is unchanged on the ~40 pre-existing API routes; only the page
routes and the new shipment routes are role-gated. So ops could read
`/products`. Accepted for a two-person business — auditing 43 routes is not worth
it — and stated here so it is a choice rather than a surprise.

### Amazon needs the merchant SKU, not the ASIN
`fba_sku` is often empty in the source CSVs, and Amazon's upload keys on it, so
those rows are rejected on their side. Now surfaced (`missing_sku_count`, a
banner on both screens, and the shipment file writes the SKU blank rather than
substituting a plausible-looking ASIN) instead of being swallowed by a bare
`except Exception: pass`. Fixing the data is a seller-central job, not a code one.

### "Nearest 10" is arithmetic, not physics
437 → 440 is right numerically and wrong if a carton holds 12.
`logic.round_to_step` takes the step as an argument precisely so
round-to-carton-multiple is a one-function change when you want it.

### No timezone handling
`datetime.utcnow` throughout while the business runs in IST, so audit timestamps
(`submitted_at`, `verified_at`) read up to 5½ hours early. Packing itself is
unaffected: `pack_date` is an explicit `String(10)` from a date picker for exactly
this reason.

### `projections.py` still uses an unversioned JSON blob
The same design the shipment plan was moved out of, and it will hit the same wall
if two people ever edit projections at once. Out of scope here.

## Git
- Branch: `claude/stoic-allen-bb3a55`
- Remote: https://github.com/shubhammithila/amazon-tracker.git
- Auth issue: `gh` CLI not installed, need PAT or `gh auth login` to push
