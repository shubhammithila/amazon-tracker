# Shipment plan: close, carry forward, and history

Design agreed 2026-08-19. Status: approved, not yet implemented.

## The problem, in the owner's words

> "When the shipment is finished or when we want to create a new shipment plan, what will
> happen to the old shipment plan data. I think we can give a button to close the shipment
> plan and if there is some uninvoiced or unverified units we can carry forward it to the
> next shipment plan and we can give a button to view recent or old shipment plan history
> where its day wise packed data, sku's, invoice numbers etc should all be saved."

The live case that prompted it: seven days shipped and invoiced (10–18 Aug), then **19 Aug
packed 400 units in 9 cartons and verified** — below the 25-carton threshold, so it cannot
ship on its own. The last 7 days of sales data have since moved, so a new plan is wanted
now. Those 400 units are in boxes on the floor and must not be packed again.

## What is true today

`close_active_plans()` (`repository.py:82`) only flips `status` to `closed`; nothing is
deleted. Plan rows, item rows, packing days and packing entries all survive.

The gap is **reachability, not retention**. `get_active_plan()` matches `active` and
nothing else, and 17 call sites in `app/routers/shipment.py` go through it — including all
five downloads (via `_document_rows`, `shipment.py:1899`), the invoice bridge, and
`attach_invoice` (`shipment.py:2578`). So the moment a plan closes:

* `download/packed.xlsx` can no longer report it — accounts cannot get last month's sheet.
* `attach_invoice` returns 404 for its days, so an invoice raised outside the app can never
  be recorded against them.

## Decision 1 — only packed-but-unshipped carries forward

`parse_stock_csv` (`shipment.py:138-143`) sums **eight** FBA columns into `fba_stock`,
including `afn-inbound-shipped-quantity`, `afn-inbound-receiving-quantity` and
`afn-inbound-working-quantity`. The next plan's need is
`deficit = projection − fba_stock` (`shipment.py:380`).

Units already shipped to Amazon are therefore **already** in the next plan's stock figure.
Three categories, three different answers:

| Category | In next plan's `fba_stock`? | Carry? |
|---|---|---|
| Packed, not shipped (held / verified-not-shipped) | No — no shipment exists | **Yes** |
| Shipped | Yes, as inbound | No — would double-count |
| Never packed (e.g. STILL TO PACK 6,446) | No, but fresh CSV recomputes need from current sales | No — would double-count |

The unpacked remainder is the trap: it looks like the obvious thing to carry, and it is the
one that would silently inflate every future plan.

Uninvoiced-but-shipped is **not** a stock problem. Carrying its units forward would not
produce the missing invoice; it would tell the packer to pack them again. That case is
solved by history/reachability (Decision 4), not by carry-forward.

## Decision 2 — carry the DAY, not a quantity

A day moves to the new plan: `plan_id` is updated and `carried_from_plan_id` stamped.

**Rejected: adding the units to `available`.** This was recommended during design and is
wrong. `available` means finished stock on a shelf that *still needs boxing*, and
`remaining_for` deliberately ignores it (`logic.py:435-450`, with a test asserting the
function's *signature* so an optional parameter cannot be added). Carried units are already
in cartons. Putting 400 into `available` would tell the packer to box 500 more on top of
the 400 already packed — 900 units against a 500-unit plan.

**Rejected: reducing `shipment_plan`.** The plan would read 100 while 500 units actually go
to Amazon; every download and the Amazon upload would understate the shipment.

**Rejected: a new carried-forward column.** A fourth "left to do" number on a screen that
already has three, and every document layout would need it.

Moving the day requires **zero new arithmetic**, because every aggregation in `logic.py`
keys off `plan_id` through `load_days_with_entries`:

* `packed_units_by_asin` counts it → packer is told to box 100, not 500
* `shippable_units_by_asin` counts it → its 9 cartons combine toward the threshold
* `verified_units_by_asin` counts it → still invoice-able
* `carry_over` (`logic.py:624`) combines it with the new plan's held days, exactly as it
  already combines held days inside one plan
* the Amazon shipment can include it

This is what "combine it with next day packing and then create a shipment" means when the
plan boundary falls in the middle of the combining.

The result for the live case:

```
Kulthi Sattu 1kg
  To Ship        500
  Packed         400   ← the 19 Aug boxes, counted
  Still to pack  100   ← what the packer actually boxes
  Cartons toward threshold: 9 + whatever the new days add
```

Verification status carries with the day: 19 Aug stays `verified`, so it stays invoice-able.

## Decision 3 — `POST /shipment/plan/{id}/close`

Admin only. Distinct from Finalise: Finalise promotes a *draft*, Close retires the *active*
plan with no replacement required — which is the case here, since the owner wants to close
before a new CSV has been uploaded.

One transaction:

1. Carry eligible days to the target plan (`plan_id` + `carried_from_plan_id`)
2. Insert To-Ship-0 rows for orphan ASINs
3. Set the old plan `closed`, stamp `closed_at`

**Target plan:** the current draft if one exists, otherwise create an empty carrier plan.
Without that second case, closing before generating would strand the boxes with nowhere to
sit.

**Eligible to carry:** `held`, `submitted` or `verified`, with no `shipment_confirmation_id`
and no `invoice_id`, and at least one entry.

**Refuses (409):**

* **A day with `inbound_plan_id` but no confirmation.** A shipment is half-created at
  Amazon; moving the day would detach it from the plan `clear_inbound_plan` scopes by
  (`repository.py:860-888`). Cancel or confirm first.
* **An `open` day.** The packer is mid-shift. Submit or delete it.

**Warns, then allows** (matching how held-days at generate and over-packing already behave —
a warning the owner acts on, not a block):

* Shipped-but-uninvoiced days, named with dates and units. They stay on the old plan;
  Decision 4 keeps them reachable.
* Verified days being carried, named — so 19 Aug moving is visible, not discovered.

**Orphan ASINs get a plan row at To Ship 0.** A carried day can hold units for an ASIN the
new plan lacks (product went inactive in the MRP sheet, or the row was excluded). Leaving it
orphaned is the GST-understatement shape the excluded-but-packed 409 already guards against:
`packed_units_by_asin` aggregates by ASIN and never consults plan items, while the invoice
bridge builds lines *from* plan items — so boxes would ship with no GST line. A row at 0
shows as over-packed, reaches the invoice payload, and gets a line. The close response names
them, because 400 packed against a plan of 0 needs an explanation.

**Required companion fix.** `attach_invoice` (`shipment.py:2578`) resolves the day through
`get_active_plan`, so closing a plan with a shipped-but-uninvoiced day makes recording its
invoice impossible through the app. Close must therefore also let `attach_invoice` resolve
by plan id, following the `plan_id`-or-active pattern `/items` already uses
(`shipment.py:1201-1204`). Without this, Close makes reconciliation worse rather than better.

## Decision 4 — history

**`GET /shipment/plans`** — every plan newest first: id, label, status, created and closed
dates, day count, total units and cartons, invoice numbers touched, and whether days carried
in or out. Enough to choose from without opening each.

**`GET /shipment/plan/{id}/detail`** — one plan in full, in the **same payload shape**
`/active` returns, so the history screen reuses the existing renderer rather than growing a
second one that can disagree with it. Carries day-wise packed units, per-day cartons,
per-SKU entries, invoice numbers, Amazon shipment and confirmation ids, destination FC and
state, and carry lineage.

**The five downloads accept `?plan_id=`.** `_document_rows` (`shipment.py:1899`) takes an
optional plan id, defaulting to active. One function, and all five inherit it — the same
single-invariant property `load_plan_items` has. This is what makes last month's
`packed.xlsx` printable.

**Carry lineage is visible both ways** — the old plan's detail says "19 Aug carried out to
Plan 12", the new one says "carried in from Plan 11". With only one direction, a day appears
to vanish from the plan being reconciled.

**Access:** admin for the plan list and detail (they carry projections). `packed.xlsx` stays
ops-and-admin at every plan id, since printing it for accounts is the packer's job.

## Schema

One migration, `batch_alter_table` for SQLite. Both columns nullable, no backfill.

```
shipment_packing_days.carried_from_plan_id  INTEGER NULL   -- lineage
shipment_plans.closed_at                    DATETIME NULL  -- for the history list
```

`ShipmentPlanItem` needs nothing: orphan rows are ordinary items at To Ship 0.

**`deploy/update-ec2.sh` needs a new baseline-detector branch at the top**, keyed on
`carried_from_plan_id`. CLAUDE.md is explicit that a stale detector is a *failed deploy*, not
a cosmetic omission — it has already stamped production backwards once.

## UI

Three additions to `templates/shipment.html`; no new page.

* **Close plan** button beside Finalise, with a confirm naming what carries, what is warned
  about, and what is refused.
* **Plan history** panel: the list, and a chosen plan rendered read-only through the existing
  renderer.
* **Carried-in badge** on day cards that moved, linking back to the source plan.

Colour comes from `static/theme.css` only — `tests/test_theme.py` fails any template that
hardcodes a colour or re-declares `:root`.

## Verification

The properties that cost money if wrong:

* **The live case end to end**: close → the day is on the new plan, still `verified`, still
  invoice-able, its 9 cartons count toward the threshold, and `remaining_for` returns
  **100, not 500**. That last assertion is what catches the rejected `available` mechanism.
* A shipped day does not carry; an invoiced day does not carry.
* A day with `inbound_plan_id` and no confirmation → 409 **and the day has not moved** (both
  asserted: a refusal that half-applied is worse than either outcome).
* `attach_invoice` succeeds on a **closed** plan's day — the bug Close would otherwise create.
* Orphan ASIN → row exists at To Ship 0, appears in the invoice payload, gets a GST line.
* All five downloads honour `?plan_id=`, parametrised so a sixth that forgets it fails.
* Carry is idempotent: closing twice moves nothing twice.
* `carry_over` combines a carried day with the new plan's held days.
* Mutation-verified, as with the rest of this build.

**Manual**, on `http://localhost:8020`: close the 19 Aug plan → confirm the day appears on
the new plan as carried-in → confirm Still to pack reads 100 → open plan history and check
10–18 Aug with their invoice numbers are still fully readable → download last plan's
`packed.xlsx`.

## Out of scope (YAGNI)

No un-close. No partial day-picking at close (all eligible days carry, or none). No
cross-plan quantity arithmetic. No history for drafts.
