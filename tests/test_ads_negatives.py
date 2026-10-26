"""Ads → Optimisation → Search terms: overspending search terms become ad-group negatives (25 Oct 2026).

Asked for as *"negative targeting based on search terms not just targets added… in auto campaigns there
is close match, loose match… some of the keywords that it targets might be overspending… add that
keyword as a negative target… negative exact only… for the asins there is a separate negative"*, with
Grow / Cut rule columns and a rule greyed once it ran today **on the window on screen**.

Measured first: of 413 terms that spent ₹200+ with no order in 30 days, 301 were the target itself
(an exact keyword's own words, an ASIN target's own ASIN). Those are skipped and named, never negated.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import ist
from app.ads import logic, negatives, repository, search_store, spapi_ads

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "templates" / "ads.html").read_text(encoding="utf-8")

GUARD = {"min_negative_spend": 100, "max_rows": 1000}
SPEND_NO_ORDERS = [{"field": "spend", "op": "gt", "value": 200}, {"field": "orders", "op": "eq", "value": 0}]


def _row(term, spend=300.0, orders=0, sales=0.0, group="g1", campaign="c1", product="sp",
         cname="MF_SP_auto", via=None):
    return {"ad_product": product, "campaign_id": campaign, "ad_group_id": group, "campaign_name": cname,
            "ad_group_name": "chana", "search_term": term, "impressions": 900, "clicks": 40,
            "spend": spend, "orders": orders, "sales": sales,
            "via": via or [{"keyword_id": "k1", "text": "close-match", "match_type":
                            "TARGETING_EXPRESSION_PREDEFINED", "targeting": "close-match", "spend": spend}]}


def _plan(rows, *, positives=None, existing=(), guard=GUARD, conditions=SPEND_NO_ORDERS):
    return negatives.plan(rows, conditions, guardrails=guard, campaign_names={},
                          positives=positives or {}, negatives=set(existing))


# ─── The planner ─────────────────────────────────────────────────────────────


def test_a_word_term_becomes_a_negative_EXACT_and_an_asin_a_negative_PRODUCT():
    plan = _plan([_row("sattu powder"), _row("b0cwgxylt6", spend=250)])
    by = {c["search_term"]: c for c in plan["changes"]}
    assert (by["sattu powder"]["kind"], by["sattu powder"]["writer"]) == ("keyword", negatives.W_SP_KEYWORD)
    assert (by["b0cwgxylt6"]["kind"], by["b0cwgxylt6"]["writer"]) == ("product", negatives.W_SP_PRODUCT)
    assert by["sattu powder"]["kind_label"] == "negative exact"
    sb = _plan([_row("sattu", product="sb"), _row("b0cwgxylt6", product="sb")])["changes"]
    assert {c["writer"] for c in sb} == {negatives.W_SB_KEYWORD, negatives.W_SB_PRODUCT}
    assert all(c["no_undo"] for c in sb)


def test_a_term_that_IS_the_ad_groups_own_keyword_or_asin_target_is_skipped_and_named():
    plan = _plan([_row("chana sattu"), _row("b0aaaaaaaa"), _row("chana sattu 1kg")],
                 positives={"g1": {"chana sattu", "b0aaaaaaaa"}})
    assert [c["search_term"] for c in plan["changes"]] == ["chana sattu 1kg"]
    assert {s["search_term"]: s["reason"] for s in plan["skipped"]} == {
        "chana sattu": negatives.SKIP_IS_TARGET, "b0aaaaaaaa": negatives.SKIP_IS_TARGET}
    # The positives are PER AD GROUP: the same words in another group are a real candidate.
    other = _plan([_row("chana sattu", group="g2")], positives={"g1": {"chana sattu"}})
    assert [c["ad_group_id"] for c in other["changes"]] == ["g2"]


def test_a_term_already_negated_in_its_ad_group_is_skipped_and_the_type_matters():
    existing = {("g1", "neg_keyword", "sattu"), ("g1", "neg_keyword", "b0bbbbbbbb")}
    plan = _plan([_row("sattu"), _row("b0bbbbbbbb")], existing=existing)
    # A negative KEYWORD with ASIN-looking text does not stop the ASIN being a product target.
    assert [c["search_term"] for c in plan["changes"]] == ["b0bbbbbbbb"]
    assert plan["skipped"][0]["reason"] == negatives.SKIP_ALREADY


def test_a_campaign_m19_or_amazon_manages_is_skipped():
    plan = _plan([_row("sattu", cname="m19 autopilot chana"), _row("ragi", cname="Adaptive Campaign x")])
    assert plan["changes"] == [] and {s["reason"] for s in plan["skipped"]} == {negatives.SKIP_MANAGED}


def test_the_spend_floor_guards_a_mistyped_rule_and_the_row_limit_blocks_the_run():
    loose = [{"field": "spend", "op": "gt", "value": 10}]
    plan = _plan([_row("sattu", spend=50), _row("ragi", spend=150)], conditions=loose)
    assert [c["search_term"] for c in plan["changes"]] == ["ragi"]
    assert plan["skipped"][0]["reason"] == negatives.SKIP_LOW_SPEND
    blocked = _plan([_row(f"t{i}") for i in range(3)], guard={**GUARD, "max_rows": 2})
    assert blocked["blocked"] and blocked["approved_ids"] == []


def test_an_ordering_term_is_not_matched_and_matched_counts_only_rule_hits():
    plan = _plan([_row("sattu"), _row("ragi", orders=2, sales=900)])
    assert plan["matched"] == 1 and [c["search_term"] for c in plan["changes"]] == ["sattu"]


def test_via_names_how_each_target_brought_the_term_in():
    assert negatives.via_of({"match_type": "TARGETING_EXPRESSION_PREDEFINED", "targeting": "loose-match"}) == "loose-match"
    assert negatives.via_of({"match_type": "BROAD", "text": "sattu"}) == "broad"
    assert negatives.via_of({"match_type": "TARGETING_EXPRESSION", "targeting": 'asin="B0AAAAAAAA"'}) == "product"
    assert negatives.via_of({"match_type": "TARGETING_EXPRESSION", "targeting": 'category="123"'}) == "category"
    # The target that spent MOST decides the rule field.
    m = negatives.metrics(_row("x", via=[
        {"match_type": "EXACT", "text": "x", "spend": 5},
        {"match_type": "TARGETING_EXPRESSION_PREDEFINED", "targeting": "substitutes", "spend": 90}]))
    assert m["via"] == "substitutes" and m["term_kind"] == "word"


def test_rule_impact_files_each_action_under_grow_or_cut():
    assert logic.rule_impact(logic.ACTION_NEGATE) == logic.IMPACT_CUT
    assert logic.rule_impact(logic.ACTION_DECREASE_PCT) == logic.IMPACT_CUT
    assert logic.rule_impact(logic.ACTION_SET_STATE, "PAUSED") == logic.IMPACT_CUT
    assert logic.rule_impact(logic.ACTION_SET_STATE, "ENABLED") == logic.IMPACT_GROW
    assert logic.rule_impact(logic.ACTION_INCREASE_PCT) == logic.IMPACT_GROW


# ─── The four Amazon writers ─────────────────────────────────────────────────


@pytest.fixture
def wire(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ads_endpoint", "https://ads.test", raising=False)

    async def token(client):
        return "t"
    monkeypatch.setattr(spapi_ads, "_access_token", token)
    sent = []

    class Response:
        def __init__(self, status, body):
            self.status_code, self._body, self.text = status, body, ""

        def json(self):
            return self._body

    class Client:
        def __init__(self, status, body):
            self.status, self.body = status, body

        async def post(self, url, json, headers):
            sent.append({"url": url, "json": json, "headers": headers})
            return Response(self.status, self.body)

    return sent, Client


ROWS = [{"entity_id": "neg-a", "campaign_id": "111", "ad_group_id": "222", "search_term": "sattu"},
        {"entity_id": "neg-b", "campaign_id": "111", "ad_group_id": "222", "search_term": "b0aaaaaaaa"}]


async def test_sp_negative_keyword_is_NEGATIVE_EXACT_with_its_media_type(wire):
    sent, Client = wire
    out = await spapi_ads.create_negatives(Client(207, {"negativeKeywords": {
        "success": [{"index": 0, "negativeKeywordId": "9"}], "error": [{"index": 1, "errors": [
            {"errorType": "x", "errorValue": {"x": {"message": "duplicate"}}}]}]}}),
        ROWS, writer=negatives.W_SP_KEYWORD)
    assert sent[0]["url"] == "https://ads.test/sp/negativeKeywords"
    assert sent[0]["headers"]["Content-Type"] == "application/vnd.spNegativeKeyword.v3+json"
    assert sent[0]["json"]["negativeKeywords"][0] == {
        "campaignId": "111", "adGroupId": "222", "keywordText": "sattu", "matchType": "NEGATIVE_EXACT",
        "state": "ENABLED"}
    assert [(r["entity_id"], r["ok"], r["new_id"]) for r in out] == [("neg-a", True, "9"), ("neg-b", False, None)]


async def test_sp_negative_product_is_ASIN_SAME_AS_upper_case(wire):
    sent, Client = wire
    out = await spapi_ads.create_negatives(Client(207, {"negativeTargetingClauses": {
        "success": [{"index": 0, "targetId": "77"}], "error": []}}), ROWS[1:], writer=negatives.W_SP_PRODUCT)
    assert sent[0]["url"] == "https://ads.test/sp/negativeTargets"
    assert sent[0]["headers"]["Content-Type"] == "application/vnd.spNegativeTargetingClause.v3+json"
    assert sent[0]["json"]["negativeTargetingClauses"][0]["expression"] == [
        {"type": "ASIN_SAME_AS", "value": "B0AAAAAAAA"}]
    assert out[0]["new_id"] == "77"


async def test_sb_negative_keyword_is_a_BARE_LIST_and_its_answer_too(wire):
    sent, Client = wire
    out = await spapi_ads.create_negatives(Client(207, [{"code": "SUCCESS", "keywordId": 5}]),
                                           ROWS[:1], writer=negatives.W_SB_KEYWORD)
    assert sent[0]["url"] == "https://ads.test/sb/negativeKeywords"
    assert sent[0]["json"] == [{"campaignId": 111, "adGroupId": 222, "keywordText": "sattu",
                                "matchType": "negativeExact"}]
    assert out == [{"entity_id": "neg-a", "ok": True, "new_id": "5", "error": None}]


async def test_sb_negative_product_is_a_200_with_the_refusals_INSIDE(wire):
    sent, Client = wire
    out = await spapi_ads.create_negatives(Client(200, {
        "createTargetSuccessResults": [],
        "createTargetErrorResults": [{"targetRequestIndex": 0, "code": "DUPLICATE", "details": "exists"}]}),
        ROWS[1:], writer=negatives.W_SB_PRODUCT)
    assert sent[0]["json"] == {"negativeTargets": [{"campaignId": 111, "adGroupId": 222, "expressions": [
        {"type": "asinSameAs", "value": "B0AAAAAAAA"}]}]}
    assert out[0]["ok"] is False and out[0]["error"] == "exists"


async def test_a_row_amazon_does_not_mention_is_FAILED(wire):
    _, Client = wire
    out = await spapi_ads.create_negatives(Client(207, {"negativeKeywords": {"success": [], "error": []}}),
                                           ROWS[:1], writer=negatives.W_SP_KEYWORD)
    assert out[0]["ok"] is False


async def test_undo_removes_an_SP_negative_and_refuses_an_SB_one(wire):
    sent, Client = wire
    out = await spapi_ads.delete_negatives(Client(207, {"negativeKeywords": {
        "success": [{"index": 0, "negativeKeywordId": "9"}], "error": []}}), ["9"], writer=negatives.W_SP_KEYWORD)
    assert sent[0]["url"] == "https://ads.test/sp/negativeKeywords/delete"
    assert sent[0]["json"] == {"negativeKeywordIdFilter": {"include": ["9"]}}
    assert out == [{"entity_id": "9", "ok": True, "error": None}]
    sb = await spapi_ads.delete_negatives(Client(207, {}), ["5"], writer=negatives.W_SB_KEYWORD)
    assert sb[0]["ok"] is False and len(sent) == 1


# ─── The ledger, undo and "ran today" ────────────────────────────────────────


def _change(key, writer=negatives.W_SP_KEYWORD, product="sp"):
    return {"entity_id": negatives.provisional_id(key), "negate": True, "writer": writer,
            "ad_product": product, "text": key, "search_term": key, "campaign_id": "c1",
            "ad_group_id": "g1", "new_state": "ENABLED", "key": key}


async def test_a_negate_run_is_ledgered_and_its_undo_removes_only_the_sp_ones(db):
    changes = [_change("a"), _change("b", negatives.W_SB_KEYWORD, "sb"), _change("c")]
    run = await repository.open_run(db, changes, rule_summary="Search terms: x -> add as negative",
                                    rule_window="2026-10-01..2026-10-07")
    rows = await repository.load_run(db, run)
    assert {r["action"] for r in rows} == {"negate"} and {r["status"] for r in rows} == {"pending"}
    await repository.record_create_results(db, run, [
        {"entity_id": changes[0]["entity_id"], "ok": True, "ad_id": "N1"},
        {"entity_id": changes[1]["entity_id"], "ok": True, "ad_id": "N2"},
        {"entity_id": changes[2]["entity_id"], "ok": False, "ad_id": None, "error": "dup"}])
    undo = await repository.build_undo(db, run)
    assert [(u["entity_id"], u["writer"], u.get("negate_undo")) for u in undo] == [
        ("N1", negatives.W_SP_KEYWORD, True)]


async def test_ran_today_is_per_rule_AND_per_window(db):
    sentence = "Search terms: spend > 200 -> add as negative"
    run = await repository.open_run(db, [_change("a")], rule_summary=sentence,
                                    rule_window="2026-10-01..2026-10-07")
    await repository.record_create_results(db, run, [
        {"entity_id": negatives.provisional_id("a"), "ok": True, "ad_id": "N1"}])
    ran = await repository.ran_today_by_rule(db, logic.ist_day(datetime.utcnow()))
    assert set(ran[sentence]) == {"2026-10-01..2026-10-07"}
    # Yesterday's run does not grey today.
    assert sentence not in await repository.ran_today_by_rule(
        db, (datetime.fromisoformat(logic.ist_day(datetime.utcnow())) - timedelta(days=1)).date().isoformat())


async def test_a_negate_action_belongs_only_to_a_search_rule(db):
    with pytest.raises(ValueError):
        await repository.save_rule(db, "x", {"conditions": SPEND_NO_ORDERS, "action": "negate",
                                        "amount": None, "level": "keyword"})
    with pytest.raises(ValueError):
        await repository.save_rule(db, "y", {"conditions": SPEND_NO_ORDERS,
                                        "action": "decrease_pct", "amount": 10, "level": "search"})


# ─── The stored search terms ─────────────────────────────────────────────────


async def test_search_terms_sum_per_ad_group_with_every_target_that_brought_them(db):
    raw = [{"date": "2026-10-01", "campaignId": "c1", "adGroupId": "g1", "searchTerm": "Sattu",
            "keywordId": "k1", "keyword": "close-match", "matchType": "TARGETING_EXPRESSION_PREDEFINED",
            "targeting": "close-match", "cost": 100, "clicks": 5, "purchases7d": 0, "sales7d": 0},
           {"date": "2026-10-02", "campaignId": "c1", "adGroupId": "g1", "searchTerm": "sattu",
            "keywordId": "k1", "keyword": "close-match", "matchType": "TARGETING_EXPRESSION_PREDEFINED",
            "targeting": "close-match", "cost": 150, "clicks": 7, "purchases7d": 1, "sales7d": 80},
           {"date": "2026-10-02", "campaignId": "c1", "adGroupId": "g1", "searchTerm": "sattu",
            "keywordId": "k2", "keyword": "sattu", "matchType": "BROAD", "cost": 20, "clicks": 1}]
    assert await search_store.save_search_term_daily(db, raw, ad_product="sp") == 3
    rows = await search_store.sum_search_terms(db, "2026-10-01", "2026-10-02")
    assert len(rows) == 1
    row = rows[0]
    assert (row["search_term"], row["spend"], row["clicks"], row["orders"]) == ("sattu", 270.0, 13, 1)
    assert sorted(v["spend"] for v in row["via"]) == [20.0, 250.0]
    # A re-store of a day REPLACES it rather than doubling.
    await search_store.save_search_term_daily(db, raw[:1], ad_product="sp")
    assert (await search_store.sum_search_terms(db, "2026-10-01", "2026-10-01"))[0]["spend"] == 100.0


async def test_an_empty_negatives_fetch_writes_nothing(db):
    await search_store.replace_negatives(db, [{"entity_type": "neg_keyword", "entity_id": "1",
                                               "parent_id": "g1", "name": "Sattu"}])
    assert await search_store.replace_negatives(db, []) == 0
    assert await search_store.load_negatives(db) == {("g1", "neg_keyword", "sattu")}


# ─── The routes, posting exactly what the screen sends ───────────────────────


def _window():
    end = ist.today() - timedelta(days=1)
    return (end - timedelta(days=6)).isoformat(), end.isoformat()


@pytest.fixture
async def stored(db):
    from app.models import AdsEntity
    start, end = _window()
    days = [(datetime.fromisoformat(start) + timedelta(days=i)).date().isoformat() for i in range(7)]
    db.add(AdsEntity(entity_type="campaign", ad_product="sp", entity_id="c1", name="MF_SP_auto",
                     state="ENABLED"))
    await db.commit()
    raw = [{"date": d, "campaignId": "c1", "campaignName": "MF_SP_auto", "adGroupId": "g1",
            "adGroupName": "chana", "searchTerm": "free sattu", "keywordId": "k1", "keyword": "close-match",
            "matchType": "TARGETING_EXPRESSION_PREDEFINED", "targeting": "close-match", "cost": 50,
            "clicks": 4, "purchases7d": 0, "sales7d": 0} for d in days]
    raw += [{"date": d, "campaignId": "c1", "campaignName": "MF_SP_auto", "adGroupId": "g1",
             "adGroupName": "chana", "searchTerm": "b0cccccccc", "keywordId": "k1", "keyword": "close-match",
             "matchType": "TARGETING_EXPRESSION_PREDEFINED", "targeting": "close-match", "cost": 40,
             "clicks": 3, "purchases7d": 0, "sales7d": 0} for d in days]
    await search_store.save_search_term_daily(db, raw, ad_product="sp")
    return start, end


@pytest.fixture
def amazon(monkeypatch):
    live, created = [], []

    async def fetch(client, *, ad_group_ids=None):
        assert ad_group_ids
        return list(live)

    async def create(client, rows, *, writer):
        created.append((writer, [r["search_term"] for r in rows]))
        return [{"entity_id": r["entity_id"], "ok": True, "new_id": f"N{len(created)}{i}", "error": None}
                for i, r in enumerate(rows)]

    monkeypatch.setattr(spapi_ads, "fetch_negatives", fetch)
    monkeypatch.setattr(spapi_ads, "create_negatives", create)
    return live, created


async def test_preview_names_the_candidates_and_sends_nothing(auth_client, stored, amazon):
    start, end = stored
    body = (await auth_client.post("/ads/negatives/preview", json={
        "start": start, "end": end, "conditions": SPEND_NO_ORDERS, "campaign_ids": [], "ad_group_ids": []})).json()
    assert {c["search_term"]: c["kind"] for c in body["changes"]} == {"free sattu": "keyword",
                                                                      "b0cccccccc": "product"}
    assert body["rule"].startswith("Search terms: ") and amazon[1] == []


async def test_preview_refuses_a_window_whose_days_are_not_held(auth_client, stored, amazon):
    end = ist.today() - timedelta(days=1)
    r = await auth_client.post("/ads/negatives/preview", json={
        "start": (end - timedelta(days=20)).isoformat(), "end": end.isoformat(), "conditions": SPEND_NO_ORDERS})
    assert r.status_code == 409


async def test_apply_sends_only_ticked_keys_drops_what_amazon_already_holds_and_ledgers_the_window(
        auth_client, stored, amazon, db):
    start, end = stored
    live, created = amazon
    live.append({"parent_id": "g1", "entity_type": "neg_target", "name": "b0cccccccc"})
    key_word = negatives.change_key("sp", "g1", "free sattu")
    key_asin = negatives.change_key("sp", "g1", "b0cccccccc")
    # Exactly what templates/ads.html posts.
    body = (await auth_client.post("/ads/negatives/apply", json={
        "start": start, "end": end, "conditions": SPEND_NO_ORDERS, "campaign_ids": [], "ad_group_ids": [],
        "keys": [key_word, key_asin, "sp|g9|forged"], "confirm_sb": False})).json()
    assert body["applied"] == 1, body
    assert created == [(negatives.W_SP_KEYWORD, ["free sattu"])]
    assert body["already_there"] == [{"key": key_asin, "search_term": "b0cccccccc"}]
    rows = await repository.load_run(db, body["run_id"])
    assert [(r["status"], r["action"]) for r in rows] == [("applied", "negate")]
    # The next preview no longer offers it, and the rule reads as ran today on THIS window only.
    again = (await auth_client.post("/ads/negatives/preview", json={
        "start": start, "end": end, "conditions": SPEND_NO_ORDERS})).json()
    assert key_word not in again["approved_ids"]
    ran = await repository.ran_today_by_rule(db, logic.ist_day(datetime.utcnow()))
    assert set(ran[again["rule"]]) == {f"{start}..{end}"}


async def test_apply_refuses_a_forged_key_and_sb_without_confirmation(auth_client, stored, amazon, db):
    start, end = stored
    r = await auth_client.post("/ads/negatives/apply", json={
        "start": start, "end": end, "conditions": SPEND_NO_ORDERS, "keys": ["sp|g9|forged"]})
    assert r.status_code == 409 and amazon[1] == []
    days = [(datetime.fromisoformat(start) + timedelta(days=i)).date().isoformat() for i in range(7)]
    await search_store.save_search_term_daily(db, [
        {"date": d, "campaignId": "c1", "campaignName": "MF_SB", "adGroupId": "s1", "searchTerm": "makhana",
         "keywordText": "makhana snack", "matchType": "PHRASE", "cost": 60, "purchases": 0} for d in days],
        ad_product="sb")
    key = negatives.change_key("sb", "s1", "makhana")
    r = await auth_client.post("/ads/negatives/apply", json={
        "start": start, "end": end, "conditions": SPEND_NO_ORDERS, "keys": [key]})
    assert r.status_code == 400 and r.json()["needs_confirm_sb"] and amazon[1] == []


async def test_keyword_apply_records_the_window_for_ran_today(auth_client, db, monkeypatch):
    """`/ads/apply` stamps `rule_window` from the start/end the screen now sends."""
    from app.routers import ads as ads_router
    assert ads_router._window_key({"start": "2026-10-01", "end": "2026-10-07"}) == "2026-10-01..2026-10-07"
    assert ads_router._window_key({}) is None
    source = (ROOT / "app" / "routers" / "ads.py").read_text(encoding="utf-8")
    assert "rule_window=_window_key(body)" in source


# ─── The screen ──────────────────────────────────────────────────────────────


def test_the_screen_has_a_search_terms_level_with_only_the_negate_action():
    assert 'data-level="search"' in PAGE and '<option value="negate">' in PAGE
    # Negate is ONLY on the search level, and only negate is there.
    assert ('o.disabled = search ? o.value !== "negate" : (o.value === "negate" || '
            '(product && o.value !== "set_state"));') in PAGE
    assert 'if(level === "search") return runNegPreview();' in PAGE


def test_the_screen_posts_keys_not_rows_and_confirms_sponsored_brands():
    body = PAGE[PAGE.index("async function applyNegatives("):][:2000]
    assert '"/ads/negatives/apply"' in body and "keys, confirm_sb" in body
    assert "CANNOT be undone" in body


def test_keyword_apply_sends_the_window():
    assert "rule: plan.rule, start: data.window[0], end: data.window[1]" in PAGE


def test_saved_rules_split_into_grow_and_cut_and_grey_per_window():
    body = PAGE[PAGE.index("function renderSaved("):][:4000]
    assert 'column("grow"' in body and 'column("cut"' in body
    assert '(r.ran_today || {})[(data.window || []).join("..")]' in body
    assert '" ran"' in body
    # Greyed, still usable: the Use button is on every card.
    assert body.count('data-use="${r.id}"') == 1


async def test_sponsored_brands_missing_days_is_left_out_whole_and_said(auth_client, stored, amazon, db):
    start, end = stored
    # SB holds ONE day of the seven: summing it would read short, so SB is left out of the preview.
    await search_store.save_search_term_daily(db, [
        {"date": end, "campaignId": "c1", "campaignName": "MF_SB", "adGroupId": "s1", "searchTerm": "makhana",
         "keywordText": "makhana snack", "matchType": "PHRASE", "cost": 900, "purchases": 0}], ad_product="sb")
    body = (await auth_client.post("/ads/negatives/preview", json={
        "start": start, "end": end, "conditions": SPEND_NO_ORDERS})).json()
    assert {c["ad_product"] for c in body["changes"]} == {"sp"}
    assert any("Sponsored Brands" in n for n in body["notes"])


async def test_ran_today_uses_the_IST_day_not_the_UTC_one(db):
    """20:00 UTC on the 9th is 01:30 IST on the 10th: it ran on the 10th."""
    from sqlalchemy import update
    from app.models import AdsMutation
    sentence = "Search terms: orders = 0 -> add as negative"
    run = await repository.open_run(db, [_change("z")], rule_summary=sentence,
                                    rule_window="2026-10-01..2026-10-07")
    await repository.record_create_results(db, run, [
        {"entity_id": negatives.provisional_id("z"), "ok": True, "ad_id": "N9"}])
    await db.execute(update(AdsMutation).where(AdsMutation.run_id == run)
                     .values(created_at=datetime(2026, 10, 9, 20, 0)))
    await db.commit()
    assert sentence not in await repository.ran_today_by_rule(db, "2026-10-09")
    assert sentence in await repository.ran_today_by_rule(db, "2026-10-10")
