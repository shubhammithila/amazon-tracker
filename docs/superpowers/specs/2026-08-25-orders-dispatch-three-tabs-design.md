# Orders dispatch: three tabs, raw-stock purchasing, and per-SKU packing

**Date:** 2026-08-25
**Status:** approved, ready for implementation planning
**Supersedes:** the single-table dispatch view shipped in `2b58745`

## Why

The dispatch screen shipped as one table and reads as haphazard. Measured against today's
production data, the problem is not styling:

| Finding | Number |
|---|---|
| Rows on screen at once | **101** (33 parent + 68 size) |
| Size rows where UNITS == ORDERS | **57 of 68 (83%)** — the Orders column is mostly duplicate |
| Products with only one pack size | **7 of 33** — parent row and size row are identical twins |
| Size rows of ≤3 units | **41 of 68** — a long tail of tiny lines |
| Weight concentration | top 10 parents = **66%** of 248.15 kg |
| Longest product name / SKU | 29 / 19 chars — so column *width* was never the constraint |

101 rows and 7 columns, two of which say the same thing 83% of the time. Splitting the screen
by *question* rather than compressing one table is what fixes it.

Three questions are being asked of the same day's data, and they belong to different people:

1. **How much weight goes out, and what must I buy to cover it?** — the owner, purchasing.
2. **What has been packed against each SKU today?** — the floor, data entry.
3. **Which orders exactly, and where are they?** — reconciliation.

## Decisions taken (user's)

- **Desktop/laptop at a desk.** Not a phone. Density is acceptable; hierarchy is the fix.
- The screen serves entry, volume *and* progress — so columns are **zoned**, not dropped.
- Tab 1's "in stock" is **RAW MATERIAL in kg**, standing (not per-day), because Easy Ship
  orders are packed the same day and nothing is packed in advance. It is typed for now and
  will later be fed by an inventory tab.
- Tab 2 is **flat** — no parent rows — in the existing sort order.
- Tab 3's statuses **auto-update**, via the 30-minute scheduled refresh plus a 60-second
  local re-read. The orders job gets its **own flag** — see below.
- Every tab has a **search box**.
- **No Tracking ID column** (see below).
- Undo = revert a row to its last saved value, plus "discard all unsaved".

## Tracking ID is not obtainable — measured, not assumed

The user asked for a tracking-id column on tab 3. Four routes were probed against the live
account before designing it in:

| Route | Result |
|---|---|
| `getOrders` (all 32 fields) | no tracking field |
| `getOrderItems` | no tracking field |
| `/easyShip/2022-03-23/package` | **403** `Access to requested resource is denied` |
| `GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL` report | 1,283 rows, **all `AFN`** (FBA); **0 of our 100** Easy Ship ids |

The Reports API itself is reachable (a 3,042-row orders report generated fine), so this is not
a permissions accident — Amazon simply does not expose Easy Ship tracking without the
`Direct to Consumer Shipping (Restricted)` role, which is refused on this account.

A column that can never populate is the same class of defect as the render target with no
`<div>`: silent, and it reads as broken data. **Omitted until the role is granted.**

## Data model

### New: `product_raw_stock` — standing, per parent product

```
product_raw_stock       UNIQUE (product)
  id          Integer   PK
  product     String    parent product name, the catalogue's `name`
  raw_kg      Numeric(10, 2)
  updated_at  DateTime
  updated_by  String    who typed it
```

**No `pack_date`, and that is the point.** Raw material on a shelf does not vanish at
midnight. A per-day field would be blank every morning, so tab 1 would read "buy everything"
at 9am daily until 33 numbers were retyped.

Keyed on the parent product NAME rather than an ASIN because raw material is bulk — there is
no such thing as 500 g-flavoured raw sattu. The name is the catalogue's own `name`, the same
key `dispatch_sheet` already groups parents by.

Built to be **replaced**: when the inventory tab exists it writes this table instead of a
human, and nothing downstream changes.

### Unchanged: `order_packed_entries` — per day, per ASIN

Stays exactly as shipped: `UNIQUE (pack_date, asin)`, IST date decided by the server.

**Deliberately a separate table from raw stock.** Stock and packed are different facts entered
at different moments by different people; a shared row means one save can clobber the other.
Same write-separation reasoning the shipment feature documents (owner writes plan rows, ops
writes packing rows, no function writes both).

### Derived, never stored

All in `app/orders/logic.py`:

| Number | Formula | Shown |
|---|---|---|
| `left` | `max(0, ordered − packed)` | tab 2, per SKU |
| `to_buy_kg` | `max(0, ordered_kg − raw_kg)` | tab 1, per parent |

`to_buy` clamps at 0 — surplus raw stock is not negative purchasing. Over-packing stays
reported separately as `over_packed`, unchanged, because the boxes physically exist.

**The to-buy TOTAL is the sum of the clamped per-product values, never
`total_ordered − total_raw`.** Those two differ whenever any product has surplus, and the
subtraction is wrong: it lets a surplus of ABC Sattu offset a shortfall of Usna Chawal, and
you cannot make rice out of sattu. Worked from the real numbers below — summing the rows gives
25.00 + 0 + 18.00 = **43.00 kg**, while the subtraction gives 206.15 kg. Only the first is a
purchasing quantity. (Caught reviewing this spec, not in code: the wrong version looks
perfectly plausible in a totals row.)

A SKU with no pack size is **excluded from every kilogram figure and named on screen**, never
counted as 0. Same rule as `picking_sheet` and `shipment_weight`: treating an unknown weight
as zero makes a 47 kg sheet report 40, and that number reaches a courier.

`is_todays_dispatch` and `bucket_for` are **untouched**. The 264-order rule (ship-by == today
AND labelled) stands.

## Layout

Shared header, always visible, so switching tabs never loses the day's totals:

```
Today's dispatch · 25 Aug (IST)                    [↻ Refresh]  [⬇ Download ▾]
264 orders   301 units   248.15 kg   29 packed   272 left
──────────────────────────────────────────────────────────────────────────────
[ Weight & purchase ]  [ By SKU ]  [ Orders ]
```

### Tab 1 — Weight & purchase (33 rows; raw stock editable)

```
🔍 search product or brand…

PRODUCT                    BRAND   ORDERED     RAW STOCK      TO BUY
Usna Chawal                MF      35.00 kg   [   10.0 ] kg  25.00 kg  ⚠
ABC Sattu                  MF      22.50 kg   [   32.0 ] kg       —    ✓
Bengali Gobindobhog Rice   HF      18.00 kg   [    0.0 ] kg  18.00 kg  ⚠
   … 30 more products
──────────────────────────────────────────────────────────────────────
TOTAL                             248.15 kg      42.00 kg     43.00 kg
                                              [ ⬇ To-buy list ]
```

The TOTAL to-buy is **43.00**, the sum of the clamped rows (25.00 + 0 + 18.00) — not
248.15 − 42.00. See the derived-numbers note above for why the subtraction is wrong.

`—` rather than `0.00` when covered: a dash reads as "nothing to do", a zero reads as a
measurement. `updated_at` on hover.

### Tab 2 — By SKU (68 flat rows; the only editable tab)

```
🔍 search SKU, product or size…

PRODUCT                   SIZE   SKU               ORDERED  PACKED TODAY   LEFT
ABC Sattu                 500g   abc_sattu500g          29        [ 29 ]      0  ✓
ABC Sattu                 1 kg   abc_sattu1kg            8        [  0 ]      8
Bengali Gobindobhog Rice  2 kg   HF_gbrice_2kg     1 / 1 ord      [  0 ]      1
──────────────────────────────────────────────────────────────────────────────
TOTAL                                                  301          29      272

[ ⬇ Download ]   2 unsaved   [ Discard unsaved ]   [ Save ]
```

- **Flat, existing sort order** (brand → category → product → weight → ASIN). Product name
  repeats per row; with no grouping there is nothing to lean on.
- Orders column **removed**; rendered inline as `1 / 1 ord` only on the 11 of 68 rows where
  units ≠ orders.
- Header says **PACKED TODAY**, so the per-day scope is on screen rather than implied.
- Per-row `↶` reverts to the last saved value. Download works at any time and warns when
  there are unsaved edits, because it sends committed numbers.

### Tab 3 — Orders (281 rows, read-only, auto-updating)

```
🔍 search order id, SKU, product, city…              updated 2 min ago

ORDER                SKU       QTY  ITEM          WEIGHT   DESTINATION   STATUS
403-1589031-6753149  5kg uc     1   Usna Chawal   5.00 kg  PUNE, MH      PickedUp
```

Grouped in the same parent order as tabs 1 and 2, so all three read together.

**Auto-update, without touching Amazon from the page.** `getOrders` allows one call every
22.5 s, so the page polls LOCAL rows every 60 s while the scheduled job refreshes from Amazon
every 30 min — a status change surfaces within ~31 minutes with no reload. The poll updates
tab 3 only and **never re-renders a tab with inputs**, or it would eat the packer's
keystrokes mid-number.

### The orders job gets its own flag

`SCHEDULER_ENABLED` is `false` on production, so nothing scheduled runs today. Turning it on
would also wake the **06:00 product scrape** (10 async workers), the **07:30 keyword track**
and the **09:15 retention purge** — three dormant jobs on a 951 MB box with no swap that has
already OOM-killed a `pip install`. Waking them as a side effect of a UI change is not a
decision this feature gets to make.

So a new setting, `ORDER_REFRESH_ENABLED`, gates the orders job alone:

```python
# app/scheduler.py — setup_scheduler()
if settings.scheduler_enabled:
    ...          # scrape, keywords, purge — unchanged, still off
if settings.scheduler_enabled or settings.order_refresh_enabled:
    scheduler.add_job(scheduled_order_refresh, IntervalTrigger(minutes=30), ...)
```

`OR`, not a replacement: an installation that already turns `SCHEDULER_ENABLED` on keeps its
order refresh without having to learn a second flag. The scheduler itself must also start when
only the orders flag is set — `setup_scheduler` opens with

```python
if not settings.scheduler_enabled:
    return
```

which would skip the orders job too, so **that guard has to move down** to the three jobs it
protects rather than being deleted.

**Default `False`, and the asymmetry with `scheduler_enabled` is deliberate.** That one
defaults to `True` (`app/config.py:52`) — production sets it to `false` explicitly. So:

| | `SCHEDULER_ENABLED` | `ORDER_REFRESH_ENABLED` | orders job |
|---|---|---|---|
| fresh install | `True` (default) | `False` (default) | **runs** — via the OR |
| production today | `false` (explicit) | unset | does not run |
| production after this change | `false` (unchanged) | **`true`** (added to `.env`) | **runs** |

A fresh install therefore behaves exactly as it does today, and production changes only
because one line is added to its `.env`. The new flag is only load-bearing where the master
flag is off, which is precisely this box.

## Downloads

```
[ ⬇ Download ▾ ]
   All three (PDF)          ← combined, the floor's sheet
   All three (Excel)        ← one workbook, 3 worksheets
   ───────────────
   1 · Weight & purchase        2 · By SKU        3 · Orders
   ───────────────
   To-buy list only         ← tab 1's ⚠ rows, for purchasing
```

| Download | Format | Content |
|---|---|---|
| Combined | PDF | 3 sections, page break between; extends `build_dispatch_pdf` |
| Combined | Excel | 3 worksheets named per tab, via `build_simple_xlsx` |
| Each tab | PDF or Excel | that tab's rows only |
| To-buy list | Excel | only products where `to_buy > 0` |

Three load-bearing properties:

**One aggregation feeds the screen and every download.** All of them go through the same
`_dispatch(db)` the screen uses — the reasoning behind the shipment feature funnelling five
downloads through `_document_rows`. A download that aggregated separately is how a printed
sheet and a monitor start disagreeing about a quantity.

**Every file states its own provenance.** Subtitles carry
`25 Aug (IST) · 264 orders · 301 units · 248.15 kg`, and tab 2's adds `29 packed at 14:32`. A
printed sheet with no timestamp gets worked from tomorrow.

**The to-buy list is filtered, not sorted.** Covered products are absent rather than shown as
zero — a purchasing list is a list of things to buy. With nothing short it prints "Nothing to
buy" rather than an empty table.

Excel for to-buy (pasted into a supplier email); PDF for floor sheets (read at a bench).

## Routes

`/orders/dispatch` gains `raw_stock` and `to_buy_kg` per parent. Additive, so nothing that
reads it today breaks.

| Route | Method | Note |
|---|---|---|
| `/orders/raw-stock` | POST | `{"entries":[{"product","raw_kg"}]}` — no date, standing |
| `/orders/packed/{pack_date}` | POST | unchanged, still refuses any date but today (IST) |
| `/orders/download/dispatch.pdf` | GET | `?tab=all\|weight\|sku\|orders` |
| `/orders/download/dispatch.xlsx` | GET | same, plus `tab=tobuy` |

Migration `<rev>_product_raw_stock` (revision id assigned at implementation time),
`down_revision = d4f9a2c68b31`, **plus a newest-first branch in `deploy/update-ec2.sh`'s
baseline detector** keyed on `product_raw_stock in tables`. A stale detector stamped
production backwards once and cost two failed deploys; `tests/test_schema_migrations.py` runs
the detector and asserts the true head.

## Files

`app/orders/logic.py` · `app/orders/repository.py` · `app/routers/orders.py` ·
`app/models.py` · `app/shipment/documents.py` · `templates/orders.html` ·
`app/scheduler.py` · `app/config.py` ·
`alembic/versions/<rev>_product_raw_stock.py` · `deploy/update-ec2.sh` · `CLAUDE.md`

Tests: `tests/test_orders_dispatch.py` · `tests/test_orders_api.py` ·
`tests/test_schema_migrations.py` · `tests/test_retention_and_scheduler.py` ·
`tests/test_theme.py` (new template)

**Deploy note.** One line goes into production's `.env`:
`echo 'ORDER_REFRESH_ENABLED=true' >> /opt/amazon-tracker/.env`, then restart. Worth doing
after the code is deployed, not before, so the first scheduled run has the new tables.

## Verification

**Automated** — each must fail against today's code:

- Raw stock **survives midnight**: saved on the 24th, still readable on the 25th. This is the
  whole reason it has no `pack_date`.
- `to_buy` clamps at 0, and a covered product is **absent** from the to-buy list rather than
  present as zero.
- **The to-buy TOTAL sums the clamped rows** rather than subtracting the totals. Tested with
  one product in surplus and one short, so the two formulas give different answers (43.00 vs
  206.15) — with every product short they agree, and the test would prove nothing.
- A product whose pack size is unknown is excluded from every kg total and named.
- Raw stock is per PRODUCT, packed is per (date, ASIN): a save to one cannot touch the other.
- A repeated raw-stock save updates one row — the UNIQUE index is what guarantees it.
- All five downloads report the same units, kg and row order as the screen.
- Tab 3's 60 s poll touches no tab containing an input (asserted on the template).
- **The orders job runs with `SCHEDULER_ENABLED=false` and `ORDER_REFRESH_ENABLED=true`**, and
  the scrape / keyword / purge jobs do **not** — asserted on the registered job ids, so a
  refactor that moved the early return back to the top fails here rather than on the box.
- The reverse case too: `SCHEDULER_ENABLED=true` alone still registers the orders job, so no
  existing installation loses it.
- `is_todays_dispatch` and `bucket_for` unchanged: the 247-bug and 264-order tests pass
  untouched.

**Mutation checks** — unclamp `to_buy`; compute the to-buy total as
`total_ordered − total_raw`; add a `pack_date` to raw stock; let the poll re-render tab 2;
collapse the `—`-vs-`0.00` distinction. Each must fail a *named* test. Two
tests this session passed with the real guard deleted (the item-priority wiring, and a
monotonic-percent test whose two page counts both computed to 25.0), so this step is not
optional.

**Manual, on production after deploy** — type raw stock, reload, it persists · verify
`to_buy` by hand on the heaviest product · download all five files · confirm tab 3's status
changes on its own within ~31 min · confirm a named non-admin `orders` user sees all three
tabs and no admin panel.

## Out of scope

The inventory tab that will later feed raw stock automatically (the field is built to be
replaced, not extended) · tracking ID until Amazon grants the restricted role · any change to
`bucket_for`, the picking sheet, or the shipment tab's packing screen · invoicing from packed
counts.
