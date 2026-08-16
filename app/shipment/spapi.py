"""Amazon Selling Partner API client — the only place that talks to Amazon's API.

Isolated the way ``catalogue.py`` isolates the Google Sheet: nothing else in the app
knows about LWA tokens or endpoint shapes, so a change to either is one file.

**Read and write are separated by function, not by convention.** Everything reachable
through ``_get`` is read-only; everything reachable through ``_post`` changes state at
Amazon. One of the writes — ``confirm_placement`` — creates a real shipment that no local
rollback can undo, so it is only ever reached from an explicit second click by the owner,
and never as a side effect of loading a page.

Everything below was verified against the live Amazon.in account (2026-08-15) rather than
read from documentation, which matters because the documentation describes a flow India
does not use. Three operations are refused outright::

    400  ListPackingOptions          "not supported for the Indian marketplace"
    400  ListShipmentBoxes           "not supported for the Indian marketplace"
    400  GetDeliveryChallanDocument  "not supported for non-PCP transportation option"

Two more traps, both of which cost real debugging time:

* **``getLabels`` keys on the ``shipmentConfirmationId``** (``FBA15M59XQFZ``), not the
  internal ``shipmentId`` (``shcc4552…``). They are different strings for the same thing.
* **``PageSize`` and ``PageStartIndex`` are mandatory** for non-partnered ("Other"
  carrier) shipments, which is every shipment this business sends. Omit them and every
  request 400s with *"pageSize must be provided for Non-Partnered and LTL Shipments"* —
  which reads exactly like a permissions failure and is not one.
* **Listing shipments is a 403** (``GET /inboundPlans/{id}/shipments``) while fetching one
  by id is 200. The ids are already in the plan detail payload, so we read them from
  there.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

#: Amazon's access tokens last 3600s. Refreshed a minute early so a request that is
#: about to be made cannot be the one that discovers the expiry — the failure would be
#: an opaque 403 in the middle of a multi-call sequence.
_TOKEN_SAFETY_MARGIN = 60

#: Enough for the "which shipment is this?" picker without paging.
DEFAULT_PLAN_PAGE_SIZE = 20

#: Parallel requests when fetching the shipments behind several plans. Amazon documents
#: 2 requests/second on these operations, so this stays at 2 rather than spending the
#: burst allowance — an intermittent 429 in a picker is worse than a slower picker.
#: Sequentially the same work measured 21 seconds from Mumbai to the EU endpoint.
_CONCURRENCY = 2

#: The label page types that WORK for a non-partnered shipment, measured. A4_2 and
#: Letter_2 return "We are not able to fetch carrier labels while it is required" for
#: this shipment type, so they are deliberately not offered — an option that always
#: errors reads as a broken app rather than as a rule.
LABEL_PAGE_TYPES = (
    "PackageLabel_Thermal",
    "PackageLabel_A4_4",
    "PackageLabel_Plain_Paper",
)


class SpApiError(RuntimeError):
    """Any SP-API failure, carrying enough to show the owner something true.

    A distinct type rather than letting ``httpx`` errors escape, because the callers are
    HTTP routes that must turn this into a message rather than a 500.
    """

    def __init__(self, message: str, *, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class SpApiNotConfigured(SpApiError):
    """No credentials. A different type because it is not a failure, it is a state.

    The screen should say "Amazon API is not set up" rather than showing an error — the
    app worked without SP-API for its whole life and must keep doing so.
    """

    def __init__(self) -> None:
        super().__init__(
            "Amazon SP-API is not configured. Set SP_API_CLIENT_ID, "
            "SP_API_CLIENT_SECRET and SP_API_REFRESH_TOKEN in .env."
        )


@dataclass
class _Token:
    value: str = ""
    expires_at: float = 0.0

    @property
    def usable(self) -> bool:
        return bool(self.value) and time.time() < self.expires_at - _TOKEN_SAFETY_MARGIN


#: Module-level so one token is shared across requests. A token per request would mean an
#: LWA round trip on every page load, and Amazon rate-limits that endpoint too.
_token = _Token()


def reset_token_cache() -> None:
    """Forget the cached token. For tests, and after a credential change."""
    global _token
    _token = _Token()


async def _access_token(client: httpx.AsyncClient) -> str:
    """A valid access token, from cache when possible.

    The refresh token is long-lived and never expires in normal use; the access token
    lasts an hour. Only the latter is exchanged here.
    """
    settings = get_settings()
    if not settings.spapi_configured:
        raise SpApiNotConfigured()

    if _token.usable:
        return _token.value

    response = await client.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": settings.sp_api_refresh_token,
            "client_id": settings.sp_api_client_id,
            "client_secret": settings.sp_api_client_secret,
        },
        timeout=settings.sp_api_timeout,
    )
    if response.status_code != 200:
        # The body carries `error` and `error_description`, and those are the only
        # useful thing about an auth failure — "invalid_grant" means the refresh token
        # was revoked, which is a different problem from a wrong client secret.
        detail = ""
        try:
            payload = response.json()
            detail = f"{payload.get('error')}: {payload.get('error_description')}"
        except Exception:
            detail = response.text[:200]
        raise SpApiError(
            f"Amazon rejected the credentials ({detail}). The refresh token may have "
            "been revoked, or the client secret rotated.",
            status=response.status_code,
        )

    payload = response.json()
    _token.value = payload["access_token"]
    _token.expires_at = time.time() + float(payload.get("expires_in") or 3600)
    return _token.value


async def _get(
    path: str,
    params: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """One GET against SP-API, with the token attached and errors typed.

    No SigV4 and no AWS credentials: SP-API dropped that requirement, and the only
    required headers are the access token and a user agent.

    ``client`` lets a caller making several calls **reuse one connection**. Measured: 3
    calls on a reused client took 2.5s total, while a fresh ``AsyncClient`` per call cost
    ~1.2s each because every request repaid the TCP and TLS handshake to Amazon's EU
    endpoint. Parallelism alone did not fix it — the handshake was the cost, not the
    waiting.
    """
    settings = get_settings()
    if not settings.spapi_configured:
        raise SpApiNotConfigured()

    async def _send(session: httpx.AsyncClient) -> httpx.Response:
        token = await _access_token(session)
        return await session.get(
            settings.sp_api_endpoint + path,
            params=params,
            headers={
                "x-amz-access-token": token,
                "user-agent": "AmazonTracker/2.0 (Language=Python)",
            },
        )

    if client is not None:
        response = await _send(client)
    else:
        async with httpx.AsyncClient(timeout=settings.sp_api_timeout) as session:
            response = await _send(session)

    return _payload_or_raise(response, path)


def _payload_or_raise(response: httpx.Response, path: str) -> dict:
    """The JSON body, or an SpApiError carrying Amazon's own message.

    Shared by the read and write helpers so a mutation cannot report failure differently
    from a read.

    **Amazon's message is surfaced verbatim.** It is written for a developer and has been
    the single most useful thing at every step: it is what said "not supported for the
    Indian marketplace", and what said *"does not require prepOwner but SELLER was
    assigned. Accepted values: [NONE]"* — naming the exact field and the exact accepted
    value. Paraphrasing it would have cost hours.

    202 counts as success: every mutating operation here is asynchronous and answers 202
    with an `operationId` to poll.
    """
    if response.status_code in (200, 202):
        try:
            return response.json()
        except ValueError as exc:
            raise SpApiError(f"Amazon returned a non-JSON body for {path}") from exc

    code, message = "", response.text[:300]
    try:
        errors = response.json().get("errors") or []
        if errors:
            code = str(errors[0].get("code") or "")
            message = str(errors[0].get("message") or message)
    except Exception:
        pass

    raise SpApiError(
        f"Amazon said: {message}", status=response.status_code, code=code
    )


async def _post(
    path: str,
    body: dict | None = None,
    client: httpx.AsyncClient | None = None,
    method: str = "POST",
) -> dict:
    """One mutating call against SP-API.

    **Separate from ``_get`` on purpose.** Everything reachable through this function
    changes state at Amazon, and one of them — ``confirmPlacementOption`` — creates a real
    shipment that no local rollback can undo. Keeping the write path in its own function
    means the read path cannot mutate by accident, and a test can assert exactly which
    callers reach here.

    ``method`` exists only for ``cancelInboundPlan``, which Amazon defines as a PUT.
    """
    settings = get_settings()
    if not settings.spapi_configured:
        raise SpApiNotConfigured()

    async def _send(session: httpx.AsyncClient) -> httpx.Response:
        token = await _access_token(session)
        return await session.request(
            method,
            settings.sp_api_endpoint + path,
            json=body if body is not None else None,
            headers={
                "x-amz-access-token": token,
                "user-agent": "AmazonTracker/2.0 (Language=Python)",
                "content-type": "application/json",
            },
        )

    if client is not None:
        response = await _send(client)
    else:
        async with httpx.AsyncClient(timeout=settings.sp_api_timeout) as session:
            response = await _send(session)

    return _payload_or_raise(response, path)


def operation_problems(payload: dict) -> list[dict]:
    """The ERROR-severity problems from an async operation status.

    Async operations answer 200 and report failure inside `operationProblems[]` with a
    `severity` of WARNING or ERROR. **A 200 is therefore not success**, and this codebase
    has been bitten by silent failures often enough that the severity has to be inspected
    rather than the status code trusted.

    WARNINGs are returned separately by the caller when it wants them; only ERRORs mean
    the operation did not do what was asked.
    """
    problems = payload.get("operationProblems") or []
    return [
        p for p in problems
        if str((p or {}).get("severity", "")).upper() == "ERROR"
    ]


@dataclass
class AmazonShipment:
    """One shipment on one inbound plan, flattened to what the invoice needs.

    ``warehouse_id`` and ``state`` come from Amazon rather than from the FC the owner
    picked, and that is the point: they are the truth about where the boxes are going,
    and the destination state decides which GSTIN applies.
    """

    inbound_plan_id: str
    shipment_id: str
    confirmation_id: str = ""     # FBA15M59XQFZ — what goes on the invoice
    warehouse_id: str = ""        # ISK3
    state: str = ""               # MAHARASHTRA
    city: str = ""
    postal_code: str = ""
    destination_type: str = ""    # AMAZON_WAREHOUSE | AMAZON_OPTIMIZED
    status: str = ""
    name: str = ""
    created_at: str = ""

    @property
    def destination_known(self) -> bool:
        """True when Amazon actually told us where it is going.

        ``AMAZON_OPTIMIZED`` is allowed to carry an empty address and warehouse id, and
        then the destination state is unknown — so the invoice must not silently claim
        one. Measured on this account it comes back ``AMAZON_WAREHOUSE`` with everything
        filled in, but that is not guaranteed for every shipment.
        """
        return bool(self.warehouse_id and self.state)

    def as_dict(self) -> dict:
        return {
            "inbound_plan_id": self.inbound_plan_id,
            "shipment_id": self.shipment_id,
            "confirmation_id": self.confirmation_id,
            "warehouse_id": self.warehouse_id,
            "state": self.state,
            "city": self.city,
            "postal_code": self.postal_code,
            "destination_type": self.destination_type,
            "destination_known": self.destination_known,
            "status": self.status,
            "name": self.name,
            "created_at": self.created_at,
        }


def _shipment_from_payload(
    inbound_plan_id: str, shipment_id: str, payload: dict, created_at: str = ""
) -> AmazonShipment:
    destination = payload.get("destination") or {}
    address = destination.get("address") or {}
    return AmazonShipment(
        inbound_plan_id=inbound_plan_id,
        shipment_id=shipment_id,
        confirmation_id=str(payload.get("shipmentConfirmationId") or ""),
        warehouse_id=str(destination.get("warehouseId") or ""),
        state=str(address.get("stateOrProvinceCode") or ""),
        city=str(address.get("city") or ""),
        postal_code=str(address.get("postalCode") or ""),
        destination_type=str(destination.get("destinationType") or ""),
        status=str(payload.get("status") or ""),
        name=str(payload.get("name") or ""),
        created_at=created_at,
    )


async def list_inbound_plans(
    page_size: int = DEFAULT_PLAN_PAGE_SIZE, client: httpx.AsyncClient | None = None
) -> list[dict]:
    """Recent inbound plans, newest first. Read-only.

    Sorted here rather than trusting Amazon's order: the live response came back with the
    10 plans NOT in date order, so a picker built on it would put a July plan above an
    August one and the owner would choose the wrong shipment.
    """
    payload = await _get(
        "/inbound/fba/2024-03-20/inboundPlans",
        {"pageSize": max(1, min(page_size, 30))},
        client=client,
    )
    plans = payload.get("inboundPlans") or []
    plans.sort(key=lambda p: str(p.get("createdAt") or ""), reverse=True)
    return plans


async def get_inbound_plan(
    inbound_plan_id: str, client: httpx.AsyncClient | None = None
) -> dict:
    """One plan in detail. Its `shipments` array is where the shipment ids live —
    the list endpoint for them is a 403 on this account."""
    return await _get(
        f"/inbound/fba/2024-03-20/inboundPlans/{inbound_plan_id}", client=client
    )


async def get_shipment(
    inbound_plan_id: str, shipment_id: str, client: httpx.AsyncClient | None = None
) -> AmazonShipment:
    """One shipment, with its confirmation id and destination."""
    payload = await _get(
        f"/inbound/fba/2024-03-20/inboundPlans/{inbound_plan_id}/shipments/{shipment_id}",
        client=client,
    )
    return _shipment_from_payload(inbound_plan_id, shipment_id, payload)


async def recent_shipments(limit: int = 10) -> list[AmazonShipment]:
    """The shipments behind the recent plans, ready for the picker.

    Two calls per plan — the plan detail, then each shipment on it — so ~20 requests for
    10 plans. **Run in bounded parallel**, because sequentially it measured **21 seconds**
    from the Mumbai box to Amazon's EU endpoint, and a lookup that slow reads as a hung
    page and gets clicked again.

    ``_CONCURRENCY`` is 2 to respect Amazon's documented 2 requests/second on these
    operations. Deliberately not higher: a 429 here would make the picker fail
    intermittently, which is far worse than it being a few seconds slow, and the burst
    allowance is not worth spending on a convenience lookup.

    Order is restored after gathering. ``asyncio.gather`` preserves the order of its
    arguments, but the plans are sorted by date beforehand and that sort is the whole
    point — the newest shipment must be the first one offered.

    A plan or shipment that fails is skipped rather than failing the whole list: one bad
    plan must not hide the nine good ones the owner is choosing between.
    """
    settings = get_settings()
    semaphore = asyncio.Semaphore(_CONCURRENCY)

    # ONE client for every call below, so the TCP and TLS handshake to Amazon is paid
    # once instead of ~20 times. This was the actual cost: parallelising without it left
    # the lookup slower than sequential.
    async with httpx.AsyncClient(timeout=settings.sp_api_timeout) as client:
        # Only as many plans as could possibly be needed. Each plan on this account has
        # exactly one shipment, so `limit` plans is enough — and fetching detail for all
        # 10 when the caller asked for 3 was costing ~7 wasted round trips. If a plan
        # ever carries several shipments the list simply comes back short, which is
        # honest, rather than slow for everyone.
        plans = [
            p for p in await list_inbound_plans(client=client) if p.get("inboundPlanId")
        ][:limit]

        async def detail_for(plan: dict):
            async with semaphore:
                plan_id = str(plan["inboundPlanId"])
                try:
                    return plan, await get_inbound_plan(plan_id, client=client)
                except SpApiError as exc:
                    logger.warning("spapi: skipping plan %s (%s)", plan_id, exc.message)
                    return plan, None

        details = await asyncio.gather(*(detail_for(p) for p in plans))

        # (plan_id, shipment_id, created_at) for every shipment across every plan, still
        # in newest-first plan order.
        wanted: list[tuple[str, str, str]] = []
        for plan, detail in details:
            if not detail:
                continue
            plan_id = str(plan["inboundPlanId"])
            created = str(plan.get("createdAt") or "")
            for entry in detail.get("shipments") or []:
                shipment_id = str(entry.get("shipmentId") or "")
                if shipment_id:
                    wanted.append((plan_id, shipment_id, created))
        wanted = wanted[:limit]

        async def shipment_for(plan_id: str, shipment_id: str, created: str):
            async with semaphore:
                try:
                    shipment = await get_shipment(plan_id, shipment_id, client=client)
                except SpApiError as exc:
                    logger.warning(
                        "spapi: skipping shipment %s (%s)", shipment_id, exc.message
                    )
                    return None
                shipment.created_at = created
                return shipment

        gathered = await asyncio.gather(*(shipment_for(*w) for w in wanted))

    shipments = [s for s in gathered if s is not None]

    return shipments


async def label_url(
    confirmation_id: str, page_type: str = "PackageLabel_Thermal"
) -> str:
    """A download URL for Amazon's box labels.

    Keyed on the **confirmation id** (`FBA15M59XQFZ`), not the internal shipment id —
    passing the wrong one 400s in a way that looks like a permissions problem.

    `PageSize`/`PageStartIndex` are mandatory because these are non-partnered ("Other"
    carrier) shipments. Both were measured as required; without them every page type
    fails.
    """
    if page_type not in LABEL_PAGE_TYPES:
        raise SpApiError(
            f"{page_type} is not a label format that works for an 'Other' carrier "
            f"shipment. Use one of: {', '.join(LABEL_PAGE_TYPES)}."
        )
    settings = get_settings()
    payload = await _get(
        f"/fba/inbound/v0/shipments/{confirmation_id}/labels",
        {
            "MarketplaceId": settings.sp_api_marketplace_id,
            "PageType": page_type,
            "LabelType": "BARCODE_2D",
            "PageSize": 1,
            "PageStartIndex": 0,
        },
    )
    url = ((payload.get("payload") or {}).get("DownloadURL") or "")
    if not url:
        raise SpApiError("Amazon returned no label download URL.")
    return str(url)


# ─── Mutating operations: these change state at Amazon ───────────────────────
#
# Everything below was executed against the live account (see
# docs/spapi-create-sequence-verified.md) EXCEPT `confirm_placement`, which is the commit
# point and is only ever reached from an explicit second click by the owner.

#: `prepOwner` on a created line. **`NONE`, not `SELLER`** — the correction the test plan
#: taught, and a genuinely surprising one. Every existing plan REPORTS
#: `prepOwner: SELLER`, so that is what the builder sent, and Amazon rejected it:
#:
#:   400 ERROR: abc_sattu500g FBA does not require prepOwner but SELLER was assigned.
#:              Accepted values: [NONE]
#:
#: A value Amazon RETURNS is not necessarily one it ACCEPTS. The error names the msku, so
#: this is per-SKU — sending NONE and letting Amazon name any SKU that needs otherwise is
#: safer than guessing per product.
PREP_OWNER = "NONE"
LABEL_OWNER = "SELLER"

#: How long to wait for an async operation. Placement took ~6s on the test plan; 90s is
#: generous for a 200-line plan without hanging a request for ever.
_OPERATION_TIMEOUT = 90
_OPERATION_POLL_SECONDS = 3


async def wait_for_operation(
    operation_id: str, client: httpx.AsyncClient | None = None
) -> dict:
    """Poll an async operation to completion, raising on ERROR-severity problems.

    Every mutating call answers **202** and does the work afterwards, so returning at 202
    would report success for something that has not happened yet and might still fail.
    Observed on the test plan: `IN_PROGRESS` → `SUCCESS`, `operationProblems: []`.

    Raises rather than returning a status: every caller would otherwise have to remember
    to check, and forgetting means silently continuing a broken sequence.
    """
    deadline = time.time() + _OPERATION_TIMEOUT
    while time.time() < deadline:
        payload = await _get(
            f"/inbound/fba/2024-03-20/operations/{operation_id}", client=client
        )
        status = str(payload.get("operationStatus") or "").upper()
        if status == "SUCCESS":
            return payload
        if status == "FAILED":
            problems = operation_problems(payload)
            detail = "; ".join(
                str(p.get("message") or "") for p in problems
            ) or "Amazon gave no reason."
            raise SpApiError(f"Amazon could not complete the operation: {detail}")
        await asyncio.sleep(_OPERATION_POLL_SECONDS)

    raise SpApiError(
        f"Amazon did not finish the operation within {_OPERATION_TIMEOUT}s. It may still "
        "complete, so check Seller Central before retrying — otherwise the same shipment "
        "could be created twice."
    )


async def create_inbound_plan(
    source_address: dict,
    items: list[dict],
    marketplace_id: str,
    name: str = "",
    client: httpx.AsyncClient | None = None,
) -> str:
    """Create an inbound plan. Returns its `inboundPlanId`.

    **This creates state at Amazon.** It is not the commit point — no shipment exists
    until placement is confirmed, and the plan can be cancelled — but it appears in Seller
    Central immediately, so it is only ever called from an explicit owner action.

    `setPackingInformation` is deliberately NOT called. India refuses both
    `ListPackingOptions` and `ListShipmentBoxes`, and the test plan generated placement
    options with no packing call and no box dimensions at all.
    """
    payload = await _post(
        "/inbound/fba/2024-03-20/inboundPlans",
        {
            "destinationMarketplaces": [marketplace_id],
            "sourceAddress": source_address,
            "items": items,
            **({"name": name} if name else {}),
        },
        client=client,
    )
    plan_id = str(payload.get("inboundPlanId") or "")
    if not plan_id:
        raise SpApiError("Amazon accepted the plan but returned no inboundPlanId.")
    operation_id = str(payload.get("operationId") or "")
    if operation_id:
        await wait_for_operation(operation_id, client=client)
    return plan_id


async def generate_placement_options(
    inbound_plan_id: str,
    warehouse_id: str,
    items: list[dict],
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Ask Amazon to place the plan, naming the destination FC. Returns the options.

    ``customPlacement`` is **India-only, and it works**: the test plan asked for ISK3 and
    got `warehouseId: ISK3`, `AMAZON_WAREHOUSE`, with the Bhiwandi address. That is what
    makes the owner's FC choice real rather than a request Amazon may ignore.

    The options carry `fees` and an `expiration`. Both are shown before confirming: the
    fee was ₹0 on the test plan, but it is Amazon's to change and an expired option cannot
    be confirmed.
    """
    payload = await _post(
        f"/inbound/fba/2024-03-20/inboundPlans/{inbound_plan_id}/placementOptions",
        {"customPlacement": [{"warehouseId": warehouse_id, "items": items}]},
        client=client,
    )
    operation_id = str(payload.get("operationId") or "")
    if operation_id:
        await wait_for_operation(operation_id, client=client)

    listed = await _get(
        f"/inbound/fba/2024-03-20/inboundPlans/{inbound_plan_id}/placementOptions",
        client=client,
    )
    return listed.get("placementOptions") or []


async def confirm_placement(
    inbound_plan_id: str,
    placement_option_id: str,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """**THE COMMIT POINT.** Confirming placement creates the real shipment.

    After this, `FBA…` shipment ids exist at Amazon, Seller Central shows a working
    shipment, and any placement fee is incurred. No database rollback can undo it — the
    same class of irreversibility as spending a number from the GST invoice series.

    So the caller must have persisted `inbound_plan_id` BEFORE calling this. A plan
    confirmed at Amazon with no local record is invisible to this app and real to them.
    """
    payload = await _post(
        f"/inbound/fba/2024-03-20/inboundPlans/{inbound_plan_id}"
        f"/placementOptions/{placement_option_id}/confirmation",
        client=client,
    )
    operation_id = str(payload.get("operationId") or "")
    if operation_id:
        await wait_for_operation(operation_id, client=client)
    return payload


async def cancel_inbound_plan(
    inbound_plan_id: str, client: httpx.AsyncClient | None = None
) -> dict:
    """Void a plan. Verified against the live account: the status becomes `VOIDED`.

    A PUT rather than a POST — Amazon's definition, not a choice. This is what made
    testing the sequence safe, and it is what lets the owner undo a plan created by
    mistake before it becomes a shipment.
    """
    payload = await _post(
        f"/inbound/fba/2024-03-20/inboundPlans/{inbound_plan_id}/cancellation",
        client=client,
        method="PUT",
    )
    operation_id = str(payload.get("operationId") or "")
    if operation_id:
        await wait_for_operation(operation_id, client=client)
    return payload


async def plan_shipments(
    inbound_plan_id: str, client: httpx.AsyncClient | None = None
) -> list[AmazonShipment]:
    """Every shipment on a plan, read from the plan detail.

    From the plan payload rather than `GET .../shipments`, which is a 403 on this account
    — and the ids are in the detail response anyway.

    Returns a LIST because a plan can split into several shipments. The test plan had one
    SKU and produced one, so a split has not been observed here; the caller handles more
    than one rather than assuming.
    """
    detail = await get_inbound_plan(inbound_plan_id, client=client)
    shipments = []
    for entry in detail.get("shipments") or []:
        shipment_id = str(entry.get("shipmentId") or "")
        if shipment_id:
            shipments.append(
                await get_shipment(inbound_plan_id, shipment_id, client=client)
            )
    return shipments


# ─── India GST compliance: required before placement ─────────────────────────
#
# Found the hard way. A plan created fine and then placement FAILED:
#
#   ERROR: Declared value need to be provided.
#
# `updateItemComplianceDetails` is the India-only operation that supplies it — HSN code,
# declared value and GST rate per merchant SKU. Amazon already held these for the SKUs
# tested (HSN 1106 at 5%, matching our own hsn_master.json), but placement still refused
# until they were re-declared, so this is sent for every SKU on every shipment rather than
# only for ones that look unset. It is idempotent and cheap.
#
# **The request body is FLAT, not a list.** Sending
# `{"complianceDetails": [{...}]}` — the shape the GET returns — is rejected with
# "3 validation errors detected: Value '' at 'request.msku' failed to satisfy
# constraint". One SKU per call, `{"msku": ..., "taxDetails": {...}}`. Another case of a
# shape Amazon RETURNS not being the shape it ACCEPTS.


async def get_item_compliance(
    mskus: list[str], client: httpx.AsyncClient | None = None
) -> dict[str, dict]:
    """What Amazon currently holds for these SKUs, keyed by msku. Read-only."""
    if not mskus:
        return {}
    settings = get_settings()
    payload = await _get(
        "/inbound/fba/2024-03-20/items/compliance",
        {
            "mskus": ",".join(mskus),
            "marketplaceId": settings.sp_api_marketplace_id,
        },
        client=client,
    )
    out: dict[str, dict] = {}
    for detail in payload.get("complianceDetails") or []:
        msku = str(detail.get("msku") or "")
        if msku:
            out[msku] = detail.get("taxDetails") or {}
    return out


async def declare_item_compliance(
    msku: str,
    hsn_code: str,
    declared_value: float,
    gst_rate: float,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Declare HSN, declared value and GST rate for ONE merchant SKU.

    One SKU per call because that is the contract — the body is flat and carries a single
    `msku`. Called for every line before placement, since placement refuses with
    "Declared value need to be provided" otherwise.

    `TOTAL_TAX` rather than CGST/SGST/IGST separately: the split depends on the destination
    state, which is not known until placement, and TOTAL_TAX is what Amazon already had on
    file for these SKUs.
    """
    settings = get_settings()
    await _post(
        "/inbound/fba/2024-03-20/items/compliance"
        f"?marketplaceId={settings.sp_api_marketplace_id}",
        {
            "msku": msku,
            "taxDetails": {
                "hsnCode": str(hsn_code or "1106"),
                "declaredValue": {"amount": round(float(declared_value or 0), 2),
                                  "code": "INR"},
                "taxRates": [
                    {"taxType": "TOTAL_TAX", "gstRate": float(gst_rate or 5),
                     "cessRate": 0}
                ],
            },
        },
        client=client,
        method="PUT",
    )
