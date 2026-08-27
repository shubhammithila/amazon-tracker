"""The Data Kiosk query and its submit/poll/download sequence.

Every fact asserted here was measured against the live amazon.in account on 2026-08-27, because
the documentation for this API is behind an auth wall and the two things that actually broke were
not guessable:

* ``ads.charge`` is an ``AggregatedDetail`` DIRECTLY. Writing
  ``charge { aggregatedDetail { totalAmount } }`` — the shape ``fees.charges`` uses — is rejected
  with *"The provided query is invalid"*.
* A nonexistent field selection returns **"We encountered an internal error"**, not a syntax
  error. That reads exactly like an outage or a permissions problem and is neither, which cost
  the most debugging time in this feature.

Both are pinned below, since a plausible-looking edit to the query would otherwise fail only in
production, minutes after someone pressed Refresh.
"""
import gzip
import json
from datetime import date
from pathlib import Path

import pytest

from app.portfolio import economics
from app.shipment.spapi import SpApiError

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"
MARKETPLACE = "A21TJRUUN4KGV"        # amazon.in, the live account's marketplace


async def _no_sleep(seconds):
    return None


# ─── The query ───────────────────────────────────────────────────────────────


def test_the_query_asks_for_a_range_per_child_asin():
    """RANGE and CHILD_ASIN, and both choices are load-bearing.

    RANGE collapses the window into one row per product, which is the question the dashboard
    asks. DAY would return the same 267 products x 30 days = ~8,000 rows and answer nothing
    extra — measured, not estimated.

    CHILD_ASIN is the pack size, which is where a kill decision is actually taken. Each row
    carries its parentAsin, so the parent rollup happens locally rather than in a second query
    that could disagree with the first.
    """
    query = economics.build_query("2026-07-28", "2026-08-26", MARKETPLACE)
    assert "date: RANGE" in query
    assert "productId: CHILD_ASIN" in query
    assert "parentAsin" in query and "childAsin" in query
    assert MARKETPLACE in query
    assert "2026-07-28" in query and "2026-08-26" in query


def test_the_query_asks_for_ad_spend_in_the_shape_amazon_accepts():
    """**The trap that cost the most time.**

    `ads.charge` is an `AggregatedDetail` directly, so `charge { totalAmount }` is correct and
    `charge { aggregatedDetail { totalAmount } }` is rejected as an invalid query. The nesting
    differs from `fees.charges`, which DOES have the intervening `aggregatedDetail` — so the
    consistent-looking version is the broken one.

    Asserted on the string because the failure is a 400 from Amazon at refresh time, and the
    error message does not name the field.
    """
    query = economics.build_query("2026-07-28", "2026-08-26", MARKETPLACE)
    assert "charge { totalAmount { amount currencyCode } }" in query, (
        "the ad charge selection is not the shape Amazon accepts"
    )
    assert "charge { aggregatedDetail" not in query, (
        "ads.charge has an aggregatedDetail wrapper, which Amazon rejects as an invalid query"
    )
    # And fees keep theirs, which is the asymmetry worth remembering.
    assert "charges { aggregatedDetail { totalAmount" in query


def test_the_query_asks_for_total_amount_rather_than_amount():
    """`totalAmount` is what the account was charged; `amount` is the rate-card figure.

    `totalAmount = amount - promotionAmount + taxAmount`. Using `amount` would understate every
    fee by its tax and overstate it by any promotion — a wrong number on a margin the owner is
    about to act on.
    """
    query = economics.build_query("2026-07-28", "2026-08-26", MARKETPLACE)
    assert "totalAmount" in query


def test_the_window_ends_yesterday_not_today():
    """Today's data is partial by construction, so it is excluded.

    The economics data set refreshes daily and an ad charge lands hours after the sale it belongs
    to. Including today would show every product at a punishing TACOS each morning that settled
    by evening — a number that moves on its own invites a decision the data does not support.
    """
    start, end = economics.window_for(date(2026, 8, 27), days=30)
    assert end == "2026-08-26", "the window includes today, whose data is incomplete"
    assert start == "2026-07-28"
    # 30 days inclusive of both ends.
    assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 29


# ─── Submit, poll, download ──────────────────────────────────────────────────


class _Kiosk:
    """A fake Data Kiosk that answers in Amazon's real response shapes."""

    def __init__(self, *, statuses, rows=None, document=True, compressed=False):
        self.statuses = list(statuses)
        self.rows = rows if rows is not None else []
        self.document = document
        self.compressed = compressed
        self.posted = []
        self.polls = 0

    async def post(self, path, body=None, client=None):
        self.posted.append((path, body))
        return {"queryId": "127782020692"}

    async def get(self, path, params=None, client=None):
        if "/documents/" in path:
            return {"documentUrl": "https://example.invalid/doc", "documentId": "d1",
                    **({"compressionAlgorithm": "GZIP"} if self.compressed else {})}
        self.polls += 1
        status = self.statuses[min(self.polls - 1, len(self.statuses) - 1)]
        payload = {"processingStatus": status}
        if status == "DONE" and self.document:
            payload["dataDocumentId"] = "amzn1.tortuga.4.eu.abc.DEF"
        return payload

    def body(self):
        text = "\n".join(json.dumps(row) for row in self.rows)
        return gzip.compress(text.encode()) if self.compressed else text.encode()


def _patch(monkeypatch, kiosk):
    monkeypatch.setattr(economics.spapi, "_post", kiosk.post)
    monkeypatch.setattr(economics.spapi, "_get", kiosk.get)

    class _Response:
        status_code = 200
        content = kiosk.body()

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Response()

    monkeypatch.setattr(economics.httpx, "AsyncClient", _Client)


async def test_a_completed_query_returns_its_rows(monkeypatch):
    """The whole sequence, driven with no real waiting."""
    rows = json.loads((FIXTURES / "economics_rows.json").read_text(encoding="utf-8"))
    kiosk = _Kiosk(statuses=["IN_PROGRESS", "IN_PROGRESS", "DONE"], rows=rows)
    _patch(monkeypatch, kiosk)

    got, start, end = await economics.fetch_economics(
        days=30, today=date(2026, 8, 27), sleep=_no_sleep
    )
    assert len(got) == len(rows)
    assert got[0]["childAsin"] == rows[0]["childAsin"]
    assert (start, end) == ("2026-07-28", "2026-08-26")
    assert kiosk.polls == 3, "it stopped polling before DONE, or kept polling after"


async def test_a_gzipped_document_is_decompressed(monkeypatch):
    """Amazon documents GZIP; the live 267-row response came back UNCOMPRESSED.

    So the field is honoured rather than either assumed — a busier month arriving gzipped would
    otherwise be parsed as text and yield zero rows, which the screen would render as "you sell
    nothing".
    """
    rows = [{"childAsin": "B0AAA00001", "parentAsin": "B0P1"}]
    kiosk = _Kiosk(statuses=["DONE"], rows=rows, compressed=True)
    _patch(monkeypatch, kiosk)

    got, _start, _end = await economics.fetch_economics(
        days=30, today=date(2026, 8, 27), sleep=_no_sleep
    )
    assert got == rows


async def test_a_fatal_query_raises_rather_than_returning_nothing(monkeypatch):
    """**"We could not ask Amazon" must not render as "you sell nothing".**

    An empty list would show an empty portfolio, which reads as a catastrophic month rather than
    a failed request. Only an exception can carry that distinction to the screen.
    """
    kiosk = _Kiosk(statuses=["FATAL"])
    _patch(monkeypatch, kiosk)

    with pytest.raises(SpApiError) as exc:
        await economics.fetch_economics(days=30, today=date(2026, 8, 27), sleep=_no_sleep)
    assert "FATAL" in str(exc.value)


async def test_a_query_that_never_finishes_gives_up_and_says_so(monkeypatch):
    """A poll loop with no ceiling is a task that runs for the life of the process."""
    kiosk = _Kiosk(statuses=["IN_PROGRESS"])
    _patch(monkeypatch, kiosk)

    with pytest.raises(SpApiError) as exc:
        await economics.fetch_economics(days=30, today=date(2026, 8, 27), sleep=_no_sleep)
    assert "still running" in str(exc.value)
    assert kiosk.polls == economics.POLL_MAX, (
        f"polled {kiosk.polls} times against a cap of {economics.POLL_MAX}"
    )


async def test_done_with_no_document_is_an_empty_portfolio_not_an_error(monkeypatch):
    """A legitimately empty answer: the query succeeded and matched nothing."""
    kiosk = _Kiosk(statuses=["DONE"], document=False)
    _patch(monkeypatch, kiosk)

    got, _start, _end = await economics.fetch_economics(
        days=30, today=date(2026, 8, 27), sleep=_no_sleep
    )
    assert got == []


async def test_a_malformed_row_does_not_lose_the_others(monkeypatch):
    """One bad line must not blank the dashboard.

    The body is JSON LINES, so a single unparseable line is recoverable — the products around it
    are still worth reading, and a hard failure would be indistinguishable from an outage.
    """
    kiosk = _Kiosk(statuses=["DONE"], rows=[{"childAsin": "B0AAA00001"}])
    _patch(monkeypatch, kiosk)
    good = kiosk.body().decode()

    class _Response:
        status_code = 200
        content = (good + "\n{not json at all\n" + good).encode()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Response()

    monkeypatch.setattr(economics.httpx, "AsyncClient", lambda *a, **k: _Client())

    got, _s, _e = await economics.fetch_economics(
        days=30, today=date(2026, 8, 27), sleep=_no_sleep
    )
    assert len(got) == 2, "a malformed line took the valid rows with it"


async def test_progress_is_reported_for_every_phase(monkeypatch):
    """The bar must move while a one-to-two-minute query runs, or it reads as hung."""
    kiosk = _Kiosk(statuses=["IN_PROGRESS", "DONE"], rows=[{"childAsin": "B0AAA00001"}])
    _patch(monkeypatch, kiosk)

    seen = []
    await economics.fetch_economics(
        days=30, today=date(2026, 8, 27), sleep=_no_sleep,
        on_progress=lambda phase, done, total: seen.append(phase),
    )
    assert "submit" in seen and "poll" in seen and "download" in seen, seen
