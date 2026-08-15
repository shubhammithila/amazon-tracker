# Plan: FBA shipment creation and box labels via Amazon SP-API

**Status: plan only, but the unknowns are now ANSWERED.** Revised 2026-08-15 after
testing real credentials against the live Amazon.in account.

---

## Verified against the live account — 2026-08-15

Credentials arrived in `.env` (`SP_API_*`, gitignored). I exchanged the refresh token and
called the real API. **Every open question in this document is now settled, and all four
answers are favourable.**

| Question this doc asked | Answer, measured |
|---|---|
| Does auth work? Do we have FBA permission? | **Yes.** LWA returns a token; `GET /inbound/fba/2024-03-20/inboundPlans` returns 200 with **10 real inbound plans** |
| Does Amazon.in return `AMAZON_OPTIMIZED` and hide the FC? *(the risk I said "decides the project")* | **No. It returns `AMAZON_WAREHOUSE`** with the FC filled in |
| Can we get the destination state for GST? | **Yes, in full** |
| Can we fetch box labels, in multiple formats? | **Yes.** Real PDFs downloaded |
| Is v0 switched off? | **No**, `/fba/inbound/v0/shipments` answers 200 |

A real shipment on the 14 Aug plan (`getShipment`) returned:

```
shipmentConfirmationId : FBA15M59XQFZ          ← the invoice's shipment ID
destinationType        : AMAZON_WAREHOUSE      ← NOT AMAZON_OPTIMIZED
warehouseId            : ISK3                  ← the FC code
address                : BHIWANDI / MAHARASHTRA / 421302
name                   : FBA STA (14/08/2026 08:15)-ISK3
status                 : IN_TRANSIT
```

**Maharashtra comes straight back from Amazon**, which resolves to GSTIN
`27AAFCF9848M1ZT` through the existing `get_gstin_for_state`. The whole GST chain works
off one API call. My worry that the destination would be unknowable was wrong for this
account.

### Labels: working, with a non-obvious parameter

`GET /fba/inbound/v0/shipments/{shipmentConfirmationId}/labels` — note it keys on the
**`shipmentConfirmationId`** (`FBA15M59XQFZ`), not the internal `shipmentId`.

**`PageSize` and `PageStartIndex` are mandatory here**, because these are non-partnered
("Other" carrier) shipments. Without them every request 400s with *"pageSize must be
provided for Non-Partnered and LTL Shipments"* — which reads like a permission failure
and is not. Measured results with `LabelType=BARCODE_2D`, `PageSize=1`,
`PageStartIndex=0`:

| PageType | Result |
|---|---|
| `PackageLabel_Thermal` | **200** — real PDF (`%PDF-1.4`, 2,219 bytes) |
| `PackageLabel_A4_4` | **200** |
| `PackageLabel_Plain_Paper` | **200** |
| `PackageLabel_A4_2` | 400 — "not able to fetch carrier labels while it is required" |
| `PackageLabel_Letter_2` | 400 — same |

So thermal, A4 4-up and plain paper are available today; the 2-up variants are refused
for this shipment type. `LabelType=UNIQUE` needs a valid `cartonIdList` and is not
usable without real carton ids.

### One genuine 403

`GET /inboundPlans/{id}/shipments` (the list) → **403 Unauthorized**, while
`GET /inboundPlans/{id}/shipments/{shipmentId}` (the single shipment) → **200**. The
shipment ids are already present in the plan detail response, so this costs nothing —
but it means the working code must read shipments from the plan payload rather than
listing them.

### What this changes

Steps 4 and 6 of the sequence below — "spike to prove auth" and "test whether the FC
comes back" — **are now done.** The risk that gated everything is retired. What remains
is ordinary implementation work, and **step 5 (label fetch) is immediately buildable**.

Still unverified: whether `customPlacement` lets us *choose* the FC when creating a plan
ourselves (all 10 existing plans were created through Send to Amazon, so this account has
never exercised it), and whether placement fees apply. Neither blocks label fetching.

---

## Read this first: the upload-file plan does not work

You asked for: build a file of SKU + quantity + FC code, upload it to Amazon, and a
shipment appears. **No such upload exists.** I checked Amazon's own machine-readable
Feeds API model and there is no FBA inbound shipment-creation feed of any kind. The only
FBA feeds are carton contents, removals and fulfilment orders.

What the various uploads actually are:

| Thing | Creates a shipment? |
|---|---|
| **Send to Amazon** spreadsheet upload | **No** — box/packing detail only |
| Legacy "Inbound Shipping Plan" upload | **No** — superseded by Send to Amazon |
| `POST_FBA_INBOUND_CARTON_CONTENTS` feed | **No** — box contents for an *already-created* shipment |
| **Fulfillment Inbound API v2024-03-20** | **Yes** — and it is the only way |

So a shipment is created either **by hand in Send to Amazon**, or **through the API**.
There is no middle path where a spreadsheet does it.

I could not retrieve literal column headers for any Send to Amazon template, because
every Seller Central help URL redirects to a login. Anyone quoting exact headers without
a logged-in session is guessing.

> **What we shipped today is therefore still useful**, just not as an Amazon upload: the
> SKU + units + FC sheet is your worksheet for typing into Send to Amazon, and it is what
> feeds the FC code into the invoice. That part works and is live.

## But the FC news is much better than my last plan assumed

My previous plan said the project's viability rested on whether Amazon would tell us the
destination FC, and warned it probably would not (`AMAZON_OPTIMIZED` returns an empty
address). That risk is largely **gone**, for a reason specific to you:

`generatePlacementOptions` accepts a `customPlacement` array, and Amazon's model says
verbatim:

> "This is only used for the India (IN - A21TJRUUN4KGV) marketplace."

`CustomPlacementInput` **requires** `warehouseId` — e.g. `ISK3`. So in India, and only in
India, **the seller can name the destination FC.** Your instinct was right; it is just an
API field rather than a spreadsheet column.

Unverified: whether Amazon.in *honours* it or treats it as a hint, and whether it carries
a fee. Both need one real call.

---

## The real constraint is GST registration, not the API

**India requires the destination FC to be registered as an Additional Place of Business
on a GST registration in that state.** That is a legal constraint that no amount of code
changes.

Measured against our own data:

- `fc_addresses.json` resolves **93 FC codes** to a state.
- `get_gstin_for_state` covers **84 of them**.
- **9 FCs are legally unusable today**: Madhya Pradesh (4), Kerala (3), Andhra Pradesh (2).

The app now names those on the picker as "no GSTIN" rather than hiding them, and the
invoice bridge warns if one is chosen. **Deciding whether to register in those three
states is your call, and it gates FC choice regardless of SP-API.**

---

## Labels: fetchable, and that is the piece you actually asked for

You asked for label downloads in different formats, and this is available:

`GET /fba/inbound/v0/shipments/{shipmentId}/labels` — **not marked deprecated** in the
current model, and the 2024 use-case guide states `getLabels` and `getBillOfLading` "are
necessary to create shipments".

- `LabelType`: `BARCODE_2D`, `UNIQUE`, `PALLET`
- `PageType`: `PackageLabel_Thermal`, `_Thermal_Unified`, **`_Thermal_NonPCP`**,
  `_Thermal_No_Carrier_Rotation`, `PackageLabel_A4_2`, `_A4_4`, `_Letter_2/_4/_6`,
  `_Plain_Paper`

`_Thermal_NonPCP` is the non-partnered-carrier variant — which is your case, since you
select "Other". For non-partnered LTL, `PageSize` and `PageStartIndex` become required.

**Labels need a `shipmentId`, so they come after the shipment exists.** That means label
download cannot be built before either (a) you paste in the shipment ID Amazon gave you,
or (b) the API creates the shipment. Option (a) is small and independent — worth doing
first.

## Cartons and "Other" as carrier

Confirmed from the model. `"Other"` is `USE_YOUR_OWN_CARRIER`. Boxes go in via
`setPackingInformation`, and `BoxInput` **requires** `contentInformationSource`,
`dimensions` (L/W/H + unit), `quantity` and `weight`.

Two things follow for us:

1. **We do not hold carton dimensions.** We know net product weight
   (`logic.shipment_weight`) and the day's carton count, but not box sizes. That is a new
   input the warehouse would have to record.
2. `BoxContentInformationSource` = `BOX_CONTENT_PROVIDED` | `MANUAL_PROCESS` |
   `BARCODE_2D`, and the model notes **`MANUAL_PROCESS` "incurs charges"**. We should
   send box content, not let Amazon do it manually.

Non-partnered tracking is mandatory: SPD needs `boxId` + `trackingId` **per box**; LTL
needs `freightBillNumber`.

---

## Revised sequence

Ordered so each step is useful on its own, and the cheap certain wins come before the
uncertain expensive ones.

| # | Step | Depends on | Value if we stop here |
|---|---|---|---|
| **1** | **Done, live today.** FC picker → SKU+units+FC sheet → shipment ID and FC carried into the invoice (address, state, GSTIN, IGST/CGST all resolved from the code). | nothing | The invoice is complete without retyping Amazon's data |
| 2 | **Store the shipment against the days.** Persist `shipment_id` + `fc_code` on the packing days so the invoice, the sheet and the label fetch all refer to one record instead of a field re-typed each time. | nothing | An audit trail, and no re-typing |
| 3 | **Decide the 3 states.** Register in MP/Kerala/AP, or accept those 9 FCs are unusable. | you, not code | Removes a silent blank GSTIN |
| 4 | **SP-API spike.** Register app, self-authorise, one read-only call. | nothing | Proves auth; ~half a day |
| 5 | **Label fetch** via `getLabels`, using a shipment ID you paste in. Thermal + A4 + Letter. | 4 | **Your label-format request, without full shipment creation** |
| 6 | **Test `customPlacement`** with one real cheap shipment. Does Amazon.in honour `ISK3`? At what fee? | 4 | Answers the only open question that matters |
| 7 | **Full creation flow** — inbound plan → packing → placement → transportation. Needs carton dimensions from step 2. | 6 | Ends manual Send to Amazon entry |

**Step 5 is the sweet spot**: it gives you the labels in the formats you asked for, needs
no shipment-creation risk, and depends only on auth working.

## Auth, briefly

LWA client ID/secret + refresh token. **No AWS SigV4 and no IAM role** — headers are
`host`, `x-amz-access-token`, `x-amz-date`, `user-agent`. Self-authorising a *draft*
private app is supported, so **no appstore listing and no Amazon review**. Role needed is
"Amazon Fulfillment", which is not restricted. Must be authorised by the account's Primary
User (you). India is on `https://sellingpartnerapi-eu.amazon.com`, marketplace
`A21TJRUUN4KGV`. Rate limits are 2 req/sec — irrelevant at ~12 calls per shipment.

`python-amazon-sp-api` (saleweaver) is actively maintained (v2.1.20, 2026-08-01) and
ships both a `fulfillment_inbound_2024_03_20` module and an **asyncio** variant, which
matters because a blocking HTTP client in a request handler would stall this app's event
loop.

## Design constraints this codebase already implies

1. **One module, `app/shipment/spapi.py`**, the way `catalogue.py` isolates the Google
   Sheet. Nothing else should know about LWA tokens.
2. **Never call SP-API from a document or invoice path.** `parser.py` is deliberately
   synchronous and offline for exactly this reason.
3. **Credentials in `.env`, never committed.** `cookies.txt` is already in git history
   with a live token; a refresh token is worth more.
4. **Persist before confirming.** A confirmed placement is a real shipment at Amazon that
   no failed transaction can undo — the same lesson as the invoice-number sequence. Write
   `inbound_plan_id` *before* `confirmPlacementOption`.
5. **`operationProblems[]` severity must be inspected.** HTTP 200 does not mean success,
   and this codebase has been bitten by silent failures repeatedly.

## Still unverified

1. Whether Amazon.in honours `customPlacement`, and its fee.
2. Whether v0 `getLabels` still answers (model presence ≠ live endpoint).
3. Literal Send to Amazon template headers (login-walled).
4. Whether Amazon's FBA pickup really files the e-way bill for us (forum claim, undated).

**None can be settled in the sandbox** — Fulfillment Inbound is static-response only, so
it returns canned data rather than exercising placement. Steps 4–6 are the only way.

## Recommendation

Do **step 2** (store the shipment against the days) and **step 3** (the GST decision)
next — neither needs Amazon. Then **step 4 + 5** to get your label downloads. Treat full
shipment creation as a separate project gated on step 6, because it is the only part that
spends money and creates real shipments.
