"""The Ads routes and template.

`POST /ads/apply` is the only endpoint in this app that changes the seller account, so the tests
that matter most here are the ones proving nothing reaches Amazon that was not previewed, approved,
and re-checked against the live bid.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import permissions
from app.ads import logic, repository


def _template() -> str:
    return (Path(__file__).parent.parent / "templates" / "ads.html").read_text(encoding="utf-8")


async def _seed(db, rows=None):
    """One window of performance data, so preview has something to read."""
    rows = rows or [
        {"keywordId": "111", "matchType": "PHRASE", "keyword": "makhana",
         "cost": 2620.0, "sales7d": 3589.4, "keywordBid": 18.75, "clicks": 120,
         "impressions": 9000, "purchases7d": 9, "campaignId": "c1", "adGroupId": "g1"},
        {"keywordId": "222", "matchType": "TARGETING_EXPRESSION_PREDEFINED",
         "keyword": "close-match", "cost": 832.0, "sales7d": 2337.9, "keywordBid": 10.66,
         "clicks": 40, "impressions": 3441, "purchases7d": 5,
         "campaignId": "c1", "adGroupId": "g1"},
        {"keywordId": "333", "matchType": "EXACT", "keyword": "dormant",
         "cost": 0.0, "sales7d": 0.0, "keywordBid": 7.0, "clicks": 0,
         "impressions": 12, "purchases7d": 0, "campaignId": "c2", "adGroupId": "g2"},
    ]
    await repository.save_performance(db, "2026-08-21", "2026-08-27", rows)
    await repository.save_entities(db, [
        {"entity_type": "campaign", "entity_id": "c1", "campaign_id": "c1",
         "name": "MF_SP_keywords", "state": "ENABLED", "daily_budget": 5000.0},
        {"entity_type": "campaign", "entity_id": "c2", "campaign_id": "c2",
         "name": "MF_SP_auto", "state": "ENABLED", "daily_budget": 2000.0},
        {"entity_type": "ad_group", "entity_id": "g1", "parent_id": "c1", "campaign_id": "c1",
         "name": "Sattu", "state": "ENABLED", "default_bid": 3.0},
    ])


# ─── Access ──────────────────────────────────────────────────────────────────


async def test_the_ads_area_is_denied_by_default(client, db):
    """**A new area is invisible until granted.** `has()` returns False for anything unrecognised,
    so adding `ads` cannot widen anyone's access on deploy."""
    assert permissions.ADS in permissions.AREA_KEYS
    assert not permissions.has(permissions.serialise([]), permissions.ADS)
    # And it is NOT in the Packer or Accounts presets — the only area that can spend money has to
    # be granted knowingly rather than arriving with a job title.
    assert permissions.ADS not in permissions.PRESETS[permissions.ROLE_PACKER]
    assert permissions.ADS not in permissions.PRESETS[permissions.ROLE_ACCOUNTS]
    assert permissions.ADS in permissions.PRESETS[permissions.ROLE_OWNER]


async def test_the_ads_page_and_api_are_gated(client, db):
    """Signed out, both redirect rather than answering."""
    for path in ("/ads-page", "/ads"):
        response = await client.get(path, follow_redirects=False)
        assert response.status_code in (302, 303, 401, 403), path


# ─── Reads ───────────────────────────────────────────────────────────────────


async def test_the_dashboard_reports_per_campaign_totals_summed_from_the_rows(auth_client, db):
    """A campaign total is summed from the SAME rows the table shows, never a second query.

    The Orders tab reported "86 orders" beside "87 lines" for exactly that reason, and here the two
    numbers would drive a bid decision.
    """
    await _seed(db)
    body = (await auth_client.get("/ads?start=2026-08-21&end=2026-08-27")).json()

    assert body["cached"] is True
    assert body["window"] == ["2026-08-21", "2026-08-27"]

    by_id = {c["campaign_id"]: c for c in body["campaigns"]}
    assert by_id["c1"]["spend"] == pytest.approx(3452.0)
    assert by_id["c1"]["targets"] == 2
    # Rolled up from the rows, so the total equals their sum.
    assert body["totals"]["spend"] == pytest.approx(3452.0)


async def test_a_campaign_with_no_spend_has_no_roas(auth_client, db):
    """None, not 0 — 0 would sort a dormant campaign beside the genuinely terrible ones."""
    await _seed(db)
    body = (await auth_client.get("/ads?start=2026-08-21&end=2026-08-27")).json()
    by_id = {c["campaign_id"]: c for c in body["campaigns"]}
    assert by_id["c2"]["roas"] is None
    assert by_id["c2"]["acos"] is None


async def test_an_uncached_window_returns_empty_rather_than_fetching(auth_client, db):
    """**A GET must never block on a 6-minute report.**

    It returns `cached: false` so the screen can offer the button. A GET that started the fetch
    would hold the connection open behind Caddy, and a second page load would start a second report.
    """
    await _seed(db)
    body = (await auth_client.get("/ads?start=2026-07-01&end=2026-07-07")).json()
    assert body["cached"] is False
    assert body["campaigns"], "the campaign list should still render"
    assert body["totals"]["spend"] == 0


async def test_a_window_over_sixty_days_is_refused(auth_client, db):
    """60 days is the owner's stated horizon for optimisation data."""
    response = await auth_client.get("/ads?days=90")
    assert response.status_code == 400
    assert "60" in response.json()["error"]


async def test_a_window_including_today_is_refused(auth_client, db):
    """Today's figures are still settling: a click costs immediately while its attributed sale can
    land hours later, so a rule reading today would cut bids on a measurement artefact."""
    from datetime import date
    today = date.today().isoformat()
    response = await auth_client.get(f"/ads?start=2026-08-01&end={today}")
    assert response.status_code == 400
    assert "today" in response.json()["error"].lower()


async def test_a_reversed_window_is_refused(auth_client, db):
    response = await auth_client.get("/ads?start=2026-08-27&end=2026-08-01")
    assert response.status_code == 400


# ─── Preview ─────────────────────────────────────────────────────────────────


async def test_preview_computes_changes_and_sends_nothing(auth_client, db):
    """**The whole point of two endpoints.** Preview stores nothing and calls no Amazon write."""
    await _seed(db)
    response = await auth_client.post("/ads/preview", json={
        "start": "2026-08-21", "end": "2026-08-27",
        "conditions": [{"field": "spend", "op": "gt", "value": 100},
                       {"field": "roas", "op": "gt", "value": 1},
                       {"field": "roas", "op": "lt", "value": 3}],
        "action": "decrease_pct", "amount": 10,
    })
    body = response.json()
    assert response.status_code == 200
    assert body["blocked"] is None
    assert body["totals"]["changing"] == 2
    # One keyword and one targeting clause — two different Amazon endpoints.
    assert body["totals"]["keywords"] == 1
    assert body["totals"]["targets"] == 1

    by_id = {c["entity_id"]: c for c in body["changes"]}
    assert by_id["111"]["new_bid"] == 16.88
    assert by_id["222"]["new_bid"] == 9.59

    # Nothing was written to the ledger by a preview.
    assert await repository.load_runs(db) == []


async def test_preview_names_the_rule_in_words(auth_client, db):
    """Stored on every ledger row, so the history reads without joining to a rule that may since
    have been edited."""
    await _seed(db)
    body = (await auth_client.post("/ads/preview", json={
        "start": "2026-08-21", "end": "2026-08-27",
        "conditions": [{"field": "spend", "op": "gt", "value": 100}],
        "action": "decrease_pct", "amount": 10,
    })).json()
    assert "Spend" in body["rule"] and "10" in body["rule"]


async def test_preview_refuses_a_rule_that_breaches_the_change_limit(auth_client, db):
    """Blocked, not skipped: the rule is wrong, so showing a table the owner might approve would be
    the wrong answer."""
    await _seed(db)
    body = (await auth_client.post("/ads/preview", json={
        "start": "2026-08-21", "end": "2026-08-27",
        "conditions": [{"field": "spend", "op": "gt", "value": 100}],
        "action": "decrease_pct", "amount": 90,
    })).json()
    assert body["blocked"]
    assert body["changes"] == []


async def test_preview_on_an_unfetched_window_says_so(auth_client, db):
    """Rather than returning "0 matches", which reads as "your rule found nothing"."""
    await _seed(db)
    response = await auth_client.post("/ads/preview", json={
        "start": "2026-07-01", "end": "2026-07-07",
        "conditions": [{"field": "spend", "op": "gt", "value": 100}],
        "action": "decrease_pct", "amount": 10,
    })
    assert response.status_code == 400
    assert "refresh" in response.json()["error"].lower()


async def test_preview_scoped_to_a_campaign_ignores_the_rest(auth_client, db):
    """"Go inside one campaign and run the rule" — the scope must bound the write."""
    await _seed(db)
    body = (await auth_client.post("/ads/preview", json={
        "start": "2026-08-21", "end": "2026-08-27",
        "conditions": [{"field": "spend", "op": "gt", "value": 100}],
        "action": "decrease_pct", "amount": 10,
        "campaign_ids": ["c2"],
    })).json()
    assert body["totals"]["changing"] == 0, "c2 has only a zero-spend row"


# ─── Apply: the guards, without touching Amazon ──────────────────────────────


async def test_apply_refuses_an_empty_change_list(auth_client, db):
    response = await auth_client.post("/ads/apply", json={"changes": []})
    assert response.status_code == 400


async def test_apply_re_validates_the_ceiling_because_a_client_is_not_a_trust_boundary(
    auth_client, db
):
    """**The browser sends the approved list back, so it must be re-checked.**

    The preview already applied the guardrails, but a hand-edited request must not be able to exceed
    the ceiling — Amazon accepted a Rs 1,000 bid in testing and will not stop it.
    """
    await _seed(db)
    response = await auth_client.post("/ads/apply", json={
        "rule": "hand-edited",
        "changes": [{"entity_id": "111", "writer": "keyword",
                     "old_bid": 18.75, "new_bid": 999.0}],
    })
    assert response.status_code == 400
    assert "ceiling" in response.json()["error"].lower()
    # And nothing was recorded — the refusal happens before the ledger is opened.
    assert await repository.load_runs(db) == []


async def test_apply_refuses_a_bid_under_the_floor(auth_client, db):
    await _seed(db)
    response = await auth_client.post("/ads/apply", json={
        "rule": "r",
        "changes": [{"entity_id": "111", "writer": "keyword",
                     "old_bid": 18.75, "new_bid": 0.2}],
    })
    assert response.status_code == 400
    assert "floor" in response.json()["error"].lower()


async def test_apply_refuses_more_rows_than_the_limit(auth_client, db):
    await _seed(db)
    response = await auth_client.post("/ads/apply", json={
        "rule": "r",
        "changes": [{"entity_id": str(i), "writer": "keyword", "old_bid": 10.0, "new_bid": 9.0}
                    for i in range(600)],
    })
    assert response.status_code == 400
    assert "600" in response.json()["error"]


# ─── Undo ────────────────────────────────────────────────────────────────────


async def test_undo_refuses_a_run_with_nothing_applied(auth_client, db):
    """An all-failed run changed nothing at Amazon, so an undo would write bids nobody set."""
    run_id = await repository.open_run(
        db, [{"entity_id": "1", "writer": "keyword", "old_bid": 10.0, "new_bid": 9.0}],
        rule_summary="r",
    )
    await repository.record_results(db, run_id, [
        {"entity_id": "1", "ok": False, "error": "nope"},
    ])
    response = await auth_client.post(f"/ads/undo/{run_id}")
    assert response.status_code == 400
    assert "undone" in response.json()["error"].lower()


async def test_a_run_detail_is_readable(auth_client, db):
    """So a failure can be read rather than guessed at."""
    run_id = await repository.open_run(
        db, [{"entity_id": "1", "writer": "keyword", "text": "makhana",
              "old_bid": 18.75, "new_bid": 16.88}],
        rule_summary="spend>100 -> bid decrease 10%",
    )
    body = (await auth_client.get(f"/ads/runs/{run_id}")).json()
    assert body["count"] == 1
    assert body["rows"][0]["old_bid"] == 18.75
    assert body["rows"][0]["new_bid"] == 16.88

    assert (await auth_client.get("/ads/runs/does-not-exist")).status_code == 404


# ─── Guardrails and rules ────────────────────────────────────────────────────


async def test_guardrails_round_trip_through_the_api(auth_client, db):
    body = (await auth_client.get("/ads/guardrails")).json()
    assert body["guardrails"]["max_bid"] == logic.DEFAULT_GUARDRAILS["max_bid"]
    assert body["help"]["max_bid"], "the ceiling has no explanation"

    saved = await auth_client.post("/ads/guardrails", json={"guardrails": {"max_bid": 45.0}})
    assert saved.json()["guardrails"]["max_bid"] == 45.0

    reset = await auth_client.post("/ads/guardrails", json={"reset": True})
    assert reset.json()["guardrails"]["max_bid"] == logic.DEFAULT_GUARDRAILS["max_bid"]


async def test_an_absurd_guardrail_is_refused_with_its_reason(auth_client, db):
    response = await auth_client.post("/ads/guardrails",
                                      json={"guardrails": {"max_change_pct": 5000}})
    assert response.status_code == 400
    assert "max_change_pct" in response.json()["error"]


async def test_a_rule_round_trips_and_an_unusable_one_is_refused(auth_client, db):
    saved = await auth_client.post("/ads/rules", json={
        "name": "cut the mediocre",
        "conditions": [{"field": "spend", "op": "gt", "value": 100}],
        "action": "decrease_pct", "amount": 10, "window_days": 7,
    })
    assert saved.status_code == 200

    listed = (await auth_client.get("/ads?days=7")).json()["rules"]
    assert any(r["name"] == "cut the mediocre" for r in listed)

    # An empty condition value would become a live `> 0` — refused on the way in.
    bad = await auth_client.post("/ads/rules", json={
        "name": "broken", "conditions": [{"field": "spend", "op": "gt", "value": ""}],
        "action": "decrease_pct", "amount": 10,
    })
    assert bad.status_code == 400

    assert (await auth_client.delete("/ads/rules/cut the mediocre")).status_code == 200
    assert (await auth_client.delete("/ads/rules/cut the mediocre")).status_code == 404


async def test_the_refresh_status_route_answers_without_a_refresh_running(auth_client, db):
    body = (await auth_client.get("/ads/refresh-status")).json()
    assert body["running"] is False
    assert "phase_label" in body, "the bar would show a raw phase key"


async def test_no_decimal_or_datetime_reaches_json(auth_client, db):
    """`JSONResponse` cannot serialise either, and this app has shipped that defect twice."""
    await _seed(db)
    run_id = await repository.open_run(
        db, [{"entity_id": "1", "writer": "keyword", "old_bid": 10.0, "new_bid": 9.0}],
        rule_summary="r",
    )
    await repository.record_results(db, run_id, [{"entity_id": "1", "ok": True}])
    # Every route that can carry a Numeric or a datetime.
    for path in ("/ads?start=2026-08-21&end=2026-08-27", "/ads/runs",
                 f"/ads/runs/{run_id}", "/ads/guardrails", "/ads/refresh-status",
                 "/ads/targets?start=2026-08-21&end=2026-08-27&campaign_id=c1"):
        response = await auth_client.get(path)
        assert response.status_code == 200, path
        json.dumps(response.json())


# ─── The template ────────────────────────────────────────────────────────────


def test_the_template_has_every_control_the_feature_needs():
    """These ids are contracts with the JavaScript.

    `tests/test_template_render_targets.py` catches a getElementById with no element; this catches
    the other direction — an element the design calls for that was never added.
    """
    source = _template()
    for element in ("window-bar", "conditions", "add-condition", "action", "amount",
                    "preview-btn", "preview-area", "table-area", "search",
                    "selection-count", "select-all", "select-none",
                    "guardrails-panel", "history-panel", "refresh-btn"):
        assert f'id="{element}"' in source, f"{element} is missing"


def test_the_template_uses_delegated_listeners_not_inline_handlers():
    """**Keyword text comes from Amazon and campaign names from the owner.**

    Building an onclick out of either is an injection waiting to happen. The Orders tab had to be
    fixed for exactly this, so the rule is asserted rather than remembered.
    """
    source = _template()
    for forbidden in ('onclick="', "onclick='", 'onchange="', "onchange='"):
        assert forbidden not in source, f"inline handler {forbidden} builds a handler from data"
    for wired in ('$("table-area").addEventListener', '$("preview-area").addEventListener',
                  '$("conditions").addEventListener', '$("window-bar").addEventListener',
                  '$("guardrails-panel").addEventListener',
                  '$("history-panel").addEventListener'):
        assert wired in source, f"{wired} is missing, so that control does nothing"


def test_the_template_escapes_every_server_string():
    """**Amazon's keyword text, the owner's campaign names, and Amazon's error messages all reach
    innerHTML.** Any of the three could contain a `<`.

    Checked field by field rather than by eyeballing the file: each of these appears in a template
    literal that becomes innerHTML, so each must be wrapped in `esc(`.
    """
    source = _template()
    for field in ("c.text", "c.campaign_name", "r.rule", "r.error", "g.name",
                  "c.name", "s.reason", "r.entity_id", "body.error", "plan.blocked"):
        # Every occurrence inside an interpolation must be escaped.
        for bad in (f"${{{field}}}", f"${{{field} "):
            assert bad not in source, (
                f"{field} reaches innerHTML unescaped — wrap it in esc(). Amazon and the owner "
                f"both supply text here."
            )
    assert "const esc = s =>" in source, "the escaper is missing"


def test_the_template_shows_which_endpoint_each_row_writes_to():
    """**Not decoration — it is the Amazon API the row will be sent to.**

    Keywords and targeting clauses are different endpoints and the report labels both ids
    `keywordId`, so showing the split is how a routing bug becomes visible rather than silent.
    """
    source = _template()
    assert "function writerTag(" in source
    body = source[source.index("function writerTag("):][:400]
    assert "keyword" in body and "auto" in body and "product" in body


def test_the_template_requires_a_second_click_before_anything_is_sent():
    """Preview and Apply are separate, and Apply is styled as the dangerous action."""
    source = _template()
    assert 'id="preview-btn"' in source and 'id="apply-btn"' in source
    assert "btn-danger" in source, "Apply is not visually distinguished from Preview"
    # The apply handler must read the APPROVED set, not the whole plan.
    body = source[source.index("async function applyPlan("):][:500]
    assert "approved.has" in body, (
        "apply sends the whole plan rather than the rows still ticked, so deselecting a row would "
        "do nothing"
    )


def test_the_template_names_every_skipped_row_with_its_reason():
    """A row missing from a 299-row run is otherwise indistinguishable from a bug."""
    source = _template()
    assert "function skippedHtml(" in source
    body = source[source.index("function skippedHtml("):][:700]
    assert "s.reason" in body or "reason" in body


def test_the_template_says_on_screen_that_this_tab_changes_live_bids():
    """The only page in the app that writes to Amazon should say so, not just in the code."""
    source = _template()
    assert "changes live bids" in source.lower()


def test_the_window_picker_cannot_offer_today():
    source = _template()
    assert "function maxDate(" in source
    assert "getDate() - 1" in source[source.index("function maxDate("):][:300]


def test_the_table_scrolls_inside_a_wrapper_rather_than_moving_the_page():
    """The defect /qa found on the Portfolio tab: 744px of sideways document scroll at 350px."""
    source = _template()
    assert ".table-wrap{" in source and "overflow-x:auto" in source
    assert '<div class="table-wrap">' in source
    rule = source[source.index("table{width:100%"):]
    assert "min-width" in rule[:rule.index("}")], (
        "without a min-width the table shrinks to the wrapper and the nowrap cells overflow their "
        "own gridlines, so the wrapper never scrolls"
    )
