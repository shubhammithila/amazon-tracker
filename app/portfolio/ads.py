"""The only caller of Amazon's Advertising API — ad spend against ATTRIBUTED sales.

**A separate application from SP-API, not a variant of it.** Different LWA client, different
refresh token, different host, and a different auth header shape: `Authorization: Bearer` plus
`Amazon-Advertising-API-ClientId` and `-Scope`, where SP-API wants `x-amz-access-token`. Trying
the SP-API credentials here returns a bare 401 that reads exactly like "this account has no
advertising access" — which is what it was mistaken for once, and is why this module has its own
token cache rather than borrowing `app.shipment.spapi`'s.

**Why this exists at all: SP-API cannot answer "do the ads pay for themselves".** The Economics
feed reports the ad CHARGE (that is how the Portfolio tab computes TACOS = spend / total sales)
but has no ad-attributed-sales column, so true ACOS = spend / attributed sales is not derivable
from it. Measured on the live account, 27 Jul – 26 Aug:

    ad cost (this API)        Rs 16,32,415
    ad cost (SP-API economics) Rs 16,35,983   <- reconciles to 0.2%
    ad-ATTRIBUTED sales       Rs 18,15,569
    TRUE ACOS                 89.9%
    TACOS                     33.1%

Rs 1 of advertising returns Rs 1.11 of attributed sales — close to break-even before product
costs, and invisible in TACOS. That gap is the reason for the whole module.

Three request-shape traps, each of which produced a real 400 and none of which the error message
names directly:

* **`groupBy` must be `["advertiser"]`.** `advertiser` is not documented as obvious; the API's own
  rejection lists the alternatives (`campaign`, `adGroup`, `campaignPlacement`).
* **`date` is not a legal column under `timeUnit: SUMMARY`.** The window comes from
  startDate/endDate instead. Including it fails the whole request.
* **`Content-Type: application/vnd.createasyncreportrequest.v3+json`**, not `application/json`.

**The report is split by CAMPAIGN even though nothing asked it to be.** Measured: 1,697 rows for
213 `(asin, sku)` pairs, up to 13 rows for one pair. This module aggregates to one row per pair
before returning, because the dashboard has no campaign view and two grains in one payload is how
a total starts disagreeing with its rows.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
REPORT_PATH = "/reporting/reports"

#: `spAdvertisedProduct` is the report keyed on the ADVERTISED product, which is the only grain
#: that joins to a catalogue ASIN. The alternatives are campaign- or target-level and answer a
#: different question.
REPORT_TYPE_ID = "spAdvertisedProduct"

#: Sponsored Products only, because that is all this account runs — measured, the report returns
#: exactly one ad type. A constant rather than a literal so Sponsored Brands or Display is a
#: one-line change when they start being used.
AD_PRODUCT = "SPONSORED_PRODUCTS"

#: The columns actually used. `attributedSalesSameSku14d` is the one SP-API cannot give, and
#: therefore the whole point: sales Amazon attributes to a click on THIS sku within 14 days.
REPORT_COLUMNS = (
    "impressions", "clicks", "cost",
    "advertisedAsin", "advertisedSku",
    "attributedSalesSameSku14d", "purchasesSameSku14d",
)

#: Content type for creating a v3 report. Plain application/json is rejected.
CREATE_CONTENT_TYPE = "application/vnd.createasyncreportrequest.v3+json"

#: Seconds between polls. Measured: a 30-day report took ~12 MINUTES to generate, so polling
#: faster only wastes calls. Deliberately slower than the economics poller for that reason.
POLL_INTERVAL = 20.0

#: Polls before giving up: 60 x 20s = 20 minutes. A ceiling, not an expectation. Giving up raises
#: rather than returning nothing, because "we could not ask" must never render as "no ad spend".
POLL_MAX = 60

#: Amazon's access tokens last 3600s; refreshed a minute early so a request that is about to be
#: made cannot be the one that discovers the expiry. Same margin as `shipment.spapi`.
_TOKEN_SAFETY_MARGIN = 60

_TERMINAL = ("COMPLETED", "FAILURE", "CANCELLED")


class AdsError(Exception):
    """An Advertising API call failed. Carries Amazon's own message where there is one."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class AdsNotConfigured(AdsError):
    """No advertising credentials.

    A distinct type so the refresh can SKIP the ACOS phase silently instead of reporting a
    failure. The Portfolio tab shipped before ACOS existed and is fully useful without it — the
    margins are the load-bearing part, ACOS is an addition.
    """

    def __init__(self) -> None:
        super().__init__(
            "Advertising API credentials are not configured, so ACOS is unavailable."
        )


@dataclass
class _Token:
    value: str = ""
    expires_at: float = 0.0


#: Module-level so one token serves the whole report sequence (create, poll, download) and any
#: later refresh in the same process. Mirrors `shipment.spapi._token`.
_token = _Token()


async def _access_token(client: httpx.AsyncClient) -> str:
    """A cached LWA access token for the ADS client.

    Deliberately not shared with `shipment.spapi._access_token`: that one is minted from the
    SP-API client id and secret, and the two applications are unrelated. A shared cache would
    hand an SP-API token to the ads host, which is exactly the 401 this module exists to avoid.
    """
    settings = get_settings()
    if not settings.ads_configured:
        raise AdsNotConfigured()

    if _token.value and time.time() < _token.expires_at - _TOKEN_SAFETY_MARGIN:
        return _token.value

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": settings.ads_refresh_token,
        "client_id": settings.ads_client_id,
        "client_secret": settings.ads_client_secret,
    })
    response = await client.post(
        LWA_TOKEN_URL, content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code >= 400:
        detail = response.text[:200]
        # `invalid_client` here means the id and secret do not match each other. It happened
        # once because an inline "# comment" in the .env was read as part of the secret — a
        # .env value runs to end-of-line — and it looks identical to having no access at all.
        raise AdsError(
            f"Amazon rejected the advertising credentials ({detail}). If this says "
            "'invalid_client', check AMAZON_CLIENT_SECRET has no trailing comment or spaces.",
            status=response.status_code,
        )

    payload = response.json()
    _token.value = payload["access_token"]
    _token.expires_at = time.time() + float(payload.get("expires_in") or 3600)
    return _token.value


def _headers(token: str) -> dict:
    """The three headers every ads call needs.

    `-Scope` is the advertising PROFILE id, not a marketplace id. A wrong value here returns
    another account's numbers rather than an error, which is why `ads_configured` treats the
    profile id as mandatory instead of defaulting it.
    """
    settings = get_settings()
    return {
        "Authorization": f"Bearer {token}",
        "Amazon-Advertising-API-ClientId": settings.ads_client_id,
        "Amazon-Advertising-API-Scope": settings.ads_profile_id,
    }


def build_report_request(start: str, end: str) -> dict:
    """The report body Amazon accepts. ONE function, so the shape is stated once.

    Every element below was arrived at by having a wrong version rejected. See the module
    docstring for the three traps; the tests pin all of them, because this body is only
    exercised for real when someone presses Refresh.
    """
    return {
        "name": f"portfolio acos {start}..{end}",
        "startDate": start,
        "endDate": end,
        "configuration": {
            "adProduct": AD_PRODUCT,
            # "advertiser", NOT "campaign": this report is per advertised product.
            "groupBy": ["advertiser"],
            "columns": list(REPORT_COLUMNS),
            "reportTypeId": REPORT_TYPE_ID,
            # SUMMARY collapses the window into one figure per product. DAILY would multiply
            # the rows by the window length and answer nothing the dashboard asks.
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        },
    }


def aggregate(rows: list[dict]) -> list[dict]:
    """Collapse the raw report to ONE row per (asin, sku).

    **The report arrives split by campaign even though `groupBy` is `advertiser`.** Measured:
    1,697 rows for 213 pairs, up to 13 rows for a single pair. Aggregating here rather than in
    the repository or the template means every consumer sees one grain, and a total computed
    from these rows cannot disagree with the rows themselves.

    Sums rather than averages, including for clicks and impressions: they are counts, and this
    is the same set of ads viewed as one product.
    """
    merged: dict[tuple, dict] = defaultdict(lambda: {
        "cost": 0.0, "attributed_sales": 0.0, "purchases": 0, "clicks": 0, "impressions": 0,
    })
    for row in rows:
        asin = (row.get("advertisedAsin") or "").strip().upper()
        sku = (row.get("advertisedSku") or "").strip()
        if not asin:
            continue
        acc = merged[(asin, sku)]
        acc["cost"] += float(row.get("cost") or 0)
        acc["attributed_sales"] += float(row.get("attributedSalesSameSku14d") or 0)
        acc["purchases"] += int(row.get("purchasesSameSku14d") or 0)
        acc["clicks"] += int(row.get("clicks") or 0)
        acc["impressions"] += int(row.get("impressions") or 0)

    return [
        {
            "child_asin": asin,
            "seller_sku": sku,
            "cost": round(acc["cost"], 2),
            "attributed_sales": round(acc["attributed_sales"], 2),
            "purchases": acc["purchases"],
            "clicks": acc["clicks"],
            "impressions": acc["impressions"],
        }
        for (asin, sku), acc in merged.items()
    ]


async def fetch_acos(
    start: str,
    end: str,
    *,
    sleep=asyncio.sleep,
    on_progress=None,
) -> list[dict]:
    """Run one advertised-product report to completion. Returns aggregated per-SKU rows.

    Create, poll, download, aggregate. `sleep` is injectable so a test drives the whole sequence
    without spending twenty minutes; `on_progress(done, total)` moves the bar during the long
    poll.

    Raises `AdsNotConfigured` when there are no credentials — the caller is expected to treat
    that as "skip ACOS", not as a failure. Raises `AdsError` on a FAILURE status or a timeout,
    **never returning an empty list to mean failure**: the screen has to tell "nothing was
    advertised" apart from "we could not ask".
    """
    settings = get_settings()
    if not settings.ads_configured:
        raise AdsNotConfigured()

    async with httpx.AsyncClient(timeout=settings.ads_timeout) as client:
        token = await _access_token(client)
        head = _headers(token)

        create = await client.post(
            settings.ads_endpoint + REPORT_PATH,
            content=json.dumps(build_report_request(start, end)),
            headers={**head, "Content-Type": CREATE_CONTENT_TYPE},
        )
        if create.status_code >= 400:
            # Amazon's validation messages here are unusually good — they name the allowed
            # values — so they are surfaced verbatim rather than replaced with a generic error.
            raise AdsError(
                f"Amazon refused the ad report request: {create.text[:300]}",
                status=create.status_code,
            )
        report_id = (create.json() or {}).get("reportId")
        if not report_id:
            raise AdsError(f"Amazon accepted the report but returned no reportId: {create.text[:200]}")
        logger.info("portfolio: ad report %s requested for %s..%s", report_id, start, end)

        url = None
        for attempt in range(POLL_MAX):
            await sleep(POLL_INTERVAL)
            status = await client.get(
                f"{settings.ads_endpoint}{REPORT_PATH}/{report_id}", headers=head
            )
            if status.status_code >= 400:
                raise AdsError(
                    f"Polling the ad report failed: {status.text[:200]}",
                    status=status.status_code,
                )
            state = status.json() or {}
            if on_progress:
                on_progress(attempt + 1, POLL_MAX)
            processing = state.get("status")
            if processing not in _TERMINAL:
                continue
            if processing != "COMPLETED":
                raise AdsError(
                    f"Amazon could not produce the ad report ({processing})"
                    + (f": {state.get('failureReason')}" if state.get("failureReason") else "")
                )
            url = state.get("url")
            # COMPLETED with no url means the window matched no advertising at all.
            if not url:
                logger.info("portfolio: ad report %s completed with no data", report_id)
                return []
            break
        else:
            raise AdsError(
                f"The ad report was still generating after "
                f"{int(POLL_MAX * POLL_INTERVAL / 60)} minutes. It may finish later — press "
                "Refresh to check again."
            )

        # The download url is pre-signed, so it is fetched WITHOUT the ads headers. Sending the
        # bearer token to S3 would leak it to a different host.
        download = await client.get(url)
        if download.status_code >= 400:
            raise AdsError(
                f"Downloading the ad report failed ({download.status_code}).",
                status=download.status_code,
            )

    payload = download.content
    # GZIP_JSON was requested, but the header is honoured rather than assumed: the economics
    # document arrived UNCOMPRESSED despite documentation to the contrary, so trusting the
    # request here would be the same mistake in a second place.
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    try:
        raw_rows = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdsError(f"The ad report was not readable JSON: {exc}")
    if not isinstance(raw_rows, list):
        raise AdsError("The ad report was not a list of rows.")

    rows = aggregate(raw_rows)
    logger.info(
        "portfolio: ad report %s -> %d raw row(s) aggregated to %d (asin, sku) pair(s)",
        report_id, len(raw_rows), len(rows),
    )
    return rows
