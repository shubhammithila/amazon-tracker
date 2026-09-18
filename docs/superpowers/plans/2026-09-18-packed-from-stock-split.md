# Packing entry: split what was MADE today from what came off the SHELF

## What was asked

> "In the warehouse shipment entry tab when they are entering the data, they want a column to
> mention if they are taking the product which is available in stock — or the qty they are taking
> from the available stock and the qty they have packed today — so that they can inform the same to
> the accounts team when they give the printout of packed today sheet to the accounts."

And, decisively, on the arithmetic:

> "if they pack 40 today and take 50 from available. then total 90 goes to fba packing"

**They ADD UP.** 40 made + 50 from the shelf = **90 units into today's cartons**. That answer is the
whole design, and it is the opposite of what I assumed when I asked — I had expected "100 packed, of
which 40 from stock", which would have left every existing total untouched. It does not.

## The consequence, stated plainly

`ShipmentPackingEntry.units` is read by **every** downstream number in this feature:

| Reader | Uses it for |
|---|---|
| `logic.packed_units` / `units_by_asin` | the day's totals, the hold threshold |
| `logic.remaining_for` | what the packer must still box — the printed morning sheet |
| `logic.over_packed` | the doubled-row warning |
| `repository._recompute_day_units` | `ShipmentPackingDay.total_units`, which decides `is_held` |
| `POST /shipment/invoice-payload` | **the quantity on a GST invoice** |
| `download/shipment-file.xlsx` | **the quantity Amazon is told to expect** |
| all five downloads | every printed figure |

So there are exactly two ways to build this, and only one is safe.

### Rejected: make `units` mean "made today" and add `from_stock` alongside

Then the real total is `units + from_stock` everywhere, and **every one of those readers has to be
found and changed**. Miss one and it under-reports: the Amazon upload says 40 when 90 cartons
arrive, or the GST invoice bills 40 units of a 90-unit shipment. That is a tax document and an FC
discrepancy, from one missed call site.

### Chosen: `units` STAYS the total that goes to FBA

`units` keeps its exact current meaning — **everything boxed today, whatever its provenance** — so
every reader above is correct with no change at all. One new column records how much of that total
came off the shelf:

```
from_stock  =  50      # taken from finished stock already on the shelf
units       =  90      # total in today's cartons  <- unchanged meaning
made today  =  40      # DERIVED: units - from_stock, never stored
```

The packer still types two numbers and sees the total; the *stored* total is the one the rest of the
app already trusts. **A stored derived value is how two numbers for one thing start disagreeing** —
the "86 orders beside 87 lines" defect this file records three times.

> This is the same shape as `day_cartons` being read off the DAY while units are per SKU: pick the
> grain the downstream consumers already use, and derive the rest.

## Decisions taken (yours)

- **They add up.** 40 + 50 = 90 to FBA.
- **Shown in three places**: the packed sheet for accounts, the owner's Shipment tab day cards, and
  the packer's own KPI strip.
- **`from_stock > units` is impossible by construction**, because the packer enters *made* and
  *from stock* and the app computes the total. There is no input that can produce a negative
  "made today", so the refuse-or-warn question dissolves.

## The rules this must not break

1. **`units` continues to mean "total boxed today".** Nothing downstream is re-pointed. A test
   asserts the GST invoice payload and the Amazon upload quantity both still read the total.
2. **Ops writes only ops rows.** `from_stock` lives on `ShipmentPackingEntry`, which only ops
   writes. It does NOT touch `ShipmentPlanItem.available`, which is the owner's planning figure —
   see below.
3. **A missing `from_stock` is 0, not null-shaped.** Every existing row predates this column and
   must read as "all of it was made", which is what the data actually says.
4. **`_recompute_day_units` still sums `units` only.** It must not start summing `from_stock` as
   well, or the day total double-counts the shelf units.

## `available` is NOT this, and the boundary is deliberate

`ShipmentPlanItem.available` already exists, is already editable, and already feeds
`logic.still_to_source` — the OWNER's "what must I make" number, per plan item.

The new field is ops', per DAY, and records what was actually taken. They are related and they are
not the same:

| | `ShipmentPlanItem.available` | `ShipmentPackingEntry.from_stock` |
|---|---|---|
| Whose | the owner's | ops' |
| Grain | per plan item | per SKU per DAY |
| Question | how much is on the shelf to plan around | how much came off it today |
| When | at planning time | at packing time |

**Taking from stock deliberately does NOT decrement `available`.** Tempting, and wrong: ops would be
writing a column the owner owns, which breaks the write separation that stopped the two roles
clobbering each other's work — the reason the plan moved out of a JSON blob in the first place. The
owner reconciles the shelf himself; the app reports what was taken rather than maintaining a balance
it cannot see (stock also arrives, and nothing tells the app when).

Recorded so nobody "finishes" this by wiring them together.

## 1. Migration and model

`ShipmentPackingEntry` gains:

```python
from_stock = Column(Integer, default=0, nullable=False, server_default="0")
```

`server_default="0"` as well as `default=0`: the Python default only applies to rows this app
creates, and existing rows need a value at migration time. `nullable=False` so "no value" cannot
mean two things.

Migration `alembic/versions/<new>_packing_from_stock.py`, `down_revision` = current head.
`deploy/update-ec2.sh` gains a newest-first detector branch keyed on the new column, and the
required-tables check is untouched (no table added).

## 2. `logic.py` — one function, derived

```python
def made_today(entry) -> int:
    """Units produced today: total boxed minus what came off the shelf.

    DERIVED, never stored. `units` is the total that goes to FBA and every downstream figure
    already reads it; storing this alongside would be a second number for one fact.
    """
```

Plus `from_stock_by_asin(days)` mirroring `units_by_asin`, so the sheet and the day cards sum it the
same way — one loop, not three.

`packed_units`, `units_by_asin`, `remaining_for`, `over_packed` are **untouched**, and a test
asserts their signatures so a later change cannot quietly fold `from_stock` in.

## 3. `repository.py`

`save_packing_entries` accepts `from_stock` per entry, clamped at ≥0 and ≤ `units` (belt and braces:
the screen cannot produce a larger value, and a hand-built request must not store one that makes
"made today" negative). A zero-unit entry is still DELETED, unchanged.

`load_days_with_entries` returns `from_stock` on each entry.

## 4. The ops screen (`templates/ops.html`)

Two inputs where there is one, and the total shown live:

```
Product            Size    Made today   From stock   = Total    Still needed
ABC Sattu 500g     500 g   [   40   ]   [   50   ]     90        160
```

- **`Total` is computed and displayed, not typed.** The packer sees 90 appear as he types, which is
  the number that goes to Amazon and onto the invoice.
- **`Still needed` and the over-packed warning use the TOTAL**, unchanged.
- The KPI strip gains `made today` and `from stock` beside the existing `Units today`, so the
  submit-time figures are checkable before he commits.
- Column order stays the work order: what · size · **how many** · what is left.

## 5. The packed sheet (the actual ask)

`download/packed.{xlsx,pdf}` gains two columns after `Units`:

```
S · M · B · Brand · ASIN · Merchant SKU · Product · Size · Units · Made today · From stock
```

`Units` stays FIRST and stays the total, because that is the figure accounts reconciles against the
invoice. The split trails it as the breakdown.

`_totals_row` already sums every trailing column by header count, so both new columns total for
free — that generality was built for exactly this.

The owner's day cards (`/shipment/active`) carry `made_today` and `from_stock` per day.

## Files

New: `alembic/versions/<new>_packing_from_stock.py` · `tests/test_packing_from_stock.py`

Changed: `app/models.py` · `app/shipment/{logic,repository}.py` · `app/routers/shipment.py` ·
`templates/ops.html` · `templates/shipment.html` · `deploy/update-ec2.sh` · `CLAUDE.md` ·
`tests/test_schema_migrations.py`

## Verification

**Automated** (2,213 green now; each must fail against current code)

- **`units` is still the total**: 40 made + 50 from stock stores `units=90`, and
  `/shipment/invoice-payload` plus `download/shipment-file.xlsx` both report **90**. This is the
  test that protects the GST document.
- `made_today` is derived and never stored — asserted at source, since no runtime test can watch a
  column not exist.
- An entry with no `from_stock` reads as "all made today" (every pre-existing row).
- `_recompute_day_units` sums `units` only: a day with 40+50 has `total_units = 90`, not 140.
- `remaining_for` and `over_packed` signatures unchanged, and their values driven by the total.
- `from_stock > units` is refused by the repository even though the screen cannot produce it.
- The packed sheet's totals row sums all three quantity columns.
- The hold threshold sees the TOTAL: 90 units of a 500 minimum still holds on units.
- The deploy detector answers the new head.
- **Mutations**: `units` storing "made" instead of the total; `_recompute_day_units` summing both
  columns; `made_today` stored rather than derived; the clamp removed; the sheet putting the split
  before the total; `from_stock` defaulting to null rather than 0.

**Manual, on production after deploy**

`/ops-page` → enter 40 made and 50 from stock on a SKU → the row shows **90** and "still needed"
drops by 90 → submit → the packed sheet shows Units 90, Made 40, From stock 50 → the owner's day
card shows the same split → a GST invoice raised from that day bills **90**.

## Out of scope

Any change to `ShipmentPlanItem.available` or to `still_to_source`. A running shelf balance — the app
cannot see stock arriving, so a balance it maintained would drift and be trusted anyway. Per-carton
provenance.
