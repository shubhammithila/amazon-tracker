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


#: The widest window the dashboard offers. Amazon allows two years, but the date picker caps at
#: 90 days because that is what was asked for and because a wider window makes the per-SKU query
#: large enough to matter on a 951 MB box.
MAX_WINDOW_DAYS = 90


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


def validate_window(start: str, end: str, today: date | None = None) -> tuple[str, str]:
    """Check an explicit window, or raise ``ValueError`` with a message fit for the screen.

    Three rules, each protecting against a real mistake rather than a hypothetical one:

    * **start <= end**, because a reversed range returns nothing and looks like an empty account.
    * **end <= yesterday**, for the reason `window_for` documents: today is partial, so a range
      including it shows a punishing TACOS every morning that settles by evening.
    * **at most 90 days**, the cap the dashboard offers.
    """
    today = today or date.today()
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
    except (TypeError, ValueError):
        raise ValueError("Dates must be YYYY-MM-DD.")

    if first > last:
        raise ValueError("The start date is after the end date.")
    yesterday = today - timedelta(days=1)
    if last > yesterday:
        raise ValueError(
            f"The window must end on or before {yesterday.isoformat()} — today's figures are "
            "still settling, and an ad charge lands hours after the sale it belongs to."
        )
    span = (last - first).days + 1
    if span > MAX_WINDOW_DAYS:
        raise ValueError(f"The window is {span} days; the most this tab reads is {MAX_WINDOW_DAYS}.")
    return first.isoformat(), last.isoformat()


def build_query(start: str, end: str, marketplace_id: str, *, by_sku: bool = False) -> str:
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

    ``by_sku=True`` switches the product grain to ``MSKU``, which is how the merchant/FBA split
    is obtained: measured, 186 of 267 child ASINs sell under both a merchant SKU and an
    identically-named "… FBA" one, and only this grain separates them. **The MSKU rows are never
    a source of totals** — they lose a little to rows Amazon cannot attribute to one SKU, so the
    CHILD_ASIN grain stays authoritative. See `repository.load_snapshot`.
    """
    return """
query PortfolioEconomics {
  analytics_economics_2024_03_15 {
    economics(
      startDate: "%(start)s"
      endDate: "%(end)s"
      marketplaceIds: ["%(marketplace)s"]
      aggregateBy: { date: RANGE, productId: %(grain)s }
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
""" % {"start": start, "end": end, "marketplace": marketplace_id,
       "grain": "MSKU" if by_sku else "CHILD_ASIN"}


async def _run_query(
    query: str, *, label: str, sleep, on_progress=None, phase: str = "econ_poll"
) -> list[dict]:
    """Submit one Data Kiosk query, poll it, download it. Returns its rows.

    Extracted so the ASIN-level and per-SKU fetches share ONE submit/poll/download
    implementation — two copies is two chances for one of them to mishandle a FATAL status.
    """
    created = await spapi._post(QUERY_PATH, {"query": query})
    query_id = created.get("queryId")
    if not query_id:
        raise SpApiError(f"Data Kiosk accepted the query but returned no queryId: {created}")
    logger.info("portfolio: %s query %s submitted", label, query_id)

    for attempt in range(POLL_MAX):
        await sleep(POLL_INTERVAL)
        state = await spapi._get(f"{QUERY_PATH}/{query_id}")
        processing = state.get("processingStatus")
        if on_progress:
            on_progress(phase, attempt + 1, POLL_MAX)
        if processing not in _TERMINAL:
            continue
        if processing != "DONE":
            # An errorDocumentId exists on failure, but it is a separate download and the
            # status alone is enough for the screen to say "it failed, try again".
            raise SpApiError(
                f"Amazon could not produce the {label} data ({processing}). "
                "Press Refresh to try again."
            )
        document_id = state.get("dataDocumentId")
        # DONE with no document is legitimate and means the query matched nothing.
        if not document_id:
            logger.info("portfolio: %s query %s returned no document", label, query_id)
            return []
        return await _download(document_id)

    raise SpApiError(
        f"The {label} query was still running after "
        f"{int(POLL_MAX * POLL_INTERVAL / 60)} minutes. It may finish later — press "
        "Refresh to check again."
    )


async def fetch_economics(
    *,
    days: int = WINDOW_DAYS,
    start: str | None = None,
    end: str | None = None,
    today: date | None = None,
    sleep=asyncio.sleep,
    on_progress=None,
) -> tuple[list[dict], list[dict], str, str]:
    """Both economics grains for one window. Returns ``(asin_rows, sku_rows, start, end)``.

    **Two queries, and the second one is optional detail.** The ASIN-level rows are the
    dashboard: sales, fees, ad charge, net proceeds per pack size, and the authoritative totals.
    The MSKU rows exist only to show the merchant/FBA split on expand — measured, they sum to
    slightly less than the ASIN figures because Amazon cannot attribute every row to one SKU, so
    they must never become a source of totals.

    A per-SKU failure is swallowed: the split is a nicety, and losing the whole refresh over it
    would trade the margins for a detail. The ASIN query's failure is raised, because without it
    there is no dashboard.

    ``start``/``end`` request an explicit window (validated); ``days`` requests the last N days
    ending yesterday. ``sleep`` is injectable so tests never spend minutes.

    Raises ``SpApiError`` on a FATAL query, a poll timeout, or a transport failure. **Never
    returns an empty list to mean failure** — the screen has to distinguish "you sell nothing"
    from "we could not ask Amazon", and only an exception does that.
    """
    settings = get_settings()
    if start and end:
        start, end = validate_window(start, end, today)
    else:
        start, end = window_for(today or date.today(), days)

    if on_progress:
        on_progress("econ_submit", 0, 1)
    asin_rows = await _run_query(
        build_query(start, end, settings.sp_api_marketplace_id),
        label="economics", sleep=sleep, on_progress=on_progress, phase="econ_poll",
    )
    if on_progress:
        on_progress("econ_download", 1, 1)

    sku_rows: list[dict] = []
    try:
        sku_rows = await _run_query(
            build_query(start, end, settings.sp_api_marketplace_id, by_sku=True),
            label="economics by SKU", sleep=sleep, on_progress=on_progress, phase="econ_poll",
        )
    except SpApiError as exc:
        # Logged, not raised: the split is presentation. The margins are already in hand and
        # losing them over a missing breakdown would be the wrong trade.
        logger.warning("portfolio: the per-SKU economics query failed (%s); split unavailable", exc)

    logger.info(
        "portfolio: economics returned %d ASIN row(s) and %d SKU row(s) for %s..%s",
        len(asin_rows), len(sku_rows), start, end,
    )
    return asin_rows, sku_rows, start, end


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
