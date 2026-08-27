"""The only caller of Amazon's Data Kiosk — the Seller Central Economics figures.

Auth, error typing and connection reuse come from ``app.shipment.spapi`` rather than being
re-implemented, the same way ``app.orders.spapi_orders`` does it: one token cache and one
error type across the app.

**This API is not in the SP-API model list, which is why it looks as though it does not
exist.** Two independent research passes concluded there was no economics endpoint, because
they enumerated ``selling-partner-api-models/models`` — where every other API lives. The
economics data is a **GraphQL schema** under ``schemas/data-kiosk``:
``analytics_economics_2024_03_15``. It was found by listing that directory instead, and then
confirmed by running it against the live account.

**Verified against the live amazon.in account on 2026-08-27**, not read from documentation:

    27 Jul – 26 Aug, CHILD_ASIN x RANGE
    267 child ASINs across 90 parent ASINs
    ordered sales   Rs 51,25,313
    ad spend        Rs 16,99,835      (SponsoredProductFee)
    fees            Rs 16,66,133      (8 distinct fee types)
    net proceeds    Rs 14,81,358
    net units       16,733

Those figures reconcile with the manually-downloaded Business Report the first analysis used,
which is what makes this a drop-in replacement for that upload.

**Ad spend arrives here, so no Advertising API is needed.** That was the significant risk in
this feature — a separate developer application, a separate LWA client and an approval of
unknown duration. It is unnecessary: Amazon reports the ad *charge* in the economics feed
because it is a charge against the account, not advertising-platform data. What is NOT here is
ad-attributed sales, which is why this app reports TACOS (spend / total sales) and never ACOS.

Three traps, each of which cost real debugging time:

* **``ads.charge`` is an ``AggregatedDetail`` directly.** Writing
  ``charge { aggregatedDetail { ... } }`` — by analogy with ``fees.charges`` — is rejected
  with *"The provided query is invalid"*.
* **A bad field selection returns "We encountered an internal error"**, not a syntax message.
  The first attempt included a nonexistent selection on ``cost`` and produced that opaque 500,
  which reads exactly like a permissions or outage problem and is not one. This is why the
  query is built by ONE function and pinned by a test.
* **``totalAmount``, not ``amount``.** ``amount`` is the rate-card figure;
  ``totalAmount`` is ``amount - promotionAmount + taxAmount``, which is what the account was
  actually charged.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
from datetime import date, timedelta

import httpx

from app.config import get_settings
from app.shipment import spapi
from app.shipment.spapi import SpApiError

logger = logging.getLogger(__name__)

#: Data Kiosk is asynchronous: submit a query, poll until it is DONE, then download a
#: document. There is no synchronous form.
QUERY_PATH = "/dataKiosk/2023-11-15/queries"
DOCUMENT_PATH = "/dataKiosk/2023-11-15/documents/{document_id}"

#: Seconds between polls. Measured on the live account: a 30-day CHILD_ASIN query reached
#: DONE in roughly 60-120 seconds. Amazon publishes no progress, so this is a fixed interval
#: rather than a backoff — a longer wait would only make a fast query look slow.
POLL_INTERVAL = 12.0

#: Polls before giving up: 40 x 12s = 8 minutes. A ceiling rather than an expectation. Giving
#: up is reported as an error the screen can show, never as an empty result — "no products"
#: and "we could not ask" must not render the same way.
POLL_MAX = 40

#: Days of history per refresh. 30 matches the Business Report window the first analysis used,
#: so the numbers are comparable with the decisions already taken. Amazon allows two years.
WINDOW_DAYS = 30

#: Amazon's own name for the ad charge on this account. Kept as a constant because the
#: dashboard shows ad spend as one number and a second ad type (Sponsored Brands, Sponsored
#: Display) must be ADDED rather than silently ignored — `total_ads` sums whatever arrives.
SPONSORED_PRODUCTS = "SponsoredProductFee"

#: Terminal states of a Data Kiosk query.
_TERMINAL = ("DONE", "FATAL", "CANCELLED")


def window_for(today: date, days: int = WINDOW_DAYS) -> tuple[str, str]:
    """The ``(start, end)`` ISO dates for a refresh ending YESTERDAY.

    **Not today.** The economics data set is refreshed daily, so today is partial by
    construction: an ad charge lands hours after the sale it belongs to. Including today would
    show a fresh product at a punishing TACOS every morning and settle by evening — a number
    that moves on its own invites a decision that the data does not support.
    """
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def build_query(start: str, end: str, marketplace_id: str) -> str:
    """The GraphQL document for one economics fetch.

    ONE function, so the screen, the tests and any future caller ask Amazon the same
    question. Every field below is verified against the live schema
    (``schemas/data-kiosk/analytics_economics_2024_03_15.graphql``) and a live response.

    **``RANGE`` and ``CHILD_ASIN``, deliberately.** ``RANGE`` collapses the whole window into
    one row per product, which is the question this dashboard asks ("how did this SKU do over
    the last 30 days"). ``DAY`` would return the same 267 products x 30 days = ~8,000 rows to
    answer nothing extra. ``CHILD_ASIN`` is the pack size — the level at which a kill decision
    is actually made — and each row carries its ``parentAsin``, so the parent rollup is done
    locally rather than by a second query that could disagree with the first.

    ``msku`` is requested but comes back null under CHILD_ASIN aggregation (the schema says so:
    it is only populated for FNSKU/MSKU aggregation). It is left in because it costs nothing
    and documents that the field was considered.
    """
    return """
query PortfolioEconomics {
  analytics_economics_2024_03_15 {
    economics(
      startDate: "%(start)s"
      endDate: "%(end)s"
      marketplaceIds: ["%(marketplace)s"]
      aggregateBy: { date: RANGE, productId: CHILD_ASIN }
    ) {
      startDate
      endDate
      parentAsin
      childAsin
      msku
      sales {
        orderedProductSales { amount currencyCode }
        netProductSales { amount currencyCode }
        refundedProductSales { amount currencyCode }
        averageSellingPrice { amount currencyCode }
        unitsOrdered
        unitsRefunded
        netUnitsSold
      }
      fees {
        feeTypeName
        charges { aggregatedDetail { totalAmount { amount currencyCode } } }
      }
      ads {
        adTypeName
        charge { totalAmount { amount currencyCode } }
      }
      netProceeds {
        total { amount currencyCode }
        perUnit { amount currencyCode }
      }
    }
  }
}
""" % {"start": start, "end": end, "marketplace": marketplace_id}


async def fetch_economics(
    *,
    days: int = WINDOW_DAYS,
    today: date | None = None,
    sleep=asyncio.sleep,
    on_progress=None,
) -> tuple[list[dict], str, str]:
    """Run one economics query to completion. Returns ``(rows, start, end)``.

    Submit, poll, download, parse. ``sleep`` is injectable so a test can exercise the whole
    sequence without spending eight minutes, and ``on_progress(phase, done, total)`` drives
    the bar in ``refresh``.

    Raises ``SpApiError`` on a FATAL query, on a poll timeout, or on a transport failure.
    **Never returns an empty list to mean failure** — the screen has to distinguish "you sell
    nothing" from "we could not ask Amazon", and only an exception does that.
    """
    settings = get_settings()
    start, end = window_for(today or date.today(), days)
    query = build_query(start, end, settings.sp_api_marketplace_id)

    if on_progress:
        on_progress("submit", 0, 1)
    created = await spapi._post(QUERY_PATH, {"query": query})
    query_id = created.get("queryId")
    if not query_id:
        raise SpApiError(f"Data Kiosk accepted the query but returned no queryId: {created}")
    logger.info("portfolio: economics query %s submitted for %s..%s", query_id, start, end)
    if on_progress:
        on_progress("submit", 1, 1)

    document_id = None
    for attempt in range(POLL_MAX):
        await sleep(POLL_INTERVAL)
        state = await spapi._get(f"{QUERY_PATH}/{query_id}")
        processing = state.get("processingStatus")
        if on_progress:
            on_progress("poll", attempt + 1, POLL_MAX)
        if processing not in _TERMINAL:
            continue
        if processing != "DONE":
            # An errorDocumentId exists on failure, but it is a separate download and the
            # status alone is enough for the screen to say "it failed, try again".
            raise SpApiError(
                f"Amazon could not produce the economics data ({processing}). "
                "Press Refresh to try again."
            )
        document_id = state.get("dataDocumentId")
        # DONE with no document is legitimate and means the query matched nothing.
        if not document_id:
            logger.info("portfolio: economics query %s returned no document", query_id)
            return [], start, end
        break
    else:
        raise SpApiError(
            f"The economics query was still running after "
            f"{int(POLL_MAX * POLL_INTERVAL / 60)} minutes. It may finish later — press "
            "Refresh to check again."
        )

    if on_progress:
        on_progress("download", 0, 1)
    rows = await _download(document_id)
    if on_progress:
        on_progress("download", 1, 1)
    logger.info("portfolio: economics returned %d row(s) for %s..%s", len(rows), start, end)
    return rows, start, end


async def _download(document_id: str) -> list[dict]:
    """Fetch and parse one Data Kiosk document into a list of dicts.

    **The body is JSON LINES, not a JSON array** — one object per line, which is why it is
    parsed line by line rather than with a single ``json.loads``.

    ``compressionAlgorithm`` is honoured rather than assumed: the live 267-row response came
    back UNCOMPRESSED, while Amazon documents GZIP for larger ones. Deciding by the field
    means a busier month cannot break this with a gzip header parsed as text.

    The document URL is pre-signed, so it is fetched with a plain client and no SP-API token —
    sending the token to S3 would leak it to a different host.
    """
    document = await spapi._get(DOCUMENT_PATH.format(document_id=document_id))
    url = document.get("documentUrl")
    if not url:
        raise SpApiError("Amazon returned an economics document with no download URL.")

    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.sp_api_timeout) as client:
        response = await client.get(url)
    if response.status_code >= 400:
        raise SpApiError(
            f"Downloading the economics document failed ({response.status_code}).",
            status=response.status_code,
        )

    payload = response.content
    if (document.get("compressionAlgorithm") or "").upper() == "GZIP":
        payload = gzip.decompress(payload)

    rows = []
    for line in payload.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # One malformed line must not lose the other 266. Logged rather than raised,
            # because a partial portfolio is still actionable and a hard failure here would
            # blank the screen over a single row.
            logger.warning("portfolio: skipped a malformed economics row")
    return rows
