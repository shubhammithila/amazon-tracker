# Plan: create the FBA shipment from the app

**Status: plan only, no code written.** Written 2026-08-15 from calls made against the
live Amazon.in account, not from documentation.

Goal: replace the manual Send to Amazon typing. The app already knows the SKUs, the
quantities and the destination FC — it should create the inbound plan itself and get back
the shipment ID and FC that complete the GST invoice.

---

## The real flow, measured

The published use-case guide describes a seven-stage flow. **India does not use most of
it.** Called live, three of those stages are refused outright:

```
400  ListPackingOptions      "not supported for the Indian marketplace"
400  ListShipmentBoxes       "not supported for the Indian marketplace"
400  GetDeliveryChallanDocument  "not supported for non-PCP transportation option"
```

So the flow we actually need is **four calls, not twelve**:

```
1. POST /inboundPlans                        → inboundPlanId + operationId
2. POST /inboundPlans/{id}/placementOptions  (generate)   → operationId
   GET  /inboundPlans/{id}/placementOptions              → option + FEES
3. POST /inboundPlans/{id}/placementOptions/{opt}/confirmation   ← COMMIT POINT
4. GET  /inboundPlans/{id}                   → shipments[].shipmentId
   GET  /inboundPlans/{id}/shipments/{sid}   → shipmentConfirmationId + destination
```

Then transportation, which for us is trivial: `GET .../transportationOptions?placementOptionId=…`
returns **`carrier: {name: "Other"}`, `shippingMode: GROUND_SMALL_PARCEL`** — exactly the
"select courier partner as Other" step you do by hand today.

### What we send, verified against a real plan

`sourceAddress` (read back from plan `wf835a768…`, so this is the shape Amazon accepts):

```json
{"name": "F2D TECH PRIVATE LIMITED, MITHILA FOODS",
 "companyName": "F2D TECH PRIVATE LIMITED, MITHILA FOODS",
 "addressLine1": "C/O DINESH PRASAD SAH, new babu para,near dadi shyam mandir",
 "addressLine2": "Dumka jharkhand", "city": "Dumka",
 "stateOrProvinceCode": "Jharkhand", "postalCode": "814101",
 "countryCode": "IN", "phoneNumber": "7870034414",
 "email": "f2dtechpvtltd@gmail.com"}
```

Items, per line: `{msku, quantity, labelOwner: "SELLER", prepOwner: "SELLER"}`.

> **Our data already fits.** `ShipmentPlanItem.fba_sku` holds `"abc_sattu500g FBA"` and
> Amazon's `msku` on the live plan is `"wss 200g FBA"` — the same format. 78 of the 81
> items on the current plan have one; **3 do not, and those cannot be sent at all.** That
> is the existing `missing_sku_count` problem, and it becomes a hard blocker here rather
> than a warning.

Prep on every live line is `ITEM_LABELING` at **`fee: {amount: 0, code: INR}`** —
seller-labelled, no charge.

### Two findings that change the risk assessment

**1. The placement fee is zero.** I feared this would cost money:

```json
"fees": [{"type": "FEE", "target": "Placement Services",
          "value": {"amount": 0, "code": "INR"},
          "description": "Placement service fee represents service to inbound with
                          minimal shipment splits and destinations of skus"}]
```

`amount: 0`. On this account, for this shipment, confirming placement was free. **Not to
be assumed permanent** — the fee is returned per option and options expire (this one
`2027-02-12`), so the code must read and display it rather than trusting ₹0.

**2. Amazon returns the destination properly.** `AMAZON_WAREHOUSE`, `warehouseId: ISK3`,
`BHIWANDI / MAHARASHTRA / 421302`. So we get the state from Amazon and no longer have to
trust the FC the owner picked — the GST chain is sourced from the shipment itself.

---

## What is still unknown, and it is the crux

**Whether `customPlacement` lets us choose the FC when we create the plan.** All 10
existing plans came from Send to Amazon, so this account has never exercised it. Amazon's
model says the field is India-only and requires a `warehouseId`, but says nothing about
whether it is honoured or merely a hint.

Three outcomes, and they lead to different products:

| If `customPlacement` … | Then |
|---|---|
| is honoured | We name ISK3 and get it. Full automation, GST resolved before Amazon replies |
| is a hint Amazon may override | We must read the destination back from `getShipment` and let it correct the owner's choice — which is why the invoice must key on the FC Amazon *returned*, not the one picked |
| is rejected | Amazon assigns the FC. Still fine, because it comes back in `getShipment`; the owner just cannot choose |

**In all three cases the invoice ends up correct, provided we read the destination back.**
That is the design decision this plan rests on: **treat the owner's FC choice as a
request, and Amazon's returned `warehouseId` as the truth.**

---

## Build sequence

Each step independently committable, suite green after each, mutation-verified.

| # | Step | Risk |
|---|---|---|
| 1 | **`app/shipment/spapi.py`**: token cache (3600 s, refresh early), typed errors, `operationProblems` severity inspection. Recorded fixtures, no live calls in the suite. | Low |
| 2 | **Model + migration**: `inbound_plan_id`, `shipment_confirmation_id`, `warehouse_id`, `destination_state`, `placement_fee`, `spapi_status` on the plan or a new `ShipmentSubmission` row. | Medium — `test_migrations_match_models` is the gate |
| 3 | **Read-only endpoint** `GET /shipment/amazon-plans`: list recent inbound plans with their FC and shipment ID. **Useful on its own** — it retires the hand-typed shipment ID immediately, before any mutation exists. | Low |
| 4 | **Dry run**: build the exact `createInboundPlan` body from the ticked days and *show it* without sending. Refuses if any line lacks an `msku`. | Low, and it is the safety net for step 5 |
| 5 | **`POST /shipment/create-amazon-shipment`** — steps 1–2 of the flow, stopping **before** confirmation. Displays the FC Amazon offers and the fee. | **Highest — creates a real inbound plan at Amazon** |
| 6 | **Explicit confirm** = `confirmPlacementOption`. Separate endpoint, separate click, fee shown. | **The commit point. Real shipment, possibly real money** |
| 7 | Read back `shipmentConfirmationId` + destination → straight into the invoice payload, overriding the picked FC if Amazon chose differently. | Medium |
| 8 | Box labels (thermal / A4_4 / plain paper) now that a confirmation id exists. | Low |

## Non-negotiables, from this codebase's own history

1. **Persist before confirming.** Write `inbound_plan_id` in its own committed
   transaction *before* `confirmPlacementOption`. A confirmed placement is a real
   shipment that no rollback can undo — the same lesson as the GST number sequence. A
   plan confirmed at Amazon with no local row is invisible to us and real to them.
2. **Two clicks, never one.** Generate-and-show, then confirm. The fee and the FC must be
   on screen before the irreversible step.
3. **A 200 is not success.** `generate*` and `confirm*` are async and return an
   `operationId`; problems arrive as `operationProblems[]` with `WARNING`/`ERROR`
   severity. This codebase has been bitten by silent failures repeatedly — inspect
   severity, do not trust the status code.
4. **Never from a document or invoice path.** `parser.py` is synchronous and offline on
   purpose. SP-API belongs behind explicit owner action only.
5. **Refuse rows with no `msku`.** Amazon keys on it; 3 of 81 items currently lack one.
   Sending them silently drops real stock from the shipment.
6. **Ops must not reach any of this.** Creating shipments is the owner's decision —
   `require_admin`, matching the plan-sheet and Amazon-upload downloads.

## Open questions for you

1. **`customPlacement`** — I would test it on one small real shipment. It is the only way
   to know, and it decides whether you keep choosing the FC.
2. **Carton dimensions.** India refuses `ListShipmentBoxes`, so box detail may not be
   needed via API at all — but if it is, nobody records box sizes today. Worth confirming
   on the first real run rather than building a data-entry screen speculatively.
3. **The 3 SKU-less items** — fixing those in Seller Central is a prerequisite, not a
   code change.

## Recommendation

**Steps 1–4 are safe and worth doing regardless of how `customPlacement` behaves** —
they add no mutation, and step 3 alone removes the hand-typed shipment ID. Then pause: do
one real shipment through steps 5–6 together, watch what Amazon does with the FC, and let
that decide the final shape of step 7.
