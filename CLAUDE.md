# Amazon Tracker v2 — Project Memory

## Project Overview
Complete rebuild of Amazon product tracker + FBA invoice generator. FastAPI + httpx + lxml replacing Flask + Playwright.

## How to Run
- Double-click `C:\Users\LENOVO\Desktop\Start Amazon Tracker.bat`
- Or manually: `cd` to project dir, `.\venv\Scripts\activate`, `uvicorn app.main:app --reload --port 8000`
- URL: http://localhost:8000
- Tests: `venv/Scripts/python -m pytest -q` (1205 tests; random order by default)

### Logins: named accounts, plus two shared passwords
Three ways in, checked in this order:

1. **A named account** from the `users` table (username + password). The only one
   with per-area permissions. Created from `/users-page`.
2. **`APP_PASSWORD`** — password only, full admin.
3. **`OPS_PASSWORD`** — password only, packing screen only.

`APP_PASSWORD` is checked before `OPS_PASSWORD`, so setting them to the same value
cannot demote you. A session cookie with no role or username means admin — the old
cookie was exactly `{"authenticated": True}`, and every pre-existing session and
test fixture would otherwise break. Privilege can only ever be *reduced* by an
explicit role/username, never escalated, because forging either needs the signing key.

> **The shared passwords stay on purpose.** They are the recovery path: this app has
> no password-reset email and no console, so if anything is wrong with the `users`
> table after a deploy they are the only way back in. The Users panel warns while
> `APP_PASSWORD` is live so it gets retired knowingly rather than by accident.

### Permissions are per AREA, not per role
`app/permissions.py` owns one list: Dashboard · Invoice · Portfolio · Projections ·
Shipment · Daily packing · Orders. A "role" is only a preset that fills that set in
(Owner / Packer / Accounts), and nothing stores which preset was used — a user who
was a Packer and then gained Invoice is not a Packer, and a stored label would drift
from the truth.

Two shared passwords could not express the case that actually exists: the accounts
person who prints the packed sheet and raises invoices but must not see projections
or purchase costs.

- **Deny by default.** `has()` returns False for anything unrecognised, so a new area
  is invisible to everyone until granted. The opposite default widens access on deploy.
- **`shipment` implies `packing`**, expanded on READ so changing an implication needs
  no data migration.
- **`is_admin` is a flag, not an area.** "Can change what other people see" is a
  different kind of power, and keeping them apart stops a user granting themselves the
  rest. An admin always passes every area check, so the owner cannot mis-tick himself
  out of a tab he has no other way to recover.

**The grant is read from the database on every request** (`require_area`), never
carried in the cookie. Sessions last a week, so a cookie-carried grant would make
"I removed his access" untrue for up to seven days. That immediacy *is* the feature.

> **`/ops-page` was the exception that broke.** It sat on `require_ops_or_admin`,
> which reads only the cookie — so a *disabled* named account kept the packing screen
> for a week while every other page cut it off at once. Found by testing the loop on
> production, not by reading the code. It now uses `require_packing`, which re-checks
> named accounts against the DB and still lets a shared-password session through on
> the cookie alone. The ~11 packing **API** routes deliberately keep the looser guard,
> because the warehouse must be able to work even if the users table is missing.

Passwords are `hashlib.scrypt` (stdlib — no wheel to fail building on a t2.micro),
with the cost parameters stored in the hash so they can be raised later without
locking everyone out. A generated password is shown **once** and is unrecoverable.

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
├── models.py            # DB models (Products, PriceHistory, BSRHistory, RatingHistory, SellerOffers, Keywords, KeywordRankings, ScrapeJobs, Invoices, ShipmentPlan/PlanItem/PackingDay/PackingEntry, AmazonOrder/AmazonOrderItem/OrderPackedEntry)
├── scheduler.py         # APScheduler (scrape 06:00, keywords 07:30, orders every 30m)
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
    ├── product_families.json # 205 ASINs → parent product + brand + weight. OFFLINE FALLBACK ONLY;
    │                         # the MRP sheet is the live source (app/shipment/catalogue.py)
    └── hsn_master.json      # Verified HSN codes (auto-saved after each invoice)
```

### Tabs
Dashboard, Invoice, Portfolio, Projections, Shipment, Products, Orders, Users — and
`/ops-page` for the warehouse, which is not in the nav because every link in it is
admin-only.

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

### The product list comes from the MRP sheet, live
`app/shipment/catalogue.py` reads the master sheet at **upload time** and it decides
**which products exist**, not merely which are still sold. ASIN · Name · Net Weight ·
Brand Name · Active, resolved by header NAME with positional fallback so inserting a
column cannot silently shift the Active flag onto Brand and mark the whole catalogue
dead.

> **This distinction was a real bug.** Triphala Sattu was in the sheet, marked Active,
> in two pack sizes — and could never reach a plan, because `generate` iterated
> `product_families.json` (a static 205-ASIN file it had never been added to) and used
> the sheet only for a yes/no flag. Editing the sheet would never have fixed it. The
> sheet's 271 ASINs are a strict superset of the file's 205, and for the 108 active
> ASINs in both, weight and brand agree exactly — checked before switching.

Two things are deliberately **not** taken from the sheet:

- **The merchant SKU.** Column M is blank on all 108 active rows, and the real value
  arrives in the uploaded stock CSV — Amazon's own export. Reading the sheet's empty
  column would blank the SKU everywhere and Amazon rejects those lines.
- **Nothing is dropped for a bad weight.** An unparseable Net Weight falls back to the
  static file, then 0. Weight affects sort order; a missing row affects a shipment.

Fallback order is sheet → cached copy (`app/invoice/active_products.json`) → static
file, and each degradation is named on screen. A Google outage must not stop the owner
building a plan, but an unfiltered plan must never look like a filtered one.

Because a hand-edited Active flag can now add or remove a row, `generate` returns a
`catalogue` block and the page reports it: source, counts, and the products that
appeared or vanished **by name**, capped at 8. A row count alone gives no way to notice
that a product quietly left.

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

### Closing a plan, and carrying its boxes forward
`POST /plan/{id}/close` retires the active plan. Packed-but-unshipped days — `held`,
`submitted` or `verified`, with no confirmed Amazon shipment and no invoice — **move to
the next plan**: `plan_id` is updated and `carried_from_plan_id` stamped. The target is
the current draft, or a new empty carrier plan when there is none, because the owner
closes *before* uploading the next CSV.

**The DAY moves; its units are never copied.** `logic.remaining_for` ignores `available`
by design (a test asserts its signature), so adding carried units there would tell the
packer to box 400 already-boxed units a second time. Because every aggregation reaches
days through `load_days(plan_id)`, moving that one column makes the new plan count them
with no new arithmetic: 500 planned − 400 carried = **100 still to pack**.

**Shipped days never carry.** `parse_stock_csv` sums three `afn-inbound-*` columns into
`fba_stock`, so a shipped day is already inside `deficit = projection − fba_stock`, and
carrying it would double-count. The unpacked remainder is not carried either, for the
same reason — the fresh CSV recomputes need from current sales.

Close **refuses with 409 having moved nothing** when a day is `open` or has an
`inbound_plan_id` with no confirmation (`clear_inbound_plan` scopes its cleanup by that
pair, so moving the day would orphan a plan Amazon holds). It **warns** about
shipped-but-uninvoiced days rather than blocking — they may have been invoiced outside the
app. Orphan ASINs get a To-Ship-0 row, inserted *before* the days move, so a crash leaves
an unused zero row rather than packed units with no plan row to hold them — which is the
GST-understatement state.

> **The blocked check must run BEFORE the carrier plan is created.** Built the obvious way
> it created the plan, then found the blocked day and refused, leaving a draft nobody asked
> for — and an abandoned draft is not inert: `get_draft_plan` shows it as the plan being
> edited, and the next `/generate` deletes whatever draft it finds without a word.
> `test_a_refused_close_leaves_no_phantom_carrier_plan` guards it.

> **`GET /draft` must LOAD its days, not pass `[]`.** That empty list was correct while a
> draft could never have packing days, and became wrong the moment a close started
> carrying days onto one. Found in a browser: after a close the draft screen read "500
> still to pack" while `/plan/{id}/detail` said 100 for the same plan, and the carried day
> was missing from the cards entirely.

> **`delete_draft_plans` must never delete a draft that holds packing days — it DID, and
> it destroyed real stock on production.** The owner closed a plan (19 Aug: 400 units in 9
> cartons, verified), which carried the day onto a new *carrier* draft, and then uploaded
> the next CSV. `generate_plan` calls `delete_draft_plans()` first and
> `ShipmentPlan.days` cascades `all, delete-orphan`, so the day and its seven packing
> entries were deleted with the draft. Not on the closed plan, not on the new one, not in
> the database — 400 units in real cartons with nothing in the app mentioning them.
>
> The function's own docstring asserted it was safe *because* "no packing endpoint can
> reach a draft, so a draft can never carry packing rows worth keeping". That was true when
> it was written and was invalidated by the carry-forward feature itself. **A comment
> stating an invariant is not the same as enforcing one**, and this is the second time the
> same assumption broke something (`GET /draft` above was the first).
>
> Now: a draft holding days is kept, its days move onto the plan being generated, and the
> emptied carrier is retired. An *empty* draft is still discarded, which was the original
> purpose. `test_generating_after_a_close_does_not_delete_the_carried_day` reproduces the
> exact sequence and fails with "Days on the new plan: []" against the old code.
> Recovered from `/home/ubuntu/tracker-backups/` — which is why `update-ec2.sh` backing up
> before every deploy is not ceremony.

### Reading a closed plan
`GET /plans` lists every plan with its day count, units, cartons, invoice numbers and
carry lineage **in both directions** — with only one direction a carried day looks as
though it vanished from the plan being reconciled. `GET /plan/{id}/detail` returns the same
shape as `/active`, so the history panel reuses the existing renderer rather than a second
one that could disagree about row order.

All five downloads take `?plan_id=`, threaded through `_document_rows` alone, so accounts
can reprint a closed plan's packed sheet. `attach-invoice` takes one too: it resolved only
the active plan, so closing used to make an invoice unrecordable against a shipped day,
with hand-editing the database the only way back.

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

**`POST /packing/{date}/reopen` sends a locked day back to `open`**, and it is open to
**ops**, not just the owner: a miscount is found on the floor by the person who did the
counting, and correcting it is the same manual work as the original entry. Before this,
`save_packing`'s 409 and the packing banner both said *"ask the owner to reopen it"* —
pointing at a route that did not exist, so the only real recovery was hand-editing the
database.

Reopening **clears `verified_at` and `submitted_at`**. The owner's approval is what
gates a GST invoice, so it must refer to the numbers actually on the day; leaving the
day `verified` would make his sign-off cover figures he never saw. Back to `open` rather
than `submitted`, so `submit` re-applies the hold threshold to the corrected totals.

> **A day with an `invoice_id` is refused (409), naming the invoice.** `/invoice/save`
> has already spent a number from the legally-sequential GST series against those exact
> quantities, so editing them would leave the tax document and the packing record
> disagreeing with nothing in the app able to detect it — the double-invoice guard fires
> on `invoice_id`, not on the numbers. The error carries `invoice_no`
> ("ST/26-27/028"), never the bare `invoice_number` integer (28), which would match
> nothing the owner can search for.

A day is `held` only when cartons **AND** units are both below the minimum
(default 25 / 500). AND, not OR: these products run from 500 g pouches to 5 kg
bags, so 30 cartons of heavy bags ships on cartons alone and 900 units of
pouches ships on units alone.

**`packed` and `shippable` are two different numbers and must stay that way.**
Held units are packed — the boxes exist, and telling the floor to pack them
again would double the order — but not shippable until the day is released or
verified. Collapsing them into one number is the subtle bug this feature is
built around.

### Two "left to do" numbers need a third: over-packing
`logic.remaining_for` clamps at 0, which is right — it reaches the printed morning
sheet and the Amazon upload quantity, where "-50 to pack" is not a quantity. The
cost is that *planned 50 / packed 50* and *planned 50 / packed 100* both read "0 to
pack", so a doubled row looked exactly like a finished one on both screens.

`logic.over_packed` reports the excess separately. It matters because
`/shipment/invoice-payload` bills what was **packed**: the surplus boxes ship and
appear on a GST invoice at the packed quantity, discovered at reconciliation.

It warns and never blocks — the boxes physically exist, and refusing the entry would
leave real stock unrecorded. Only the owner can resolve it, and only two ways: raise
To Ship to match, or have the surplus unpacked. The packer's warning is computed in
the browser as he types, because a server figure would arrive only after a save, by
which point he has boxed more of it.

### Units are per SKU. Cartons are per DAY.
Everything else in this feature is per-SKU; this one thing is not, and the
asymmetry is deliberate. "carton is not item wise. it is random. like 500 units
packed today in 20 cartons." A carton is filled with whatever is being packed at
the time, so a mixed box belongs to several ASINs and to none of them.

> **Never read `cartons` off a packing entry.** The field is gone, and JavaScript
> prints `undefined` rather than complaining — the owner's day columns showed
> "100/undefined" for exactly that reason. `tests/test_shipment_admin_ui.py` greps
> for `e.cartons`.

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

**An invoice raised outside this flow is recorded with the same endpoint.** When the
owner downloads the sheet, creates the shipment at Amazon and raises the invoice from
a CSV on the Invoice tab, nothing had told the app about it — those days read
`verified` for ever while app-invoiced days read "On invoice ST/26-27/046". Same
real-world state, two labels, and the verified-looking ones still offered a tick box
that would spend a **second** GST number on boxes that already had one.

"Already invoiced…" on the Shipment tab posts the ticked days to
`/shipment/attach-invoice` — the identical endpoint the automatic path uses, which is
idempotent and refuses a *different* invoice on an already-invoiced day. The invoice is
**chosen from `/invoice/history`**, never typed: days store an `invoice_id` (a row id),
and the owner only ever sees "ST/26-27/046".

> **Every message that names an invoice goes through `_invoice_numbers()`.** Three
> refusals interpolated the raw `invoice_id` — "already on invoice #5" names something
> the owner has never seen and cannot search for. One of them even carried a comment
> claiming it printed "ST/26-27/031" while printing the id. `invoice_no` also travels on
> each day in `/active`, resolved in one query rather than per card.

### The destination FC is the owner's choice, and it completes the invoice
He picks the FC (ISK3, DED3, BLR4 …) **before** downloading the upload sheet, so the
app knows the destination before Amazon does. From that one code `get_fc_info`
resolves the recipient address, the state, and — via `get_gstin_for_state` — which of
the 15 GSTINs applies and therefore whether GST is inter-state or intra-state. Those
three fields were previously left blank for him to retype from Seller Central.

`/shipment/download/shipment-file.xlsx?mode=verified&pack_dates=…&fc_code=ISK3` writes
the code onto **every row**, not once at the top: the sheet gets sorted and filtered by
hand before it is used, and a value on the row cannot be detached from it. The column is
omitted entirely when no FC is given, because an empty destination column invites
someone to fill it in later.

- **An unrecognised code is refused (400).** A typo like `ISK33` would otherwise produce
  a plausible sheet naming a warehouse that does not exist, and the same code then
  resolves to no state and no GSTIN on the invoice. We hold all 93 codes, so checking is
  free.
- **Codes are upper-cased.** `get_fc_info` upper-cases internally so the GSTIN resolves
  either way — but `ship_to` is rendered as the recipient *name*, so an un-normalised
  code puts "Amazon FC isk3" on a tax document. A test asserting only the GSTIN let a
  mutation removing this survive.
- **9 of the 93 FCs are legally unusable**: Madhya Pradesh (4), Kerala (3), Andhra
  Pradesh (2), where we hold no registration. India requires the destination FC to be an
  Additional Place of Business on a GST registration *in that state*. `/shipment/fcs`
  flags these rather than hiding them, and the payload warns if one is chosen — otherwise
  the owner gets a GST document with an empty recipient GSTIN and no hint why.

> **`intakeFromShipment` used to throw these away.** It carried a comment saying
> `shipment_id`, the FC and the place of supply "stay EMPTY" — correct while Amazon chose
> the FC and would not say which until the shipment existed, and obsolete the moment the
> owner started choosing it. The server sent all three and the screen discarded them, so
> the invoice still showed blanks with nothing failing. Found in a browser, not by a test.

### The owner picks the days; the threshold does not
"crossing the min benchmark is not the final call for making shipment." Passing
25/500 makes a day *shippable*, not *shipped* — a day over the line can still be
worth combining with the next one for a fuller truck.

So the days carry tick boxes and the owner sends a chosen subset. The previous
button took **every** verified day silently, which meant two days that should have
been two shipments could only ever be one invoice.

`invoicePick` is reconciled against the current verified days on every render
(`stillValid`), because re-verifying or generating a plan changes the list under a
selection made minutes earlier — and a tick that survived would put an unintended
day on a GST document. Leaving a verified day out is legitimate but is confirmed,
since each invoice spends a number from a legally-sequential series and the day
left behind needs its own.

### Shipment weight is calculated, never typed from scratch
`logic.shipment_weight` sums `units × pack size` and **both** paths call it — the
Shipment tab handoff and the CSV/TSV upload (`app/invoice/parser.py`) — so an
invoice raised either way cannot disagree about the weight of the same boxes.
`templates/invoice.html` has one filler, `applyShipmentWeight`, for the same reason.

Three properties, each a way to put a wrong number on a GST document:

- **It is NET.** Cartons, filler and tape are not in the catalogue, so the
  weighbridge reads higher. The label says so, and the field stays editable — a
  number that silently disagrees with the truck is worse than no number.
- **A line with no pack size is excluded AND counted.** Treating it as 0 kg would
  make a 130 kg shipment quietly report 90 while still looking complete. The
  missing lines are *named* on screen, not just tallied.
- **A hand-typed weight is never overwritten.** `weightTouched` latches on the
  first keystroke so a weighbridge figure survives a re-parse — and the tag then
  keeps reading "entered by hand". Blanking it left the owner's 412.5 under a note
  reading "390 kg", which is two numbers on screen with a label on neither.

`get_pack_weight` returns 0.0 rather than guessing, and reads
`product_families.json` rather than the live MRP sheet: the parser is synchronous
and runs during an upload, so a network fetch there would make the invoice screen
wait on Google and fail when Google is unreachable.

> **A render target must exist in the markup.** `renderInvoiceBar` was complete and
> had five passing tests, and was invisible in the browser: there was no
> `<div id="invoice-bar">`, and the function's own `if(!bar) return;` guard made
> that silent — no console error, no failing test. `tests/test_template_render_targets.py`
> now checks every `getElementById` that is written to against the ids each template
> declares, across all templates.

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

### Production — **http://13.233.144.148** (live)
- EC2 t2.micro (951 MB, no swap), Ubuntu, **Python 3.14**, SQLite on disk
- Caddy in front of uvicorn on `127.0.0.1:8000`; `systemd` unit `tracker`
- `/opt/amazon-tracker` is a **git checkout** on branch `claude/stoic-allen-bb3a55`
- Key: `C:\Users\LENOVO\Desktop\old downloads\amazon-tracker-key.pem`
- Deploy: `ssh ubuntu@13.233.144.148`, then
  `cd /opt/amazon-tracker && ./deploy/update-ec2.sh`

`deploy/update-ec2.sh` backs up and integrity-checks the DB first, installs only
missing packages, migrates, restarts, verifies over HTTP, and rolls the code back on
any failure. Four things it knows that cost two failed deploys to learn:

1. **`master` is not what runs here.** GitHub's `master` still holds the original
   *Flask* app; production was never deployed from it. Pushing to master deploys
   nothing. The `pre-v2-rebuild-backup` tag preserves that old master.
2. **Never `pip install -r requirements.txt` on this box.** It runs Python 3.14 with
   *newer* libraries than the pins (pandas 3.0.3 vs 2.2.3), so `-r` tries to
   downgrade, pandas 2.2.3 has no 3.14 wheel, pip compiles from source, and 951 MB
   OOM-kills it. The pins are a floor for a fresh install, not a target. The script
   installs only what is absent, `--only-binary=:all:`.
3. **`app/invoice/hsn_master.json` is tracked *and* written at runtime.** Git has 15
   entries; production has 87 hand-verified GST classifications. A plain checkout
   destroys them, so local changes are stashed and this file restored afterwards.
4. **The recorded Alembic revision can be behind the real schema.** `create_all()`
   used to run on every boot and would create missing tables at their *final* shape,
   skipping the migrations in between — then `upgrade head` died on "table
   product_categories already exists". Fixed at both ends: `create_all()` now runs
   only on an empty database, and the script detects the baseline by inspecting
   columns and stamps it.
5. **Every new migration MUST add a branch to that baseline detector**, newest first,
   keyed on a column the revision adds. The list going stale is a *failed deploy*, not
   a cosmetic omission: after `7c1a4e9b2d38` shipped, the newest branch still said
   `users in tables → 394fc6f28429`, so the detector saw a head schema, decided it was
   one revision older, and **stamped production backwards** — then `upgrade head`
   re-ran a migration whose columns already existed and died on "duplicate column
   name". `tests/test_schema_migrations.py` now *runs* the detector against a
   freshly-migrated database and asserts it answers with the real head. Grepping the
   script for the revision id was not enough: the id also appears in the comment
   explaining the bug, so a substring check passed with the branch deleted.

> **The script replaces itself mid-deploy, so a broken detector is self-perpetuating.**
> The rollback restores the *previous* checkout — including the previous
> `deploy/update-ec2.sh` — so the fixed script never gets a chance to run and every
> retry repeats the same failure. Break the loop by checking out just that file first:
> `git fetch origin <branch> && git checkout origin/<branch> -- deploy/update-ec2.sh`,
> then deploy normally.

**The shared logins are EMPTY by default, and that is what makes "not set" safe.**

This section used to say `OPS_PASSWORD` was not set in the server's `.env` "so the shared
packing login does not work there". That was **false**, and the reason is worth keeping:
unset did not mean disabled, it meant *the class default applied* — and the defaults in
`app/config.py` were `admin123` and `ops123`, in a public repository.

Measured, not assumed: a blank username with `admin123` returned **303 to `/` with a full
admin session**, and it did so *with* named accounts present, because `auth.py` only takes
the named-user path `if username.strip()`. Creating users adds accounts; it does not
retire the shared door.

Now all three default to `""`:

- an unset `APP_PASSWORD`/`OPS_PASSWORD` **disables** that shared login (`login` skips a
  falsy shared password, and the `settings.app_password and` guard stops a blank form
  authenticating, since `Form(...)` accepts an empty string);
- an unset `SECRET_KEY` **stops the app from starting**. It signs the session cookie, and
  a cookie with no role resolves to admin by design, so a guessable key is a full
  authentication bypass rather than a weaker mode. Failing loudly at startup is the only
  safe behaviour — `update-ec2.sh` verifies over HTTP and rolls back, so a missing key
  surfaces immediately instead of living in a log nobody reads.

The shared passwords remain **supported on purpose**: there is no password-reset email and
no console, so they are the only way back in if the `users` table is damaged by a deploy.
They just have to be set deliberately. For day-to-day access prefer a named Packer account
from `/users-page`, which can be revoked per person.

**SP-API credentials ARE set** (`SP_API_CLIENT_ID`, `SP_API_CLIENT_SECRET`,
`SP_API_REFRESH_TOKEN`, `SP_API_MARKETPLACE_ID`), and the `.env` is `chmod 600`. The
refresh token is the most valuable secret on the box — worth more than the app
password, since it reaches the seller account itself.

---

## Dependencies
```
fastapi, uvicorn[standard], sqlalchemy[asyncio], aiosqlite, alembic,
pydantic-settings, httpx, lxml, pandas, openpyxl, apscheduler,
python-multipart, itsdangerous, jinja2, aiofiles, reportlab
```

## Orders tab — today's dispatch

`/orders-page`. **The default and, for the warehouse, the only view is today's dispatch**:
parent product → pack size, sorted by kilograms, with a units-packed box per SKU and a
printable PDF. Asked for as *"show them only the data which is waiting for pickup — rest of
the orders are the responsibility of the ecom team to ship on portal"* and *"Each parent item
total weight orders … uske niche 500g, 1kg - kitne kitne units. sort it total weight wise"*.

The owner's four-section picking sheet (to pack · awaiting pickup · later · pending payment)
still exists behind an **admin-only** toggle, because those three other groups are the ecom
team's queue and showing them to the floor is what this redesign removed.

### "Today's dispatch" is NOT "awaiting pickup", and that distinction is measured
The rule is **ship-by == today AND Amazon has a label** (`logic.is_todays_dispatch`,
`LABELLED_EASYSHIP`). Verified on the live account on 2026-08-25:

| ship_by (IST) | status | Easy Ship status | orders |
|---|---|---|---|
| **25 Aug** | Shipped | **PickedUp** | **200** |
| **25 Aug** | Shipped | **PendingPickUp** | **64** |
| 25 Aug | Pending | — | 3 |
| 26–31 Aug | Unshipped | PendingSchedule | 128 |

264 orders is the day's work. All 264 had a ship-by of exactly today — none past, none
future, none missing — so `== today` is unambiguous.

> **Keying on "not yet collected" empties the screen mid-shift.** The first version used the
> `awaiting_pickup` bucket, and overnight Amazon flipped 200 of those 264 orders to
> `PickedUp`: the list drained 264 → 64 and would have taken the day's packed tally with it.
> The reconcile pass was working correctly — the *question* was wrong. A collected order was
> still packed today, so it stays. `test_a_packed_count_survives_the_order_being_collected`
> pins it.

**`bucket_for` is deliberately untouched.** It assigns one bucket per order, and today's
dispatch *crosses two of them* (`PendingPickUp` → `awaiting_pickup`, `PickedUp` → `done`), so
this is a separate predicate rather than a change to the bucketing. Folding it in would
silently move the owner's four section totals and the Excel export, both pinned by the tests
that caught the 247-order bug.

**An `Unshipped` order never appears.** No label means it is still on Seller Central for the
ecom team; putting it on the floor's sheet asks someone to box a parcel with nothing to stick
on it.

### Packed counts are the warehouse's own, and the only rows here the app writes
`order_packed_entries`, UNIQUE on **(pack_date, asin)**. Amazon does not know how many units
are in a box on this floor, so this is not a second source of truth — it writes no status and
reaches no invoice. Keyed on the **IST calendar date** decided by the server, not the browser:
`POST /orders/packed/{date}` refuses any date but today with a 409 naming the real one, since
a laptop in another timezone (or a page open past midnight) would otherwise file this
morning's count against yesterday.

Deliberately *not* a copy of `ShipmentPackingDay` — no status lifecycle, cartons, hold
threshold or submit/verify. Those exist because that data reaches a GST invoice; this does
not, and unused columns invite a future reader to wire them up.

Over-packing **warns and never blocks**, the same rule as `logic.over_packed`: the boxes
physically exist, and refusing the entry would leave real stock unrecorded. `remaining` clamps
at 0 because it reaches a printed sheet; the excess is reported separately as `over_packed`.

### The refresh reports a real percentage
`refresh.PHASE_BOUNDS` splits the bar 0–50 paging, 50–60 reconcile, 60–100 items. The
weighting is uneven because the phases are: a full run is ~2 minutes of paging and ~8 of
items. Only the item phase has a true denominator (`len(pending)`, known before the first
call) — Amazon reveals `NextToken` one page at a time, so paging progress is a fraction of the
page *cap*. The percentage is therefore **monotonic**: the actionable pass can stop at page 2
of 8 and the pending pass then starts its own page 1 of 2, which would otherwise send the bar
backwards, and a bar that jumps back reads as a fault.

### The PDF is one document with two sections
`documents.build_dispatch_pdf` — parent summary (heaviest first, sizes indented beneath) then
every order line, grouped in the same parent order. One file, not two: they are read together
at a bench, and two files get separated. A PDF has no dropdown, so nesting is indentation plus
weight. `build_simple_pdf` cannot express parent/child rows, which is why this is a sibling
function. Every cell is a `Paragraph` — reportlab draws a bare string straight over the next
column's gridline, the bug that printed SKUs on top of product names with the whole suite
green.

### It is a cache, refreshed in the background — never fetched on request
`getOrders` is rate-limited to **0.045 req/sec — one call every 22.5 seconds**, measured
(`x-amzn-RateLimit-Limit: 0.04512`). A page that called Amazon would hang, and two people
opening the tab would 429. So `app/orders/` stores orders locally and every route reads
rows. A scheduled job refreshes every **30 minutes** (90 days, 8 pages); the *Refresh now*
button pages deeper and the screen polls progress.

**The routine refresh MUST page.** Amazon caps a page at 100 orders and the measured
actionable backlog is 371, so a one-page refresh sees a quarter of the work.

### The fetch is bounded by STATUS, not by date — and that took four attempts
Seller Central showed **247 waiting for pickup, 114 unshipped, 12 pending** while every
section of the sheet read zero. Four independent causes, each of which hid the others:

1. `OPEN_STATUSES` asked only for `Unshipped` — but Amazon marks an order `Shipped` the
   instant a label exists, so the entire awaiting-pickup set was never requested.
2. The bucketing keyed on `LabelGenerated` / `ReadyForPickup`, names from Amazon's **docs**.
   The value amazon.in actually sends is **`PendingPickUp`**.
3. `bucket_for` branched on the ORDER status first, so every `Shipped` order — labelled this
   morning or delivered a fortnight ago — landed in `done`.
4. **`getOrders` pages OLDEST-FIRST and has no sort parameter.** With the first three fixed,
   a 14-day window over open statuses still spent 6 pages and returned 165 orders that were
   every one `Delivered`. Paging to reach today would have cost minutes per refresh and got
   worse with every order shipped; narrowing the window could not help either, because 100+
   orders change status daily.

The fix for (4) is `EasyShipShipmentStatuses=PendingSchedule,PendingPickUp`, which bounds the
answer by **relevance instead of date**. Measured: page 1 goes from 100 `Delivered` to 97
`PendingSchedule` + 3 `PendingPickUp`, and the complete actionable set is **371 orders in 4
pages** regardless of window. That inverted the tuning — `ORDER_REFRESH_DAYS` went from 14 to
**90**, because a wide window is now free and a narrow one silently drops an order that has
been open three weeks. `FulfillmentChannels=MFN` drops FBA at Amazon's end: the unfiltered
`Pending` page held 100 FBA `Expedited` orders and not one Easy Ship order.

**Pending-payment orders need their own pass.** They carry no `EasyShipShipmentStatus` at
all, so the actionable filter excludes all 6 of them by construction.

**Filtering creates the opposite bug, so there is a reconcile pass.** An order that gets
picked up DISAPPEARS from the actionable query, and an upsert only corrects rows it was
given — so its local row would read "waiting for pickup" for ever. Rows we hold as actionable
that a *complete* fetch did not return are re-read **by id** (`AmazonOrderIds`, verified to
work with no date filter, 50 per call). Amazon is asked what they became rather than assumed
picked up: this table is a cache of Amazon's data, and guessing would make it a second source
of truth. An empty fetch reconciles nothing — that is a failure, not an empty warehouse.

**Actionable orders win the item budget.** Items are what the sheet counts, and an order with
none is invisible on it even though its section counts the order. Ordered by purchase date
alone, 165 delivered orders took the 200-call cap and the sheet read *168 units across 265
orders* — fewer units than orders. `ids_missing_items(priority_statuses=…)` fixes the
ordering; a separate test asserts `refresh.run` actually **passes** it, because the first
version implemented the ordering and never used it.

**`getOrderItems` is one call every 2.0 seconds** — `x-amzn-RateLimit-Limit: 0.5`, measured.
`ITEMS_MIN_INTERVAL` was set to exactly 2.0 under a comment guessing the interval was academic
because the account sees "3-4 new orders a day"; the real backlog is 235 orders needing items,
and a 200-call run at dead-on 2.0s took `You exceeded your quota` on 54 of them. Now 2.2, the
same undershoot margin `ORDERS_MIN_INTERVAL` already documents. A run of 5 consecutive quota
errors abandons the item phase: once the bucket is empty every further call fails identically,
those orders keep `items_fetched_at` NULL, and stopping is what lets the next run succeed
sooner. A single 404 does not trigger it — that is one cancelled order, not an empty bucket.

### Four facts measured against the live account, each of which the obvious code gets wrong
- **Easy Ship is `ShipServiceLevel` containing `EZ`, not `FulfillmentChannel == "MFN"`.**
  Both Easy Ship and plain self-ship report MFN. Three real `S02-…` orders are MFN
  `"Standard"` and carry a ship-by of **1995-01-01** — a sentinel that, rendered as a date,
  sits at the top of every morning's sheet as 31 years overdue. It is treated as "no
  deadline".
- **Every `LatestShipDate` is `18:29Z` = `23:59 IST`.** Amazon means end of day in India.
  Timestamps are stored UTC in `*_utc` columns and converted once, on the server. The
  suffix is half the guard.
- **`ship_by_ist` is a DATE and must never go through a time formatter.** It did:
  `new Date("2026-08-25")` is UTC midnight by spec, so IST rendered it as **05:30 the next
  morning** — five and a half hours into the wrong day, in the column the warehouse plans
  against. Found in a browser; both halves were individually correct. `dateIST()` splits
  the string and a test asserts it contains no `new Date(`.
- **The catalogue join is by ASIN, never `SellerSKU`.** An order carries
  `SellerSKU: "0.5kg cs 1"`, absent from `pricing_data.json`, while its ASIN is in the
  catalogue. Names and weights come from the **live MRP sheet** (271 ASINs), not
  `product_families.json` (205, and it calls Chana Sattu just "sattu").

### The two actionable sections are two different physical jobs
`EasyShipShipmentStatus` distinguishes them, and **it decides the bucket before the order
status is consulted** — that ordering is cause (3) above. `PendingSchedule` means no label
yet, so the job is pick-pack-label; `PendingPickUp` means labelled and boxed, so the job is
hand it to the courier. Both arrive under different ORDER statuses (`Unshipped` and
`Shipped`), which is exactly why the order status cannot lead.

`PendingPickUp` is **measured, not documented**. The doc names `LabelGenerated` and
`ReadyForPickup` are kept as aliases — harmless, possibly right on another marketplace — but
neither is ever sent here. An unrecognised status on a `Shipped` order is filed `done` rather
than guessed into a picking section: absent from the sheet is recoverable, a phantom pick is a
wasted trip and a doubt about every other row.

All four sections render even when empty — on the admin panel, which is where they now live:
one that vanished would read as a bug rather than an empty queue. `awaiting_pickup` is
deliberately absent from that panel, because it IS today's dispatch above and showing the same
parcels twice under two different rules is how two numbers for one thing start to disagree.

### Read-only towards AMAZON, and deliberately so
No ship button, no label download. **Every Easy Ship endpoint returns 403** — the app lacks
`Direct to Consumer Shipping (Restricted)`, and an RDT scoped to Easy Ship is refused with
*"Application do not have access to some or all requested resource"*. `GET_EASYSHIP_DOCUMENTS`,
`/mfn/v0` and `/shipping/v2` are all 403 too. The role is being applied for; a control that
always errors would read as a broken app.

**Nothing overwrites an order status**, and there is no local "shipped" or "delivered" tick:
Amazon's status is the single source of truth for what Amazon knows, and a second one is the
class of bug the shipment feature's write separation exists to avoid.

> The units-packed counter added later is not that second source of truth, and the distinction
> is worth keeping straight: it records something Amazon has **no opinion about** — how many
> units are physically boxed on this floor right now. It writes no order row, no status and no
> invoice. An earlier note here said "no local packed tick" flatly; that was right when the
> screen only mirrored Amazon and became wrong when the warehouse needed its own worksheet.

> **Real-time is possible but needs AWS.** `ORDER_CHANGE` notifications are reachable —
> `GET /notifications/v1/subscriptions/ORDER_CHANGE` returns 404 ("no subscription"), not
> 403, and `/notifications/v1/destinations` returns 200 with a **grantless**
> (`client_credentials`) token rather than the seller's refresh token. But Amazon delivers
> only to SQS or EventBridge, so it needs a queue plus a consumer process. Deferred: 30
> minutes was judged enough.

`orders` is a per-area permission, so the warehouse can be granted the sheet without the
rest of the app.

## Known gaps (deliberate, not oversights)

### The deal badge depends on which page Amazon serves you
`extract_deal` keys on STRUCTURE, not on the sale's name: `#dealBadgeSupportingText`
(the badge's visible text) and `data-csa-c-painter="dp-deal"` (Amazon's own marker).
It previously matched a hardcoded phrase list, so during the Freedom Sale — badge text
**"Freedom Sale Deal"** — every row read No. A phrase list cannot work: Amazon renames
the sale every few months, and the failure is silent because No is usually correct.

Three traps, all covered by `tests/test_scraper_deal_badge.py`:

- `dealBadge_feature_div` is on **every** product page, deal or not. Its presence cannot
  be the test.
- The `aok-hidden` screen-reader spans hold the text with the countdown unsubstituted
  (`"Freedom Sale Deal NO_OF_HOURS hours"`) because that JavaScript never runs here.
  Text containing `NO_OF_` is rejected.
- Ad carousels carry deal markup for **other** products — 7 such mentions on one page —
  so the XPath is scoped to the badge region.

> **The same ASIN legitimately returns Yes locally and No on EC2, and it is not a parser
> bug.** Measured on six ASINs, three fetches each, both machines:
>
> | ASIN | laptop | EC2 | |
> |---|---|---|---|
> | B0CWGXYLT6 | Yes ₹173 | Yes ₹173 | agree |
> | B0CY88658Y | Yes ₹178 | Yes ₹178 | agree |
> | B0D8157HND | Yes ₹118 | Yes ₹118 | agree |
> | B0D817HX57 | No ₹289 | No ₹289 | agree |
> | B0CY84RYRG | **Yes ₹295** | **No ₹349** | differ |
> | B0CY85DH38 | **Yes ₹414** | **No ₹569** | differ |
>
> **Deal and price move together in every row.** That is the signature of two different
> OFFERS being served, not of a mis-read badge — a parser fault would disagree about the
> badge while the price stayed identical. Where the price matches, the badge matches; the
> two rows that differ are ₹54 and ₹155 cheaper from the laptop, because the deal is what
> sets that price. The EC2 page is complete (buy box, price block, 2.07 MB) — it simply
> carries the non-deal offer, and 6/6 repeat fetches agree.
>
> So Amazon shows datacentre IPs a different offer, exactly as it blocks httpx from this
> machine (below). Deals appear on the dashboard only for products whose deal is offered
> to the EC2 IP as well. Fixing it properly needs residential-proxy egress for the
> scraper, which is a cost and an operations decision rather than a code change — noted
> here rather than silently worked around.


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
