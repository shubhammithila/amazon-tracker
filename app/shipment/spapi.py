"""Amazon Selling Partner API client — the only place that talks to Amazon's API.

Isolated the way ``catalogue.py`` isolates the Google Sheet: nothing else in the app
knows about LWA tokens or endpoint shapes, so a change to either is one file.

**Everything here is read-only.** No function creates, confirms or modifies anything at
Amazon. Creating an inbound plan is a real shipment and possibly a real fee, so it is a
deliberate later step with its own explicit confirmation — not something that can happen
as a side effect of a page load.

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

import logging
import time
from dataclasses import dataclass, field
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


async def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    """One GET against SP-API, with the token attached and errors typed.

    No SigV4 and no AWS credentials: SP-API dropped that requirement, and the only
    required headers are the access token and a user agent.
    """
    settings = get_settings()
    if not settings.spapi_configured:
        raise SpApiNotConfigured()

    async with httpx.AsyncClient(timeout=settings.sp_api_timeout) as client:
        token = await _access_token(client)
        response = await client.get(
            settings.sp_api_endpoint + path,
            params=params,
            headers={
                "x-amz-access-token": token,
                "user-agent": "AmazonTracker/2.0 (Language=Python)",
            },
        )

    if response.status_code == 200:
        try:
            return response.json()
        except ValueError as exc:
            raise SpApiError(f"Amazon returned a non-JSON body for {path}") from exc

    # Amazon's errors are `{"errors": [{"code", "message", "details"}]}`. The message is
    # written for a developer and is genuinely the most useful thing to surface — it is
    # what said "not supported for the Indian marketplace" and saved building a flow
    # that cannot work.
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


async def list_inbound_plans(page_size: int = DEFAULT_PLAN_PAGE_SIZE) -> list[dict]:
    """Recent inbound plans, newest first. Read-only.

    Sorted here rather than trusting Amazon's order: the live response came back with the
    10 plans NOT in date order, so a picker built on it would put a July plan above an
    August one and the owner would choose the wrong shipment.
    """
    payload = await _get(
        "/inbound/fba/2024-03-20/inboundPlans", {"pageSize": max(1, min(page_size, 30))}
    )
    plans = payload.get("inboundPlans") or []
    plans.sort(key=lambda p: str(p.get("createdAt") or ""), reverse=True)
    return plans


async def get_inbound_plan(inbound_plan_id: str) -> dict:
    """One plan in detail. Its `shipments` array is where the shipment ids live —
    the list endpoint for them is a 403 on this account."""
    return await _get(f"/inbound/fba/2024-03-20/inboundPlans/{inbound_plan_id}")


async def get_shipment(inbound_plan_id: str, shipment_id: str) -> AmazonShipment:
    """One shipment, with its confirmation id and destination."""
    payload = await _get(
        f"/inbound/fba/2024-03-20/inboundPlans/{inbound_plan_id}/shipments/{shipment_id}"
    )
    return _shipment_from_payload(inbound_plan_id, shipment_id, payload)


async def recent_shipments(limit: int = 10) -> list[AmazonShipment]:
    """The shipments behind the recent plans, ready for the picker.

    Two calls per plan (detail, then each shipment), which is why `limit` is small. At
    2 requests/second and ~10 plans this is a couple of seconds — acceptable for an
    explicit "look up my shipments" click, and the reason this is not done on page load.

    A plan that fails is skipped rather than failing the whole list: one bad plan must
    not hide the nine good ones the owner is trying to choose between.
    """
    plans = await list_inbound_plans()
    shipments: list[AmazonShipment] = []

    for plan in plans:
        if len(shipments) >= limit:
            break
        plan_id = str(plan.get("inboundPlanId") or "")
        if not plan_id:
            continue
        created = str(plan.get("createdAt") or "")
        try:
            detail = await get_inbound_plan(plan_id)
        except SpApiError as exc:
            logger.warning("spapi: skipping plan %s (%s)", plan_id, exc.message)
            continue

        for entry in detail.get("shipments") or []:
            shipment_id = str(entry.get("shipmentId") or "")
            if not shipment_id:
                continue
            try:
                shipment = await get_shipment(plan_id, shipment_id)
            except SpApiError as exc:
                logger.warning(
                    "spapi: skipping shipment %s (%s)", shipment_id, exc.message
                )
                continue
            shipment.created_at = created
            shipments.append(shipment)
            if len(shipments) >= limit:
                break

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
