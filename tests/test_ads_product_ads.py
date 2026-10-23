"""Rules that turn ONE SKU off (or on) inside an ad group (23 Oct 2026).

Asked for as *"just like I was able to turn off or on a keyword I should be able to turn off on a
product in an ad group. product means sku. since one asin can have multiple sku's for flex, easy ship
and fba."* A SKU in an ad group is a Sponsored Products PRODUCT AD. Measured on production: 6,404 of
them, and 952 ad groups hold exactly ONE enabled ad — so turning the last one off silences every
keyword in the group, which the owner chose to refuse.
"""
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.ads import logic, refresh, repository, spapi_ads

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "templates" / "ads.html").read_text(encoding="utf-8")

PAUSE = dict(action=logic.ACTION_SET_STATE, amount="PAUSED", level=logic.LEVEL_PRODUCT)
SPEND = [{"field": "spend", "op": "gt", "value": 500}]


def _ad(ad_id, *, group="g1", sku="0.5kg cs 1", asin="B0CWGXYLT6", spend=900.0, sales=0.0,
        orders=0, clicks=40, campaign="SP keywords"):
    return {"ad_id": ad_id, "entity_id": ad_id, "campaign_id": "c1", "campaign_name": campaign,
            "ad_group_id": group, "ad_group_name": "chana sattu", "sku": sku, "asin": asin,
            "spend": spend, "sales": sales, "orders": orders, "clicks": clicks, "impressions": 900}


# ─── The SKU's channel ───────────────────────────────────────────────────────


@pytest.mark.parametrize("sku, channel", [
    ("0.5kg cs 1 FBA", "fba"),
    ("HF_CBchana_0.25kg_FBA", "fba"),          # Howrah: underscore-separated
    ("0.5kg cs 1 flex", "flex"),
    ("pea_isolate_sattu1kg flex", "flex"),
    ("PR_BP_0.2-FLEX", "flex"),
    ("0.5kg cs 1", "easy_ship"),
    ("fbagel 1kg", "easy_ship"),                # a TOKEN test, never a substring test
    ("flexseed 1kg", "easy_ship"),
    ("", "easy_ship"),
])
def test_the_sku_channel_is_read_off_its_last_token(sku, channel):
    assert logic.sku_channel(sku) == channel


def test_two_skus_of_one_asin_in_one_ad_group_are_told_apart():
    rows = [_ad("A1", sku="0.5kg cs 1"), _ad("A2", sku="0.5kg cs 1 flex"), _ad("A3", sku="0.5kg cs 1 FBA")]
    plan = logic.plan_run(rows, conditions=SPEND + [{"field": "channel", "op": "is", "value": "easy_ship"}],
                          enabled_ads={"g1": {"A1", "A2", "A3"}}, **PAUSE)
    assert [c["entity_id"] for c in plan["changes"]] == ["A1"]
    plan = logic.plan_run(rows, conditions=SPEND + [{"field": "channel", "op": "is_not", "value": "fba"}],
                          enabled_ads={"g1": {"A1", "A2", "A3"}}, **PAUSE)
    assert sorted(c["entity_id"] for c in plan["changes"]) == ["A1", "A2"]


def test_sku_or_asin_contains_is_case_insensitive_text():
    rows = [_ad("A1", sku="0.5kg cs 1"), _ad("A2", sku="1kg cs 1", asin="B0OTHER001")]
    cond = SPEND + [{"field": "sku_asin", "op": "contains", "value": "0.5KG"}]
    plan = logic.plan_run(rows, conditions=cond, enabled_ads={"g1": {"A1", "A2"}}, **PAUSE)
    assert [c["entity_id"] for c in plan["changes"]] == ["A1"]
    cond = SPEND + [{"field": "sku_asin", "op": "not_contains", "value": "b0other"}]
    plan = logic.plan_run(rows, conditions=cond, enabled_ads={"g1": {"A1", "A2"}}, **PAUSE)
    assert [c["entity_id"] for c in plan["changes"]] == ["A1"]


# ─── The last active SKU ─────────────────────────────────────────────────────


def test_the_last_active_sku_keeps_the_best_seller_on():
    rows = [_ad("A1", sales=100.0), _ad("A2", sales=300.0)]
    refused = logic.last_active_refusals(
        [logic.product_metrics(r) for r in rows], {"g1": {"A1", "A2"}})
    assert refused == {"A2"}


def test_a_group_with_another_enabled_sku_loses_nothing():
    rows = [logic.product_metrics(_ad("A1")), logic.product_metrics(_ad("A2"))]
    assert logic.last_active_refusals(rows, {"g1": {"A1", "A2", "A3"}}) == set()


def test_an_ad_group_with_unknown_siblings_keeps_one_on():
    rows = [logic.product_metrics(_ad("A1")), logic.product_metrics(_ad("A2", sales=5.0))]
    assert logic.last_active_refusals(rows, {}) == {"A2"}


def test_a_matched_sku_that_is_already_paused_does_not_count_as_the_one_left():
    # A2 is paused already; pausing A1 would leave nothing enabled, so A1 is refused.
    rows = [logic.product_metrics(_ad("A1")), logic.product_metrics(_ad("A2"))]
    assert logic.last_active_refusals(rows, {"g1": {"A1"}}) == {"A1"}


def test_the_plan_lists_the_last_active_sku_as_skipped_with_its_reason():
    plan = logic.plan_run([_ad("A1", sales=10.0), _ad("A2")], conditions=SPEND,
                          enabled_ads={"g1": {"A1", "A2"}}, **PAUSE)
    assert [c["entity_id"] for c in plan["changes"]] == ["A2"]
    assert plan["approved_ids"] == ["A2"]
    assert [(s["entity_id"], s["reason"]) for s in plan["skipped"]] == [("A1", logic.SKIP_LAST_ACTIVE)]
    assert plan["totals"]["last_active"] == 1 and plan["totals"]["product_ads"] == 1


def test_turning_skus_ON_needs_no_last_active_check():
    plan = logic.plan_run([_ad("A1")], conditions=SPEND, enabled_ads={"g1": set()},
                          action=logic.ACTION_SET_STATE, amount="ENABLED", level=logic.LEVEL_PRODUCT)
    assert [c["entity_id"] for c in plan["changes"]] == ["A1"]


def test_a_product_rule_cannot_change_a_bid_and_a_keyword_rule_has_no_sku():
    plan = logic.plan_run([_ad("A1")], conditions=SPEND, action=logic.ACTION_DECREASE_PCT,
                          amount=10, level=logic.LEVEL_PRODUCT)
    assert plan["blocked"] and "no bid" in plan["blocked"]
    assert logic.condition_error({"field": "channel", "op": "is", "value": "fba"}) is not None
    assert logic.condition_error({"field": "bid", "op": "gt", "value": 1}, logic.LEVEL_PRODUCT)
    assert logic.condition_error({"field": "channel", "op": "is", "value": "amazon"},
                                 logic.LEVEL_PRODUCT)


def test_m19_campaigns_and_the_pause_floor_still_apply_to_skus():
    rows = [_ad("A1", campaign="SP - m19 autopilot - x"), _ad("A2", spend=50.0)]
    plan = logic.plan_run(rows, conditions=[{"field": "spend", "op": "gt", "value": 10}],
                          enabled_ads={"g1": {"A1", "A2", "A3"}}, **PAUSE)
    reasons = {s["entity_id"]: s["reason"] for s in plan["skipped"]}
    assert reasons == {"A1": logic.SKIP_AUTOMATED, "A2": logic.SKIP_BELOW_PAUSE_SPEND}


# ─── Storage: a separate table, never the keyword rows ───────────────────────


def _report_rows(day, *ads):
    return [{"date": day, "adId": a, "campaignId": "c1", "adGroupId": "g1", "advertisedAsin": "B0CWGXYLT6",
             "advertisedSku": "0.5kg cs 1", "cost": 100.0, "sales7d": 0.0, "purchases7d": 0,
             "clicks": 5, "impressions": 50} for a in ads]


async def test_product_rows_never_reach_the_keyword_totals(db):
    await repository.save_product_daily(db, _report_rows("2026-10-20", "A1", "A2"))
    assert await repository.sum_daily(db, "2026-10-20", "2026-10-20") == []
    assert await repository.daily_days_held(db) == set()
    summed = await repository.sum_product_daily(db, "2026-10-20", "2026-10-20")
    assert sorted(r["ad_id"] for r in summed) == ["A1", "A2"] and summed[0]["spend"] == 100.0


async def test_a_day_split_in_the_report_is_summed_and_a_refetch_replaces_it(db):
    await repository.save_product_daily(db, _report_rows("2026-10-20", "A1", "A1"))
    assert (await repository.sum_product_daily(db, "2026-10-20", "2026-10-20"))[0]["spend"] == 200.0
    await repository.save_product_daily(db, _report_rows("2026-10-20", "A1"))
    assert (await repository.sum_product_daily(db, "2026-10-20", "2026-10-20"))[0]["spend"] == 100.0


async def test_product_completeness_is_per_day(db):
    await repository.save_product_daily(db, _report_rows("2026-10-20", "A1") + _report_rows("2026-10-22", "A1"))
    answer = await repository.product_range_completeness(db, "2026-10-20", "2026-10-22")
    assert answer["complete"] is False and answer["missing"]["product"] == ["2026-10-21"]
    assert (await repository.product_range_completeness(db, "2026-10-20", "2026-10-20"))["complete"]


async def test_the_purge_keeps_product_rows_for_the_same_60_days(db):
    today = date(2026, 10, 23)
    old = (today - timedelta(days=repository.DAILY_RETENTION_DAYS)).isoformat()
    await repository.save_product_daily(db, _report_rows(old, "A1") + _report_rows("2026-10-22", "A1"))
    await repository.purge_daily(db, today=today)
    assert await repository.product_days_held(db) == {"2026-10-22"}


async def test_the_cached_list_is_overridden_by_a_pause_we_applied_since(db):
    from datetime import datetime

    from app.models import AdsMutation
    await repository.replace_product_ads(db, [
        {"entity_id": "A1", "parent_id": "g1", "state": "ENABLED"},
        {"entity_id": "A2", "parent_id": "g1", "state": "ENABLED"},
        {"entity_id": "A3", "parent_id": "g1", "state": "PAUSED"},
    ])
    db.add(AdsMutation(run_id="r1", entity_id="A2", entity_type="product_ad", writer="product_ad",
                       action="state", old_state="ENABLED", new_state="PAUSED", status="applied",
                       rule_summary="x", created_at=datetime.utcnow() + timedelta(minutes=1)))
    await db.commit()
    assert await repository.enabled_product_ads(db, ["g1"]) == {"g1": {"A1"}}
    # An empty fetch must not wipe the list the guard reads.
    assert await repository.replace_product_ads(db, []) == 0
    assert await repository.enabled_product_ads(db, ["g1"]) == {"g1": {"A1"}}


# ─── The routes ──────────────────────────────────────────────────────────────


def _days(n=7):
    end = date.today() - timedelta(days=1)
    return [(end - timedelta(days=i)).isoformat() for i in range(n)][::-1]


async def _seed(db, ads=("A1", "A2")):
    for day in _days():
        await repository.save_product_daily(db, _report_rows(day, *ads))
    await repository.replace_product_ads(db, [{"entity_id": a, "parent_id": "g1", "state": "ENABLED"}
                                              for a in ads])


async def test_the_preview_of_a_sku_rule_holds_back_the_last_active_one(auth_client, db):
    await _seed(db)
    days = _days()
    r = await auth_client.post("/ads/preview", json={
        "start": days[0], "end": days[-1], "level": "product", "action": "set_state",
        "amount": "PAUSED", "conditions": SPEND})
    body = r.json()
    assert r.status_code == 200, body
    assert body["rule"].startswith("Products: ")
    assert len(body["changes"]) == 1 and body["changes"][0]["writer"] == logic.WRITER_PRODUCT_AD
    assert [s["reason"] for s in body["skipped"]] == [logic.SKIP_LAST_ACTIVE]


async def test_a_sku_preview_names_the_missing_product_days(auth_client, db):
    days = _days()
    r = await auth_client.post("/ads/preview", json={
        "start": days[0], "end": days[-1], "level": "product", "action": "set_state",
        "amount": "PAUSED", "conditions": SPEND})
    assert r.status_code == 400 and "product-ad" in r.json()["error"]


@pytest.fixture
def amazon(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ads_endpoint", "https://ads.test", raising=False)
    sent = []
    live_states = {"A1": "ENABLED", "A2": "ENABLED", "A3": "PAUSED"}

    async def live(_client, changes):
        return {a: {"bid": None, "state": s, "ad_group_id": "g1", "writer": logic.WRITER_PRODUCT_AD}
                for a, s in live_states.items()}

    async def push(_client, rows, *, writer):
        sent.extend((writer, r["entity_id"], r.get("new_state")) for r in rows)
        return [{"entity_id": r["entity_id"], "ok": True} for r in rows]

    monkeypatch.setattr(spapi_ads, "fetch_current_bids", live)
    monkeypatch.setattr(spapi_ads, "apply_changes", push)
    return sent, live_states


def _change(ad_id, **extra):
    return {"entity_id": ad_id, "writer": logic.WRITER_PRODUCT_AD, "ad_product": "sp",
            "text": "0.5kg cs 1", "ad_group_id": "g1", "campaign_id": "c1",
            "new_state": "PAUSED", "spend": 900.0, "sales": 0.0, **extra}


async def test_apply_checks_the_last_active_sku_again_on_live_states(auth_client, amazon):
    sent, _ = amazon
    # Exactly what the page posts: {changes, rule}.
    body = (await auth_client.post("/ads/apply", json={
        "rule": "Products: Spend > 500 -> turn OFF (pause)",
        "changes": [_change("A1"), _change("A2", sales=50.0)]})).json()
    assert body["applied"] == 1, body
    assert [c["entity_id"] for c in body["last_active"]] == ["A2"]
    assert sent == [(logic.WRITER_PRODUCT_AD, "A1", "PAUSED")]


async def test_apply_lets_both_go_when_the_group_keeps_another_enabled_sku(auth_client, amazon):
    sent, live_states = amazon
    live_states["A3"] = "ENABLED"
    body = (await auth_client.post("/ads/apply", json={
        "rule": "Products: x -> turn OFF (pause)", "changes": [_change("A1"), _change("A2")]})).json()
    assert body["applied"] == 2 and body["last_active"] == [], body


async def test_apply_refuses_skus_mixed_with_keywords_or_given_a_bid(auth_client, amazon):
    keyword = {"entity_id": "K1", "writer": logic.WRITER_KEYWORD, "new_state": "PAUSED"}
    r = await auth_client.post("/ads/apply", json={"rule": "x", "changes": [_change("A1"), keyword]})
    assert r.status_code == 400 and "mixes" in r.json()["error"]
    bid = {**_change("A1"), "new_state": None, "new_bid": 5.0}
    r = await auth_client.post("/ads/apply", json={"rule": "x", "changes": [bid]})
    assert r.status_code == 400 and "no bid" in r.json()["error"]
    assert amazon[0] == []


async def test_the_product_ad_write_is_the_sp_shape_keyed_by_adId():
    class Response:
        status_code = 207

        def json(self):
            return {"productAds": {"success": [{"index": 0, "adId": "A1"}],
                                   "error": [{"index": 1, "errors": [{"errorType": "x"}]}]}}

    class Client:
        calls = []

        async def put(self, url, json, headers):
            self.calls.append((url, json, headers))
            return Response()

    async def token(_client):
        return "t"

    import app.ads.spapi_ads as module
    original = module._access_token
    module._access_token = token
    try:
        client = Client()
        results = await spapi_ads.apply_changes(
            client, [_change("A1"), _change("A2")], writer=logic.WRITER_PRODUCT_AD)
    finally:
        module._access_token = original
    url, payload, headers = client.calls[0]
    assert url.endswith("/sp/productAds")
    assert payload == {"productAds": [{"adId": "A1", "state": "PAUSED"}, {"adId": "A2", "state": "PAUSED"}]}
    assert headers["Content-Type"] == "application/vnd.spProductAd.v3+json"
    assert [(r["entity_id"], r["ok"]) for r in results] == [("A1", True), ("A2", False)]


async def test_a_paused_sku_is_undone_as_an_enable(db):
    run = await repository.open_run(db, [{**_change("A1"), "old_state": "ENABLED"}], rule_summary="x")
    await repository.record_results(db, run, [{"entity_id": "A1", "ok": True}])
    undo = await repository.build_undo(db, run)
    assert undo == [{**undo[0], "writer": logic.WRITER_PRODUCT_AD, "new_state": "ENABLED"}]


# ─── Rule identity and saved rules ───────────────────────────────────────────


def test_the_same_numbers_at_the_two_levels_are_two_different_rules():
    from app.routers.ads import _rule_summary
    keyword = _rule_summary(SPEND, logic.ACTION_SET_STATE, "PAUSED")
    product = _rule_summary(SPEND, logic.ACTION_SET_STATE, "PAUSED", level=logic.LEVEL_PRODUCT)
    assert keyword == "Spend > 500 -> turn OFF (pause)"       # unchanged, so old ledger rows still match
    assert product == "Products: Spend > 500 -> turn OFF (pause)"
    words = _rule_summary(SPEND + [{"field": "channel", "op": "is", "value": "easy_ship"},
                                   {"field": "sku_asin", "op": "contains", "value": "0.5kg"}],
                          logic.ACTION_SET_STATE, "PAUSED", level=logic.LEVEL_PRODUCT)
    assert 'SKU channel is Easy Ship' in words and 'SKU or ASIN contains "0.5kg"' in words


async def test_a_saved_sku_rule_keeps_its_level_and_cannot_hold_a_bid(auth_client):
    rule = {"name": "easy ship off", "conditions": SPEND + [
        {"field": "channel", "op": "is", "value": "easy_ship"}],
        "action": "set_state", "amount": "PAUSED", "level": "product"}
    assert (await auth_client.post("/ads/rules", json=rule)).status_code == 200
    rules = (await auth_client.get("/ads?days=7")).json()["rules"]
    assert rules[0]["level"] == "product"
    bad = {**rule, "name": "x", "action": "decrease_pct", "amount": 10}
    assert (await auth_client.post("/ads/rules", json=bad)).status_code == 400


# ─── The refresh ─────────────────────────────────────────────────────────────


async def test_the_refresh_stores_product_ads_and_a_failure_costs_nothing_else(monkeypatch, db_schema):
    from app.ads import reports
    from app.portfolio.ads import AdsError

    class S:
        ads_configured = True
        ads_timeout = 5

    monkeypatch.setattr(refresh, "get_settings", lambda: S())
    refresh.reset_state()

    async def nothing(*a, **kw):
        return []

    for name in ("fetch_campaigns", "fetch_sb_campaigns", "fetch_ad_groups", "fetch_sb_ad_groups"):
        monkeypatch.setattr(spapi_ads, name, nothing)

    async def product_ads(client, **kw):
        return [{"entity_id": "A1", "parent_id": "g1", "state": "ENABLED"}]

    monkeypatch.setattr(spapi_ads, "fetch_product_ads", product_ads)
    day = (date.today() - timedelta(days=1)).isoformat()
    fail = {"product": False}

    async def fetch_targeting(start, end, *, report="targeting", on_chunk=None, **kw):
        if report == "product":
            if fail["product"]:
                raise AdsError("throttled")
            await on_chunk(_report_rows(day, "A1"), start, end)
        return []

    monkeypatch.setattr(reports, "fetch_targeting", fetch_targeting)
    result = await refresh.run(days=1)
    assert result["phase"] == "done" and result["product_rows"] == 1 and result["product_ads"] == 1

    fail["product"] = True
    refresh.reset_state()
    result = await refresh.run(days=2)          # a day the product table lacks
    assert result["phase"] == "done" and "throttled" in (result["product_error"] or "")
    refresh.reset_state()


# ─── The page ────────────────────────────────────────────────────────────────


def test_the_page_offers_the_level_and_posts_it():
    assert 'data-level="keyword"' in PAGE and 'data-level="product"' in PAGE
    assert "level: level," in PAGE                               # the preview request
    assert "window_days: 7, level," in PAGE                      # the saved rule
    assert 'row.writer === "product_ad"' in PAGE                 # channel and ASIN cells
    assert "body.last_active" in PAGE


def test_the_on_off_choice_follows_the_action_shown_even_without_a_change_event():
    """Reported as "turn the keyword 10%" on both levels: after a reload the browser put the
    previous action back WITHOUT firing `change`, so the number box stayed up and the ON/OFF choice
    stayed hidden. The controls are now synced on every render, through `renderLevel`."""
    import re
    level = re.search(r"function renderLevel\(\)\{[\s\S]*?\n\}", PAGE).group(0)
    assert "syncActionControls();" in level
    sync = re.search(r"function syncActionControls\(\)\{[\s\S]*?\n\}", PAGE).group(0)
    assert '$("state-amount").style.display = state ? "" : "none";' in sync
    assert '$("action").addEventListener("change", syncActionControls);' in PAGE
    render = re.search(r"function render\(\)\{[\s\S]*?\n\}", PAGE).group(0)
    assert "renderLevel();" in render
