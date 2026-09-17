# Probe: what is actually missing to complete an FBA shipment

**Executed read-only against the live Amazon.in account on 2026-09-15.** No mutations, no
plans created, nothing confirmed. Every line below is a response, not documentation.

This probe was run because the report was *"create shipment is not making the complete
shipment, it is just making a plan and that too with error"*, and because this codebase has
twice recently mis-diagnosed a shipment problem by theorising instead of reading the data.

## The headline: two brainstorm assumptions were WRONG

| I assumed | Measured |
|---|---|
| Box labels are the riskiest unknown, possibly needing a new role | **Labels already work**, all 3 formats, on a shipment created today |
| Declaring 26 boxes of mixed contents is the hard blocker | **India refuses box endpoints entirely** — `ListShipmentBoxes` is a 400 |

So the feature is far closer than the brainstorm supposed. Recorded because the wrong
version of both is entirely plausible and would have cost days.

## The two plans compared

`wfa42d5d15…` created today 13:51Z, and `wf2744a751…` created 14 Sep which shipped.

| Field | Today — `READY_TO_SHIP` | 14 Sep — `IN_TRANSIT` |
|---|---|---|
| `shipmentConfirmationId` | `FBA15MGGGPKK` | `FBA15MG3ZWVW` |
| `placementOptions[].status` | `ACCEPTED` | `ACCEPTED` |
| `selectedTransportationOptionId` | `tof0733cd7…` | `to8e7b12c7…` |
| destination | ISK3 / BHIWANDI / MAHARASHTRA | same |
| **`dates`** | **`{}`** | **`{readyToShipWindow: {start: 2026-09-18T18:30Z, end: …}}`** |
| **`selfShipAppointmentDetails`** | **absent** | **`[{appointmentId: 1118021014973, status: "Confirmed", slot 2026-09-26 08:45–09:00Z}]`** |

**Those last two rows are the entire gap.** Everything else already matches a shipment that
successfully went out. They correspond exactly to the two screenshots the app cannot yet
produce: the "Ship date DD/MM/YYYY" box, and "View FC Appointment Slot" /
"Fc Appointment confirmation".

Today's shipment carries the right contents — **8 lines, 640 units**, matching the
screenshot's `SKUs: 8  Units: 640` to the unit:

```
1kg mka FBA 10 · 1kg jas FBA 50 · 1kg cs FBA 200 · 0.5kg jas FBA 100
1kg ria FBA 50 · abc_sattu500g FBA 100 · kuDa 500g FBA 100 · triphala_sattu500g FBA 30
```

## Labels are available NOW, at `READY_TO_SHIP`

All three page types returned a signed S3 URL for **both** shipments:

```
FBA15MGGGPKK  PackageLabel_Thermal      OK  https://fba-labels-prod-eu.s3…
FBA15MGGGPKK  PackageLabel_A4_4         OK
FBA15MGGGPKK  PackageLabel_Plain_Paper  OK
```

So a confirmed appointment is **not** a precondition for labels — one of the three open
questions `docs/spapi-create-sequence-verified.md` left unanswered. It is answered: no.

## Transportation: exactly one option, and the ids are mandatory

`GET /inboundPlans/{id}/transportationOptions` **400s with neither id supplied**:

```
ERROR: Operation ListTransportationOptions cannot be processed, because neither
       shipment id nor placement option id was provided.
```

With either `placementOptionId` or `shipmentId` it returns **one** option on both plans:

```
carrier: Other | shippingMode: GROUND_SMALL_PARCEL
shippingSolution: USE_YOUR_OWN_CARRIER | preconditions: []
```

`preconditions: []` matters — this option needs nothing supplied before it can be
confirmed, which is consistent with India refusing the box endpoints.

## Three endpoints India refuses, measured

```
400  ListShipmentBoxes         "not supported for the Indian marketplace"
400  ListDeliveryWindowOptions "not supported for the Indian marketplace"
400  GetSelfShipAppointmentSlots
     "can be processed only for shipments which have appointment slots generated at
      least once. Please use GenerateSelfShipAppointmentSlots"
```

The third is not a refusal but an **ordering rule**: slots must be *generated* before they
can be listed — the same generate-then-list shape as placement options. So the appointment
is a two-call sequence, not one.

## No plan in this account was created by our app

Every one of the 20 plans returns an **empty `name`**. `create_inbound_plan` always passes
a non-empty `name` (`"{plan label} · {dates} · {fc}"`, truncated to 60). Therefore none of
the 20 came from this app — all were made in Seller Central. The shipment-level names are
Amazon's own auto-format (`FBA STA (15/09/2026 13:54)-ISK3`).

`list_inbound_plans` also does **not** return `VOIDED` plans: the test plan
`wf6e9c00b5…` from the August probe is absent though its creation date falls inside the
returned range. So an app-created plan that failed and was voided would be invisible here.

**Conclusion: the reported error is in the create/placement half, and this probe cannot see
it** — it needs the app's own error text or the production log. Not guessed at here.

## What this means for the build

1. **`confirmTransportationOptions` with a `readyToShipWindow` is the missing stage.** It is
   what turns `READY_TO_SHIP`-without-a-date into a shipment with a ship date.
2. **The self-ship appointment is a separate generate-then-confirm pair**, and it is *not*
   needed for labels — so it can ship after, and the owner is not blocked meanwhile.
3. **No box-contents data entry is needed.** The brainstorm's biggest worry does not apply
   to India, and `ShipmentPackingEntry.cartons` can stay deleted.
4. The create-time error still needs its actual text before that half is touched.
