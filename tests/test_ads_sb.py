"""Sponsored Brands, and keeping automated campaigns out of our rules.

Two features that arrived together because using the tab exposed both: SB was invisible (never
fetched — `spapi_ads` only called `/sp/`), and campaigns optimised by M19 or Amazon Adaptive were
being edited by rules that would immediately be overwritten.

Every number here was measured against the live account on 2026-08-29.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ads import logic, reports, spapi_ads


def _sb_fixture() -> list[dict]:
    """717 real rows from a live `sbTargeting` report, captured 2026-08-29.

    Trimmed from the full 2,914 but **chosen to preserve the result**: it keeps every row a
    `spend > 50` rule touches, 30+ of each of the four match types, and 20 zero-spend rows. The rule
    produces identical totals on the fixture and on the full report (623 matched, 611 changing, 338
    SB keywords + 273 SB targets), which the tests below assert — a blind sample would not have.

    Note the column names: SB reports plain `sales`/`purchases` where Sponsored Products reports
    `sales7d`/`purchases7d`. That difference is the reason `metrics_for` reads both.
    """
    path = Path(__file__).parent / "fixtures" / "sb_targeting_rows.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ─── Who manages a campaign ──────────────────────────────────────────────────


def test_the_real_m19_and_adaptive_names_are_classified():
    """**Measured: all 4 M19 campaigns carry `m19 autopilot` in the NAME**, and all 3 of Amazon's
    are named `Adaptive Campaign - <timestamp>`.

    A convention of this account rather than an Amazon rule, which is why it is one function with
    the evidence in its docstring — the same treatment `portfolio.logic._channel_of` gives the
    trailing ` FBA` SKU suffix.
    """
    m19 = [
        "SP -  - All products -  - auto - m19 autopilot - p7WvQFKmOjE",
        "SP -  - All products -  - exact - m19 autopilot - yQ30JKqbm+",
        "SP -  - All products -  - phrase - m19 autopilot - 6mY5CQKQ/",
        "SP -  - All products -  - product - m19 autopilot - LRbC0bJ7",
    ]
    for name in m19:
        assert logic.manager_of(name) == logic.MANAGER_M19, name
        assert logic.is_automated(name)

    adaptive = [
        "Adaptive Campaign - 09/07/2026 17:31:19.828",
        "Adaptive Campaign - 16/01/2026 13:25:28.700",
        "Adaptive Campaign - 25/09/2025 15:00:59.703",
    ]
    for name in adaptive:
        assert logic.manager_of(name) == logic.MANAGER_AMAZON, name
        assert logic.is_automated(name)


def test_our_own_campaigns_are_ours_including_ones_that_do_not_exist_yet():
    """**An unrecognised name is `us`, and that default is deliberate.**

    A campaign the owner has just created must appear and be tunable. Defaulting the other way would
    silently hide his own new campaigns, which is the worse failure — he would look for them, not
    find them, and have no way to tell a filter from a bug.
    """
    for name in ("MF_SP_keywords", "HF_SP_Auto", "MF_SB_Sattu",
                 "a campaign made five minutes ago", "", None):
        assert logic.manager_of(name) == logic.MANAGER_US, name
        assert not logic.is_automated(name)


def test_the_marker_match_is_case_insensitive_and_substring():
    """Amazon echoes the name as typed, so case must not decide whether a rule can edit a campaign."""
    assert logic.manager_of("SP - ALL PRODUCTS - M19 AUTOPILOT - x") == logic.MANAGER_M19
    assert logic.manager_of("adaptive campaign - 2026") == logic.MANAGER_AMAZON
    # ...but an unrelated name containing neither marker stays ours.
    assert logic.manager_of("Adaptive pricing test") == logic.MANAGER_US


# ─── Rules refuse automated campaigns ────────────────────────────────────────


def _row(entity_id, campaign_name, *, spend=500.0, sales=1000.0, bid=10.0,
         match_type="EXACT", campaign_id=None):
    return {
        "keywordId": str(entity_id), "matchType": match_type, "keyword": f"kw{entity_id}",
        "campaignName": campaign_name, "campaignId": campaign_id or f"c{entity_id}",
        "adGroupId": f"g{entity_id}", "cost": spend, "sales7d": sales, "keywordBid": bid,
    }


def test_a_rule_refuses_automated_campaigns_and_names_the_reason():
    """**The exclusion lives in `plan_run`, not the UI**, and that placement is the point.

    A screen-level filter would leave `POST /ads/apply` editable by a hand-built request, and this is
    the one router in the app that spends money. Every path — preview, apply, a saved rule, an undo —
    goes through `plan_run`.

    Skipped and NAMED rather than dropped: a row missing from a 285-row run is indistinguishable from
    a bug, which is the rule the whole preview screen follows.
    """
    rows = [
        _row(1, "MF_SP_keywords"),
        _row(2, "SP -  - All products -  - exact - m19 autopilot - x"),
        _row(3, "Adaptive Campaign - 25/09/2025 15:00:59.703"),
    ]
    plan = logic.plan_run(
        rows, conditions=[{"field": "spend", "op": "gt", "value": 100}],
        action=logic.ACTION_DECREASE_PCT, amount=10,
    )

    assert [c["entity_id"] for c in plan["changes"]] == ["1"]
    assert plan["totals"]["automated"] == 2
    reasons = {s["entity_id"]: s["reason"] for s in plan["skipped"]}
    assert reasons["2"] == logic.SKIP_AUTOMATED
    assert reasons["3"] == logic.SKIP_AUTOMATED
    # The manager is reported, so the preview can say WHICH system owns the row.
    managers = {s["entity_id"]: s["manager"] for s in plan["skipped"]}
    assert managers == {"2": logic.MANAGER_M19, "3": logic.MANAGER_AMAZON}


def test_an_automated_row_is_counted_as_MATCHED_not_hidden():
    """The count must stay honest: "12 matched but M19 manages them" rather than a silently smaller
    match count that reads as the rule not working."""
    rows = [_row(1, "SP - m19 autopilot - x"), _row(2, "SP - m19 autopilot - y")]
    plan = logic.plan_run(
        rows, conditions=[{"field": "spend", "op": "gt", "value": 100}],
        action=logic.ACTION_DECREASE_PCT, amount=10,
    )
    assert plan["totals"]["matched"] == 2
    assert plan["totals"]["changing"] == 0
    assert plan["totals"]["automated"] == 2


def test_scoping_a_rule_TO_an_automated_campaign_still_changes_nothing():
    """Deliberately targeting one must not be a way around the refusal.

    The obvious bug here would be an exclusion applied only to unscoped runs.
    """
    rows = [_row(1, "SP - m19 autopilot - x", campaign_id="cM19")]
    plan = logic.plan_run(
        rows, conditions=[{"field": "spend", "op": "gt", "value": 100}],
        action=logic.ACTION_DECREASE_PCT, amount=10, scope_campaign_ids=["cM19"],
    )
    assert plan["changes"] == []
    assert plan["totals"]["automated"] == 1


def test_a_row_with_no_manager_field_is_classified_rather_than_trusted():
    """`plan_run` recomputes `manager` when absent, so a row assembled by an older code path — or a
    hand-built request that simply omits it — cannot slip through unclassified."""
    m = logic.metrics_for(_row(1, "SP - m19 autopilot - x"))
    del m["manager"]
    plan = logic.plan_run(
        [m], conditions=[{"field": "spend", "op": "gt", "value": 100}],
        action=logic.ACTION_DECREASE_PCT, amount=10,
    )
    assert plan["changes"] == []
    assert plan["skipped"][0]["reason"] == logic.SKIP_AUTOMATED


# ─── Routing: the ad product decides the endpoint ────────────────────────────


def test_the_same_match_type_routes_differently_per_ad_product():
    """**`EXACT` is legal on BOTH products and they are different endpoints.**

    So the match type alone cannot say where a bid goes — routing needs the ad product, and getting
    it wrong sends an SB id to `/sp/keywords`.
    """
    assert logic.writer_for("EXACT", logic.AD_PRODUCT_SP) == logic.WRITER_KEYWORD
    assert logic.writer_for("EXACT", logic.AD_PRODUCT_SB) == logic.WRITER_SB_KEYWORD
    # SB sends lowercase; SP sends uppercase. Both must route.
    assert logic.writer_for("exact", logic.AD_PRODUCT_SB) == logic.WRITER_SB_KEYWORD
    assert logic.writer_for("phrase", logic.AD_PRODUCT_SB) == logic.WRITER_SB_KEYWORD
    # The SAME string routes to two different endpoints depending on the product, which is exactly
    # why `writer_for` needs both arguments.
    assert logic.writer_for("TARGETING_EXPRESSION", logic.AD_PRODUCT_SP) == logic.WRITER_TARGET
    assert logic.writer_for("TARGETING_EXPRESSION", logic.AD_PRODUCT_SB) == logic.WRITER_SB_TARGET
    # `THEME` exists ONLY on Sponsored Brands.
    assert logic.writer_for("THEME", logic.AD_PRODUCT_SB) == logic.WRITER_SB_TARGET
    assert logic.writer_for("THEME", logic.AD_PRODUCT_SP) is None


def test_the_default_ad_product_keeps_every_existing_caller_working():
    """`ad_product` defaults to `sp`, so pre-existing stored rows and older call sites keep their
    exact meaning — everything stored before Sponsored Brands came from `/sp/`."""
    assert logic.writer_for("EXACT") == logic.WRITER_KEYWORD
    assert logic.writer_for("TARGETING_EXPRESSION_PREDEFINED") == logic.WRITER_TARGET


def test_split_by_writer_keeps_all_three_apart():
    """The last point where three different Amazon endpoints are still in one list."""
    changes = [
        {"entity_id": "1", "writer": logic.WRITER_KEYWORD},
        {"entity_id": "2", "writer": logic.WRITER_TARGET},
        {"entity_id": "3", "writer": logic.WRITER_SB_KEYWORD},
        {"entity_id": "4", "writer": logic.WRITER_SB_KEYWORD},
    ]
    changes.append({"entity_id": "5", "writer": logic.WRITER_SB_TARGET})
    out = logic.split_by_writer(changes)
    assert [c["entity_id"] for c in out[logic.WRITER_KEYWORD]] == ["1"]
    assert [c["entity_id"] for c in out[logic.WRITER_TARGET]] == ["2"]
    assert [c["entity_id"] for c in out[logic.WRITER_SB_KEYWORD]] == ["3", "4"]
    assert [c["entity_id"] for c in out[logic.WRITER_SB_TARGET]] == ["5"]


# ─── The SB write payload ────────────────────────────────────────────────────


def test_the_sb_payload_carries_ad_group_id():
    """**SB requires `adGroupId` and SP does not, and omitting it fails EVERY row inside a 207.**

    Measured against the live API, sending SP's shape to `/sb/keywords`:

        207 [{"code": "INVALID_ARGUMENT",
              "description": "Keyword was specified without an ad group id",
              "errors": [{"KeywordError": {"reason": "KEYWORD_MISSING_AD_GROUP_ID"}}]}]

    Nothing applied, and the HTTP status says success. With `adGroupId`: `[{"code": "SUCCESS"}]`.
    """
    row = spapi_ads._sb_payload_row({
        "entity_id": "102932256635969", "ad_group_id": "552988060449155", "new_bid": 20.0,
    })
    assert row == {
        "keywordId": 102932256635969, "adGroupId": 552988060449155, "bid": 20.0,
    }
    assert "adGroupId" in row, "without adGroupId every SB row is refused inside a 207"


def test_the_sb_bid_is_rounded_to_two_decimals():
    row = spapi_ads._sb_payload_row({
        "entity_id": "1", "ad_group_id": "2", "new_bid": 11.412,
    })
    assert row["bid"] == 11.41


# ─── The SB 207 response, which is a different shape ────────────────────────


def test_the_sb_success_shape_is_a_bare_array():
    """Measured live: `[{"code": "SUCCESS", "keywordId": 102932256635969}]` — no `success` key, no
    `error` key, no wrapping object."""
    body = [{"code": "SUCCESS", "keywordId": 102932256635969}]
    results = spapi_ads._parse_sb_outcome(body, ["102932256635969"], "keywordId")
    assert results == [{"entity_id": "102932256635969", "ok": True, "error": None}]


def test_the_sb_refusal_keeps_amazons_own_reason():
    """Their messages name the cause — they are how the `adGroupId` requirement was found."""
    body = [{
        "code": "INVALID_ARGUMENT",
        "description": "Keyword was specified without an ad group id",
        "errors": [{"KeywordError": {
            "message": "Keyword was specified without an ad group id",
            "reason": "KEYWORD_MISSING_AD_GROUP_ID",
        }}],
        "keywordId": 102932256635969,
    }]
    results = spapi_ads._parse_sb_outcome(body, ["102932256635969"], "keywordId")
    assert results[0]["ok"] is False
    assert "ad group id" in results[0]["error"], (
        "Amazon's own message was replaced with a generic one — theirs names the cause"
    )


def test_feeding_the_sb_shape_to_the_sp_parser_reports_unknown_rather_than_raising():
    """**This is why the two parsers are separate functions rather than one with a branch.**

    The shapes share no structure: `_parse_sp_outcome` looks for `body[key]["success"]`, which an SB
    array does not have. Before this was defended, it raised `AttributeError` mid-run — after some
    batches had already been sent and the ledger was half-written. Now every row comes back as an
    unknown outcome, which is reported and recoverable.
    """
    body = [{"code": "SUCCESS", "keywordId": 102932256635969}]
    results = spapi_ads._parse_sp_outcome(body, ["102932256635969"], "keywordId", "keywords")
    assert results[0]["ok"] is False
    assert "did not report an outcome" in results[0]["error"]


def test_an_sb_row_amazon_never_mentions_is_failed_not_applied():
    """Silence about a bid change is not evidence it happened — recording it as applied would
    corrupt the undo chain."""
    body = [{"code": "SUCCESS", "keywordId": 111}]
    results = spapi_ads._parse_sb_outcome(body, ["111", "222"], "keywordId")
    by_id = {r["entity_id"]: r for r in results}
    assert by_id["111"]["ok"] is True
    assert by_id["222"]["ok"] is False
    assert "did not report an outcome" in by_id["222"]["error"]


# ─── The SB report ───────────────────────────────────────────────────────────


def test_the_sb_report_asks_for_sbTargeting_and_sb_column_names():
    """**SB and SP disagree about column names**: SB reports plain `sales`/`purchases`, SP reports
    `sales7d`/`purchases7d`. A bad column name fails the whole request, so the two lists are separate
    tuples rather than one filtered copy."""
    sp = reports.build_report_request("2026-08-22", "2026-08-28")
    sb = reports.build_report_request("2026-08-22", "2026-08-28", ad_product="sb")

    assert sp["configuration"]["reportTypeId"] == "spTargeting"
    assert sp["configuration"]["adProduct"] == "SPONSORED_PRODUCTS"
    assert "sales7d" in sp["configuration"]["columns"]

    assert sb["configuration"]["reportTypeId"] == "sbTargeting"
    assert sb["configuration"]["adProduct"] == "SPONSORED_BRANDS"
    assert "sales" in sb["configuration"]["columns"]
    assert "sales7d" not in sb["configuration"]["columns"], (
        "sales7d is not a legal SB column and would fail the whole report"
    )


def test_aggregate_sums_both_products_column_names():
    """A chunked window sums the same entity twice. Summing only SP's names would silently discard
    SB sales, leaving an SB ROAS of zero that looks like a real result."""
    sb = [
        {"keywordId": "1", "cost": 100.0, "sales": 200.0, "clicks": 5, "purchases": 1},
        {"keywordId": "1", "cost": 50.0, "sales": 75.0, "clicks": 3, "purchases": 1},
    ]
    merged = reports.aggregate(sb)[0]
    assert merged["cost"] == 150.0
    assert merged["sales"] == 275.0
    assert merged["purchases"] == 2

    sp = [
        {"keywordId": "2", "cost": 10.0, "sales7d": 20.0, "clicks": 1, "purchases7d": 1},
        {"keywordId": "2", "cost": 5.0, "sales7d": 7.0, "clicks": 1, "purchases7d": 0},
    ]
    merged_sp = reports.aggregate(sp)[0]
    assert merged_sp["cost"] == 15.0
    assert merged_sp["sales7d"] == 27.0


def test_a_throttled_report_creation_waits_rather_than_failing():
    """**`sbTargeting` returns 429 and then succeeds** — measured: three immediate creates all
    throttled, one after a 60-second pause returned 200, while `sbCampaigns` was accepted first time.

    So a 429 is a WAIT. Treating it as a failure would make the SB refresh fail most of the time it
    is asked to run.
    """
    source = (Path(__file__).parent.parent / "app" / "ads" / "reports.py").read_text(
        encoding="utf-8")
    assert "THROTTLE_ATTEMPTS" in source
    assert "if create.status_code != 429:" in source, (
        "report creation does not retry on 429, so a throttled SB report fails instead of waiting"
    )


def test_a_duplicate_report_response_follows_amazons_existing_report():
    """**425 means "duplicate of <reportId>" and is a success in disguise.**

    Measured while capturing a fixture: after two 429s, the third create returned
    `425 {"detail":"The Request is a duplicate of : 9553f2aa-..."}` — Amazon had accepted an identical
    request during a throttled attempt and deduplicated this one. That report existed and completed
    normally, so treating 425 as a failure throws away a report already paid for. The retry above
    makes hitting this LIKELY rather than rare.
    """
    source = (Path(__file__).parent.parent / "app" / "ads" / "reports.py").read_text(
        encoding="utf-8")
    assert "425" in source and "duplicate_id" in source, (
        "a 425 duplicate response is treated as a failure, discarding a report Amazon already built"
    )


# ─── Against the real SB report ──────────────────────────────────────────────


def test_the_real_sb_report_normalises_and_routes():
    """Every real row must produce a usable metric set and route to one of the SB writers."""
    rows = _sb_fixture()
    assert len(rows) > 500, "the SB fixture is unexpectedly small"

    unroutable = set()
    for raw in rows:
        m = logic.metrics_for(raw, logic.AD_PRODUCT_SB)
        assert m["ad_product"] == logic.AD_PRODUCT_SB
        if m["writer"] is None:
            unroutable.add(raw.get("matchType"))
        # SB's own column names must be read, not SP's.
        assert m["sales"] == pytest.approx(float(raw.get("sales") or 0))
        assert m["orders"] == int(raw.get("purchases") or 0)

    assert not unroutable, f"unroutable SB match types in the live report: {unroutable}"


def test_a_rule_over_the_real_sb_report_produces_sb_writes_only():
    """End to end on real data: an SB rule must route every change to `/sb/keywords`."""
    rows = _sb_fixture()
    metrics = [logic.metrics_for(r, logic.AD_PRODUCT_SB) for r in rows]

    plan = logic.plan_run(
        metrics,
        conditions=[{"field": "spend", "op": "gt", "value": 50}],
        action=logic.ACTION_DECREASE_PCT, amount=10,
    )
    assert plan["blocked"] is None or "rows" in plan["blocked"]
    assert plan["changes"], "the real SB report produced no changes at all"

    sb_writers = {logic.WRITER_SB_KEYWORD, logic.WRITER_SB_TARGET}
    assert all(c["writer"] in sb_writers for c in plan["changes"])
    # Never an SP writer: that would send an SB id to /sp/keywords.
    assert plan["totals"]["keywords"] == 0 and plan["totals"]["targets"] == 0
    assert (plan["totals"]["sb_keywords"] + plan["totals"]["sb_targets"]
            == plan["totals"]["changing"])
    # **Both SB writers are exercised by the real data**, which is the point: targets and themes are
    # 51% of SB spend, so a keyword-only implementation would leave half of it unmanageable.
    assert plan["totals"]["sb_targets"] > 0, (
        "no SB target changed — product/category targets are 666 rows and Rs 45,854 of real spend"
    )
    # Pinned against the FULL 2,914-row report: the trimmed fixture was chosen to reproduce these
    # exactly, so a future trim that changes the answer fails here rather than passing quietly.
    assert plan["totals"]["changing"] == 611
    assert plan["totals"]["sb_keywords"] == 338
    assert plan["totals"]["sb_targets"] == 273


def test_sb_and_sp_ids_do_not_collide_in_the_real_data():
    """Verified live (0 overlaps across 500 SP and 4,888 SB ids), and re-checked from the fixtures so
    the assumption is pinned rather than remembered.

    It matters because a misroute would otherwise edit a real entity on the wrong product instead of
    failing.
    """
    sb_ids = {str(r.get("keywordId")) for r in _sb_fixture()}
    sp_path = Path(__file__).parent / "fixtures" / "ads_targeting_rows.json"
    sp_ids = {str(r.get("keywordId")) for r in json.loads(sp_path.read_text(encoding="utf-8"))}
    assert not (sb_ids & sp_ids), "SP and SB id spaces overlap — routing could edit the wrong entity"


def test_the_sb_page_size_respects_amazons_lower_cap():
    """**SB caps `maxResults` at 100 where SP allows 500, and it REFUSES rather than clamping.**

    Found by calling the real endpoint after every unit test was already green — a fake that echoes
    whatever it is sent could never have caught it, which is why this assertion exists as a constant
    check rather than a mocked call. Amazon names the limit in its own error:

        "rangeError": {"cause": {"location": "$.maxResults", "trigger": "500"},
                       "lowerLimit": "1", "upperLimit": "100",
                       "reason": "LIST_REQUEST_MAX_RESULTS_OUT_OF_RANGE"}

    Borrowing the SP pager would therefore have failed EVERY Sponsored Brands list call.
    """
    assert spapi_ads.SB_PAGE_SIZE == 100
    assert spapi_ads.PAGE_SIZE == 500, "the SP cap is different and must not be conflated"
    assert spapi_ads.SB_PAGE_SIZE < spapi_ads.PAGE_SIZE


def test_the_sb_lists_have_their_own_pager():
    """Two reasons SB cannot use `_list`: the 100-row cap above, and the paths already end in
    `/list` where `_list` appends it. 66 ad groups fit one page today; the pagination exists so a
    growing account does not silently truncate."""
    source = (Path(__file__).parent.parent / "app" / "ads" / "spapi_ads.py").read_text(
        encoding="utf-8")
    assert "async def _sb_list(" in source
    body = source[source.index("async def _sb_list("):]
    body = body[:body.index("async def fetch_sb_campaigns(")]
    assert "SB_PAGE_SIZE" in body, "the SB pager does not use the SB cap"
    assert "nextToken" in body, "the SB pager does not follow pagination"


def test_a_throttled_sb_report_says_what_it_means_and_is_isolated():
    """**A corrected assumption, recorded so it is not re-made.**

    I first measured "429 three times, then 200 after a 60-second pause" and concluded the throttle
    was a short burst limit — so the retry backed off for 10.5 minutes. On production the SB create
    then returned 429 through ALL of it, and still 429 after a further 15 minutes completely idle.
    Amazon sends no `Retry-After`. Report creation for `sbTargeting` is limited over a window of
    HOURS, counted across every report created that day.

    So a longer backoff is the wrong fix — it would hold a background job open for an hour to *maybe*
    succeed. The retry stays short for the genuine burst case, and the error says what it means
    instead of echoing Amazon's bare "Throttled".

    What makes that safe is the isolation: the SP figures are committed before the SB report is
    requested, so a throttled SB report leaves Sponsored Products entirely current. Verified on
    production: SP stored 12,213 window rows and 43,605 daily rows while SB reported 0 and an error.
    """
    source = (Path(__file__).parent.parent / "app" / "ads" / "reports.py").read_text(
        encoding="utf-8")
    assert reports.THROTTLE_ATTEMPTS <= 4, (
        "the retry is long enough to hold a background job open for a limit measured in hours"
    )
    assert "rate-limiting report creation" in source, (
        "a 429 surfaces Amazon's bare 'Throttled', which tells the owner nothing actionable"
    )

    refresh_source = (Path(__file__).parent.parent / "app" / "ads" / "refresh.py").read_text(
        encoding="utf-8")
    assert 'STATE["sb_error"]' in refresh_source, (
        "an SB failure is not isolated, so it would mark the whole refresh failed and hide the fact "
        "that the Sponsored Products figures are current"
    )
    # And the SP data must be COMMITTED before the SB report is attempted.
    #
    # The marker changed with the code: the SP figures used to be assigned once after a single
    # `fetch_targeting` returned, and are now written per chunk by `store_sp_chunk`, because a 60-day
    # window is two reports per product and one 429 used to discard up to 40 minutes of good data.
    # The REQUIREMENT is unchanged and is what this asserts — the SP store happens first, so a
    # Sponsored Brands throttle cannot cost the Sponsored Products figures too.
    assert refresh_source.index("async def store_sp_chunk(") < refresh_source.index(
        'ad_product="sb"'
    ), "the SB report runs before the SP figures are committed, so a throttle would cost both"
    assert refresh_source.index("on_chunk=store_sp_chunk") < refresh_source.index(
        'ad_product="sb"'
    ), "the SP report is not actually wired to store per chunk before SB is attempted"


# ─── Only ACTIVE targets are written ─────────────────────────────────────────


async def test_apply_skips_targets_that_are_paused_or_archived_at_amazon(auth_client, db,
                                                                        monkeypatch):
    """**The `spTargeting` report has NO state column**, so a plan cannot tell active from paused.

    Measured on the live account: 168 of 12,205 report rows (1.4%) are PAUSED or ARCHIVED, because
    Amazon reports whatever had activity in the WINDOW regardless of what it is now. Editing the bid
    of something that is not serving does nothing useful and makes the run's own count a lie.

    Checked at apply rather than at preview because the live-bid re-read already happens there and
    its response carries the state — no extra request, and the state is exactly as fresh as the bid.

    The skipped rows are reported SEPARATELY from `moved`: "someone changed the bid" and "this is not
    serving" are different facts and lead to different actions.
    """
    from app.ads import repository as ads_repo
    from app.ads import spapi_ads as sp

    # A DAILY row — `save_performance` and its per-window table are deleted, because holding the same
    # figures at two grains is what made Sponsored Brands vanish from whichever path lacked them.
    await ads_repo.save_daily(db, [
        {"keywordId": "111", "matchType": "EXACT", "keyword": "live one", "date": "2026-08-27",
         "cost": 500.0, "sales7d": 1000.0, "keywordBid": 10.0,
         "campaignId": "c1", "adGroupId": "g1"},
    ])

    sent: list[dict] = []

    async def fake_live(_client, changes):
        return {
            "111": {"bid": 10.0, "state": "ENABLED"},
            "222": {"bid": 8.0, "state": "PAUSED"},
            "333": {"bid": 5.0, "state": "ARCHIVED"},
        }

    async def fake_apply(_client, rows, *, writer, **_kwargs):
        sent.extend(rows)
        return [{"entity_id": str(r["entity_id"]), "ok": True, "error": None} for r in rows]

    monkeypatch.setattr(sp, "fetch_current_bids", fake_live)
    monkeypatch.setattr(sp, "apply_bids", fake_apply)

    from app.config import Settings, get_settings

    class _Settings(Settings):
        ads_client_id: str = "id"
        ads_client_secret: str = "secret"
        ads_refresh_token: str = "Atzr|test"
        ads_profile_id: str = "1"

    get_settings.cache_clear()
    monkeypatch.setattr("app.routers.ads.get_settings", lambda: _Settings())

    response = await auth_client.post("/ads/apply", json={
        "rule": "spend > 100 -> bid -10%",
        "changes": [
            {"entity_id": "111", "writer": "keyword", "text": "live one",
             "old_bid": 10.0, "new_bid": 9.0},
            {"entity_id": "222", "writer": "keyword", "text": "paused one",
             "old_bid": 8.0, "new_bid": 7.2},
            {"entity_id": "333", "writer": "keyword", "text": "archived one",
             "old_bid": 5.0, "new_bid": 4.5},
        ],
    })
    body = response.json()
    assert response.status_code == 200, body

    # ONLY the enabled row reached Amazon.
    assert [str(r["entity_id"]) for r in sent] == ["111"]
    assert body["applied"] == 1

    inactive = {r["entity_id"]: r for r in body["inactive"]}
    assert set(inactive) == {"222", "333"}
    assert inactive["222"]["live_state"] == "PAUSED"
    assert inactive["333"]["live_state"] == "ARCHIVED"
    # Each says WHY, so a shrunken count is explained rather than mysterious.
    assert "not serving" in inactive["222"]["reason"]

    # And the ledger holds only what was actually sent — a paused row must not appear as a change
    # that never happened, because that would corrupt the undo chain.
    runs = await ads_repo.load_runs(db)
    rows = await ads_repo.load_run(db, runs[0]["run_id"])
    assert [r["entity_id"] for r in rows] == ["111"]


async def test_the_row_limit_allows_the_rule_that_hit_it(auth_client, db):
    """**Raised from 500 to 1000 because a legitimate rule was blocked.**

    `spend > 100, roas < 2, -10%` matched 729 rows on the real account — real work, not a mistake —
    and the block forced it to be split by campaign for no safety gain. Every one of those rows is
    still previewed and individually ticked before anything is sent.

    1000 rather than higher: at 2000 the limit stops discriminating, because an account-wide
    `spend > 0` would fit under it.
    """
    assert logic.DEFAULT_GUARDRAILS["max_rows"] == 1000
    assert logic.DEFAULT_GUARDRAILS["max_rows"] >= 729, (
        "the limit still blocks the real 729-row rule that prompted raising it"
    )

    rows = [{"keywordId": str(i), "matchType": "EXACT", "cost": 500.0, "sales7d": 1000.0,
             "keywordBid": 10.0, "campaignId": "c1", "adGroupId": "g1"}
            for i in range(729)]
    plan = logic.plan_run(
        rows, conditions=[{"field": "spend", "op": "gt", "value": 100}],
        action=logic.ACTION_DECREASE_PCT, amount=10,
    )
    assert plan["blocked"] is None, "a 729-row rule is still blocked"
    assert plan["totals"]["changing"] == 729


# ─── The 406 on /sb/targets/list ──────────────────────────────────────────────


async def test_sb_targets_are_listed_with_no_media_type_headers_at_all(monkeypatch):
    """**"Listing Sponsored Brands targets failed on page 1: HTTP 406 Not Acceptable."**

    `/sb/targets/list` takes no vendor media type, which was expressed by threading `""` through
    `_media` — and `_media` set `Content-Type: ""` and `Accept: ""` from it. Amazon rejects literally
    empty headers with a 406, so **every** SB target fetch failed on its first page.

    "No type" is a distinct case from "this type", not a value to be defaulted, and the right answer is
    not the obvious one. Measured against the live endpoint:

        (neither header)                               200  <- what it wants
        application/json                               406  "No match for accept header"
        application/vnd.sbtargetingresource.v4+json    415  "Cannot consume content type"
        ... and three other plausible vnd spellings    415

    So this asserts the headers are ABSENT rather than empty. A fake client that ignores headers — as
    the other tests in this file use — cannot catch it, which is why the assertion is on what was sent.
    """
    from app.ads import spapi_ads as sp

    sent: list[dict] = []

    class Recorder:
        async def post(self, url, json=None, headers=None):
            sent.append({"url": url, "headers": dict(headers or {})})

            class R:
                status_code = 200

                @staticmethod
                def json():
                    return {"targets": [], "nextToken": None}
            return R()

    async def fake_token(_client):
        return "token"

    monkeypatch.setattr(sp, "_access_token", fake_token)
    await sp.fetch_sb_targets(Recorder())

    assert sent, "no request was made"
    headers = sent[0]["headers"]
    assert "Content-Type" not in headers, (
        f"Content-Type was sent as {headers.get('Content-Type')!r} — an empty value is a 406, and "
        f"this endpoint wants the header omitted entirely"
    )
    assert "Accept" not in headers, (
        f"Accept was sent as {headers.get('Accept')!r} — Amazon answers "
        f'{{"code":"406","details":"HTTP 406 Not Acceptable"}}'
    )
    # The auth headers must still be there — omitting the media type must not omit everything.
    assert headers.get("Authorization"), "the bearer token was dropped along with the media type"
    assert headers.get("Amazon-Advertising-API-ClientId")
    assert headers.get("Amazon-Advertising-API-Scope")


def test_an_empty_media_type_means_omit_rather_than_send_empty():
    """The unit behind the 406, asserted directly on `_media`.

    Pinned separately from the fetch because this helper is shared: SP campaigns, ad groups and the SB
    v4 lists all route through it, and a future endpoint that also wants no type will pass the same
    empty string.
    """
    from app.ads import spapi_ads as sp

    omitted = sp._media(sp.SB_NO_MEDIA_TYPE, "tok")
    assert "Content-Type" not in omitted and "Accept" not in omitted
    assert omitted.get("Authorization")

    # And a real type is still set on both, which is what the SP and SB v4 list endpoints need.
    typed = sp._media("application/vnd.sbcampaignresource.v4+json", "tok")
    assert typed["Content-Type"] == "application/vnd.sbcampaignresource.v4+json"
    assert typed["Accept"] == typed["Content-Type"]
