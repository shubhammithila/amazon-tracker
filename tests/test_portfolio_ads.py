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
    """Twelve minutes with a still bar reads as a hang, so the poll count drives it.

    The total is `POLL_MAX` for a SINGLE-report window (30 days here). A longer window is several
    reports and the denominator scales with them — see
    `test_the_progress_bar_spans_all_the_chunks_rather_than_restarting`.
    """
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


# ─── Regression: Amazon caps ONE report at 31 days ────────────────────────────
#
# Found on PRODUCTION on 2026-08-28 by pressing Refresh on a 90-day window:
#
#   {"code":"400","detail":"startDate to endDate range (89 days)
#                           must not exceed maximum range (31 days)"}
#
# The 60d and 90d buttons were broken from the moment they shipped. They were never exercised
# end to end: the economics API has no such cap and answers 90 days happily, so the margins
# loaded and only ACOS was missing — which is exactly the isolation the refresh was built for,
# and also why nothing failed loudly enough to notice during development.


def test_a_window_inside_the_cap_is_still_a_single_report():
    """The 7d and 30d presets — the ones used for decisions — must not become multi-report.

    Chunking is the fix for long windows, not a change to the common path. 31 days is the boundary
    and must stay INCLUSIVE: Amazon's message says "must not exceed", so 31 is legal.
    """
    assert ads.split_window("2026-07-29", "2026-08-27") == [("2026-07-29", "2026-08-27")]
    assert len(ads.split_window("2026-07-01", "2026-07-31")) == 1, "31 days is legal, not 2 reports"
    assert len(ads.split_window("2026-08-01", "2026-08-01")) == 1, "a single day is one report"


def test_a_long_window_is_split_into_contiguous_chunks_that_cover_it_exactly():
    """**An off-by-one here silently drops or double-counts a day of ad spend.**

    Neither is visible in the result: a total that is one day light still looks like a plausible
    ACOS. So this asserts the three properties that make the sum correct rather than the chunk
    boundaries themselves — no chunk over the cap, no gap, no overlap, and the covered days add up
    to the requested span.
    """
    from datetime import date, timedelta

    for start, end in [("2026-06-01", "2026-08-29"), ("2026-07-01", "2026-08-01"),
                       ("2026-05-15", "2026-08-12")]:
        chunks = ads.split_window(start, end)
        span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1

        covered = 0
        for index, (chunk_start, chunk_end) in enumerate(chunks):
            first, last = date.fromisoformat(chunk_start), date.fromisoformat(chunk_end)
            length = (last - first).days + 1
            assert length <= ads.MAX_REPORT_DAYS, f"{chunk_start}..{chunk_end} exceeds the cap"
            assert first <= last, "a chunk runs backwards"
            covered += length
            if index:
                previous_end = date.fromisoformat(chunks[index - 1][1])
                assert first == previous_end + timedelta(days=1), (
                    "chunks must be contiguous — a gap loses a day's spend, an overlap counts "
                    "one twice, and both look plausible in the total"
                )

        assert covered == span, f"{start}..{end}: covered {covered} days of {span}"
        assert chunks[0][0] == start and chunks[-1][1] == end, "the window edges moved"


def test_ninety_days_is_three_reports_and_thirty_is_one():
    """The two window presets that mattered, stated as the counts the fix exists to produce."""
    assert len(ads.split_window("2026-06-01", "2026-08-29")) == 3
    assert len(ads.split_window("2026-07-31", "2026-08-29")) == 1


def test_a_reversed_window_is_refused_rather_than_returning_nothing():
    """An empty chunk list would mean "no advertising", which is a different answer."""
    with pytest.raises(ValueError):
        ads.split_window("2026-08-27", "2026-07-29")


async def test_a_ninety_day_window_runs_several_reports_and_sums_them(monkeypatch):
    """The end-to-end fix: three reports, one aggregated result at one grain.

    Costs SUM across the chunks — the same ASIN advertised in all three months is one row whose
    cost is the total, not the last chunk's. `aggregate` is reused unchanged for this, so there is
    one summing rule rather than a second one for merging.
    """
    row = [{"advertisedAsin": "B0AAA00001", "advertisedSku": "sku-1", "cost": 100.0,
            "attributedSalesSameSku14d": 250.0, "purchasesSameSku14d": 2,
            "clicks": 10, "impressions": 1000}]
    fake = _Ads(statuses=["COMPLETED"], rows=row)
    _patch(monkeypatch, fake)

    out = await ads.fetch_acos("2026-06-01", "2026-08-29", sleep=_no_sleep)

    assert len(fake.created) == 3, "a 90-day window must be fetched as three reports"
    # Every request Amazon received is inside the cap — the actual production failure.
    from datetime import date
    for body in fake.created:
        span = (date.fromisoformat(body["endDate"]) - date.fromisoformat(body["startDate"])).days + 1
        assert span <= ads.MAX_REPORT_DAYS, f"a {span}-day report would be refused by Amazon"

    assert len(out) == 1, "the three chunks were not collapsed to one row per (asin, sku)"
    assert out[0]["cost"] == 300.0, "chunk costs must SUM, not overwrite"
    assert out[0]["attributed_sales"] == 750.0
    assert out[0]["clicks"] == 30 and out[0]["impressions"] == 3000


async def test_the_progress_bar_spans_all_the_chunks_rather_than_restarting(monkeypatch):
    """Three reports must read 0->100% once, not snap back to zero twice.

    A bar that restarts reads as a fault, which is the same reasoning as the orders refresh's
    monotonic percentage.
    """
    fake = _Ads(statuses=["PENDING", "COMPLETED"],
                rows=[{"advertisedAsin": "B0AAA00001", "advertisedSku": "s", "cost": 1.0}])
    _patch(monkeypatch, fake)

    seen = []
    await ads.fetch_acos(
        "2026-06-01", "2026-08-29", sleep=_no_sleep,
        on_progress=lambda done, total: seen.append((done, total)),
    )
    assert seen, "no progress during three consecutive reports"
    assert seen[-1][1] == ads.POLL_MAX * 3, "the denominator does not account for the chunks"
    fractions = [done / total for done, total in seen]
    assert fractions == sorted(fractions), "the bar went backwards between chunks"
    assert fractions[-1] > fractions[0]


async def test_one_failed_chunk_fails_the_whole_window(monkeypatch):
    """**A partial sum is not a smaller ACOS, it is a wrong one — and it looks plausible.**

    Two chunks of spend against three chunks of sales would understate ACOS by a third, which is
    the direction that talks someone into more advertising. So a failure propagates rather than
    returning what succeeded.
    """
    class _FailsOnTheSecond(_Ads):
        def __init__(self):
            super().__init__(statuses=["COMPLETED"],
                             rows=[{"advertisedAsin": "B0AAA00001", "advertisedSku": "s",
                                    "cost": 5.0}])

    fake = _FailsOnTheSecond()
    _patch(monkeypatch, fake)

    original = ads._one_report
    calls = {"n": 0}

    async def flaky(client, start, end, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ads.AdsError("Amazon refused the ad report request: chunk two broke")
        return await original(client, start, end, **kwargs)

    monkeypatch.setattr(ads, "_one_report", flaky)

    with pytest.raises(ads.AdsError) as exc:
        await ads.fetch_acos("2026-06-01", "2026-08-29", sleep=_no_sleep)
    assert "chunk two broke" in str(exc.value)
    assert calls["n"] == 2, "it kept going after a failed chunk"


# ─── poll_get: a token that expires mid-poll must not kill the report ──────────
#
# Reported as "I am seeing the same message daily and unable to optimize my ads." The Ads tab
# said Sponsored Brands was missing days for a week. Measured on production: POLL_MAX *
# POLL_INTERVAL is 45 minutes of polling on a token that lives 3600s and had already been spent
# for ~17 minutes by the preceding Sponsored Products reports — exactly ONE LWA mint per nightly
# run. The token expired mid-poll, the next poll 401'd, and the loop discarded a report Amazon
# had already produced.


def _poll_settings(monkeypatch):
    """Ads credentials configured, with a fresh token cache. Mirrors `_patch`'s settings half,
    without its report-fixture machinery — these tests drive `poll_get` directly."""
    from app.config import Settings, get_settings

    class _Settings(Settings):
        ads_client_id: str = "amzn1.application-oa2-client.test"
        ads_client_secret: str = "amzn1.oa2-cs.v1.test"
        ads_refresh_token: str = "Atzr|test"
        ads_profile_id: str = "473573783863246"

    get_settings.cache_clear()
    monkeypatch.setattr("app.portfolio.ads.get_settings", lambda: _Settings())
    monkeypatch.setattr(ads, "_token", ads._Token())


class _PollResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text
        self.content = b""

    def json(self):
        return self._payload


async def test_a_401_mid_poll_is_retried_with_a_fresh_token(monkeypatch):
    """**The bug this test exists to catch: Sponsored Brands failed every night for a week.**

    The old loop bound `headers=head` once and reused it for the whole poll window, so an expired
    token turned into `401 {"message":"Unauthorized exception while handling 3P Request: Invalid
    token"}` and a fatal AdsError — throwing away a report Amazon had already generated.
    """
    _poll_settings(monkeypatch)
    mints, polls = [], []

    class _Client:
        async def post(self, url, content=None, headers=None):
            assert "auth/o2/token" in url
            mints.append(url)
            return _PollResponse(200, {"access_token": f"tok-{len(mints)}", "expires_in": 3600})

        async def get(self, url, headers=None):
            polls.append(headers.get("Authorization"))
            if len(polls) == 1:
                return _PollResponse(401, text='{"message":"Invalid token"}')
            return _PollResponse(200, {"status": "COMPLETED"})

    response = await ads.poll_get(_Client(), "https://ads.invalid/reporting/reports/rep-1")

    assert response.status_code == 200, "a 401 mid-poll was not retried"
    assert len(mints) == 2, f"the token was not re-minted after the 401 ({len(mints)} mints)"
    assert polls[0] != polls[1], "the retry reused the same expired token"


async def test_a_permanent_401_fails_after_the_retry_bound(monkeypatch):
    """A genuinely revoked refresh token must surface, not loop for the full 45 minutes."""
    _poll_settings(monkeypatch)
    mints = []

    class _Client:
        async def post(self, url, content=None, headers=None):
            mints.append(url)
            return _PollResponse(200, {"access_token": f"tok-{len(mints)}", "expires_in": 3600})

        async def get(self, url, headers=None):
            return _PollResponse(401, text='{"message":"Invalid token"}')

    response = await ads.poll_get(
        _Client(), "https://ads.invalid/reporting/reports/rep-1", force_refresh_attempts=3,
    )
    assert response.status_code == 401, "a permanent 401 should be returned for the caller to report"
    assert len(mints) <= 4, f"unbounded re-minting: {len(mints)} mints"


async def test_the_happy_path_mints_only_once(monkeypatch):
    """Rebuilding headers per poll must NOT mean re-authenticating per poll.

    `_access_token` returns the cached token until `expires_at - _TOKEN_SAFETY_MARGIN`, so the
    per-call rebuild is nearly free. Getting this wrong would turn one mint into 135 LWA calls
    per report.
    """
    _poll_settings(monkeypatch)
    mints = []

    class _Client:
        async def post(self, url, content=None, headers=None):
            mints.append(url)
            return _PollResponse(200, {"access_token": "tok", "expires_in": 3600})

        async def get(self, url, headers=None):
            return _PollResponse(200, {"status": "COMPLETED"})

    client = _Client()
    for _ in range(5):
        await ads.poll_get(client, "https://ads.invalid/reporting/reports/rep-1")

    assert len(mints) == 1, f"the cached token was not reused: {len(mints)} mints for 5 polls"
