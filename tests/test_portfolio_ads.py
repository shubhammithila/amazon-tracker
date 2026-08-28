"""The Advertising API: request shape, aggregation, and true ACOS.

Every fact here was measured against the live amazon.in account on 2026-08-27, because the ads
documentation is behind an auth wall and three of the four things that broke were not guessable
from the SDK either.

`tests/fixtures/ads_rows.json` holds 73 REAL rows from a `spAdvertisedProduct` report, chosen to
cover the cases that matter: a pair split across 9 and 10 campaign rows (proving the aggregation),
spend with zero attributed sales, a merchant SKU beside its FBA twin on one ASIN, and healthy rows
under 30% ACOS.
"""
import gzip
import json
from pathlib import Path

import pytest

from app.portfolio import ads

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture():
    return json.loads((FIXTURES / "ads_rows.json").read_text(encoding="utf-8"))


async def _no_sleep(seconds):
    return None


# ─── The fixture itself ──────────────────────────────────────────────────────


def test_the_fixture_still_carries_the_cases_that_shaped_this_module():
    """A guard on every other test here.

    Re-captured from a quiet week, this fixture would exercise none of the four behaviours below
    and would pass while proving nothing — the same guard the orders and economics fixtures carry.
    """
    rows = _fixture()
    assert len(rows) >= 40, "the fixture has shrunk; it can no longer cover the cases"

    pairs = {(r.get("advertisedAsin"), r.get("advertisedSku")) for r in rows}
    assert len(rows) > len(pairs) * 2, (
        "no pair is split across several campaign rows, so the aggregation is untested"
    )

    by_pair: dict = {}
    for r in rows:
        key = (r.get("advertisedAsin"), r.get("advertisedSku"))
        acc = by_pair.setdefault(key, [0.0, 0.0])
        acc[0] += float(r.get("cost") or 0)
        acc[1] += float(r.get("attributedSalesSameSku14d") or 0)

    assert any(c > 0 and a == 0 for c, a in by_pair.values()), (
        "no row has spend with zero attributed sales, so the infinite-ACOS case is untested"
    )
    skus = {sku for _asin, sku in pairs if sku}
    assert any(s.upper().endswith("FBA") for s in skus), "no FBA SKU: the channel split is untested"
    assert any(not s.upper().endswith("FBA") for s in skus), "no merchant SKU"


# ─── The request shape: three traps, each a real 400 ─────────────────────────


def test_the_report_groups_by_advertiser():
    """**`groupBy: ["advertiser"]`, and nothing else works for a per-product report.**

    A wrong value 400s with Amazon naming the alternatives: *"invalid groupBy values:
    (advertiser). Allowed values: (campaign, adGroup, campaignPlacement)"*. Pinned because those
    other values are all plausible and none of them answers "how did this ASIN do".
    """
    body = ads.build_report_request("2026-07-28", "2026-08-26")
    assert body["configuration"]["groupBy"] == ["advertiser"]
    assert body["configuration"]["reportTypeId"] == "spAdvertisedProduct"


def test_the_report_does_not_ask_for_a_date_column():
    """**`date` is not legal under `timeUnit: SUMMARY`** — it 400s the whole request.

    Amazon's message is clear once seen (*"date is not a supported column for this time unit"*)
    but the column is an obvious thing to want, so the absence needs a test to stay absent.
    """
    body = ads.build_report_request("2026-07-28", "2026-08-26")
    assert "date" not in body["configuration"]["columns"]
    assert body["configuration"]["timeUnit"] == "SUMMARY"
    # The window travels as start/end instead.
    assert body["startDate"] == "2026-07-28" and body["endDate"] == "2026-08-26"


def test_the_report_asks_for_attributed_sales():
    """The one column SP-API cannot provide, and therefore the reason this module exists.

    Without `attributedSalesSameSku14d` there is no ACOS — only TACOS, which the economics feed
    already gives. Measured: TACOS 33.1% against a true ACOS of 89.9% on the same spend.
    """
    body = ads.build_report_request("2026-07-28", "2026-08-26")
    assert "attributedSalesSameSku14d" in body["configuration"]["columns"]
    assert "cost" in body["configuration"]["columns"]
    assert "advertisedAsin" in body["configuration"]["columns"]
    # The SKU too, because it carries the fulfilment channel ("… FBA").
    assert "advertisedSku" in body["configuration"]["columns"]


def test_the_create_content_type_is_the_versioned_one():
    """`application/vnd.createasyncreportrequest.v3+json`; plain application/json is rejected."""
    assert ads.CREATE_CONTENT_TYPE == "application/vnd.createasyncreportrequest.v3+json"


# ─── Aggregation: 1,697 rows to 213 pairs ────────────────────────────────────


def test_the_report_is_collapsed_to_one_row_per_asin_and_sku():
    """**Amazon splits the report by CAMPAIGN even though groupBy is `advertiser`.**

    Measured: 1,697 rows for 213 (asin, sku) pairs, up to 13 rows for one pair. Aggregating here
    rather than downstream means every consumer sees one grain — two grains in one payload is how
    a total starts disagreeing with the rows beneath it.
    """
    raw = _fixture()
    out = ads.aggregate(raw)
    pairs = {(r["child_asin"], r["seller_sku"]) for r in out}
    assert len(out) == len(pairs), "aggregate() returned duplicate pairs"
    assert len(out) < len(raw), (
        f"{len(raw)} raw rows collapsed to {len(out)} — no aggregation happened"
    )
    # And nothing is lost in the collapse.
    assert round(sum(r["cost"] for r in out), 2) == round(
        sum(float(r.get("cost") or 0) for r in raw), 2
    )
    assert round(sum(r["attributed_sales"] for r in out), 2) == round(
        sum(float(r.get("attributedSalesSameSku14d") or 0) for r in raw), 2
    )


def test_clicks_and_impressions_are_summed_not_averaged():
    """They are counts of the same ads viewed as one product, so they add."""
    raw = _fixture()
    out = ads.aggregate(raw)
    assert sum(r["clicks"] for r in out) == sum(int(r.get("clicks") or 0) for r in raw)
    assert sum(r["impressions"] for r in out) == sum(int(r.get("impressions") or 0) for r in raw)


def test_a_row_with_no_asin_is_dropped():
    """An ASIN-less row cannot be joined to anything, so it would only distort the totals."""
    out = ads.aggregate([
        {"advertisedAsin": "", "advertisedSku": "x", "cost": 99.0},
        {"advertisedAsin": "B0AAA00001", "advertisedSku": "y", "cost": 10.0},
    ])
    assert len(out) == 1
    assert out[0]["cost"] == 10.0


def test_the_asin_is_upper_cased_so_it_joins_to_the_economics():
    """The economics rows upper-case their ASINs; a lower-case one here would silently not join,
    and the product would show no ACOS while its spend vanished from the total."""
    out = ads.aggregate([{"advertisedAsin": "b0aaa00001", "advertisedSku": "s", "cost": 5.0}])
    assert out[0]["child_asin"] == "B0AAA00001"


# ─── The report sequence ─────────────────────────────────────────────────────


class _Ads:
    """A fake Advertising API answering in the real response shapes."""

    def __init__(self, *, statuses, rows=None, compressed=False, create_status=200):
        self.statuses = list(statuses)
        self.rows = rows if rows is not None else []
        self.compressed = compressed
        self.create_status = create_status
        self.polls = 0
        self.created = []

    def body(self):
        raw = json.dumps(self.rows).encode()
        return gzip.compress(raw) if self.compressed else raw


def _patch(monkeypatch, fake, *, configured=True):
    from app.config import Settings, get_settings

    class _Settings(Settings):
        ads_client_id: str = "amzn1.application-oa2-client.test" if configured else ""
        ads_client_secret: str = "amzn1.oa2-cs.v1.test" if configured else ""
        ads_refresh_token: str = "Atzr|test" if configured else ""
        ads_profile_id: str = "473573783863246" if configured else ""

    get_settings.cache_clear()
    monkeypatch.setattr("app.portfolio.ads.get_settings", lambda: _Settings())
    # A fresh token cache per test, or a cached token from another test would skip the mint.
    monkeypatch.setattr(ads, "_token", ads._Token())

    class _Response:
        def __init__(self, status, payload=None, content=None, text=""):
            self.status_code = status
            self._payload = payload
            self.content = content if content is not None else b""
            self.text = text

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            if "auth/o2/token" in url:
                return _Response(200, {"access_token": "tok", "expires_in": 3600})
            fake.created.append(json.loads(content))
            if fake.create_status >= 400:
                return _Response(fake.create_status, text="Amazon says no")
            return _Response(200, {"reportId": "rep-1"})

        async def get(self, url, headers=None):
            if url.startswith("https://example.invalid"):
                return _Response(200, content=fake.body())
            fake.polls += 1
            status = fake.statuses[min(fake.polls - 1, len(fake.statuses) - 1)]
            payload = {"status": status}
            if status == "COMPLETED":
                payload["url"] = "https://example.invalid/report"
            return _Response(200, payload)

    monkeypatch.setattr(ads.httpx, "AsyncClient", _Client)


async def test_a_completed_report_returns_aggregated_rows(monkeypatch):
    """The whole sequence — mint, create, poll, download, aggregate — with no real waiting."""
    raw = _fixture()
    fake = _Ads(statuses=["PENDING", "PENDING", "COMPLETED"], rows=raw)
    _patch(monkeypatch, fake)

    out = await ads.fetch_acos("2026-07-28", "2026-08-26", sleep=_no_sleep)
    assert out, "no rows returned"
    assert len(out) < len(raw), "the rows were not aggregated"
    assert fake.polls == 3
    assert fake.created[0]["configuration"]["reportTypeId"] == "spAdvertisedProduct"


async def test_a_gzipped_report_is_decompressed(monkeypatch):
    """GZIP_JSON is requested, but the MAGIC BYTES decide.

    The economics document arrived uncompressed despite the documentation, so trusting the request
    here would repeat that mistake in a second place — and a gzip header parsed as text yields
    zero rows, which renders as "you advertise nothing".
    """
    fake = _Ads(statuses=["COMPLETED"],
                rows=[{"advertisedAsin": "B0AAA00001", "advertisedSku": "s", "cost": 12.0}],
                compressed=True)
    _patch(monkeypatch, fake)

    out = await ads.fetch_acos("2026-07-28", "2026-08-26", sleep=_no_sleep)
    assert out[0]["cost"] == 12.0


async def test_no_credentials_raises_the_distinct_not_configured_error(monkeypatch):
    """**A distinct type, so the refresh can skip ACOS instead of reporting a failure.**

    The Portfolio tab shipped before ACOS existed and is fully useful without it: the margins are
    load-bearing, ACOS is an addition. Conflating "no ads keys" with "the ad report failed" would
    make every install without advertising credentials look broken.
    """
    fake = _Ads(statuses=["COMPLETED"])
    _patch(monkeypatch, fake, configured=False)

    with pytest.raises(ads.AdsNotConfigured):
        await ads.fetch_acos("2026-07-28", "2026-08-26", sleep=_no_sleep)


async def test_a_failed_report_raises_rather_than_returning_nothing(monkeypatch):
    """"We could not ask" must never render as "no ad spend"."""
    fake = _Ads(statuses=["FAILURE"])
    _patch(monkeypatch, fake)

    with pytest.raises(ads.AdsError) as exc:
        await ads.fetch_acos("2026-07-28", "2026-08-26", sleep=_no_sleep)
    assert "FAILURE" in str(exc.value)


async def test_a_report_that_never_finishes_gives_up_and_says_so(monkeypatch):
    """A poll loop with no ceiling is a task that runs for the life of the process.

    Measured: generation takes ~12 minutes, so the ceiling is 20 — generous, and still bounded.
    """
    fake = _Ads(statuses=["PENDING"])
    _patch(monkeypatch, fake)

    with pytest.raises(ads.AdsError) as exc:
        await ads.fetch_acos("2026-07-28", "2026-08-26", sleep=_no_sleep)
    assert "still generating" in str(exc.value)
    assert fake.polls == ads.POLL_MAX


async def test_a_refused_request_surfaces_amazons_own_message(monkeypatch):
    """Amazon's 400s here NAME the allowed values, which is unusually useful.

    Replacing that with a generic "the request failed" would throw away the one thing that made
    this integration tractable.
    """
    fake = _Ads(statuses=["COMPLETED"], create_status=400)
    _patch(monkeypatch, fake)

    with pytest.raises(ads.AdsError) as exc:
        await ads.fetch_acos("2026-07-28", "2026-08-26", sleep=_no_sleep)
    assert "Amazon says no" in str(exc.value)


async def test_progress_is_reported_while_the_report_generates(monkeypatch):
    """Twelve minutes with a still bar reads as a hang, so the poll count drives it."""
    fake = _Ads(statuses=["PENDING", "PENDING", "COMPLETED"],
                rows=[{"advertisedAsin": "B0AAA00001", "advertisedSku": "s", "cost": 1.0}])
    _patch(monkeypatch, fake)

    seen = []
    await ads.fetch_acos(
        "2026-07-28", "2026-08-26", sleep=_no_sleep,
        on_progress=lambda done, total: seen.append((done, total)),
    )
    assert seen, "no progress was reported during a 12-minute wait"
    assert seen[-1][1] == ads.POLL_MAX
