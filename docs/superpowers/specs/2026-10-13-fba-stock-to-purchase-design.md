# FBA stock from the API, and "To purchase" on Projections and Profit

Date: 13 Oct 2026. Status: design approved, not built.

## Why

Asked for as *"check how we get the FBA stock from this file… calculate the weights of each sku and
then total it to find the parent's weight which is in the FBA warehouse or in transit… the units to
be considered as there in FBA if the shipment has been created or is inbound or receiving or
anything… check these columns in the API… get the data every day… update it in the projections tab
as stock available in FBA and subtract it from the stock to hold to get the stock to purchase"*, then
*"add the column to purchase in the profit tab as well."*

### What was measured (13 Oct 2026, Manage FBA Inventory CSV `130704020737.csv` + a live API call)

- **The Shipment tab's CSV rule under-counts by 2,925 units.** `parse_stock_csv` sums nine columns
  (K, M, P-V) over FBA SKUs: **7,711 units**. Amazon's own total (N) minus unsellable (L) for the
  same SKUs: **10,636**. The gap sits on 17 SKUs, for example:

  | SKU | 9 columns | column N | API inbound shipped |
  |---|---|---|---|
  | abc_sattu500g FBA | 389 | 913 | 722 |
  | 0.25 fc hj FBA | 4 | 436 | 432 |
  | HF_gbrice_1kg_FBA | 0 | 340 | 340 |
  | 0.4kg mk FBA | 0 | 240 | 240 |

  The CSV's `afn-inbound-shipped-quantity` column does not carry these shipments; Amazon's total
  and the API do. So the Shipment tab currently plans production for boxes already on their way to
  Amazon.
- **`GET /fba/inventory/v1/summaries` works on this account** (`details=true`,
  `granularityType=Marketplace`, India marketplace): 8 pages, 354 SKUs (242 FBA, 112 flex/merchant),
  a few seconds, no report queue. Per SKU it returns fulfillable, inbound working / shipped /
  receiving, reserved (total, incl. pending transshipment = FC transfers), researching,
  unfulfillable (damaged) and future supply.
- Live total held in FBA or in transit: **10,026 units, about 7,735 kg** (Chana Sattu 1,426 kg, ABC
  Sattu 837 kg, Kulthi Dal 774 kg, ...).
- Flex SKUs report a placeholder 999 fulfillable. They are not FBA stock and stay excluded, by the
  existing `portfolio.logic._channel_of` token rule (the FBA suffix, any case, after space,
  underscore or hyphen).
- The only Amazon skill available (`ads-amazon`) covers ads audits, not inventory, so the design
  rests on the API probe above.

## Decisions (the owner's)

1. **Approach A**: the FBA Inventory API, not the Reports API copy of the CSV and not the upload.
2. **Both tabs use the API stock.** The Shipment tab keeps the uploaded CSV for sales and merchant
   SKUs; its FBA stock comes from the API.
3. **Projections gets two columns only**: In FBA / transit, and To purchase. No "to send" column
   until warehouse inventory exists.
4. **Freshness: daily plus on demand.** A morning job stores the day's snapshot; generating a
   shipment plan fetches fresh first; a Refresh stock button on Projections.
5. **The Profit tab also gets "To purchase"** (after this ships), like its "Total stock to hold".

## The rule

```
units held (one FBA SKU) = fulfillable + inbound working + inbound shipped + inbound receiving
                         + reserved (total) + researching + future supply (reserved + buyable)
                           -- unfulfillable (damaged) is excluded
FBA kg (one row)         = sum over the row's child ASINs of units held x pack weight (MRP sheet)
To purchase (one row)    = max(0, Total stock to hold - FBA kg)
To purchase (total)      = sum of the per-row figures, never Total - FBA at the total level
```

- The same rule the owner gave for the CSV ("add the columns; created, inbound, receiving or
  anything counts"), applied to fields that are complete.
- **Clamped per row, summed after clamping**, the Orders tab's `to_buy` rule: a surplus of one
  product must never cancel a shortfall of another.
- **Unknown is a dash, never 0.** Unknown pack weight, no snapshot, or no Total stock to hold makes
  FBA kg and/or To purchase a dash. A product with no snapshot is never shown as "buy everything".
- Warehouse stock is NOT subtracted yet. The screen says "before warehouse stock" in the column's
  hover note; the team subtracts warehouse stock by hand until the warehouse inventory feature lands.

## Components

### `app/inventory/` (new, read-only towards Amazon)

- **`fetch.py`**: `fetch_summaries(client=None) -> list[dict]`. Pages `nextToken` to exhaustion through
  `shipment.spapi._get` (its token cache and error types). A page cap (30) that is hit is an error,
  not a silent truncation.
- **`logic.py`** (pure):
  - `held_units(details) -> int`, the rule above.
  - `fba_only(summaries)`: drops non-FBA SKUs via `_channel_of`.
  - `by_asin(rows) -> {asin: units}`: summed across an ASIN's FBA SKUs (an ASIN can carry two).
  - `looks_empty(rows)`: True when more than `ALL_ZERO_STOCK_THRESHOLD` (10, the Shipment tab's
    constant, imported) SKUs are all zero, the same parse-failure guard the CSV has.
- **`repository.py`**: `save_run`, `latest_good_run`, `units_by_asin(run_id)`, `purge(keep=30)`.
- **`refresh.py`**: `run(source)` does fetch, guard, store, and returns the run. One flag prevents
  two at once; `finally` clears it (the Portfolio refresh lesson).

### Storage (one migration, plus a branch in `deploy/update-ec2.sh`'s detector, newest first)

- `fba_inventory_refresh`: `id`, `started_at`, `finished_at`, `status` (`done` / `failed` /
  `refused`), `source` (`nightly` / `button` / `shipment`), `sku_count`, `units`, `error`.
- `fba_inventory_row`: `refresh_id` FK, `seller_sku`, `asin`, `fnsku`, and one integer column per
  quantity (fulfillable, inbound_working, inbound_shipped, inbound_receiving, reserved, researching,
  future_supply, unfulfillable), `amazon_updated_at`. UNIQUE `(refresh_id, seller_sku)`.
- **Only a `done` run is ever read as current.** A failed or refused run is stored so the banner
  can say what happened; yesterday's figures stay current. Last 30 runs kept.

### Scheduler

`fba_inventory_refresh` at **07:45 IST**, on the same guard as the 07:30 portfolio job
(`SCHEDULER_ENABLED` or `ORDER_REFRESH_ENABLED`), so production runs it without other flags.
Stated in IST through `ist.utc_hhmm`; tests assert the IST value and the job id.

### Projections (`app/projections/service.py`, `templates/projections.html`)

- `build` reads the latest good run. **Row mapping**: child ASIN to row key via
  `portfolio.repository.flavour_keys`, else the parent ASIN stored on `economics_daily`; weight from
  the MRP catalogue (`catalogue.load_catalogue`), i.e. the same sources the Profit rows use. Not via
  the 30-day sizes list: a pack size with stock but no sales in 30 days still weighs in.
- New row fields `fba_kg`, `to_purchase`; `sum_rows` sums both (To purchase as clamped rows).
- Stock held for an ASIN whose row is not on screen (no sales, or inactive while hidden) is listed
  by name with its kg in a note, never silently dropped.
- Payload carries `fba_stock_as_of`, `fba_stock_status`, and the last error when the newest run
  failed. Banner only when there is a problem: no good run yet, newest run failed, or the good run
  is more than 36 hours old.
- The cache version gains the latest good run id, so a refresh is visible at once.
- Columns: Product, Category, Total stock to hold, **In FBA / transit**, **To purchase**, Ideal FBA,
  then the rest. KPI tiles: Total stock to hold, In FBA / transit, To purchase, Projection. Category
  cards show to hold and to purchase.
- **Refresh stock** button: `POST /projections/refresh-stock` (projections area), runs the fetch
  (seconds) and returns the new payload; 409 if one is already running.
- Downloads (Stock to hold, Full table, Excel and PDF) gain both columns.

### Profit tab (after the Projections part ships)

- **"To purchase"** column beside "Total stock to hold": product rows only (a pack size shows a
  dash), kg, whole numbers, from the same Projections figure by row id. `_attach_stock` attaches
  both from one call, so the two tabs cannot disagree; a failure leaves both as dashes and never
  takes the Profit tab down.
- `app/portfolio/columns.py` vocabulary, `FIELDS`, `COLUMN_DEFS`, `FALLBACK_ORDER`, `computeTotals`
  (sum of the per-row figures) and `export.COLUMN_KINDS` / `totals` all gain it, as
  `total_stock` did. A saved column layout picks it up after `total_stock` via
  `normalise_column_layout`.

### Shipment tab (`app/routers/shipment.py` `generate`)

Stock source, in order, stated on the plan ("FBA stock: Amazon API, 13 Oct 09:12"):

1. a fresh API fetch at generate time;
2. if Amazon fails, the latest good run if under 36 hours old, with a banner;
3. otherwise the uploaded CSV, with its rule corrected to `afn-total-quantity - afn-unsellable-quantity`
   per FBA SKU (falling back to the nine-column sum when column N is absent), with a banner.

The CSV still supplies sales and the merchant SKU map exactly as today. The existing all-zero and
no-quantity-column guards stay for path 3.

## Error handling

- Every Amazon failure is caught in `refresh.run`, recorded on the run, and surfaced as a banner on
  the next load. No screen ever reads a failed run.
- The guard against "dozens of active products all zero" applies to the API answer too.
- Nothing here writes to Amazon. A source test forbids imports of inbound-plan, ads-apply and
  economics-writing functions in `app/inventory/`.

## Testing

- `tests/test_inventory.py`: `held_units` on the real shapes; flex rows excluded (the 999
  placeholder); two FBA SKUs on one ASIN summed; a fixture built from the 13 Oct CSV-vs-API
  comparison where the 17 under-counted SKUs come out at the API figure; a failed and a refused run
  never become current; purge keeps 30.
- Projections: kg per row from weights; To purchase clamped per row and the total is the sum of
  clamped rows (fixture with one product in surplus and two short, so the wrong formula differs);
  dash cases; unmapped stock named; cache refreshes on a new run; refresh route and 409.
- Shipment: the three sources in order, each named on the plan; path 3 uses N minus L.
- Profit: the column on product rows only, equal to Projections for the same id; totals; export;
  layout normalisation; failure leaves dashes.
- Scheduler: the job id and its IST time.
- Migration detector test covers the new revision.
- `scripts/mutate_inventory.py`: drop a quantity term, count unfulfillable, include flex, clamp the
  total instead of each row, read a failed run, treat unknown weight as 0, skip the fresh fetch at
  generate, drop the Profit column's dash on size rows. Must print "All N mutations caught".

## Verification on production

- Download a fresh Manage FBA Inventory CSV and compare per SKU against the stored run: equal to
  column N minus unsellable, up to the minutes between the two.
- Projections: In FBA / transit total about equal to the live kg figure; To purchase per row equal
  to Total stock to hold minus In FBA, floored at 0.
- Profit tab "To purchase" equals Projections for every product row.
- Generate a plan and check it names the API as its stock source and its deficits drop by the
  shipped boxes.
- Check `alembic current` after the deploy (one migration: check out `deploy/update-ec2.sh` first).

## Out of scope

- Warehouse (own) inventory and "To send to FBA".
- Measuring lead-time slips from receipt dates.
- Easy Ship / merchant stock.
- Any write to Amazon.
