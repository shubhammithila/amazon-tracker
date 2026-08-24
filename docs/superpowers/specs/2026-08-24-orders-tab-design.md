# Orders tab: a daily picking sheet from Amazon Easy Ship orders (phase A)

Design agreed 2026-08-24. Status: approved, not yet implemented.

## Context

Asked for: *"I want to build an orders tab now in this app which will fetch easy ship
orders from amazon and display them in a tabular format. I want to make a orders tab from
where I can bulk ship my orders and download shipping labels."*

And, crucially, the reason: *"after the orders are packed and shipped on the portal my
warehouse team is able to reconcile the data. the orders which have to be shipped today.
item wise weight wise qty totalled. total number of orders of each item and total orders."*

**That second statement is the feature.** This is not an order log — it is a **daily picking
sheet** that the warehouse prints, plus a reconciliation view. The order table is the raw
material; the aggregate is the product. Prototyped against live data and it works:

```
=== PICKING SHEET (sample of 5 real orders) ===
PRODUCT              SIZE    BR   QTY  ORDERS
Ragi Atta            1 kg    MF     2       2
Chana Sattu          500g    MF     1       1
Posta                100g    HF     1       1
Roasted Chana        500g    MF     1       1
TOTAL                               5       5 orders
```

**Bulk shipping and label download are not buildable today.** Probed against the live
account: `getOrders` returns 200, but every Easy Ship endpoint returns
`403 Unauthorized` — as do the Easy Ship reports and the Merchant Fulfillment fallback.
An RDT scoped to Easy Ship is refused with `400 — "Application do not have access to some
or all requested resource"`, which is Amazon stating the role gate is absolute.

| Surface | Result |
|---|---|
| `/orders/v0/orders`, `/orders/v0/orders/{id}`, `.../orderItems` | 200 |
| Reports API, Feeds API, Tokens API (generally) | 200 / 201 |
| `/easyShip/2022-03-23/*` (all five operations) | **403** |
| `GET_EASYSHIP_DOCUMENTS`, `GET_EASYSHIP_WAITING_FOR_PICKUP` reports | **403** |
| `/mfn/v0/*`, `/shipping/v2/*` | **403** |
| RDT scoped to Easy Ship | **400, "Application do not have access"** |

The missing role is `Direct to Consumer Shipping (Restricted)` — one of four restricted
SP-API roles, requiring a data-protection attestation, a security questionnaire and a
three-stage review. The owner is applying for it in parallel; this spec is **phase A**, the
read-only screen, which needs only the roles already held.

**Investigated and rejected:** Unicommerce and Shipway are Appstore-listed *public* apps,
but public-vs-private governs distribution, not roles — Amazon gates roles on data
sensitivity. Their Easy Ship "support" is in fact self-ship: they read orders, ship with
their own couriers (Delhivery, Xpressbees), and push tracking back via
`POST_ORDER_FULFILLMENT_DATA`. That feed *is* reachable on the current roles (verified,
200), so the architecture is available — but it means abandoning Easy Ship, its Prime badge
and Amazon pickup. A business decision, out of scope here. A seller cannot borrow a third
party's role: RDT delegation is application-to-application and initiated by the role
holder.

**Decisions taken (yours):** read-only tab now, role application in parallel · fetch line
items as well as orders · **90-day** rolling retention · no ship button at all · an
`orders` permission area so the warehouse can see the picking sheet.

## Two measured constraints that force the architecture

**1. `getOrders` is rate-limited to 0.045 req/sec — one call every 22 seconds.**
Amazon returned `x-amzn-RateLimit-Limit: 0.04512`. 100 orders came back with a
`NextToken`, so 90 days needs paging, and a full fetch takes minutes. Fetching on page load
would hang the screen for 22s per page and 429 as soon as two people opened it.

**2. Every `LatestShipDate` is `18:29 UTC`, which is `23:59 IST`.**
Amazon means "end of day in India". Rendering raw UTC shows every ship-by deadline ~5½
hours early, on a screen whose only job is "what must go out today". CLAUDE.md lists "no
timezone handling" as a known gap; this feature is where it starts to cost something.

## Architecture: stored, not fetched

Orders live in the database. A background job refreshes them. The tab reads local rows
only, so it opens instantly. This is the pattern the app already uses for `price_history`
and `bsr_history` via the 06:00 scheduled scrape — followed rather than reinvented.

```
APScheduler job (daily) + a manual "Refresh now" button
    ↓  getOrders      — paged, 22s apart, then filtered to ShipServiceLevel containing EZ
    ↓  getOrderItems  — only for orders with items_fetched_at IS NULL
amazon_orders + amazon_order_items   (90-day rolling)
    ↓
GET /orders  →  local rows only, instant
```

Three new modules, each with one responsibility, mirroring the `app/shipment/` split that
has held up well:

| File | Responsibility |
|---|---|
| `app/orders/spapi_orders.py` | The ONLY caller of the Orders API. Paging, rate limiting, retries. |
| `app/orders/repository.py` | The ONLY reader/writer of order rows. Upsert by `amazon_order_id`. |
| `app/routers/orders.py` | HTTP surface: list, refresh, export. Makes no SP-API calls. |

This split is also what makes phase B additive: the ship/label actions become a fourth
module calling `spapi_orders.py`, with no change to the table.

**Refresh is serialized and reports progress.** It refuses to start while one is running —
two concurrent refreshes would both burn the 22-second budget and 429 each other — and
exposes a status the screen polls, because a silent multi-minute job is indistinguishable
from a broken one. A 429 or crash mid-page must not lose the pages already stored.

## Data

```
amazon_orders        amazon_order_id (UNIQUE), purchase_date_utc, status,
                     easyship_status, order_total, currency, ship_service_level,
                     latest_ship_date_utc, city, state, postal_code,
                     items_ordered, items_shipped, is_prime, is_cod,
                     first_seen_at, last_refreshed_at, items_fetched_at
amazon_order_items   order_id FK, asin, seller_sku, title, quantity_ordered,
                     quantity_shipped, item_price, item_tax, promotion_discount
```

`amazon_order_id` is UNIQUE so a re-refresh updates rather than duplicates — the same
reasoning as the `(plan_id, pack_date)` index on packing days.

**Timestamps are stored UTC and displayed IST, converted in ONE place.** Columns are named
`*_utc`; one helper does the conversion for rendering. Naming the columns is half the guard:
a future reader cannot mistake the stored value for local time.

**The join to the catalogue is by ASIN, never by SellerSKU.** Measured: an order carries
`SellerSKU: "R-bss 1 kg"`, absent from `pricing_data.json`, while its `ASIN: "B0G2MKVVB8"`
*is* in the catalogue. Easy Ship SKUs are a different namespace from FBA SKUs. Joining on
SKU would match nothing and render every row as an unknown product. `seller_sku` is still
stored — it is what Amazon's label shows — but it is not the key.

**Product name and weight come from the LIVE MRP sheet** (`app/shipment/catalogue.py`),
not from `product_families.json`. Measured: the live sheet holds **271** ASINs and names the
product `"Chana Sattu"`; the static file holds **205** and calls the same thing `"sattu"`.
Using the file would give the warehouse worse names on its picking sheet and miss 66
products entirely. `catalogue.load_catalogue()` already degrades sheet → cached copy →
static file and reports which, so a Google outage does not empty the sheet.

**An ASIN the catalogue does not know is shown, not dropped** — with its raw title and
SellerSKU, flagged as unrecognised. A missing row on a picking sheet is stock that never
gets packed; a flagged row is a question someone answers.

**These rows are a cache of Amazon's data, not a record of our own.** If a value is wrong
the fix is a refresh, not an edit, which is why the feature has no editing at all.
`first_seen_at` distinguishes "new since I last looked" from "Amazon changed it".

**Retention:** 90 days rolling, matching the app's existing `DATA_RETENTION_DAYS=90` and
purged by the job that already prunes `price_history`, rather than adding a second
retention concept.

**Easy Ship orders only, filtered on `ShipServiceLevel` containing `EZ`** — not on
`FulfillmentChannel=MFN`. Measured: three `S02-…` orders came back as MFN with
`ShipServiceLevel: "Standard"` and a ship-by of **1 January 1995**, a sentinel from a
different channel. Bucketing those by date would put a 31-year-overdue row at the top of
the packer's sheet every morning. A 1995 date is treated as "no deadline" and never
rendered as one.

## The two groups the warehouse actually works from

Asked for: *"unshipped due today and waiting for pickup due today, and the rest of them
separately … the orders which have been shipped on the portal and generated labels which
are waiting for pickup today, and the orders which are unshipped on the portal but need to
be shipped since it is to be shipped today."*

Those are **two different physical actions**, and `EasyShipShipmentStatus` distinguishes
them:

| Group | The physical job | Detected by |
|---|---|---|
| **To pack & ship** | pick, pack, generate the label in Seller Central | `OrderStatus` Unshipped/PartiallyShipped **and** `EasyShipShipmentStatus = PendingSchedule` |
| **Waiting for pickup** | already boxed and labelled — hand to the courier | an Easy Ship status that is neither pending nor finished |
| Done / not actionable | — | `PickedUp`, `Delivered`, `ReturnedToSeller`, `LabelCanceled`, `Canceled` |

Measured 2026-08-24: **97 unshipped Easy Ship orders, every one `PendingSchedule`** —
nothing labelled and awaiting pickup, consistent with a morning before packing starts.

> **The "waiting for pickup" bucket is defined by exclusion, deliberately.** Across 90 days
> this account only ever showed `PendingSchedule`, `PickedUp`, `Delivered`,
> `ReturnedToSeller` and `LabelCanceled` — never `LabelGenerated` or `ReadyForPickup`,
> presumably because labels are generated and collected the same day. Hardcoding two status
> strings that may never appear would silently produce an always-empty section. So the
> bucket is "actionable but not pending", and the raw status is rendered on the row so an
> unexpected value is visible rather than mis-bucketed.

## The screen

Nav gains an **Orders** tab via `templates/nav.html`, which is `include`d —
`tests/test_nav_consistency.py` exists because the nav was once copy-pasted and
`projections.html` silently lost the Shipment link.

**Default view: the picking sheet**, three sections on one page.

```
TO PACK & SHIP TODAY — due 24 Aug or earlier            67 orders · 89 units
  PRODUCT              SIZE   BR   QTY  ORDERS      KG
  Chana Sattu          500g   MF    24      22   12.00
  Ragi Atta            1 kg   MF    12      12   12.00
  Bengali Posta        100g   HF     2       2    0.20
  …
  TOTAL                             89      67   47.30

WAITING FOR PICKUP — labelled, not yet collected          0 orders
  (empty until labels exist; the section says so rather than vanishing)

LATER — due 25 Aug onwards                               30 orders
  (collapsed)
```

Aggregated by **product + weight + brand**, quantity-descending so the big picks lead.
`ORDERS` is how many orders contain that line, which is what makes "24 units across 22
orders" readable.

**Both a size breakdown AND a kilogram total, because they answer different questions.**
The size column tells the packer which shelf to visit — 500g and 1kg of one product are
separate lines, never collapsed, or he goes to the wrong bin. The `KG` column is
`pack size × quantity`, and its total is what the courier and the vehicle care about. One
without the other leaves a real question unanswered: 24 pouches of 500g and 12 bags of 1kg
are the same 12 kg but a completely different picking job.

Verified against the live sheet: **all 271 catalogue ASINs carry a real weight — none is 0
or missing** — so the kilogram total is always complete rather than quietly under-counting.
It is **net weight**, exactly as `logic.shipment_weight` is on the shipment side: cartons,
filler and tape are not in the catalogue, so a weighbridge reads higher. The column is
labelled net for that reason — the same trap already documented for the invoice weight.

**A line whose weight is unknown is excluded from the KG total AND named on screen**, never
treated as 0. A 47 kg sheet that silently reports 40 is worse than one that says "47 kg,
plus 2 lines with no pack size".

**Overdue orders sit inside the first section, flagged red** — not in a fourth section. A
missed deadline should make today's sheet louder, not hide in its own box.

**Buckets are computed at render time from today in IST**, never stored. A sheet opened
tomorrow is correct without a refresh having run.

**Second view: the order list**, for reconciliation — order id, dates, status, Easy Ship
status, items, total, destination. Status chips, search over order id / SKU / title / city,
Excel export via the existing `documents.build_simple_xlsx`.

Both views come from the same stored rows, and the aggregate is computed by **one pure
function** so the sheet and the list cannot disagree — the same single-source property
`logic.sort_key` has for row order.

A **refresh banner**: "Refreshed 14 minutes ago · 3 new orders", with a *Refresh now*
button that disables while running and polls progress. Prominent, because reconciliation
against a stale sheet is the failure mode here: an order shipped in Seller Central leaves
the picking sheet only on the next refresh.

**Access: a new `orders` area in `app/permissions.py`.** The warehouse needs the picking
sheet, so this is granted per person like every other area — deny-by-default, read from the
database on every request, so revoking is immediate. Admin passes as always.

**Deliberately absent:** no ship button, no label download (phase B, and 403 today — a
control that always errors reads as a broken app). No local "packed" tick: Amazon's status
is the single source of truth, and a second one would be the class of bug the shipment
feature's write-separation design exists to avoid.

## Verification

**Automated**
- The SP-API client is tested against fixtures **recorded from the real payloads probed on
  2026-08-24**, not invented shapes. Invented fixtures are what would have missed the
  `SellerSKU` namespace difference and the 1995 sentinel date.
- **The aggregate is the feature, so it gets the most tests.** One pure function takes order
  rows plus the catalogue and returns the picking sheet. Asserted: quantities sum per
  product+weight+brand; `ORDERS` counts orders not lines (two units of one SKU in one order
  is qty 2, orders 1); the totals row equals the sum of the rows; and an order containing two
  different sizes of one product produces two lines, not one — collapsing sizes would send
  the packer to the wrong shelf.
- **The KG column and the size lines are tested together**, on a case where they disagree:
  24 × 500g and 12 × 1kg are both 12 kg, so a test that used equal weights could not tell a
  correct total from one that summed pack sizes without multiplying by quantity.
- A line whose pack size is unknown is EXCLUDED from the kilogram total and named on screen —
  asserted directly, because treating it as 0 makes a 47 kg sheet quietly report 40.
- IST bucketing on the real case: `2026-07-12T18:29Z` renders as `12 Jul 23:59`, and an order
  due `23:59 IST` today is in the "today" bucket, not tomorrow's.
- A `1995-01-01` ship-by is bucketed as "no deadline" and never rendered as a date.
- Only `EZ` service levels appear; the three `S02-…` `"Standard"` orders are excluded.
- An ASIN absent from the catalogue still appears on the sheet, flagged — asserted directly,
  because the failure mode is silent omission of stock that must be packed.
- `getOrderItems` is called only where `items_fetched_at IS NULL`, so refreshing 100 known
  orders makes zero item calls.
- Refresh refuses to run concurrently; a 429 partway through paging keeps the pages already
  stored and the next run resumes.
- A SKU-based catalogue join is asserted **absent**, so reintroducing it fails.
- The `orders` permission area denies by default: a user without it gets 403/redirect, and
  granting it takes effect on the next request (read from the DB, never the cookie).
- Nav consistency, theme (no hardcoded colour, no second `:root`) and render-target guards.

**Manual**, on `http://localhost:8020`: run a refresh and watch progress · confirm the
picking sheet totals match a hand count of one product · confirm ship-by reads 23:59 not
18:29 · confirm the three sections split as expected · print the sheet and check it is
legible on paper · export to Excel · sign in as the warehouse account and confirm the tab is
visible with the `orders` area and gone without it.

## Out of scope

Phase B (pickup scheduling, label download) pending the restricted role. No self-ship /
own-courier route. No buyer PII — `BuyerInfo` is empty without the PII role, and street
addresses are deliberately not sought; the picking sheet needs city and state only. No
editing of order rows, and no local "packed" tick.

No **gross** weight and no carton count on this sheet. The KG column is net, from the
catalogue; packaging weight is recorded nowhere in this app, and a guessed gross figure put
in front of a courier is the same mistake as the per-SKU carton count that was removed from
the packed sheet — a number the warehouse reads as a measurement when it is an invention.
