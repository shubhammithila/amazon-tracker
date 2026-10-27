"""Ads → Keyword growth: relevant keywords and competitor targets, added at a bid the owner chooses.

Spec: `docs/superpowers/specs/2026-10-26-ads-keyword-growth-design.md`.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import ist
from app.ads import growth, growth_judge, logic, pages, search_store, spapi_ads

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "templates" / "ads_growth.html").read_text(encoding="utf-8")


# ─── The tabs ────────────────────────────────────────────────────────────────


async def test_the_three_ads_tabs_read_in_order_on_every_ads_page(auth_client):
    for path, on in (("/ads-page", "Optimisation"), ("/ads-page/expansion", "SKU coverage"),
                     ("/ads-page/growth", "Keyword growth")):
        response = await auth_client.get(path)
        assert response.status_code == 200, path
        html = response.text
        positions = [html.index(f">{label}</a>") for label in ("Optimisation", "SKU coverage", "Keyword growth")]
        assert positions == sorted(positions), path
        assert f'on" href="{path}">{on}</a>' in html, path



# ─── The product page ────────────────────────────────────────────────────────

CATALOG = {"summaries": [{"itemName": "MITHILA FOODS 500 g Bihari Chana Sattu"}],
           "attributes": {"bullet_point": [{"value": "High protein"}, {"value": "<b>Roasted</b> gram"}],
                          "product_description": [{"value": "Sattu <br><br> drink"}]}}


def test_the_catalog_answer_becomes_title_bullets_and_plain_description():
    assert pages.parse_catalog(CATALOG) == {"title": "MITHILA FOODS 500 g Bihari Chana Sattu",
                                            "bullets": ["High protein", "Roasted gram"],
                                            "description": "Sattu drink"}


async def test_a_page_is_reread_only_when_stale_and_a_failed_read_keeps_the_old_one(db):
    calls = []

    async def fetch(asin):
        calls.append(asin)
        return pages.parse_catalog(CATALOG)

    now = datetime(2026, 10, 26, 9, 0)
    first = await pages.load_pages(db, ["b0aaaaaaaa"], fetch=fetch, now=now)
    assert first["B0AAAAAAAA"]["title"].endswith("Chana Sattu") and calls == ["B0AAAAAAAA"]
    await pages.load_pages(db, ["B0AAAAAAAA"], fetch=fetch, now=now + timedelta(days=6))
    assert calls == ["B0AAAAAAAA"], "a 6-day-old page is not read again"

    async def boom(asin):
        raise RuntimeError("Amazon down")
    stale = await pages.load_pages(db, ["B0AAAAAAAA"], fetch=boom, now=now + timedelta(days=8))
    assert stale["B0AAAAAAAA"]["title"].endswith("Chana Sattu")
    assert await pages.load_pages(db, ["B0BBBBBBBB"], fetch=boom, now=now) == {}


# ─── The pure core ───────────────────────────────────────────────────────────


def test_a_group_kind_comes_from_what_it_targets():
    assert growth.group_kind({"EXACT", "TARGETING_EXPRESSION"}) == growth.KIND_KEYWORD
    assert growth.group_kind({"TARGETING_EXPRESSION"}) == growth.KIND_PRODUCT
    assert growth.group_kind({"THEME"}) == growth.KIND_PRODUCT
    assert growth.group_kind({"TARGETING_EXPRESSION_PREDEFINED"}) == growth.KIND_AUTO


def test_cards_are_enabled_groups_we_manage_and_never_auto():
    campaigns = {"c1": {"name": "MF_SP_keywords", "state": "ENABLED", "ad_product": "sp"},
                 "c2": {"name": "m19 autopilot", "state": "ENABLED", "ad_product": "sp"},
                 "c3": {"name": "MF_SB", "state": "ENABLED", "ad_product": "sb"}}
    groups = {"g1": {"name": "sattu", "state": "ENABLED", "campaign_id": "c1"},
              "g2": {"name": "paused", "state": "PAUSED", "campaign_id": "c1"},
              "g3": {"name": "m19", "state": "ENABLED", "campaign_id": "c2"},
              "g4": {"name": "auto", "state": "ENABLED", "campaign_id": "c1"},
              "g5": {"name": "brand", "state": "ENABLED", "campaign_id": "c3"}}
    out = growth.cards(groups, campaigns, {"g4": growth.KIND_AUTO, "g5": growth.KIND_PRODUCT},
                       {"g1": ["b0aaaaaaaa"], "g5": ["B0CCCCCCCC"]})
    assert [(c["ad_group_id"], c["ad_product"], c["kind"]) for c in out] == [
        ("g5", "sb", "product"), ("g1", "sp", "keyword")]
    assert out[1]["asins"] == ["B0AAAAAAAA"]


def test_candidates_merge_across_sources_and_route_words_and_asins_apart():
    merged = growth.merge_candidates(growth.KIND_KEYWORD, {
        growth.SRC_SOLD: [{"term": "Chana  Sattu", "orders": 3, "spend": 120}, {"term": "b0aaaaaaaa", "orders": 9}],
        growth.SRC_AMAZON: [{"term": "chana sattu", "rank": 4}, {"term": "sattu", "rank": 1}],
        growth.SRC_PAGE: [{"term": "sattu"}]})
    by = {m["term"]: m for m in merged}
    assert set(by) == {"chana sattu", "sattu"}, "an ASIN never becomes a keyword"
    assert by["chana sattu"]["sources"] == ["sold", "amazon"] and by["chana sattu"]["orders"] == 3
    assert by["sattu"]["amazon_rank"] == 1 and by["sattu"]["sources"] == ["amazon", "page"]
    assert [m["term"] for m in merged][0] == "chana sattu", "proven orders first"
    products = growth.merge_candidates(growth.KIND_PRODUCT, {growth.SRC_SOLD: [{"term": "b0aaaaaaaa"},
                                                                               {"term": "sattu"}]})
    assert [m["term"] for m in products] == ["B0AAAAAAAA"]


def test_anything_held_in_any_state_or_negated_is_left_out():
    cands = [{"term": t} for t in ("sattu", "chana sattu", "sattu drink", "jau sattu")]
    held = {("sattu", "EXACT"), ("sattu", "PHRASE"),        # both held (one may be archived)
            ("chana sattu", "EXACT"), ("sattu drink", "BROAD")}
    kept, dropped = growth.exclude_existing(cands, kind=growth.KIND_KEYWORD, held_keywords=held,
                                            held_asins=set(), negatives={"jau sattu"})
    assert {k["term"]: k["missing_matches"] for k in kept} == {
        "chana sattu": ["PHRASE"], "sattu drink": ["EXACT", "PHRASE"]}, "BROAD does not count as held"
    assert dropped == {"already": 1, "negative": 1}
    prods, d2 = growth.exclude_existing([{"term": "B0AAAAAAAA"}, {"term": "B0BBBBBBBB"}, {"term": "B0CCCCCCCC"}],
                                        kind=growth.KIND_PRODUCT, held_keywords=set(),
                                        held_asins={"b0aaaaaaaa"}, negatives={"B0BBBBBBBB"})
    assert [p["term"] for p in prods] == ["B0CCCCCCCC"] and d2 == {"already": 1, "negative": 1}


def test_the_bid_is_amazons_low_end_or_the_custom_one():
    assert growth.choose_bid({"low": 5.75}, growth.BID_LOW, 9) == 5.75
    assert growth.choose_bid({"low": None}, growth.BID_LOW, "9") == 9.0, "no suggestion -> custom"
    assert growth.choose_bid({"low": 5.75}, growth.BID_CUSTOM, 9) == 9.0
    assert growth.choose_bid({}, growth.BID_LOW, "") is None
    assert growth.choose_bid({}, growth.BID_CUSTOM, 0) is None


def _sel(term, kind=growth.KIND_KEYWORD, product="sp", missing=None, bids=None):
    return {"key": growth.key_of(product, "g1", kind, term), "term": term, "kind": kind, "ad_product": product,
            "campaign_id": "c1", "ad_group_id": "g1", "missing_matches": missing or [], "bids": bids or {}}


def test_every_keyword_goes_as_exact_and_phrase_an_asin_once_and_max_bid_holds():
    rows, refused = growth.rows_to_send(
        [_sel("chana sattu", bids={"low": 4.0}), _sel("sattu drink", missing=["PHRASE"], bids={"low": 5.0}),
         _sel("B0CCCCCCCC", kind=growth.KIND_PRODUCT, product="sb"),
         _sel("sattu 1kg", bids={"low": 80.0}), _sel("bihari sattu", product="sb")],
        mode=growth.BID_LOW, custom=6, guardrails={"max_bid": 60})
    got = sorted((r["text"], r["match_type"], r["writer"], r["new_bid"]) for r in rows)
    assert got == sorted([
        ("chana sattu", "EXACT", logic.WRITER_KEYWORD, 4.0), ("chana sattu", "PHRASE", logic.WRITER_KEYWORD, 4.0),
        ("sattu drink", "PHRASE", logic.WRITER_KEYWORD, 5.0),
        ("B0CCCCCCCC", "ASIN_SAME_AS", logic.WRITER_SB_TARGET, 6.0),
        ("bihari sattu", "EXACT", logic.WRITER_SB_KEYWORD, 6.0),
        ("bihari sattu", "PHRASE", logic.WRITER_SB_KEYWORD, 6.0)])
    assert [r["term"] for r in refused] == ["sattu 1kg"] and refused[0]["reason"] == growth.REFUSE_OVER_MAX
    assert len({r["entity_id"] for r in rows}) == len(rows) and all(r["entity_id"].startswith("new-") for r in rows)
    none, refused2 = growth.rows_to_send([_sel("sattu")], mode=growth.BID_CUSTOM, custom="", guardrails={})
    assert none == [] and refused2[0]["reason"] == growth.REFUSE_NO_BID


# ─── Amazon reads and writes ─────────────────────────────────────────────────


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
        def __init__(self, *answers):
            self.answers = list(answers)

        async def post(self, url, json=None, headers=None):
            sent.append({"url": url, "json": json, "headers": headers or {}})
            return Response(*self.answers.pop(0))

        async def get(self, url, params=None, headers=None):
            sent.append({"url": url, "params": params, "headers": headers or {}})
            return Response(*self.answers.pop(0))

    return sent, Client


async def test_amazon_keyword_suggestions_are_read_and_their_v5_bid_ignored(wire):
    sent, Client = wire
    out = await spapi_ads.fetch_sp_keyword_suggestions_live(Client((200, {"keywordTargetList": [
        {"keyword": "sattu", "bidInfo": [{"matchType": "EXACT", "bid": 282}], "searchTermImpressionRank": 1,
         "searchTermImpressionShare": 24.6}]})), ["b0aaaaaaaa"])
    assert out == [{"term": "sattu", "rank": 1, "share": 24.6}]
    assert sent[0]["headers"]["Content-Type"] == "application/vnd.spkeywordsrecommendation.v5+json"
    assert sent[0]["json"]["asins"] == ["B0AAAAAAAA"]
    sb = await spapi_ads.fetch_sb_keyword_suggestions_live(Client((200, [
        {"type": "addKeyword", "value": "chana sattu", "searchTermImpressionRank": 2},
        {"type": "somethingElse", "value": "x"}])), ["B0AAAAAAAA"])
    assert sb == [{"term": "chana sattu", "rank": 2, "share": None}]
    pt = await spapi_ads.fetch_product_target_suggestions_live(Client((200, {"recommendations": [
        {"recommendedAsin": "b0gmqvmxdl", "themes": ["Top converting targets"]}]})), ["B0AAAAAAAA"])
    assert pt == [{"asin": "B0GMQVMXDL", "themes": ["Top converting targets"], "rank": 1}]


async def test_autocomplete_never_raises(wire):
    sent, Client = wire
    ok = await spapi_ads.fetch_autocomplete_live(Client((200, {"suggestions": [{"value": "chana sattu 1kg"}]})),
                                                 "chana sattu")
    assert ok == ["chana sattu 1kg"] and sent[0]["url"] == "https://www.amazon.in/suggestions"
    assert await spapi_ads.fetch_autocomplete_live(Client((502, {})), "x") == []


async def test_creates_go_to_the_four_endpoints_in_their_own_shapes(wire):
    sent, Client = wire
    base = {"campaign_id": "111", "ad_group_id": "222", "new_bid": 4.5}
    sp_kw = await spapi_ads.create_targeting_live(Client((207, {"keywords": {
        "success": [{"index": 0, "keywordId": "9"}], "error": []}})),
        [{**base, "entity_id": "new-a", "text": "chana sattu", "match_type": "PHRASE"}], writer=logic.WRITER_KEYWORD)
    assert sent[-1]["url"] == "https://ads.test/sp/keywords"
    assert sent[-1]["json"] == {"keywords": [{"campaignId": "111", "adGroupId": "222", "keywordText": "chana sattu",
                                              "matchType": "PHRASE", "bid": 4.5, "state": "ENABLED"}]}
    assert sp_kw == [{"entity_id": "new-a", "ok": True, "new_id": "9", "error": None}]
    await spapi_ads.create_targeting_live(Client((207, {"targetingClauses": {"success": [], "error": []}})),
                                          [{**base, "entity_id": "new-b", "text": "b0aaaaaaaa", "match_type": "ASIN_SAME_AS"}],
                                          writer=logic.WRITER_TARGET)
    assert sent[-1]["json"]["targetingClauses"][0]["expression"] == [{"type": "ASIN_SAME_AS", "value": "B0AAAAAAAA"}]
    sb_kw = await spapi_ads.create_targeting_live(Client((207, [{"code": "SUCCESS", "keywordId": 5}])),
                                                  [{**base, "entity_id": "new-c", "text": "sattu", "match_type": "EXACT"}],
                                                  writer=logic.WRITER_SB_KEYWORD)
    assert sent[-1]["json"] == [{"campaignId": 111, "adGroupId": 222, "keywordText": "sattu", "matchType": "exact",
                                 "bid": 4.5}]
    assert sb_kw[0]["new_id"] == "5"
    sb_t = await spapi_ads.create_targeting_live(Client((200, {"createTargetSuccessResults": [],
                                                               "createTargetErrorResults": [{"targetRequestIndex": 0,
                                                                                             "details": "Ad group was not found."}]})),
                                                 [{**base, "entity_id": "new-d", "text": "B0AAAAAAAA", "match_type": "ASIN_SAME_AS"}],
                                                 writer=logic.WRITER_SB_TARGET)
    assert sent[-1]["json"] == {"targets": [{"campaignId": 111, "adGroupId": 222,
                                             "expressions": [{"type": "asinSameAs", "value": "B0AAAAAAAA"}], "bid": 4.5}]}
    assert sb_t[0]["ok"] is False and sb_t[0]["error"] == "Ad group was not found."


# ─── The judge ───────────────────────────────────────────────────────────────


def _answer(verdicts, ideas=()):
    return {"output": {"message": {"content": [{"toolUse": {"name": "record_verdicts", "input": {
        "verdicts": verdicts, "ideas": list(ideas)}}}]}}, "usage": {"inputTokens": 100, "outputTokens": 20}}


@pytest.fixture
def bedrock_key(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "bedrock_api_key", "k", raising=False)


async def test_the_judge_keeps_what_claude_keeps_rejects_what_it_skips_and_caches(db, bedrock_key):
    calls = []

    async def converse(**kw):
        calls.append(kw["messages"][0]["content"][0]["text"])
        return _answer([{"term": "sattu drink", "keep": True, "reason": "same product, drink use"},
                        {"term": "atta", "keep": False, "reason": "generic flour"}],
                       [{"term": "bihari sattu", "reason": "regional name on the page"}])
    cands = [{"term": t} for t in ("sattu drink", "atta", "besan")]
    out = await growth_judge.judge(db, key="P1", kind=growth.KIND_KEYWORD, page_text="Title: Chana Sattu",
                                   candidates=cands, converse=converse)
    assert out["verdicts"]["sattu drink"] == (True, "same product, drink use")
    assert out["verdicts"]["atta"][0] is False
    assert out["verdicts"]["besan"] == (False, "not judged"), "a term Claude skips is rejected"
    assert out["ideas"] == [{"term": "bihari sattu", "reason": "regional name on the page"}]
    again = await growth_judge.judge(db, key="P1", kind=growth.KIND_KEYWORD, page_text="x",
                                     candidates=cands, converse=converse)
    assert len(calls) == 1, "every term already judged: no second call"
    assert again["ideas"] == [{"term": "bihari sattu", "reason": "regional name on the page"}]


async def test_a_failed_judge_raises_and_the_daily_cap_refuses(db, bedrock_key):
    from app.assistant import bedrock

    async def broken(**kw):
        raise bedrock.BedrockError("throttled")
    with pytest.raises(growth_judge.JudgeError):
        await growth_judge.judge(db, key="P2", kind=growth.KIND_KEYWORD, page_text="x",
                                 candidates=[{"term": "sattu"}], converse=broken)

    async def prose(**kw):
        return {"output": {"message": {"content": [{"text": "they all look fine"}]}}}
    with pytest.raises(growth_judge.JudgeError):
        await growth_judge.judge(db, key="P3", kind=growth.KIND_KEYWORD, page_text="x",
                                 candidates=[{"term": "sattu"}], converse=prose)
    async def fine(**kw):
        return _answer([{"term": "sattu", "keep": True, "reason": "ok"}])
    # Two checks already ran today (both failed, both logged), so a cap of 2 refuses a third that would work.
    with pytest.raises(growth_judge.JudgeError, match="used up"):
        await growth_judge.judge(db, key="P4", kind=growth.KIND_KEYWORD, page_text="x",
                                 candidates=[{"term": "sattu"}], converse=fine, limit=2)
    ok = await growth_judge.judge(db, key="P4", kind=growth.KIND_KEYWORD, page_text="x",
                                  candidates=[{"term": "sattu"}], converse=fine, limit=3)
    assert ok["verdicts"]["sattu"][0] is True


# ─── The routes ──────────────────────────────────────────────────────────────


@pytest.fixture
async def seeded(db, monkeypatch, bedrock_key):
    from app.models import AdsEntity, AdsPerformanceDaily
    db.add(AdsEntity(entity_type="campaign", ad_product="sp", entity_id="c1", name="MF_SP_keywords", state="ENABLED"))
    db.add(AdsEntity(entity_type="ad_group", ad_product="sp", entity_id="g1", parent_id="c1", name="sattu",
                     state="ENABLED"))
    db.add(AdsEntity(entity_type="product_ad", ad_product="sp", entity_id="a1", parent_id="g1", campaign_id="c1",
                     name="0.5kg cs 1", asin="B0AAAAAAAA", state="ENABLED"))
    db.add(AdsPerformanceDaily(day="2026-10-20", ad_product="sp", entity_id="k1", entity_type="keyword",
                               campaign_id="c1", ad_group_id="g1", text="sattu", match_type="EXACT"))
    await db.commit()
    end = ist.today() - timedelta(days=1)
    await search_store.save_search_term_daily(db, [
        {"date": end.isoformat(), "campaignId": "c1", "adGroupId": "g1", "searchTerm": "sattu drink powder",
         "keyword": "sattu", "matchType": "BROAD", "cost": 90, "purchases7d": 3, "sales7d": 600}], ad_product="sp")

    async def page(asin):
        return pages.parse_catalog(CATALOG)
    monkeypatch.setattr(pages, "fetch_catalog", page)

    from app.shipment import catalogue as catalogue_mod

    async def catalogue(**_):
        return {"B0AAAAAAAA": {"name": "Chana Sattu", "weight": 0.5, "active": True}}, None, "sheet"
    monkeypatch.setattr(catalogue_mod, "load_catalogue", catalogue)

    async def sp_kw(client, asins):
        return [{"term": "sattu", "rank": 1}, {"term": "atta", "rank": 2}, {"term": "chana sattu", "rank": 3}]
    monkeypatch.setattr(spapi_ads, "fetch_sp_keyword_suggestions", sp_kw)

    async def auto(client, prefix):
        return ["chana sattu 1kg"]
    monkeypatch.setattr(spapi_ads, "fetch_autocomplete", auto)
    held = {"keywords": {("sattu", "EXACT"), ("sattu", "PHRASE")}, "asins": set(), "states": {"ENABLED": 2}}

    async def targeting(client, group, product):
        return held
    monkeypatch.setattr(spapi_ads, "fetch_group_targeting", targeting)

    async def bids(client, rows):
        return {r["entity_id"]: {"low": 3.5, "suggested_bid": 4.0, "high": 5.0} for r in rows}
    monkeypatch.setattr(spapi_ads, "fetch_bid_recommendations", bids)

    async def converse(**kw):
        return _answer([{"term": "sattu drink powder", "keep": True, "reason": "sold, same product"},
                        {"term": "chana sattu", "keep": True, "reason": "product name"},
                        {"term": "chana sattu 1kg", "keep": True, "reason": "size we sell"},
                        {"term": "atta", "keep": False, "reason": "generic flour"}])
    from app.assistant import bedrock
    monkeypatch.setattr(bedrock, "converse", converse)
    from app.routers import ads_growth
    ads_growth._sb_asins_cache.update(at=10**12, map={})
    return held


async def test_find_offers_only_kept_new_terms_with_their_sources_and_bids(auth_client, seeded):
    body = (await auth_client.post("/ads/growth/find", json={"ad_group_id": "g1"})).json()
    by = {s["term"]: s for s in body["suggestions"]}
    assert "sattu" not in by, "held in both match types: left out"
    assert by["atta"]["keep"] is False and by["atta"]["reason"] == "generic flour"
    kept = {t for t, s in by.items() if s["keep"]}
    assert kept == {"sattu drink powder", "chana sattu", "chana sattu 1kg"}
    assert by["sattu drink powder"]["sources"] == ["sold"] and by["sattu drink powder"]["figures"]["orders"] == 3
    assert by["chana sattu"]["bids"]["low"] == 3.5
    assert body["dropped"]["already"] == 1


async def test_add_sends_exact_and_phrase_for_ticked_kept_keys_only(auth_client, seeded, monkeypatch, db):
    await auth_client.post("/ads/growth/find", json={"ad_group_id": "g1"})
    sent = []

    async def create(client, rows, *, writer):
        sent.extend((writer, r["text"], r["match_type"], r["new_bid"]) for r in rows)
        return [{"entity_id": r["entity_id"], "ok": True, "new_id": f"N{i}", "error": None} for i, r in enumerate(rows)]
    monkeypatch.setattr(spapi_ads, "create_targeting", create)
    key = growth.key_of("sp", "g1", growth.KIND_KEYWORD, "chana sattu")
    rejected = growth.key_of("sp", "g1", growth.KIND_KEYWORD, "atta")
    r = await auth_client.post("/ads/growth/add", json={"ad_group_id": "g1", "keys": [key, rejected],
                                                        "bid_mode": "low", "custom_bid": 5})
    assert r.status_code == 409 and sent == [], "a rejected row can never be added"
    # Exactly what templates/ads_growth.html posts.
    body = (await auth_client.post("/ads/growth/add", json={"ad_group_id": "g1", "keys": [key], "bid_mode": "low",
                                                            "custom_bid": 5, "progress_id": "t-growth-0001"})).json()
    assert body["applied"] == 2, body
    assert sorted(sent) == [("keyword", "chana sattu", "EXACT", 3.5), ("keyword", "chana sattu", "PHRASE", 3.5)]
    from app.ads import repository
    rows = await repository.load_run(db, body["run_id"])
    assert {(r["status"], r["action"]) for r in rows} == {("applied", "create")}
    undo = await repository.build_undo(db, body["run_id"])
    assert sorted((u["entity_id"], u["new_state"]) for u in undo) == [("N0", "PAUSED"), ("N1", "PAUSED")]
    after = (await auth_client.get("/ads/growth/campaign/c1")).json()
    terms = {s["term"] for c in after["groups"] for s in c["suggestions"]}
    assert "chana sattu" not in terms, "an added keyword leaves the list"


async def test_add_drops_a_match_amazon_already_holds_by_the_time_it_is_sent(auth_client, seeded, monkeypatch):
    await auth_client.post("/ads/growth/find", json={"ad_group_id": "g1"})
    seeded["keywords"].add(("chana sattu", "EXACT"))        # added in Seller Central since the Find
    sent = []

    async def create(client, rows, *, writer):
        sent.extend((r["text"], r["match_type"]) for r in rows)
        return [{"entity_id": r["entity_id"], "ok": True, "new_id": "N", "error": None} for r in rows]
    monkeypatch.setattr(spapi_ads, "create_targeting", create)
    body = (await auth_client.post("/ads/growth/add", json={
        "ad_group_id": "g1", "keys": [growth.key_of("sp", "g1", growth.KIND_KEYWORD, "chana sattu")],
        "bid_mode": "custom", "custom_bid": 4})).json()
    assert sent == [("chana sattu", "PHRASE")]
    assert body["already_there"] == [{"term": "chana sattu", "match_type": "EXACT"}]


async def test_a_failed_relevance_check_offers_nothing(auth_client, seeded, monkeypatch):
    from app.assistant import bedrock

    async def down(**kw):
        raise bedrock.BedrockError("down")
    monkeypatch.setattr(bedrock, "converse", down)
    r = await auth_client.post("/ads/growth/find", json={"ad_group_id": "g1"})
    assert r.status_code == 502 and r.json()["judge_failed"]
    assert all(not c["suggestions"] for c in (await auth_client.get("/ads/growth/campaign/c1")).json()["groups"])


async def test_the_campaign_list_reads_only_the_database_and_counts_its_groups(auth_client, seeded, monkeypatch):
    from app.routers import ads_growth

    async def no_sb(*a, **k):
        raise AssertionError("the campaign list must not call Amazon")
    monkeypatch.setattr(ads_growth, "_sb_ad_asins", no_sb)
    before = (await auth_client.get("/ads/growth/campaigns")).json()["campaigns"]
    assert [(c["campaign_id"], c["keyword_groups"], c["found_groups"]) for c in before] == [("c1", 1, 0)]
    await auth_client.post("/ads/growth/find", json={"ad_group_id": "g1"})
    after = (await auth_client.get("/ads/growth/campaigns")).json()["campaigns"][0]
    assert after["found_groups"] == 1 and after["kept"] == 3 and after["last_found"]


async def test_group_kinds_are_cached_until_the_next_ads_refresh(auth_client, seeded, db):
    from app.models import AdsPerformanceDaily, AdsRefresh
    from app.routers import ads_growth
    first = await ads_growth._group_kinds(db)
    assert first["g1"] == growth.KIND_KEYWORD
    db.add(AdsPerformanceDaily(day="2026-10-21", ad_product="sp", entity_id="t9", entity_type="target",
                               campaign_id="c1", ad_group_id="g9", text="x", match_type="TARGETING_EXPRESSION"))
    await db.commit()
    assert "g9" not in await ads_growth._group_kinds(db), "served from the cache"
    db.add(AdsRefresh(window_start="2026-10-21", window_end="2026-10-21", status="done"))
    await db.commit()
    assert (await ads_growth._group_kinds(db))["g9"] == growth.KIND_PRODUCT, "a new refresh re-reads"


async def test_a_campaign_shows_its_own_ad_groups_and_an_unknown_one_is_404(auth_client, seeded):
    body = (await auth_client.get("/ads/growth/campaign/c1")).json()
    assert body["campaign"]["campaign_name"] == "MF_SP_keywords"
    assert [g["ad_group_id"] for g in body["groups"]] == ["g1"]
    assert (await auth_client.get("/ads/growth/campaign/nope")).status_code == 404


async def test_a_left_out_row_moves_to_relevant_stays_there_and_can_be_added(auth_client, seeded, monkeypatch):
    await auth_client.post("/ads/growth/find", json={"ad_group_id": "g1"})
    atta = growth.key_of("sp", "g1", growth.KIND_KEYWORD, "atta")
    bad = await auth_client.post("/ads/growth/keep", json={"ad_group_id": "g1", "keys": [atta, "nope"], "keep": True})
    assert bad.status_code == 409
    moved = (await auth_client.post("/ads/growth/keep", json={"ad_group_id": "g1", "keys": [atta], "keep": True})).json()
    row = next(s for s in moved["suggestions"] if s["term"] == "atta")
    assert row["keep"] is True and row["reason"] == growth.MOVED_KEEP
    # The next Generate keeps the owner's choice instead of re-asking the check.
    again = (await auth_client.post("/ads/growth/find", json={"ad_group_id": "g1"})).json()
    assert next(s for s in again["suggestions"] if s["term"] == "atta")["keep"] is True
    sent = []

    async def create(client, rows, *, writer):
        sent.extend(r["text"] for r in rows)
        return [{"entity_id": r["entity_id"], "ok": True, "new_id": f"N{i}", "error": None} for i, r in enumerate(rows)]
    monkeypatch.setattr(spapi_ads, "create_targeting", create)
    body = (await auth_client.post("/ads/growth/add", json={"ad_group_id": "g1", "keys": [atta], "bid_mode": "low",
                                                            "custom_bid": 5})).json()
    assert body["applied"] == 2 and sent == ["atta", "atta"]


# ─── The screen ──────────────────────────────────────────────────────────────


def test_the_screen_posts_keys_and_the_bid_choice_and_never_the_rows():
    body = PAGE[PAGE.index("async function add("):][:2600]
    assert '"/ads/growth/add"' in body
    assert "JSON.stringify({ad_group_id: groupId, keys, bid_mode: mode, custom_bid: custom, progress_id: bar.id})" in body
    assert "const {mode, custom} = bidMode[groupId];" in body, "the bid choice is the ad group's own"
    assert "} finally {\n    bar.stop();" in body
    assert "Exact AND Phrase" in body, "the confirm says each keyword goes twice"


def test_every_server_string_is_escaped_on_the_screen():
    for field in ("s.term", "s.reason", "f.title", "card.campaign_name", "card.ad_group_name", "body.error",
                  "r.error", "r.term"):
        for bad in (f"${{{field}}}", f"${{{field} "):
            assert bad not in PAGE, f"{field} reaches innerHTML unescaped"


def test_left_out_rows_are_tickable_only_to_MOVE_them_never_to_add():
    body = PAGE[PAGE.index("function groupHtml("):][:6000]
    assert "kept.map(s => rowHtml(card, s, set))" in body
    assert "left.map(s => rowHtml(card, s, leftSet))" in body
    assert 'data-list="${s.keep ? "keep" : "left"}"' in PAGE
    assert 'setOf(box.dataset.list === "keep" ? picked : pickedLeft, box.dataset.group)' in PAGE
    move = PAGE[PAGE.index("async function move("):][:600]
    assert "setOf(pickedLeft, groupId)" in move and "keep: true" in move


def test_the_bid_choice_sits_on_each_ad_group_not_at_the_top():
    assert 'name="bid-mode"' not in PAGE and 'id="custom-bid"' not in PAGE
    body = PAGE[PAGE.index("function groupHtml("):][:6000]
    assert "data-bid-mode=" in body and "data-custom-bid=" in body


def test_the_screen_opens_campaigns_first_and_back_works_through_the_address():
    assert 'fetch("/ads/growth/campaigns")' in PAGE
    assert "fetch(`/ads/growth/campaign/${encodeURIComponent(r.campaign)}`)" in PAGE
    assert 'window.addEventListener("hashchange", render);' in PAGE
    assert "data-back" in PAGE


async def test_ideas_are_asked_for_once_even_when_every_candidate_is_already_judged(db, bedrock_key):
    from sqlalchemy import delete
    from app.models import GrowthVerdict
    calls = []

    async def no_ideas(**kw):
        calls.append(kw["messages"][0]["content"][0]["text"])
        return _answer([{"term": "sattu drink", "keep": True, "reason": "ok"}])
    cands = [{"term": "sattu drink"}]
    await growth_judge.judge(db, key="P9", kind=growth.KIND_KEYWORD, page_text="x", candidates=cands, converse=no_ideas)
    assert "In `ideas`, propose up to 15 NEW keywords" in calls[0]
    await growth_judge.judge(db, key="P9", kind=growth.KIND_KEYWORD, page_text="x", candidates=cands, converse=no_ideas)
    assert len(calls) == 1, "ideas were asked for and none came: not asked again on every Find"

    # A product judged before ideas existed (no record that they were asked for) is asked once.
    await db.execute(delete(GrowthVerdict).where(GrowthVerdict.term == growth_judge.IDEAS_ASKED))
    await db.commit()

    async def ideas(**kw):
        calls.append(kw["messages"][0]["content"][0]["text"])
        return _answer([], [{"term": "bihari sattu", "reason": "on the page"}])
    out = await growth_judge.judge(db, key="P9", kind=growth.KIND_KEYWORD, page_text="x", candidates=cands,
                                   converse=ideas)
    assert len(calls) == 2 and "(none)" in calls[1], "nothing new to judge, so only ideas are asked for"
    assert out["ideas"] == [{"term": "bihari sattu", "reason": "on the page"}]
    assert out["verdicts"]["sattu drink"] == (True, "ok"), "the cached verdict still stands"
