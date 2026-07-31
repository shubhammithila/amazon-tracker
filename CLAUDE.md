# Amazon Tracker v2 — Project Memory

## Project Overview
Complete rebuild of Amazon product tracker + FBA invoice generator. FastAPI + httpx + lxml replacing Flask + Playwright.

## How to Run
- Double-click `C:\Users\LENOVO\Desktop\Start Amazon Tracker.bat`
- Or manually: `cd` to project dir, `.\venv\Scripts\activate`, `uvicorn app.main:app --reload --port 8000`
- URL: http://localhost:8000
- Tests: `venv/Scripts/python -m pytest -q` (454 tests; random order by default)

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

### One place for each rule
- **Sorting** happens once, in `repository.load_plan_items()`' ORDER BY — the
  only SELECT of plan items anywhere. The dashboard, the ops screen and all four
  downloads render that order as-is. Requirement 3 came from the screen and the
  Excel disagreeing; a client-side `.sort()` would recreate it, so both templates
  are grepped for one.
- **Rounding** happens once, in `POST /shipment/generate`, and is persisted. Use
  `logic.round_to_10`, never `round(n/10)*10` — the builtin does banker's
  rounding and turns **25 into 20** and **5 into 0**. A real need also never
  rounds away to nothing: 4 units becomes 10, because a stockout on a slow SKU
  costs the Buy Box.

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
| Route | Who | What |
|---|---|---|
| `download/packing-plan.xlsx` / `.pdf` | admin | the full plan, both formats |
| `download/remaining.pdf` | ops + admin | the morning clipboard sheet, portrait A4 |
| `download/packed.xlsx` | admin | daily packed units + cartons |
| `download/shipment-file.xlsx?mode=` | admin | Amazon upload; `remaining\|all\|verified` |

All go through `_document_rows()`, so a download cannot disagree with the screen
about order or any computed number — one code path produces both.

### Invoice bridge
`POST /shipment/invoice-payload` turns **verified** days into the payload
`templates/invoice.html` already consumes, aggregating units per ASIN across the
selected dates (which is how combined held days become one invoice) and summing
cartons to prefill Boxes. It refuses anything unverified with a 400 and anything
already invoiced with a 409.

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
