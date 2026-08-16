# The verified create sequence (Amazon.in, self-ship / "Other" carrier)

**Everything here was executed against the live account on 2026-08-16**, using a 1-unit
test plan that was then cancelled (`VOIDED`). Not read from documentation.

Test plan: `wf6e9c00b5-bb0d-4f5e-b237-e376e0977885` — created, placement generated,
cancelled. All 10 real plans unaffected and still `ACTIVE`.

## What the test settled

| Question | Answer, measured |
|---|---|
| Does `customPlacement` let us CHOOSE the FC? | **YES.** Asked for ISK3, got `warehouseId: ISK3`, `AMAZON_WAREHOUSE`, Bhiwandi address |
| Is `setPackingInformation` required in India? | **No.** Placement generated straight after create, no packing call, no box dimensions |
| Placement fee? | **₹0** (`Placement Services`, `amount: 0`, `INR`) |
| Is a plan reversible? | **Yes** — `PUT /inboundPlans/{id}/cancellation` → `VOIDED` |
| `prepOwner` value? | **`NONE`**, not `SELLER` — see below |

## The `prepOwner` correction

My builder sent `prepOwner: SELLER` because that is what every existing plan *reports*.
Amazon rejected it:

```
400 ERROR: abc_sattu500g FBA does not require prepOwner but SELLER was assigned.
           Accepted values: [NONE]
```

So a value Amazon **returns** on a created plan is not necessarily one it **accepts** on
creation. `labelOwner: SELLER` + `prepOwner: NONE` was accepted (202).

This is per-SKU: the message names the msku, so a SKU that genuinely needs prep would
accept `SELLER`. The safe approach is to send `NONE` and let Amazon's error name any SKU
that needs otherwise, rather than guessing per product.

## The sequence, as it actually runs

```
1. POST /inboundPlans                                   -> 202 {inboundPlanId, operationId}
   GET  /operations/{operationId}                        -> poll to SUCCESS
2. POST /inboundPlans/{id}/placementOptions              -> 202 {operationId}
      body: {"customPlacement":[{"warehouseId":"ISK3","items":[...]}]}
   GET  /operations/{operationId}                        -> poll to SUCCESS
   GET  /inboundPlans/{id}/placementOptions              -> option + fees + shipmentIds
3. POST .../placementOptions/{optionId}/confirmation     <- COMMIT POINT, not yet run
4. POST /inboundPlans/{id}/transportationOptions         (carrier "Other")
   POST .../transportationOptions/confirmation
5. GET  /inboundPlans/{id}/shipments/{sid}               -> shipmentConfirmationId FBA15…
6. GET  /fba/inbound/v0/shipments/{FBA15…}/labels        -> box labels
```

Steps 1–2 are proven. Step 3 onwards has not been executed — that is the irreversible
half, and it needs a real shipment.

## Operation polling

`createInboundPlan` and `generatePlacementOptions` both return `202` with an
`operationId`. Statuses observed: `IN_PROGRESS` → `SUCCESS`, with `operationProblems: []`.
Placement took ~6 seconds. **A 202 is not success** — the operation must be polled, and
`operationProblems` inspected for `ERROR` severity.

## What a real self-ship plan looks like (existing plan, for reference)

```
transportationOptions: carrier={'name': 'Other'}
                       shippingMode=GROUND_SMALL_PARCEL
                       shippingSolution=USE_YOUR_OWN_CARRIER
                       preconditions=[]
selfShipAppointmentDetails: [{appointmentId, appointmentStatus: "Confirmed",
                              appointmentSlotTime: {startTime, endTime}}]
dates: {readyToShipWindow: {start, end}}
```

`GenerateTransportationOptionsRequest` requires `placementOptionId` and, per shipment, a
`readyToShipWindow` — a date the owner must supply. `Pallet` and `freightInformation` are
optional and irrelevant to GROUND_SMALL_PARCEL.

## Still unknown

- Whether `confirmPlacementOption` on a **multi-SKU, multi-carton** plan splits into
  several shipments. The test had one SKU, one unit. If it splits, each shipment gets its
  own `FBA15…` and the invoice must handle more than one.
- Whether a self-ship **appointment** is mandatory before labels are available, or only
  before the truck arrives.
- Whether `readyToShipWindow` must be in the future by some margin.
