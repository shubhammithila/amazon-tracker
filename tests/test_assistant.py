"""The Portfolio "Ask" assistant: tools slice the tabs' own payloads; the loop, route and panel.

No test calls Bedrock: `conftest.no_live_bedrock` refuses it, and the loop takes a fake `converse`.
"""
import ast
import json
from pathlib import Path

import httpx
import pytest

from app.assistant import bedrock, service, tools
from app.assistant.bedrock import converse as real_converse
from app.config import get_settings
from app.models import AssistantLog

pytestmark = pytest.mark.regression
ROOT = Path(__file__).resolve().parent.parent


# ── fixtures: payload shapes as the tabs build them, with unequal values ────────────────────────

def parent(name, sales, ad, net, refunded=0.0, fees=0.0, verdict="MONITOR", sizes=None, **kw):
    return {"product": name, "brand": kw.get("brand", "Mithila Foods"), "verdict": verdict,
            "verdict_reason": "r", "sales": sales, "ad_spend": ad, "net": net, "refunded": refunded,
            "fees_total": fees, "net_pct": net / sales if sales else None,
            "tacos": ad / sales if sales else None, "acos": kw.get("acos"),
            "acos_infinite": kw.get("acos_infinite", False), "units": kw.get("units", 10),
            "units_ordered": kw.get("units_ordered", 11), "sizes": sizes or [{"product": name}],
            "inactive": kw.get("inactive", False), "fees": {}}


def profit_data(parents, complete=True, **kw):
    return {"parents": parents, "window": ("2026-09-10", "2026-10-09"),
            "completeness": {"complete": complete, "missing_count": 0 if complete else 4},
            "category_totals": {"categories": []}, **kw}


CATS = {"chana sattu": 1, "jau sattu": 1, "usna chawal": 4}


# ── Profit tools ────────────────────────────────────────────────────────────────────────────────

def test_profit_products_filters_by_the_owners_category_and_sorts_lowest_net_first():
    data = profit_data([parent("Chana Sattu", 1000, 300, -50), parent("Jau Sattu", 400, 100, 80),
                        parent("Usna Chawal", 900, 90, 300)])
    out = tools.profit_products(data, CATS, {"category": "Sattu", "sort_by": "net", "ascending": True})
    assert [r["product"] for r in out["rows"]] == ["Chana Sattu", "Jau Sattu"]
    assert out["rows"][0]["category"] == "Sattu" and out["matching_products"] == 2


def test_a_filtered_total_RECOMPUTES_its_percentages_from_the_rupee_sums():
    """1,000 at -5% beside 400 at +20% is (-50+80)/1,400 = 2.1%, not the 7.5% an average gives."""
    data = profit_data([parent("Chana Sattu", 1000, 300, -50), parent("Jau Sattu", 400, 100, 80)])
    t = tools.profit_products(data, CATS, {"category": "Sattu"})["total_of_matching"]
    assert t["net"] == 30 and t["net_pct"] == 2.1 and t["tacos"] == 28.6


def test_a_missing_figure_sorts_LAST_both_ways_and_stays_null():
    data = profit_data([parent("Chana Sattu", 0, 0, 0), parent("Jau Sattu", 400, 100, 80),
                        parent("Usna Chawal", 900, 90, 300)])
    for asc in (True, False):
        rows = tools.profit_products(data, CATS, {"sort_by": "net_pct", "ascending": asc})["rows"]
        assert rows[-1]["product"] == "Chana Sattu" and rows[-1]["net_pct"] is None


def test_acos_with_spend_and_no_attributed_sales_is_said_in_words_not_as_zero():
    data = profit_data([parent("Chana Sattu", 1000, 300, 50, acos=None, acos_infinite=True)])
    assert tools.profit_products(data, CATS, {})["rows"][0]["acos"] == "no attributed sales"


def test_an_incomplete_window_is_named_so_the_model_does_not_answer_from_it():
    out = tools.profit_products(profit_data([], complete=False), CATS, {})
    assert "cannot be summed" in out["incomplete"]


def test_hidden_inactive_products_are_mentioned_with_how_to_see_them():
    out = tools.profit_products(profit_data([], inactive_hidden_parents=53), CATS, {})
    assert "53" in out["hidden_inactive_products"] and "include_inactive" in out["hidden_inactive_products"]


def test_the_limit_is_capped_so_a_slice_stays_small():
    data = profit_data([parent(f"P{i}", 100 + i, 1, 1) for i in range(100)])
    assert len(tools.profit_products(data, {}, {"limit": 500})["rows"]) == tools.MAX_LIMIT


def test_products_filter_finds_names_from_another_tab():
    data = profit_data([parent("Chana Sattu", 1000, 300, 50), parent("Jau Sattu", 400, 100, 80),
                        parent("Usna Chawal", 900, 90, 300)])
    out = tools.profit_products(data, CATS, {"products": ["usna", "Jau Sattu"]})
    assert sorted(r["product"] for r in out["rows"]) == ["Jau Sattu", "Usna Chawal"]


def test_profit_product_prefers_an_exact_name_and_lists_several_otherwise():
    data = profit_data([parent("Chana Sattu", 1000, 300, 50), parent("Jeera Chana Sattu", 400, 100, 80)])
    assert tools.profit_product(data, CATS, {"product": "chana sattu"})["product"]["product"] == "Chana Sattu"
    assert len(tools.profit_product(data, CATS, {"product": "chana"})["several_match"]) == 2


async def test_a_named_product_hidden_as_inactive_is_found_by_asking_again_with_inactive():
    shown = profit_data([parent("Chana Sattu", 1000, 300, 50)])
    everything = profit_data([parent("Chana Sattu", 1000, 300, 50),
                              parent("Raw Flaxseed", 200, 80, -10, inactive=True)], include_inactive=True)
    asked = []

    async def profit(inp):
        asked.append(bool(inp.get("include_inactive")))
        return (everything if inp.get("include_inactive") else shown), CATS
    out = await tools.run("profit_product", {"product": "Raw Flaxseed"},
                          tools.Sources(profit, None, None), {})
    assert asked == [False, True] and out["product"]["inactive"] is True


async def test_the_screens_window_fills_a_profit_call_that_names_none():
    seen = {}

    async def profit(inp):
        seen.update(inp)
        return profit_data([]), {}
    await tools.run("profit_products", {}, tools.Sources(profit, None, None),
                    {"start": "2026-09-01", "end": "2026-09-30"})
    assert (seen["start"], seen["end"]) == ("2026-09-01", "2026-09-30")


# ── Repeat and value tools ──────────────────────────────────────────────────────────────────────

def repeat_data(available=True):
    def row(name, cat, buyers, same, units, share):
        return {"product": name, "category": cat, "fba_share": share, "reorder_days": 31,
                "w": {"90": {"buyers": buyers, "same_pct": same, "units_pct": units}},
                "flows": {"90": {"came_from": [{"product": "Jau Sattu", "customers": 4}],
                                 "went_on": []}},
                "basket": {"orders": 50, "multi_pct": 0.1, "with": [{"product": "Jau Sattu", "orders": 5}]}}
    return {"brand": "Mithila Foods", "as_of": "2026-10-06", "fba_partial_below": 0.6,
            "windows": {"90": {"period": ["2026-06-09", "2026-07-08"], "available": available,
                               "reason": "history too short"}},
            "total": {"90": {"buyers": 900, "repeat_pct": 0.15, "units_pct": 0.3}},
            "categories": [{"category": "Sattu", "total": {"90": {"buyers": 500, "repeat_pct": 0.2,
                                                                  "units_pct": 0.4}}}],
            "rows": [row("Chana Sattu", "Sattu", 400, 0.188, 0.39, 0.95),
                     row("Usna Chawal", "Rice", 300, 0.216, 0.42, 0.40)]}


def test_repeat_products_sorts_and_flags_a_row_seen_mostly_through_easy_ship():
    out = tools.repeat_products(repeat_data(), {"sort_by": "same_pct"})
    assert [r["product"] for r in out["rows"]] == ["Usna Chawal", "Chana Sattu"]
    assert out["rows"][0]["partial_fba_data"] is True and out["rows"][1]["partial_fba_data"] is False
    assert out["rows"][0]["same_pct"] == 21.6


def test_a_category_scope_reports_the_categorys_OWN_unique_customer_total():
    out = tools.repeat_products(repeat_data(), {"category": "Sattu"})
    assert out["scope_total"] == {"scope": "Sattu", "buyers": 500, "same_pct": 20.0, "units_pct": 40.0}


def test_an_unavailable_window_returns_no_figures_and_says_why():
    out = tools.repeat_products(repeat_data(available=False), {})
    assert out["window_unavailable"] == "history too short"
    assert all(r["same_pct"] is None and r["buyers"] is None for r in out["rows"])


def test_repeat_flows_names_where_a_products_customers_came_from():
    out = tools.repeat_flows(repeat_data(), {"product": "Chana Sattu"})
    assert out["came_from"] == [{"product": "Jau Sattu", "customers": 4}]
    assert out["bought_together"]["share_with_another_product_pct"] == 10.0


def value_data():
    def row(name, ltv90, cac, ratio):
        return {"product": name, "category": "Sattu", "new_customers": 100,
                "ltv": {"30": 100, "60": 120, "90": ltv90, "180": None}, "cac": cac,
                "ltv_cac": ratio, "payback_days": 0}
    return {"brand": "Mithila Foods", "as_of": "2026-10-06", "history_start": "2026-01-01",
            "priced_from": "2026-01-01",
            "total": {"new_customers": 300, "ltv": {"90": 394.1}, "cac": 179.65, "ltv_cac": 2.19},
            "cac_months": [{"month": "2026-09", "cac": 184.11, "cac_customers": 6038}],
            "categories": [], "grid": [{"month": "2026-01", "customers": 10,
                                        "cells": [{"active_pct": 1.0, "revenue_per_customer": 390.7}, None]}],
            "rows": [row("Raw Flaxseed", 156, 183, 0.85), row("Kulthi Dal", 300, 58, 4.2),
                     row("New Thing", None, None, None)]}


def test_value_products_lowest_ltv_cac_first_with_a_missing_ratio_last():
    rows = tools.value_products(value_data(), {"sort_by": "ltv_cac", "ascending": True})["rows"]
    assert [r["product"] for r in rows] == ["Raw Flaxseed", "Kulthi Dal", "New Thing"]
    assert rows[0]["ltv_90"] == 156 and rows[2]["ltv_cac"] is None and rows[0]["ltv_180"] is None


def test_cohorts_keep_an_incomplete_month_as_null():
    c = tools.cohorts(value_data(), {})["cohorts"][0]
    assert c["still_buying_pct"] == [100.0, None] and c["spend_per_customer"] == [391, None]


# ── read-only, by construction ──────────────────────────────────────────────────────────────────

def test_the_tool_list_is_exactly_the_seven_read_only_tools():
    assert set(tools.TOOL_NAMES) == {"profit_products", "profit_product", "profit_categories",
                                     "repeat_products", "repeat_flows", "value_products", "cohorts"}


@pytest.mark.parametrize("module", ["app/assistant/tools.py", "app/assistant/service.py"])
def test_the_assistant_imports_nothing_that_writes(module):
    tree = ast.parse((ROOT / module).read_text(encoding="utf-8"))
    names = [a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in n.names] + [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    forbidden = ("ads", "refresh", "spapi", "save_decision", "apply", "fetch")
    assert not [x for x in names if any(f in x.split(".") for f in forbidden)], names


# ── the loop ────────────────────────────────────────────────────────────────────────────────────

def fake_converse(replies):
    calls = []

    async def converse(**kw):
        calls.append(json.loads(json.dumps(kw, default=str)))
        return replies[len(calls) - 1]
    converse.calls = calls
    return converse


def tool_turn(name, inp, uid="t1"):
    return {"stopReason": "tool_use", "usage": {"inputTokens": 100, "outputTokens": 20,
                                                "cacheReadInputTokens": 3000},
            "output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": uid, "name": name, "input": inp}}]}}}


def text_turn(text):
    return {"stopReason": "end_turn", "usage": {"inputTokens": 400, "outputTokens": 80},
            "output": {"message": {"role": "assistant", "content": [{"text": text}]}}}


def sources_for(data):
    async def profit(inp):
        return data, CATS
    return tools.Sources(profit, None, None)


async def test_ask_runs_a_tool_feeds_its_result_back_and_returns_the_answer():
    data = profit_data([parent("Chana Sattu", 1000, 300, -50)])
    conv = fake_converse([tool_turn("profit_products", {"sort_by": "net"}), text_turn("**Chana** loses.")])
    out = await service.ask("who loses?", [], {"tab": "profit"}, sources_for(data), converse=conv)
    assert out["answer"] == "**Chana** loses."
    assert out["sources"] == ["Profit · 2026-09-10 → 2026-10-09"]
    assert out["usage"] == {"input": 500, "output": 100, "cache_read": 3000}
    result = conv.calls[1]["messages"][-1]["content"][0]["toolResult"]
    assert result["toolUseId"] == "t1" and result["content"][0]["json"]["rows"][0]["net"] == -50


async def test_the_system_prompt_and_tools_sit_before_a_cache_point():
    conv = fake_converse([text_turn("hi")])
    await service.ask("q", [], {}, sources_for(profit_data([])), converse=conv)
    assert conv.calls[0]["system"][-1] == {"cachePoint": {"type": "default"}}
    assert conv.calls[0]["tools"][-1] == {"cachePoint": {"type": "default"}}


async def test_only_the_last_turns_travel_and_the_screen_context_is_stated():
    history = [{"q": f"q{i}", "a": f"a{i}"} for i in range(6)]
    conv = fake_converse([text_turn("ok")])
    await service.ask("now?", history, {"tab": "repeat", "brand": "Howrah Foods"},
                      sources_for(profit_data([])), converse=conv)
    msgs = conv.calls[0]["messages"]
    assert len(msgs) == 2 * service.HISTORY_TURNS + 1 and msgs[0]["content"][0]["text"] == "q3"
    assert "Repeat customers tab" in msgs[-1]["content"][0]["text"]
    assert "Howrah Foods" in msgs[-1]["content"][0]["text"]


async def test_a_tool_that_breaks_becomes_an_error_result_not_a_500():
    async def broken(inp):
        raise ValueError("boom")
    conv = fake_converse([tool_turn("profit_products", {}), text_turn("sorry")])
    out = await service.ask("q", [], {}, tools.Sources(broken, None, None), converse=conv)
    result = conv.calls[1]["messages"][-1]["content"][0]["toolResult"]
    assert result["status"] == "error" and "boom" in result["content"][0]["json"]["error"]
    assert out["answer"] == "sorry"


async def test_the_loop_stops_after_MAX_ROUNDS_of_lookups():
    conv = fake_converse([tool_turn("profit_products", {}, uid=f"t{i}") for i in range(service.MAX_ROUNDS)])
    out = await service.ask("q", [], {}, sources_for(profit_data([])), converse=conv)
    assert len(conv.calls) == service.MAX_ROUNDS and "narrower" in out["answer"]


def test_an_oversized_result_drops_rows_and_says_so():
    big = {"source": "Profit", "rows": [{"product": "x" * 200} for _ in range(400)]}
    fitted = service._fit(big)
    assert len(json.dumps(fitted)) <= service.MAX_RESULT_CHARS and "truncated" in fitted


# ── the Bedrock client ──────────────────────────────────────────────────────────────────────────

async def test_converse_sends_the_bearer_key_to_the_models_converse_url(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "bedrock_api_key", "KEY123")
    monkeypatch.setattr(s, "assistant_model", "us.anthropic.claude-sonnet-5-5")
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"stopReason": "end_turn"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await real_converse(system=[], messages=[], tools=[], client=client)
    assert seen["url"].endswith("/model/us.anthropic.claude-sonnet-5-5/converse")
    assert seen["auth"] == "Bearer KEY123"
    assert seen["body"]["additionalModelRequestFields"] == {"thinking": {"type": "between_tools"}}


async def test_a_bedrock_refusal_raises_with_amazons_own_message():
    def handler(request):
        return httpx.Response(403, json={"message": "Access denied to model"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(bedrock.BedrockError, match="403.*Access denied"):
            await real_converse(system=[], messages=[], tools=[], client=client)


# ── the route ───────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "bedrock_api_key", "test-key")


async def test_the_route_says_so_when_bedrock_is_not_configured(auth_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "bedrock_api_key", "")
    r = await auth_client.post("/portfolio/ask", json={"question": "hi"})
    assert r.status_code == 503 and "not configured" in r.json()["error"]


async def test_an_answer_is_returned_and_logged_with_its_tokens_and_tools(auth_client, configured,
                                                                          monkeypatch, count_rows):
    async def fake_ask(question, history, context, sources):
        assert context == {"tab": "value", "brand": "Mithila Foods"}
        return {"answer": "A", "sources": ["Customer value"], "tools": [{"name": "value_products", "input": {}}],
                "usage": {"input": 10, "output": 5, "cache_read": 3000}}
    monkeypatch.setattr(service, "ask", fake_ask)
    r = await auth_client.post("/portfolio/ask", json={
        "question": "best?", "context": {"tab": "value", "brand": "Mithila Foods", "evil": "x"}})
    assert r.status_code == 200 and r.json()["answer"] == "A"
    assert await count_rows(AssistantLog, question="best?", output_tokens=5, tab="value") == 1


async def test_the_daily_limit_refuses_before_any_call(auth_client, configured, monkeypatch, db):
    monkeypatch.setattr(get_settings(), "assistant_daily_limit", 2)
    for _ in range(2):
        db.add(AssistantLog(question="q"))
    await db.commit()
    r = await auth_client.post("/portfolio/ask", json={"question": "third"})
    assert r.status_code == 429 and "resets at midnight IST" in r.json()["error"]


async def test_a_bedrock_failure_is_a_502_and_is_logged(auth_client, configured, monkeypatch, count_rows):
    async def fail(*a, **k):
        raise bedrock.BedrockError("Bedrock answered 429: Too many requests")
    monkeypatch.setattr(service, "ask", fail)
    r = await auth_client.post("/portfolio/ask", json={"question": "q"})
    assert r.status_code == 502 and "429" in r.json()["error"]
    assert await count_rows(AssistantLog, error="Bedrock answered 429: Too many requests") == 1


async def test_an_empty_question_is_refused(auth_client, configured):
    assert (await auth_client.post("/portfolio/ask", json={"question": "  "})).status_code == 400


async def test_a_login_without_portfolio_cannot_ask(ops_client, configured):
    assert (await ops_client.post("/portfolio/ask", json={"question": "q"})).status_code in (303, 403)


# ── the panel ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("page,tab", [("portfolio.html", "profit"), ("portfolio_repeat.html", "repeat"),
                                      ("portfolio_value.html", "value")])
def test_every_portfolio_sub_tab_carries_the_panel_and_says_what_is_on_screen(page, tab):
    text = (ROOT / "templates" / page).read_text(encoding="utf-8")
    assert '{% include "_ask_panel.html" %}' in text
    assert "function askContext()" in text and f'tab: "{tab}"' in text


def test_the_panel_escapes_everything_before_formatting_the_answer():
    panel = (ROOT / "templates" / "_ask_panel.html").read_text(encoding="utf-8")
    assert "return esc(s).replace(" in panel
    assert "/portfolio/ask" in panel and "history.slice(-3)" in panel


async def test_the_daily_cap_counts_the_IST_day_not_the_UTC_one(db, monkeypatch):
    """00:30 IST on the 10th is 19:00 UTC on the 9th: today's question. 23:30 IST on the 9th is not."""
    from datetime import date, datetime
    monkeypatch.setattr("app.ist.today", lambda: date(2026, 10, 10))
    db.add(AssistantLog(question="just after midnight IST", created_at=datetime(2026, 10, 9, 19, 0)))
    db.add(AssistantLog(question="late last night IST", created_at=datetime(2026, 10, 9, 18, 0)))
    await db.commit()
    assert await service.questions_today(db) == 1


async def test_ask_uses_the_live_client_when_none_is_injected(monkeypatch):
    conv = fake_converse([text_turn("from the module client")])
    monkeypatch.setattr("app.assistant.bedrock.converse", conv)
    out = await service.ask("q", [], {}, sources_for(profit_data([])))
    assert out["answer"] == "from the module client" and len(conv.calls) == 1


# ── totals the model quotes rather than adds up (reported: "the results of the ai chat are off") ──

def weighed(name, sales, net, wo, wn, units_ordered, units, **kw):
    p = parent(name, sales, sales * 0.3, net, units=units, units_ordered=units_ordered, **kw)
    p.update(weight_ordered_kg=wo, weight_kg=wn, net_weighed=net if wn is not None else 0.0)
    return p


SATTU = [weighed("Chana Sattu", 428447, 140830, 1486.5, 1402.5, 1364, 1302),
         weighed("Jau Sattu", 226728, 63940, 844.5, 790.5, 1036, 968),
         weighed("Kulthi Sattu", 78181, -22885, 182.0, 173.0, 268, 255),
         weighed("Bengali Chana Sattu", 1200, 100, None, None, 4, 4)]


def test_a_filtered_total_carries_BOTH_weights_and_both_unit_bases():
    t = tools.profit_products(profit_data(SATTU), CATS, {})["total_of_matching"]
    assert t["weight_ordered_kg"] == 2513.0 and t["net_weight_kg"] == 2366.0
    assert t["units_ordered"] == 2672 and t["net_units"] == 2529


def test_a_row_carries_weight_ordered_beside_net_weight():
    row = tools.profit_products(profit_data(SATTU), CATS, {"search": "Jau"})["rows"][0]
    assert (row["weight_ordered_kg"], row["net_weight_kg"]) == (844.5, 790.5)


def test_the_total_is_the_PAGES_OWN_totals_row_on_the_same_rows():
    """The Profit tab's `computeTotals`, executed, is the reference: same weights, same Net ₹/kg."""
    from tests.js_harness import run_portfolio_js
    js = run_portfolio_js(f"const t = computeTotals({json.dumps(SATTU)}); "
                          "emit([t.weight, t.weightOrdered, t.netWeighed / t.weight]);")
    t = tools.profit_products(profit_data(SATTU), CATS, {})["total_of_matching"]
    assert [t["net_weight_kg"], t["weight_ordered_kg"], t["net_per_kg"]] == [
        round(js[0], 1), round(js[1], 1), round(js[2], 2)]


def test_categories_carry_their_weights_too():
    data = profit_data(SATTU + [weighed("Usna Chawal", 179601, 88800, 350.0, 334.0, 334, 330)])
    data["category_totals"] = {"categories": [{"category": "Sattu", "products": 4},
                                              {"category": "Rice", "products": 1}]}
    owner = {**CATS, "kulthi sattu": 1, "bengali chana sattu": 1}
    cats = {c["category"]: c for c in tools.profit_categories(data, owner, {})["categories"]}
    assert cats["Sattu"]["net_weight_kg"] == 2366.0 and cats["Rice"]["weight_ordered_kg"] == 350.0


def test_the_prompt_says_to_give_both_weights_and_to_quote_totals():
    assert "weight_ordered_kg" in service.SYSTEM and "never add rows up yourself" in service.SYSTEM


def test_a_set_whose_pack_weights_are_all_unknown_totals_to_a_dash_not_zero_kg():
    t = tools.profit_products(profit_data([SATTU[-1]]), CATS, {})["total_of_matching"]
    assert t["net_weight_kg"] is None and t["weight_ordered_kg"] is None and t["net_per_kg"] is None
