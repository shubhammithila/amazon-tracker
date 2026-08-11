# Plan: create FBA shipments from the app via Amazon SP-API

**Status: plan only. No code written.** Written 2026-08-11.

## The problem, stated precisely

The invoice cannot be completed until Amazon has told us three things:

| Needed on the invoice | Who decides it | Today |
|---|---|---|
| Shipment ID (`FBA15…`) | Amazon, when the shipment is created | typed in by hand |
| Destination FC | Amazon, from placement options | typed in by hand |
| Destination **state** | follows from the FC | picks which of our 15 GSTINs applies, and IGST vs CGST/SGST |

So the sequence is forced: create the shipment at Amazon → get ID + FC → *then* invoice.
Moving shipment creation into the app is what would collapse those three manual steps
into one automatic one.

We already hold the pieces downstream of the FC: `fc_addresses.json` resolves **93 FC
codes** to a state, and `get_gstin_for_state` covers **14 of the 17 states** those FCs
sit in. So **an FC code alone is enough** — we do not need Amazon to give us an address.

> **Gap worth fixing regardless of this project:** FCs exist in **Madhya Pradesh (4),
> Kerala (3) and Andhra Pradesh (2)** and we hold no GSTIN for any of them. If Amazon
> ever routes a shipment there, the invoice has no correct GSTIN to use — today that
> surfaces as a blank, not as an error. That is a registration question for you, not a
> code change, and it is independent of SP-API.

---

## The one thing that decides whether this works

**`destinationType` can come back as `AMAZON_OPTIMIZED`, and then the address and
`warehouseId` are both allowed to be empty.**

Straight from Amazon's own API model: those fields "can be empty if the destination type
is `AMAZON_OPTIMIZED`", and their use-case guide says that for that type the address "may
differ from the actual address… Refer to the carton label for the correct address."

If Amazon.in returns `AMAZON_OPTIMIZED` for our shipments, **the API cannot tell us the
destination state**, and the GST automation — the entire point — does not work. We would
get the shipment ID automatically and still be typing the FC in by hand from the carton
label.

There is a promising counterweight, also found in the model: `generatePlacementOptions`
accepts a `customPlacement[]` array (`warehouseId` + items) marked **"only used for the
India marketplace"**. If that lets us *name* the FC, the problem disappears entirely —
we would know the state before Amazon does.

**Neither can be settled from documentation, and neither can be tested in the sandbox**
(Fulfillment Inbound is static-response only, so it returns canned data rather than
exercising real placement). This is the first thing to find out, and it is cheap to find
out: one real low-value shipment, created through the API, and read what `getShipment`
actually returns.

**I would not build anything else until that call has been made.** Everything below is
contingent on it.

---

## What I verified about the API

Confirmed against Amazon's machine-readable model
(`amzn/selling-partner-api-models`, `fulfillmentInbound_2024-03-20.json`) rather than the
HTML docs.

- **Fulfillment Inbound v2024-03-20 is the API.** The old v0 (`createInboundShipmentPlan`
  etc.) was deprecated 2022-09-30 with removal scheduled **2024-12-11** — nearly two years
  before today. Build only on v2024-03-20. *(The v0 model is still in the repo; that is
  not evidence it still answers. Verify.)*
- **Auth is simpler than it used to be.** LWA client ID/secret + a per-seller refresh
  token. **No AWS SigV4, no IAM role** — the required headers are `host`,
  `x-amz-access-token`, `x-amz-date`, `user-agent`. Self-authorisation of a *draft*
  private app still exists, so **no appstore listing and no Amazon app review**. The role
  needed is "Amazon Fulfillment", which is **not** a restricted role — no PII approval.
  Must be authorised by the account's Primary User (you).
- **India is on the EU endpoint** — marketplace `A21TJRUUN4KGV`,
  `https://sellingpartnerapi-eu.amazon.com`.
- **Rate limits are a non-issue.** 2 requests/second on everything; a whole shipment is
  ~12 calls.
- **The flow is asynchronous.** `generate*`/`confirm*` return an `operationId` you poll.
  Problems come back as `operationProblems[]` with `severity: WARNING | ERROR`, so **an
  HTTP 200 does not mean success** — the severity has to be inspected. Exactly the class
  of silent failure this codebase has been bitten by before.
- **Two India-specific features suggest real support**, both in the model:
  `deliveryChallanDocument` ("for PCP transportation in IN marketplace") and
  `updateItemComplianceDetails`, which is India-only and takes **`hsnCode`**,
  `declaredValue` and tax rates typed `CGST`/`SGST`/`IGST`. That last one lines up with
  our `hsn_master.json` — we may be able to push our 87 verified HSN codes upstream.
- **Placement options carry fees.** Each option has `fees[]`/`discounts[]` targeting
  "Placement Services" and "Fulfillment Fee Discount", so the cost of consolidated
  placement vs a free multi-FC split is machine-comparable. Options **expire**, so they
  cannot be cached.
- **No official Amazon Python SDK.** `python-amazon-sp-api` (saleweaver) is credible and
  actively maintained (v2.1.20, 2026-08-01), and ships both a
  `fulfillment_inbound_2024_03_20` module **and an asyncio variant** — which matters,
  because this app is async throughout and a blocking HTTP client inside a request
  handler would stall the event loop.

---

## The call sequence

Roughly twelve calls, five of them asynchronous:

1. `POST /inboundPlans` — source address, marketplace, items (`msku`, `quantity`,
   `prepOwner`, `labelOwner`). One marketplace per request. → `inboundPlanId`
2. `generatePackingOptions` → `getPackingOptions` → `confirmPackingOption`
3. `setPackingInformation` — box dimensions and weights
4. `generatePlacementOptions` → `getPlacementOptions` → **`confirmPlacementOption`**
   ← *this is the call that creates the shipments*
5. `generateTransportationOptions` → `getTransportationOptions` →
   `confirmTransportationOptions`
6. `getShipment` → `shipmentConfirmationId` (the `FBA…` ID) and `destination`

Two points of substance. **Step 4 is the commit point** — before it there are no
shipments, after it there are, and it cannot be undone by not writing our row. And
**step 3 needs box dimensions and weights we do not currently hold**: we know net product
weight (`logic.shipment_weight`) but not carton dimensions. That is a new input the
warehouse would have to give us.

---

## How it would fit this codebase

Five things follow from decisions already made here:

1. **The mutation belongs behind one module**, `app/shipment/spapi.py`, the way
   `catalogue.py` isolates the Google Sheet. Nothing else should know about LWA tokens.
2. **The parser stays synchronous and offline.** `app/invoice/parser.py` deliberately
   reads `product_families.json` rather than fetching the live sheet, because it runs
   during an upload and must not make the invoice screen wait on the network. SP-API is
   the same argument, more so — it must never be called from a document path.
3. **Credentials are `.env`, never committed.** Note `cookies.txt` is already in git
   history with a live token; a refresh token is worth more and must not repeat that.
4. **A created shipment is a fact in the world.** Like the invoice number, it cannot be
   rolled back by a failed transaction. The `invoice_id` attach window is documented as a
   known gap for exactly this reason, and this is worse: an inbound plan confirmed at
   Amazon with no local row is invisible to the app but real to Amazon. So the
   `inbound_plan_id` must be persisted **before** `confirmPlacementOption`, not after —
   the same lesson as "never let a bookkeeping bug roll back a committed invoice".
5. **`AMAZON_OPTIMIZED` must block, not guess.** If the state cannot be determined, the
   invoice must say so and fall back to manual entry. A guessed FC puts the wrong state's
   GSTIN on a tax document, and `get_gstin_for_state` does *partial* matching — it would
   return something plausible for a wrong input rather than failing.

---

## Sequence, if the spike says yes

| Step | What | Risk |
|---|---|---|
| 0 | **Spike.** Register the app, self-authorise, call `getInboundOperationStatus` or a read-only op. Confirm auth works at all. | Low — no mutation |
| 1 | **Answer the `destinationType` question** with one real cheap shipment. Record what `getShipment` returns. **Stop here if it is `AMAZON_OPTIMIZED` and `customPlacement` does not help.** | Creates a real shipment |
| 2 | `app/shipment/spapi.py`: token handling, retries, `operationProblems` severity. Tests against recorded fixtures, no live calls in the suite. | Low |
| 3 | Model + migration: `inbound_plan_id`, `shipment_confirmation_id`, `warehouse_id`, `destination_type`, `placement_fees`, status. Persisted before the commit point. | Medium |
| 4 | The flow behind an explicit two-step confirm, showing placement **fees** before step 4. | **Highest — spends money and creates real shipments** |
| 5 | Wire the returned ID + FC into the invoice payload, with the `AMAZON_OPTIMIZED` fallback. | Medium |
| 6 | Optional: push HSN via `updateItemComplianceDetails`; fetch the delivery challan. | Low |

## Open questions for the live account

1. Does Amazon.in return `AMAZON_OPTIMIZED` (no address, no `warehouseId`)? **Decides the
   project.**
2. Does India's `customPlacement` let us choose the FC?
3. Is v0 actually switched off?
4. Do India placement options carry fees at all?
5. Who supplies carton dimensions and weights for `setPackingInformation`?

## What I recommend

Do step 0 and step 1 — a day's work, one cheap shipment — before committing to anything
else. The honest position is that **this project's value rests on an unverified
assumption**, and one API call settles it. If the state comes back, the plan above is
worth building. If it does not, the automation saves you typing a shipment ID and
nothing more, and the manual Excel route we shipped today is most of the value already.
