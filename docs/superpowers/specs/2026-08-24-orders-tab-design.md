# Orders tab: Amazon Easy Ship orders, read-only (phase A)

Design agreed 2026-08-24. Status: approved, not yet implemented.

## Context

Asked for: *"I want to build an orders tab now in this app which will fetch easy ship
orders from amazon and display them in a tabular format. I want to make a orders tab from
where I can bulk ship my orders and download shipping labels."*

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
items as well as orders · 30-day rolling retention.

## Two measured constraints that force the architecture

**1. `getOrders` is rate-limited to 0.045 req/sec — one call every 22 seconds.**
Amazon returned `x-amzn-RateLimit-Limit: 0.04512`. 100 orders came back with a
`NextToken`, so 30 days needs paging, and a full fetch takes minutes. Fetching on page load
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
    ↓  getOrders      — paged, 22s apart, FulfillmentChannels=MFN
    ↓  getOrderItems  — only for orders with items_fetched_at IS NULL
amazon_orders + amazon_order_items   (30-day rolling)
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

**The join to the existing catalogue is by ASIN, never by SellerSKU.** Measured: an order
carries `SellerSKU: "R-bss 1 kg"`, which is absent from `pricing_data.json`, while its
`ASIN: "B0G2MKVVB8"` *is* in `product_families.json`. Easy Ship SKUs are a different
namespace from FBA SKUs. Joining on SKU would match nothing and render every row as an
unknown product. `seller_sku` is still stored — it is what Amazon's label and the packer
show — but it is not the key.

**These rows are a cache of Amazon's data, not a record of our own.** If a value is wrong
the fix is a refresh, not an edit, which is why the feature has no editing at all.
`first_seen_at` distinguishes "new since I last looked" from "Amazon changed it".

**Retention:** 30 days rolling, purged by the job that already prunes `price_history`,
reusing the existing `DATA_RETENTION_DAYS` machinery rather than adding a second retention
concept.

## The screen

Nav gains an **Orders** tab via `templates/nav.html`, which is `include`d —
`tests/test_nav_consistency.py` exists because the nav was once copy-pasted and
`projections.html` silently lost the Shipment link.

One table, **Unshipped first** by default. Same reasoning as unpriced-active-first on the
Products tab: the screen exists to surface what needs action, and sorting by date buries
the two orders that must go out today among 98 delivered ones.

```
ORDER          DATE (IST)     SHIP BY (IST)   STATUS      EASY SHIP   ITEMS                TOTAL  DESTINATION
403-758…5960   24 Aug 11:59   26 Aug 23:59    Unshipped   —           Chana Sattu 1kg ×2   ₹738   NAVSARI, GUJARAT
405-966…6768   23 Aug 09:14   25 Aug 23:59    Shipped     Delivered   Black Sesame 1kg     ₹247   DUMKA, JHARKHAND
```

Status chips (`Unshipped / Shipped / Cancelled / All`), a search box over order id, SKU,
title and city, and an Excel export of the current view through the existing `openpyxl`
helpers.

A **refresh banner**: "Refreshed 14 minutes ago · 3 new orders", with a *Refresh now*
button that disables while running and polls progress.

**Access: admin only.** Orders carry order totals and buyer destinations. Giving the
warehouse this screen means adding an `orders` area to `app/permissions.py`, which is
per-area and deny-by-default — worth doing deliberately in phase B when there is a ship
button worth granting. Admin-only today is the safe default and widening later needs no
data migration.

**Deliberately absent:** no editing, no ship button, no label download. Those are phase B
and would 403 today, and a button that always errors reads as a broken app rather than a
missing permission. The table reserves a row-actions column so they slot in without a
rewrite.

## Verification

**Automated**
- The SP-API client is tested against fixtures **recorded from the real payloads probed on
  2026-08-24**, not invented shapes. Invented fixtures are exactly what would have missed
  the `SellerSKU` namespace surprise.
- IST conversion unit-tested on the real case: `2026-07-12T18:29Z` must render as
  `12 Jul 23:59`, not `12 Jul 18:29`.
- Refresh refuses to run twice concurrently.
- A 429 partway through paging keeps the pages already stored, and the next refresh
  resumes rather than restarting.
- `getOrderItems` is called only for orders with `items_fetched_at IS NULL`, so a second
  refresh of 100 known orders makes zero item calls.
- The ASIN join resolves against `product_families.json`; a SKU-based join is asserted
  *absent*, so reintroducing it fails.
- Nav consistency, theme (no hardcoded colour, no second `:root`) and render-target guards,
  as for every screen.

**Manual**, on `http://localhost:8020`: run a refresh and watch progress · confirm ship-by
reads 23:59 not 18:29 · confirm Unshipped sorts first · search by city and by SKU · export
to Excel · confirm the tab 403s or redirects for an ops-only login.

## Out of scope

Phase B (pickup scheduling, label download) pending the restricted role. No self-ship /
own-courier route. No buyer PII — `BuyerInfo` is empty without the PII role, and street
addresses are deliberately not sought. No editing of order rows. No `orders` permission
area until there is an action to grant.
