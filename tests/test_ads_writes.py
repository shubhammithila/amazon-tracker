"""The write path — the only code in this app that changes the seller account.

`207 Multi-Status` is the response shape Amazon actually returns, measured on the live account, and
every test here exists because some plausible reading of it loses a failure. A read bug shows a
wrong number; a write bug spends money and leaves the ledger disagreeing with Amazon, which also
breaks undo.

The fake below answers in the REAL shapes, copied from live probes:

    success:  {"keywords": {"success": [{"index": 0, "keywordId": "123"}], "error": []}}
    refusal:  {"keywords": {"success": [], "error": [{"index": 0, "errors": [
                  {"errorType": "rangeError", "errorValue": {"rangeError": {
                      "message": "Bid is lower than the minimum allowed by the marketplace",
                      "reason": "TOO_LOW"}}}]}]}}
"""
from __future__ import annotations

import pytest

from app.ads import logic, spapi_ads


async def _no_sleep(_seconds):
    return None


class _Ads:
    """A fake Advertising API that records what it was sent."""

    def __init__(self, *, outcome=None, status=207, list_rows=None):
        #: `outcome(payload_rows) -> (success_list, error_list)`; default: everything succeeds.
        self.outcome = outcome
        self.status = status
        self.list_rows = list_rows or []
        self.puts: list[dict] = []
        self.put_paths: list[str] = []
        self.list_bodies: list[dict] = []


def _patch(monkeypatch, fake):
    from app.config import Settings, get_settings

    class _Settings(Settings):
        ads_client_id: str = "amzn1.application-oa2-client.test"
        ads_client_secret: str = "amzn1.oa2-cs.v1.test"
        ads_refresh_token: str = "Atzr|test"
        ads_profile_id: str = "473573783863246"

    get_settings.cache_clear()
    settings = _Settings()
    monkeypatch.setattr("app.ads.spapi_ads.get_settings", lambda: settings)
    monkeypatch.setattr("app.portfolio.ads.get_settings", lambda: settings)
    from app.portfolio import ads as portfolio_ads
    monkeypatch.setattr(portfolio_ads, "_token", portfolio_ads._Token())

    class _Response:
        def __init__(self, status, payload=None, text=""):
            self.status_code = status
            self._payload = payload
            self.text = text

        def json(self):
            return self._payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, json=None, headers=None):
            if "auth/o2/token" in url:
                return _Response(200, {"access_token": "tok", "expires_in": 3600})
            # A /list call. The response key differs per entity and Amazon is strict about it —
            # the fake mirrors that rather than returning one generic key, because a test whose
            # fake is laxer than Amazon proves nothing about the parsing.
            fake.list_bodies.append(json or {})
            if "/sp/campaigns" in url:
                key = "campaigns"
            elif "/sp/adGroups" in url:
                key = "adGroups"
            elif "/sp/keywords" in url:
                key = "keywords"
            else:
                key = "targetingClauses"
            return _Response(200, {key: list(fake.list_rows)})

        async def put(self, url, json=None, headers=None):
            fake.puts.append(json or {})
            fake.put_paths.append(url)
            body_key = "keywords" if "/sp/keywords" in url else "targetingClauses"
            rows = (json or {}).get(body_key) or []
            if fake.status >= 400 and fake.status != 207:
                return _Response(fake.status, None, text="Amazon is unhappy")
            if fake.outcome:
                success, error = fake.outcome(rows)
            else:
                id_field = "keywordId" if body_key == "keywords" else "targetId"
                success = [{"index": i, id_field: r[id_field]} for i, r in enumerate(rows)]
                error = []
            return _Response(207, {body_key: {"success": success, "error": error}})

    monkeypatch.setattr(spapi_ads.httpx, "AsyncClient", lambda *a, **k: _Client())
    return _Client()


def _change(entity_id, *, writer=logic.WRITER_KEYWORD, old=10.0, new=9.0):
    return {"entity_id": str(entity_id), "writer": writer, "old_bid": old, "new_bid": new}


# ─── Routing ─────────────────────────────────────────────────────────────────


async def test_keywords_and_targets_go_to_different_urls_with_different_id_fields(monkeypatch):
    """**The trap that would be silent.** A targetId sent to /sp/keywords comes back as a 207 with
    the failure inside an `error` array, so the run reports success for every other row.
    """
    fake = _Ads()
    client = _patch(monkeypatch, fake)

    await spapi_ads.apply_bids(client, [_change("111")], writer=logic.WRITER_KEYWORD,
                               sleep=_no_sleep)
    assert fake.put_paths[-1].endswith("/sp/keywords")
    assert fake.puts[-1] == {"keywords": [{"keywordId": "111", "bid": 9.0}]}

    await spapi_ads.apply_bids(client, [_change("222", writer=logic.WRITER_TARGET)],
                               writer=logic.WRITER_TARGET, sleep=_no_sleep)
    assert fake.put_paths[-1].endswith("/sp/targets")
    assert fake.puts[-1] == {"targetingClauses": [{"targetId": "222", "bid": 9.0}]}


async def test_the_bid_is_rounded_to_two_decimals_in_the_request(monkeypatch):
    """Amazon takes 2dp. Sending 11.412 relies on Amazon rounding the way we would have, and the
    preview has already shown the owner 11.41."""
    fake = _Ads()
    client = _patch(monkeypatch, fake)
    await spapi_ads.apply_bids(client, [_change("1", new=11.412)],
                               writer=logic.WRITER_KEYWORD, sleep=_no_sleep)
    assert fake.puts[0]["keywords"][0]["bid"] == 11.41


# ─── 207 Multi-Status ────────────────────────────────────────────────────────


async def test_a_partial_failure_is_reported_per_row_not_as_overall_success(monkeypatch):
    """**Measured: a bid below the marketplace minimum fails for that row alone.**

    Every other row in the same request succeeds, and the HTTP status is still 207. Treating 207 as
    success is how a refused bid becomes invisible — and worse, how the ledger records an `applied`
    row whose bid never changed, so a later undo restores a value Amazon never had.
    """
    def outcome(rows):
        success = [{"index": 0, "keywordId": rows[0]["keywordId"]}]
        error = [{
            "index": 1,
            "errors": [{
                "errorType": "rangeError",
                "errorValue": {"rangeError": {
                    "message": "Bid is lower than the minimum allowed by the marketplace",
                    "reason": "TOO_LOW",
                }},
            }],
        }]
        return success, error

    fake = _Ads(outcome=outcome)
    client = _patch(monkeypatch, fake)
    results = await spapi_ads.apply_bids(
        client, [_change("111"), _change("222")], writer=logic.WRITER_KEYWORD, sleep=_no_sleep
    )

    by_id = {r["entity_id"]: r for r in results}
    assert by_id["111"]["ok"] is True
    assert by_id["222"]["ok"] is False
    assert "minimum allowed by the marketplace" in by_id["222"]["error"], (
        "Amazon's own message was replaced with a generic one — theirs names the cause"
    )


async def test_a_failure_identified_only_by_index_is_matched_to_the_right_row(monkeypatch):
    """Amazon identifies failures by `index` into the REQUEST array and may omit the id.

    So request order is the only link back to a row. If that mapping is wrong, the ledger blames
    the wrong keyword — and an undo then restores the wrong bid to the wrong target.
    """
    def outcome(rows):
        # No id field at all, only the index — a shape Amazon really uses.
        return ([{"index": 0, "keywordId": rows[0]["keywordId"]},
                 {"index": 1, "keywordId": rows[1]["keywordId"]}],
                [{"index": 2, "errors": [{"errorType": "rangeError"}]}])

    fake = _Ads(outcome=outcome)
    client = _patch(monkeypatch, fake)
    results = await spapi_ads.apply_bids(
        client, [_change("aaa"), _change("bbb"), _change("ccc")],
        writer=logic.WRITER_KEYWORD, sleep=_no_sleep,
    )
    by_id = {r["entity_id"]: r["ok"] for r in results}
    assert by_id == {"aaa": True, "bbb": True, "ccc": False}


async def test_a_row_amazon_never_mentions_is_recorded_as_failed_not_applied(monkeypatch):
    """**Silence about a bid change is not evidence that it happened.**

    Recording an unmentioned row as applied would put a wrong `old_bid` chain in the ledger, so a
    later undo would write a bid Amazon never held.
    """
    def outcome(rows):
        return ([{"index": 0, "keywordId": rows[0]["keywordId"]}], [])   # says nothing about #2

    fake = _Ads(outcome=outcome)
    client = _patch(monkeypatch, fake)
    results = await spapi_ads.apply_bids(
        client, [_change("111"), _change("222")], writer=logic.WRITER_KEYWORD, sleep=_no_sleep
    )
    by_id = {r["entity_id"]: r for r in results}
    assert by_id["222"]["ok"] is False
    assert "did not report an outcome" in by_id["222"]["error"]


async def test_a_transport_failure_marks_every_row_in_the_batch_failed(monkeypatch):
    """A 401/429/500 means NOTHING in the batch was applied.

    Reporting those rows as unknown-but-silent would leave `pending` ledger rows that look like a
    crash; reporting them as failed is the truth and lets the run be retried.
    """
    fake = _Ads(status=429)
    client = _patch(monkeypatch, fake)
    results = await spapi_ads.apply_bids(
        client, [_change("1"), _change("2")], writer=logic.WRITER_KEYWORD, sleep=_no_sleep
    )
    assert len(results) == 2
    assert all(r["ok"] is False for r in results)
    assert all("429" in r["error"] for r in results)


async def test_every_input_row_gets_exactly_one_result(monkeypatch):
    """The caller writes the ledger from these results, so a missing or duplicated row would leave
    a mutation stuck at `pending` or overwrite another's outcome."""
    fake = _Ads()
    client = _patch(monkeypatch, fake)
    changes = [_change(i) for i in range(120)]
    results = await spapi_ads.apply_bids(client, changes, writer=logic.WRITER_KEYWORD,
                                         sleep=_no_sleep)
    assert len(results) == 120
    assert len({r["entity_id"] for r in results}) == 120


async def test_a_large_run_is_batched_and_every_batch_is_sent(monkeypatch):
    """The owner's real rule matched 299 rows; a 600-row run must not silently send only the first
    batch — the failure mode of every unpaginated Amazon call in this codebase."""
    fake = _Ads()
    client = _patch(monkeypatch, fake)
    changes = [_change(i) for i in range(spapi_ads.WRITE_BATCH + 200)]
    results = await spapi_ads.apply_bids(client, changes, writer=logic.WRITER_KEYWORD,
                                         sleep=_no_sleep)
    assert len(fake.puts) == 2, "the run was not split into batches"
    assert sum(len(p["keywords"]) for p in fake.puts) == len(changes)
    assert len(results) == len(changes)


async def test_no_changes_sends_no_request(monkeypatch):
    """An empty approved list must not produce an empty PUT — Amazon would accept it, and the
    ledger would record a run that did nothing."""
    fake = _Ads()
    client = _patch(monkeypatch, fake)
    assert await spapi_ads.apply_bids(client, [], writer=logic.WRITER_KEYWORD,
                                      sleep=_no_sleep) == []
    assert fake.puts == []


# ─── The stale-bid guard ─────────────────────────────────────────────────────


async def test_current_bids_are_re_read_by_id_from_both_endpoints(monkeypatch):
    """**A percentage of a stale bid silently undoes manual work.**

    The plan comes from a performance report that may be hours old. If someone edited a bid in
    Seller Central since, applying `-10%` to the reported value overwrites their change with a
    number derived from one that no longer exists. Re-reading by id is one page per 500 rows.
    """
    fake = _Ads(list_rows=[{"keywordId": "111", "bid": 12.5}, {"targetId": "222", "bid": 7.25}])
    client = _patch(monkeypatch, fake)

    live = await spapi_ads.fetch_current_bids(client, [
        _change("111", writer=logic.WRITER_KEYWORD),
        _change("222", writer=logic.WRITER_TARGET),
    ])
    assert live["111"] == 12.5
    assert live["222"] == 7.25

    # It must filter BY ID rather than paging the whole account — 148,291 keywords exist.
    assert any("keywordIdFilter" in b for b in fake.list_bodies), "keywords were not filtered by id"
    assert any("targetIdFilter" in b for b in fake.list_bodies), "targets were not filtered by id"


async def test_a_row_with_no_live_bid_is_reported_as_none_not_dropped(monkeypatch):
    """A target that has since switched to inheriting the ad group default has no bid.

    `None` rather than a missing key, so the caller can tell "Amazon says no bid" from "we did not
    ask" — the same distinction `_ratio` draws for ROAS.
    """
    fake = _Ads(list_rows=[{"keywordId": "111", "bid": None}])
    client = _patch(monkeypatch, fake)
    live = await spapi_ads.fetch_current_bids(client, [_change("111")])
    assert live["111"] is None


# ─── Reads: filtering and normalisation ──────────────────────────────────────


async def test_keywords_default_to_enabled_only(monkeypatch):
    """**A rule must not resurrect paused spend.**

    7,983 keywords are PAUSED on this account. Including them by default would mean a bid rule
    quietly re-bidding things the owner deliberately stopped.
    """
    fake = _Ads(list_rows=[])
    client = _patch(monkeypatch, fake)
    await spapi_ads.fetch_keywords(client, campaign_ids=["c1"])
    body = fake.list_bodies[-1]
    assert body["stateFilter"] == {"include": ["ENABLED"]}
    assert body["campaignIdFilter"] == {"include": ["c1"]}


async def test_archived_campaigns_are_excluded_by_default(monkeypatch):
    """An archived campaign cannot be edited, so offering it on an editing screen offers an action
    that always fails."""
    fake = _Ads(list_rows=[])
    client = _patch(monkeypatch, fake)
    await spapi_ads.fetch_campaigns(client)
    assert "ARCHIVED" not in fake.list_bodies[-1]["stateFilter"]["include"]


async def test_a_targets_match_type_is_normalised_to_the_reports_vocabulary(monkeypatch):
    """The entity API says `expressionType: AUTO`; the report says
    `TARGETING_EXPRESSION_PREDEFINED`. **Both must land on the same word**, because
    `logic.writer_for` routes on it — two vocabularies is how a target reaches the keyword endpoint.
    """
    fake = _Ads(list_rows=[
        {"targetId": "1", "adGroupId": "g", "campaignId": "c", "bid": 11.0, "state": "ENABLED",
         "expressionType": "AUTO", "resolvedExpression": [{"type": "QUERY_HIGH_REL_MATCHES"}]},
        {"targetId": "2", "adGroupId": "g", "campaignId": "c", "bid": 8.0, "state": "ENABLED",
         "expressionType": "MANUAL",
         "resolvedExpression": [{"type": "ASIN_CATEGORY_SAME_AS", "value": "4860253031"}]},
    ])
    client = _patch(monkeypatch, fake)
    rows = await spapi_ads.fetch_targets(client, campaign_ids=["c"])

    assert rows[0]["match_type"] == "TARGETING_EXPRESSION_PREDEFINED"
    assert rows[1]["match_type"] == "TARGETING_EXPRESSION"
    # ...and both route back to the endpoint they came from.
    assert logic.writer_for(rows[0]["match_type"]) == logic.WRITER_TARGET
    assert logic.writer_for(rows[1]["match_type"]) == logic.WRITER_TARGET
    # The auto target is named the way Seller Central and the report name it, not as raw JSON.
    assert rows[0]["name"] == "close-match"
    assert "4860253031" in rows[1]["name"]


async def test_a_campaigns_nested_budget_is_flattened(monkeypatch):
    """Amazon returns `{"budget": {"budget": 5000.0, "budgetType": "DAILY"}}`. Storing the object
    would push that shape into every consumer."""
    fake = _Ads(list_rows=[{"campaignId": "1", "name": "MF_SP_auto", "state": "ENABLED",
                            "targetingType": "AUTO",
                            "budget": {"budget": 5000.0, "budgetType": "DAILY"}}])
    client = _patch(monkeypatch, fake)
    rows = await spapi_ads.fetch_campaigns(client)
    assert rows[0]["daily_budget"] == 5000.0


async def test_pagination_follows_the_next_token(monkeypatch):
    """**`maxResults` caps at 500 and pagination is mandatory.**

    Measured: 148,291 keywords over 297 pages. A single unpaginated call returns 500 rows and looks
    like a small account rather than an error — the same class of bug as the Orders tab's 4-page cap.
    """
    pages = [
        {"keywords": [{"keywordId": "1"}], "nextToken": "t1"},
        {"keywords": [{"keywordId": "2"}]},
    ]
    calls = {"n": 0}

    class _Response:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.text = ""

        def json(self):
            return self._payload

    class _Client:
        async def post(self, url, content=None, json=None, headers=None):
            if "auth/o2/token" in url:
                return _Response({"access_token": "tok", "expires_in": 3600})
            page = pages[calls["n"]]
            calls["n"] += 1
            return _Response(page)

    from app.config import Settings, get_settings

    class _Settings(Settings):
        ads_client_id: str = "id"
        ads_client_secret: str = "secret"
        ads_refresh_token: str = "Atzr|test"
        ads_profile_id: str = "1"

    get_settings.cache_clear()
    settings = _Settings()
    monkeypatch.setattr("app.ads.spapi_ads.get_settings", lambda: settings)
    monkeypatch.setattr("app.portfolio.ads.get_settings", lambda: settings)
    from app.portfolio import ads as portfolio_ads
    monkeypatch.setattr(portfolio_ads, "_token", portfolio_ads._Token())

    rows = await spapi_ads._list(_Client(), spapi_ads.KEYWORDS)
    assert [r["keywordId"] for r in rows] == ["1", "2"], "the second page was not fetched"


async def test_a_list_failure_raises_rather_than_returning_a_short_list(monkeypatch):
    """A partial list silently becomes a smaller account, and a rule then edits a subset while
    reporting success."""
    class _Response:
        status_code = 403
        text = "not allowed"

        def json(self):
            return {}

    class _Client:
        async def post(self, url, content=None, json=None, headers=None):
            if "auth/o2/token" in url:
                class _Ok:
                    status_code = 200
                    text = ""

                    def json(self):
                        return {"access_token": "tok", "expires_in": 3600}
                return _Ok()
            return _Response()

    from app.config import Settings, get_settings

    class _Settings(Settings):
        ads_client_id: str = "id"
        ads_client_secret: str = "secret"
        ads_refresh_token: str = "Atzr|test"
        ads_profile_id: str = "1"

    get_settings.cache_clear()
    settings = _Settings()
    monkeypatch.setattr("app.ads.spapi_ads.get_settings", lambda: settings)
    monkeypatch.setattr("app.portfolio.ads.get_settings", lambda: settings)
    from app.portfolio import ads as portfolio_ads
    monkeypatch.setattr(portfolio_ads, "_token", portfolio_ads._Token())

    with pytest.raises(spapi_ads.AdsError):
        await spapi_ads._list(_Client(), spapi_ads.KEYWORDS)
