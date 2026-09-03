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

Four request-shape traps, each of which produced a real 400:

* **`groupBy` must be `["advertiser"]`.** `advertiser` is not documented as obvious; the API's own
  rejection lists the alternatives (`campaign`, `adGroup`, `campaignPlacement`).
* **`date` is not a legal column under `timeUnit: SUMMARY`.** The window comes from
  startDate/endDate instead. Including it fails the whole request.
* **`Content-Type: application/vnd.createasyncreportrequest.v3+json`**, not `application/json`.
* **One report covers at most 31 DAYS**, so `fetch_acos` splits a longer window into several
  reports and sums them — see `MAX_REPORT_DAYS`. This one reached production, and the reason it got
  that far is worth keeping: the economics API has no equivalent limit, so a 90-day refresh stored
  every margin and failed on ACOS alone.

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
from datetime import date, timedelta

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

#: **Amazon caps ONE report at 31 days**, so a longer window is fetched as several reports and
#: summed. Measured by having a 90-day request rejected on production:
#:
#:     {"code":"400","detail":"startDate to endDate range (89 days)
#:                             must not exceed maximum range (31 days)"}
#:
#: The economics API has no such limit and happily answers 90 days, which is why the 60d and 90d
#: buttons looked fine: the margins loaded and only ACOS was missing. Both halves of the window
#: therefore need their own cap, and they are NOT the same number — `economics.MAX_WINDOW_DAYS`
#: is 90 because that is what the dashboard offers, this is 31 because Amazon says so.
MAX_REPORT_DAYS = 31

#: Seconds between polls. Amazon's report generation is measured in MINUTES, so polling faster
#: only wastes calls. Deliberately far slower than the economics poller for that reason.
POLL_INTERVAL = 20.0

#: Polls before giving up: 135 x 20s = **45 minutes**.
#:
#: **This was 60 (20 minutes) and it timed out on production.** Measured from Amazon's own
#: timestamps on a completed report: `createdAt 09:55:46Z` to `updatedAt 10:14:15Z` — **18.5
#: minutes** for a SIX-day window. The 20-minute ceiling therefore had about 90 seconds of margin
#: on the smallest report this app asks for, and the 30-day window it actually requests is larger
#: still: the first production run hit the cap at 100% with the margins stored and ACOS missing.
#:
#: 45 minutes is a CEILING, not an expectation, and the cost of being generous is nothing: this
#: runs as a background task, the bar keeps moving, and the margins are already committed before
#: this phase starts. The cost of being tight is a refresh that reports failure on a report Amazon
#: would have delivered.
POLL_MAX = 135

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


async def poll_get(
    client: httpx.AsyncClient, url: str, *, force_refresh_attempts: int = 3,
) -> httpx.Response:
    """GET an ads URL with a token refreshed as needed, retrying a 401 with a fresh one.

    **The headers are rebuilt per call rather than bound once by the caller, and that is the whole
    point of this function.** `POLL_MAX * POLL_INTERVAL` is 45 minutes of polling, while an LWA
    access token lives 3600s — and by the time the Sponsored Brands report starts polling, the
    preceding Sponsored Products reports have already spent ~17 of those minutes. Measured on
    production over a week of failures: exactly ONE token mint per nightly run
    (`journalctl | grep -c 'o2/token'` = 1), a real SB report needing ~9 minutes of polling, and
    the token expiring underneath it — so every night returned
    `401 {"message":"Unauthorized exception while handling 3P Request: Invalid token"}` and the
    poll loop discarded a report Amazon had already produced. Sponsored Brands went stale, no
    window could be summed, and no bid rule could be previewed at all.

    **The tell that made it diagnosable:** one MANUAL 7-day refresh succeeded mid-week. Short runs
    leave the token nearly full when SB starts; the nightly 60-day run burns SP first, so it could
    never succeed. "Fails nightly, works when I press the button" is a token-lifetime signature,
    not the rate limit this module's docstring describes for report CREATION.

    **Calling `_access_token` per poll is cheap, not a stampede**: it returns the cached token
    until `expires_at - _TOKEN_SAFETY_MARGIN`, so only a genuinely near-expiry token costs an LWA
    round trip. A test pins that five polls mint once.

    **A 401 is retried; every other status is returned untouched.** The caller owns its own error
    prose and its own reading of `FAILURE`/`CANCELLED`/no-url — interpreting report state here
    would merge two callers' messages into one. `force_refresh_attempts` bounds the retry so a
    genuinely revoked refresh token surfaces as a 401 the caller can report, rather than looping
    for the full 45 minutes.

    **NOT for the report download.** That url is pre-signed and must be fetched with NO ads
    headers, or the bearer token leaks to S3. See both callers' download step.
    """
    response = None
    for attempt in range(force_refresh_attempts + 1):
        token = await _access_token(client)
        response = await client.get(url, headers=_headers(token))
        if response.status_code != 401:
            return response
        # The token died mid-poll. Drop it so the next `_access_token` mints a fresh one, and try
        # the SAME poll again — Amazon's report keeps generating regardless of our auth.
        logger.info(
            "ads: poll returned 401, re-minting the access token (attempt %d of %d)",
            attempt + 1, force_refresh_attempts,
        )
        _token.value = ""
        _token.expires_at = 0.0
    return response


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


def split_window(start: str, end: str, *, max_days: int = MAX_REPORT_DAYS) -> list[tuple[str, str]]:
    """Break a window into consecutive chunks of at most `max_days`, inclusive of both ends.

    Amazon caps one report at 31 days, so 60- and 90-day windows are fetched as 2 and 3 reports
    and summed. Pure and separately tested, because an off-by-one here either drops a day of spend
    or double-counts one, and both are invisible in a total that still looks plausible.

    Chunks are **contiguous and non-overlapping**: the next chunk starts the day after the previous
    one ends. A one-day window returns one chunk of that single day.

    > **Summing chunks is sound for COST but slightly conservative for attributed SALES**, and that
    > is a real property of the 14-day attribution column rather than of this function. A click on
    > 30 Jul can be credited a sale up to 13 Aug; asked as one 90-day report Amazon counts it,
    > asked as three 31-day reports the chunk ending 31 Jul cannot see it and the chunk starting
    > 1 Aug does not own the click. So a chunked ACOS can read a little HIGHER than a single-report
    > ACOS (same numerator, marginally smaller denominator).
    >
    > Accepted deliberately, and it is the safe direction: ACOS is a "do the ads pay for
    > themselves" check, and erring pessimistic cannot talk the owner into more spend. The
    > alternative — overlapping the chunks by 14 days to catch those sales — would double-count
    > cost in the overlap, which distorts the numerator, and the numerator is the number that
    > reconciles against the economics feed to 0.2%. Losing a little attribution at 2 internal
    > boundaries is a smaller error than inflating spend by 14 days out of every 31.
    >
    > The 7- and 30-day presets — the ones actually used for decisions — are single reports and
    > are unaffected.
    """
    if max_days < 1:
        raise ValueError("max_days must be at least 1")
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if first > last:
        raise ValueError(f"start {start} is after end {end}")

    chunks: list[tuple[str, str]] = []
    cursor = first
    while cursor <= last:
        stop = min(cursor + timedelta(days=max_days - 1), last)
        chunks.append((cursor.isoformat(), stop.isoformat()))
        cursor = stop + timedelta(days=1)
    return chunks


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
    """Fetch ad cost and attributed sales for a window. Returns aggregated per-SKU rows.

    **A window longer than 31 days becomes several reports, summed** — Amazon's cap, see
    `MAX_REPORT_DAYS`. One chunk is the common case (the 7d and 30d presets); 60d is two and 90d
    is three. `aggregate` sums by `(asin, sku)` and is therefore reused unchanged to merge the
    chunks, so a multi-chunk result has exactly the same shape and grain as a single one.

    Raises `AdsNotConfigured` when there are no credentials — the caller is expected to treat
    that as "skip ACOS", not as a failure. Raises `AdsError` on a FAILURE status or a timeout,
    **never returning an empty list to mean failure**: the screen has to tell "nothing was
    advertised" apart from "we could not ask".

    **A failed chunk fails the whole window** rather than returning a partial sum. Half a window's
    spend against a full window's sales is not a smaller ACOS, it is a wrong one — and it would
    look entirely plausible on screen.
    """
    settings = get_settings()
    if not settings.ads_configured:
        raise AdsNotConfigured()

    chunks = split_window(start, end)
    raw: list[dict] = []

    async with httpx.AsyncClient(timeout=settings.ads_timeout) as client:
        for index, (chunk_start, chunk_end) in enumerate(chunks):
            if len(chunks) > 1:
                logger.info(
                    "portfolio: ad report chunk %d/%d (%s..%s) — Amazon caps a report at %d days",
                    index + 1, len(chunks), chunk_start, chunk_end, MAX_REPORT_DAYS,
                )

            # The bar is shared across the chunks, so three reports still read 0->100% once
            # rather than snapping back to zero twice.
            def chunk_progress(done, total, _i=index):
                if on_progress:
                    on_progress(_i * total + done, total * len(chunks))

            raw.extend(await _one_report(
                client, chunk_start, chunk_end, sleep=sleep, on_progress=chunk_progress,
            ))

    rows = aggregate(raw)
    logger.info(
        "portfolio: %d ad report(s) for %s..%s -> %d raw row(s) aggregated to %d (asin, sku) pair(s)",
        len(chunks), start, end, len(raw), len(rows),
    )
    return rows


async def _one_report(
    client: httpx.AsyncClient,
    start: str,
    end: str,
    *,
    sleep,
    on_progress=None,
) -> list[dict]:
    """Create, poll and download ONE report. Returns its RAW rows, un-aggregated.

    Raw rather than aggregated so `fetch_acos` can sum several chunks through the single
    `aggregate` path — aggregating per chunk and then merging would be two summing rules for one
    grain, which is how a total starts disagreeing with its rows.
    """
    settings = get_settings()
    token = await _access_token(client)
    head = _headers(token)

    create = await client.post(
        settings.ads_endpoint + REPORT_PATH,
        content=json.dumps(build_report_request(start, end)),
        headers={**head, "Content-Type": CREATE_CONTENT_TYPE},
    )
    if create.status_code >= 400:
        # Amazon's validation messages here are unusually good — they name the allowed values,
        # and the 31-day cap was discovered from one — so they are surfaced verbatim rather than
        # replaced with a generic error.
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
        # Same reason as app/ads/reports.py — see poll_get's own docstring. This path had the
        # IDENTICAL defect and had simply never been bitten, because the economics reports finish
        # in ~30s and the token never got the chance to expire underneath them.
        status = await poll_get(client, f"{settings.ads_endpoint}{REPORT_PATH}/{report_id}")
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
        # COMPLETED with no url means this window matched no advertising at all. An empty list
        # is correct here and is NOT how failure is reported — every failure raises.
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

    logger.info("portfolio: ad report %s -> %d raw row(s)", report_id, len(raw_rows))
    return raw_rows
