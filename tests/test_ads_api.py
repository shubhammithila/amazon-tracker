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


def _code_only(source: str) -> str:
    """Strip JS comments, so an assertion tests the CODE rather than the prose explaining it.

    Needed because the comments in this template deliberately name the things they forbid —
    "never use toISOString", "renderConditions() rebuilds every row" — so a naive substring check
    matches the warning and reports the opposite of the truth. Both `/* ... */` and `//`.
    """
    import re
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(
        line for line in without_blocks.splitlines() if not line.strip().startswith("//")
    )


def _js_function(source: str, name: str) -> str:
    """One JS function's body, by matching braces.

    Brace-matched rather than sliced up to the next comment or `function` keyword: both of those are
    positional accidents, and a slice keyed on one silently starts testing the WRONG function when
    something is inserted between them. This raises instead of quietly passing.
    """
    start = source.index(f"function {name}(")
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"{name} has no closing brace")


#: The window every test in this file reads, and every day inside it.
#:
#: Seeded as PER-DAY rows because that is now the only grain — `ads_performance` is deleted, and
#: `daily_range_complete` requires every day of a requested range to be held before anything sums it.
SEED_START, SEED_END = "2026-08-21", "2026-08-27"
SEED_DAYS = ("2026-08-21", "2026-08-22", "2026-08-23", "2026-08-24",
             "2026-08-25", "2026-08-26", "2026-08-27")


async def _seed(db, rows=None, *, ad_product="sp"):
    """One window of performance data, so preview has something to read.

    **Writes DAILY rows, one set per day of the window.** This used to call `save_performance` with a
    single window-grain set; that table is gone, because holding the same figures at two grains is
    what made Rs 1,26,328 of Sponsored Brands spend vanish from any window nobody had fetched
    exactly.

    **Each row's totals are put on ONE day, not on every day.** Spreading them would multiply every
    asserted figure in this file by seven; the other six days carry a zero-value filler row purely so
    `daily_range_complete` sees a complete range. The filler uses its own entity id so it cannot be
    mistaken for a real row by a test that counts them.
    """
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
    # The real rows land on the window's LAST day, so `sum_daily` over the window returns exactly
    # these figures and every pre-existing assertion in this file still means what it meant.
    await repository.save_daily(
        db, [{**row, "date": SEED_END} for row in rows], ad_product=ad_product,
    )
    # Filler for the remaining days, so the range is complete. Zero-valued, on its own entity id AND
    # its own campaign/ad group — otherwise it counts as an extra target under c1/g1 and the "2
    # targets" assertions become 3. It belongs to no cached entity, so no campaign row claims it.
    filler = [
        {"keywordId": "__filler__", "matchType": "EXACT", "keyword": "filler", "date": day,
         "cost": 0.0, "sales7d": 0.0, "sales": 0.0, "purchases7d": 0, "purchases": 0,
         "keywordBid": None, "clicks": 0, "impressions": 0,
         "campaignId": "__filler_c__", "adGroupId": "__filler_g__"}
        for day in SEED_DAYS if day != SEED_END
    ]
    await repository.save_daily(db, filler, ad_product=ad_product)
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


async def test_today_is_allowed_but_the_future_is_not(auth_client, db):
    """**This assertion FLIPPED, and the reason is worth recording.**

    It previously demanded that a window including today be REFUSED, because today's ROAS reads low —
    a click costs immediately while its attributed sale arrives hours later. That was a defensible
    default and it was wrong for this owner, who asked for near-real-time figures and cannot get them
    from a tab capped at yesterday.

    **Amazon answers for today**: verified against the live API, a report ending today returns
    HTTP 200 with real spend. So the cap moved to today and the SCREEN carries the caveat instead —
    refusing to show today's spend is the wrong trade for a dashboard whose purpose is watching spend.

    Tomorrow is still refused, because that is not a caveat, it is a mistake.
    """
    from datetime import date, timedelta
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    assert (await auth_client.get(f"/ads?start=2026-08-01&end={today}")).status_code == 200

    future = await auth_client.get(f"/ads?start=2026-08-01&end={tomorrow}")
    assert future.status_code == 400
    assert "future" in future.json()["error"].lower()


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
    """Derived from the limit, not hardcoded — a fixed count stops testing anything the moment the
    limit is raised, which is what happened when max_rows went 500 -> 1000."""
    from app.ads import logic as ads_logic

    await _seed(db)
    over = ads_logic.DEFAULT_GUARDRAILS["max_rows"] + 1
    response = await auth_client.post("/ads/apply", json={
        "rule": "r",
        "changes": [{"entity_id": str(i), "writer": "keyword", "old_bid": 10.0, "new_bid": 9.0}
                    for i in range(over)],
    })
    assert response.status_code == 400
    assert str(over) in response.json()["error"]


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


# ─── Per-day rows: any sub-range without a refetch ───────────────────────────


def _daily_rows(days, entity="111", spend=100.0, sales=250.0):
    """Report-shaped DAILY rows: the same entity on several days."""
    return [
        {"keywordId": entity, "matchType": "PHRASE", "keyword": "makhana",
         "cost": spend, "sales7d": sales, "keywordBid": 10.0 + index, "clicks": 5,
         "impressions": 100, "purchases7d": 1, "campaignId": "c1", "adGroupId": "g1",
         "date": day}
        for index, day in enumerate(days)
    ]


async def test_a_sub_range_is_summed_from_daily_rows_with_no_amazon_call(db):
    """**The answer to "I have 30 days — why must I refetch to see 20 of them?"**

    `ads_performance` is per WINDOW, so a range nobody fetched has no row. The daily rows are
    summable, so any range inside the coverage is a GROUP BY rather than another ~6-minute report.
    """
    days = ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"]
    await repository.save_daily(db, _daily_rows(days, spend=100.0, sales=250.0))

    # Three of the five days: 300 spend, 750 sales.
    rows = await repository.sum_daily(db, "2026-08-02", "2026-08-04")
    assert len(rows) == 1
    assert rows[0]["spend"] == pytest.approx(300.0)
    assert rows[0]["sales"] == pytest.approx(750.0)
    assert rows[0]["clicks"] == 15
    assert rows[0]["roas"] == pytest.approx(2.5)

    # All five: 500 / 1250.
    everything = await repository.sum_daily(db, "2026-08-01", "2026-08-05")
    assert everything[0]["spend"] == pytest.approx(500.0)


async def test_a_summed_range_takes_the_latest_bid_not_the_sum_of_bids(db):
    """Adding bids across days would produce a number that means nothing.

    The bid is a rate, not a quantity: 10 + 11 + 12 is not "the bid over three days".
    """
    days = ["2026-08-01", "2026-08-02", "2026-08-03"]
    await repository.save_daily(db, _daily_rows(days))     # bids 10.0, 11.0, 12.0
    rows = await repository.sum_daily(db, "2026-08-01", "2026-08-03")
    assert rows[0]["bid"] == 12.0, "the bid should be the most recent day's, not a sum or an average"


async def test_an_incomplete_range_is_refused_rather_than_summed_short(db):
    """**A gap in the middle would make the total quietly understate spend.**

    And an understated spend is what a bid rule would then act on — so a partial range counts as
    absent and the owner is told to refresh, rather than being shown a plausible wrong number.
    """
    await repository.save_daily(db, _daily_rows(["2026-08-01", "2026-08-02", "2026-08-04"]))

    assert await repository.daily_range_complete(db, "2026-08-01", "2026-08-02") is True
    # 08-03 is missing from the middle.
    assert await repository.daily_range_complete(db, "2026-08-01", "2026-08-04") is False
    # And beyond the coverage entirely.
    assert await repository.daily_range_complete(db, "2026-07-25", "2026-08-01") is False


async def test_refetching_one_day_does_not_disturb_the_others(db):
    """Delete-then-insert is scoped per DAY, so a 7-day refresh cannot wipe the other 23."""
    await repository.save_daily(db, _daily_rows(["2026-08-01", "2026-08-02"], spend=100.0))
    # Refetch only 08-02, with a corrected figure.
    await repository.save_daily(db, _daily_rows(["2026-08-02"], spend=999.0))

    held = await repository.daily_days_held(db)
    assert held == {"2026-08-01", "2026-08-02"}, "refetching one day removed another"
    rows = await repository.sum_daily(db, "2026-08-01", "2026-08-02")
    assert rows[0]["spend"] == pytest.approx(1099.0), "the refetched day did not replace cleanly"


async def test_daily_rows_replace_rather_than_double_on_a_repeat_save(db):
    """The same day stored twice must not count twice — a repeated refresh is normal."""
    rows = _daily_rows(["2026-08-01"], spend=100.0)
    await repository.save_daily(db, rows)
    await repository.save_daily(db, rows)
    summed = await repository.sum_daily(db, "2026-08-01", "2026-08-01")
    assert summed[0]["spend"] == pytest.approx(100.0), "a repeated save doubled the spend"


async def test_a_daily_row_with_no_date_is_skipped_rather_than_guessed(db):
    """Filing it under a default day would put one day's spend into another."""
    rows = _daily_rows(["2026-08-01"])
    rows.append({**rows[0], "keywordId": "999", "date": None})
    stored = await repository.save_daily(db, rows)
    assert stored == 1
    assert "999" not in [r["entity_id"] for r in
                         await repository.sum_daily(db, "2026-08-01", "2026-08-01")]


async def test_old_daily_rows_are_purged_to_keep_the_disk_bounded(db):
    """**Not optional housekeeping.** Production is at 91% disk and every deploy copies the whole
    database, so an unbounded daily table would break both SQLite writes and the deploy."""
    from datetime import date, timedelta
    today = date(2026, 8, 29)
    recent = (today - timedelta(days=3)).isoformat()
    ancient = (today - timedelta(days=60)).isoformat()
    await repository.save_daily(db, _daily_rows([recent, ancient]))

    removed = await repository.purge_daily(db, keep_days=30, today=today)
    assert removed == 1
    held = await repository.daily_days_held(db)
    assert recent in held and ancient not in held


async def test_any_sub_range_of_the_daily_rows_is_answered_without_a_fetch(auth_client, db):
    """The route path, end to end: a range nobody fetched as such, summed from the days it covers.

    **This assertion CHANGED, and the reason is the point.** It used to also require
    `body["derived"] is True`, distinguishing "summed from daily rows" from "read from a window
    fetched as such" — a real distinction while both existed, and the source of a 28% error: the
    per-window table held Sponsored Brands rows and the daily table did not, so which path ran
    decided whether Rs 1,26,328 appeared. That table is deleted and `derived` with it, because every
    figure is now summed and a flag that is always true says nothing.

    What survives is the requirement the flag existed to serve: any range inside the coverage is
    answered instantly and correctly.
    """
    days = ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"]
    await repository.save_daily(db, _daily_rows(days, spend=100.0, sales=250.0))
    await repository.save_entities(db, [
        {"entity_type": "campaign", "entity_id": "c1", "campaign_id": "c1",
         "name": "MF_SP_keywords", "state": "ENABLED"},
    ])

    body = (await auth_client.get("/ads?start=2026-08-02&end=2026-08-03")).json()
    assert body["cached"] is True, "a range inside the daily rows should not read as uncached"
    assert "derived" not in body, (
        "`derived` is back — it can only ever be True now, and a flag that is always true invites "
        "a reader to believe there is another case"
    )
    assert body["daily_coverage"] == ["2026-08-01", "2026-08-04"]

    campaign = next(c for c in body["campaigns"] if c["campaign_id"] == "c1")
    assert campaign["spend"] == pytest.approx(200.0), "two days at 100 each"


async def test_a_rule_previews_against_a_derived_range(auth_client, db):
    """A rule must work on any range the tab will show, not only on fetched windows — otherwise
    picking 20 days out of 30 shows figures no rule can act on."""
    days = [f"2026-08-{d:02d}" for d in range(1, 8)]
    await repository.save_daily(db, _daily_rows(days, spend=500.0, sales=1000.0))

    response = await auth_client.post("/ads/preview", json={
        "start": "2026-08-02", "end": "2026-08-05",
        "conditions": [{"field": "spend", "op": "gt", "value": 100}],
        "action": "decrease_pct", "amount": 10,
    })
    body = response.json()
    assert response.status_code == 200, body
    assert body["totals"]["changing"] == 1
    # 4 days x 500 spend, and the bid is the latest day's (10.0 + index 4 = 14.0) -> -10% = 12.6
    assert body["changes"][0]["spend"] == pytest.approx(2000.0)
    assert body["changes"][0]["new_bid"] == pytest.approx(12.6)


def test_the_daily_report_asks_for_the_date_column_and_summary_does_not():
    """**Amazon's own asymmetry, and getting it wrong fails the whole request.**

    `date` is ILLEGAL under `timeUnit: SUMMARY` — it rejects the report — and required under DAILY to
    be of any use. One flag, two column lists.
    """
    from app.ads import reports

    summary = reports.build_report_request("2026-08-01", "2026-08-07")
    assert summary["configuration"]["timeUnit"] == "SUMMARY"
    assert "date" not in summary["configuration"]["columns"]

    daily = reports.build_report_request("2026-08-01", "2026-08-07", daily=True)
    assert daily["configuration"]["timeUnit"] == "DAILY"
    assert "date" in daily["configuration"]["columns"]


def test_daily_rows_are_not_collapsed_by_aggregate():
    """`aggregate` keys on entity id alone, so running it over daily rows would silently discard 29
    days out of 30. The daily path must bypass it."""
    from app.ads import reports

    source = (Path(__file__).parent.parent / "app" / "ads" / "reports.py").read_text(
        encoding="utf-8")
    assert "raw if daily else aggregate(raw)" in source, (
        "daily rows are passed through aggregate(), which would collapse them to one row per entity"
    )


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
    """Preview and Apply are separate, and Apply is styled as the dangerous action.

    The Apply marker changed from `id="apply-btn"` to `data-apply`, because the controls are now
    rendered TWICE — above and below a table that can hold 1,005 rows — and an id may appear only once
    in a document. The requirement is unchanged and is what this asserts.
    """
    source = _template()
    assert 'id="preview-btn"' in source and "data-apply" in source
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


def test_no_date_in_the_picker_goes_through_toisostring():
    """**A real bug: at 00:39 IST the picker offered the 27th when the 28th was available.**

    `toISOString()` converts to UTC first. IST is UTC+5:30, so between midnight and 05:30 the UTC
    date is still yesterday — and "local yesterday, then formatted as UTC" came out a further day
    early. It only misbehaved in those five and a half hours, which is exactly when nobody is
    watching.

    Same defect class CLAUDE.md records for the Orders tab, where `new Date("2026-08-25")` rendered
    as 05:30 the following morning. The rule is: build a bare date from local parts, never round-trip
    it through UTC.
    """
    source = _template()
    assert "function localDate(" in source, "there is no local date formatter"

    # Comments NAME the banned function while explaining why it is banned, so they must be stripped
    # before asserting — otherwise the test passes or fails on the prose rather than the code.
    script = _code_only(source[source.index("<script>"):])
    assert "toISOString" not in script, (
        "a date still goes through toISOString(), which shifts it by a day for the 5.5 hours after "
        "midnight IST"
    )
    body = source[source.index("function localDate("):][:500]
    assert "getFullYear()" in body and "getMonth()" in body and "getDate()" in body


def test_the_picker_allows_today_and_labels_it():
    """Today is selectable because Amazon answers for it (verified: HTTP 200 for today's date).

    What today is not is SETTLED, so the note beside the dates says ROAS will read low until
    tomorrow. Hiding today entirely was the previous behaviour and it made near-real-time impossible.
    """
    source = _template()
    assert "function settledDate(" in source, (
        "there is no separate notion of the last settled day, so presets cannot end on yesterday "
        "while the picker allows today"
    )

    # Bounded at the NEXT function, not by a character count — a fixed slice ran past maxDate into
    # settledDate, which correctly DOES subtract a day, and reported the opposite of the truth.
    start = source.index("function maxDate(")
    max_body = source[start:source.index("function settledDate(", start)]
    assert "getDate() - 1" not in max_body, "maxDate still subtracts a day, so today is unselectable"
    assert "localDate(new Date())" in max_body, "maxDate is not simply today"

    start = source.index("function settledDate(")
    settled = source[start:start + 300]
    assert "getDate() - 1" in settled, "settledDate must be yesterday"

    note = source[source.index("function renderWindowNote("):][:1400]
    assert "settledDate()" in note and "today" in note.lower(), (
        "nothing warns that a window ending today reads a low ROAS"
    )


def test_clicking_anywhere_in_a_date_box_opens_the_calendar():
    """A native `<input type="date">` only opens its picker from the small icon at the right edge.

    Clicking the middle of the box focuses a text field instead, which looks like nothing happened.
    `showPicker()` is the standards call; it is wrapped in try/catch because Firefox and older Safari
    do not implement it, and there typing still works.
    """
    source = _template()
    assert "showPicker()" in source, "clicking the box does not open the calendar"
    handler = source[source.index('$("window-bar").addEventListener("click"'):][:900]
    assert ".win-date" in handler, "the click handler does not target the date inputs"
    assert "try {" in handler, (
        "showPicker() is unguarded, so a browser without it would throw on every click in the box"
    )


def test_the_window_note_says_whether_a_range_costs_a_fetch():
    """TWO genuinely different costs, and the screen must not present them alike.

    **This test previously asserted THREE, and that assertion has flipped.** It required
    `exactlyCached()` — a range someone had fetched as such, read from its own per-window table —
    beside `insideDailyCoverage()`. Those two were only ever different in provenance, never in cost,
    and holding the same figures at both grains is what let Rs 1,26,328 of Sponsored Brands spend
    disappear from whichever path did not have it. The window table is gone, so the honest question
    is the one that remains: is this range summable from the days we hold, or does it cost a report?
    """
    source = _template()
    assert "function insideDailyCoverage(" in source
    assert "function exactlyCached(" not in source, (
        "the per-window path is back; it was deleted because two grains disagreed by 28% of spend"
    )
    note = source[source.index("function renderWindowNote("):][:1400]
    assert "insideDailyCoverage(" in note
    assert "no fetch needed" in note, "a summable range does not say that it is instant"
    assert "press Refresh" in note, "an unfetched range does not say what it will cost"


def test_changing_a_conditions_field_does_not_rebuild_the_row_being_edited():
    """**Found in a browser: building "roas > 1" produced "roas < 2".**

    `renderConditions()` rebuilds every row from scratch, so re-rendering on a `field` change
    destroys the `<select>` and `<input>` the user is still working through — the operator and value
    set immediately afterwards landed on detached elements and were silently lost. That is the normal
    left-to-right way to build a condition, and the result was a rule that ran with the seeded
    default while the screen looked correct.

    Measured: the wrong rule matched 197 rows where the intended one matches 283, so a bid change
    would have been applied to 86 targets the owner never selected.

    Only the unit hint depends on the field, so only that is updated in place.
    """
    source = _template()
    start = source.index('$("conditions").addEventListener("change"')
    # Up to the NEXT listener registration, so the whole handler body is covered. Slicing to the
    # first "});" stops inside the `if` block and would miss a re-render added after it.
    end = source.index('$("conditions").addEventListener("click"', start)
    handler = source[start:end]

    # The comment explaining the bug names the function, so comments must be stripped before
    # asserting on the code — otherwise the test matches the explanation rather than the fix. A
    # line-prefix filter is not enough here: the comment is a `/* ... */` block whose middle lines
    # start with ordinary prose.
    import re
    code = re.sub(r"/\*.*?\*/", "", handler, flags=re.S)
    code = "\n".join(
        line for line in code.splitlines() if not line.strip().startswith("//")
    )
    assert "renderConditions()" not in code, (
        "the change handler re-renders the conditions, which destroys the row being edited and "
        "silently discards the next edit the user makes"
    )
    assert "unitFor(" in code, "the unit hint no longer follows the field"


# ─── The five follow-up requests ─────────────────────────────────────────────


async def test_paused_campaigns_and_ad_groups_are_hidden_by_default(auth_client, db):
    """**Measured on the live account: all 11 paused campaigns carry Rs 0 of spend**, and so do all
    4 paused ad groups that have rows. So hiding them cannot conceal money.

    It stays a PARAMETER rather than a hard exclusion, because a campaign paused *today* may have
    spent earlier in the window and that spend must remain findable.
    """
    await _seed(db)
    await repository.save_entities(db, [
        {"entity_type": "campaign", "entity_id": "c9", "campaign_id": "c9",
         "name": "Old paused campaign", "state": "PAUSED"},
        {"entity_type": "ad_group", "entity_id": "g9", "parent_id": "c1", "campaign_id": "c1",
         "name": "paused group", "state": "PAUSED", "default_bid": 2.0},
    ])

    default = (await auth_client.get("/ads?start=2026-08-21&end=2026-08-27")).json()
    assert default["include_paused"] is False
    assert "c9" not in [c["campaign_id"] for c in default["campaigns"]]

    with_paused = (await auth_client.get(
        "/ads?start=2026-08-21&end=2026-08-27&include_paused=true")).json()
    assert "c9" in [c["campaign_id"] for c in with_paused["campaigns"]]

    # Ad groups follow the same rule.
    groups = (await auth_client.get(
        "/ads/ad-groups?campaign_id=c1&start=2026-08-21&end=2026-08-27")).json()["ad_groups"]
    assert "g9" not in [g["ad_group_id"] for g in groups]
    all_groups = (await auth_client.get(
        "/ads/ad-groups?campaign_id=c1&start=2026-08-21&end=2026-08-27"
        "&include_paused=true")).json()["ad_groups"]
    assert "g9" in [g["ad_group_id"] for g in all_groups]


async def test_the_ad_group_count_matches_what_expanding_will_show(auth_client, db):
    """A campaign reading "12 ad groups" that opens to 3 rows reads as a bug.

    So the count respects the same paused filter as the rows it predicts.
    """
    await _seed(db)
    await repository.save_entities(db, [
        {"entity_type": "ad_group", "entity_id": "g9", "parent_id": "c1", "campaign_id": "c1",
         "name": "paused group", "state": "PAUSED"},
    ])
    body = (await auth_client.get("/ads?start=2026-08-21&end=2026-08-27")).json()
    campaign = next(c for c in body["campaigns"] if c["campaign_id"] == "c1")
    groups = (await auth_client.get(
        "/ads/ad-groups?campaign_id=c1&start=2026-08-21&end=2026-08-27")).json()["ad_groups"]
    assert campaign["ad_groups"] == len(groups), (
        "the count beside a campaign disagrees with the rows expanding it produces"
    )


async def test_ad_groups_carry_their_own_performance_rolled_up_from_the_targets(auth_client, db):
    """Asked for: "need the ad group data also to be shown here in the rows".

    **Rolled up from the target rows, never stored.** An ad group's spend is by definition the sum of
    its keywords and targets, and the rows sit directly beneath it — two independent figures would
    visibly fail to add up, the defect class the Orders tab hit reporting 86 orders beside 87 lines.
    """
    await _seed(db)
    groups = (await auth_client.get(
        "/ads/ad-groups?campaign_id=c1&start=2026-08-21&end=2026-08-27")).json()["ad_groups"]
    g1 = next(g for g in groups if g["ad_group_id"] == "g1")

    # The two seeded rows in c1/g1: spend 2620 + 832, sales 3589.4 + 2337.9
    assert g1["spend"] == pytest.approx(3452.0)
    assert g1["sales"] == pytest.approx(5927.3)
    assert g1["targets"] == 2
    assert g1["roas"] == pytest.approx(5927.3 / 3452.0)

    # And it equals the campaign row shown above it.
    body = (await auth_client.get("/ads?start=2026-08-21&end=2026-08-27")).json()
    campaign = next(c for c in body["campaigns"] if c["campaign_id"] == "c1")
    assert campaign["spend"] == pytest.approx(g1["spend"]), (
        "an ad group's spend must sum to its campaign's — they are rendered one above the other"
    )


async def test_an_ad_group_with_no_spend_has_no_roas(auth_client, db):
    """None, not 0 — otherwise a dormant ad group sorts beside the genuinely terrible ones."""
    await _seed(db)
    await repository.save_entities(db, [
        {"entity_type": "ad_group", "entity_id": "gx", "parent_id": "c1", "campaign_id": "c1",
         "name": "dormant group", "state": "ENABLED"},
    ])
    groups = (await auth_client.get(
        "/ads/ad-groups?campaign_id=c1&start=2026-08-21&end=2026-08-27")).json()["ad_groups"]
    dormant = next(g for g in groups if g["ad_group_id"] == "gx")
    assert dormant["spend"] == 0
    assert dormant["roas"] is None
    assert dormant["acos"] is None


def test_every_numeric_column_is_sortable_and_nulls_sort_last():
    """Asked for: "make the columns sortable. spend/roas etc".

    **Nulls last in BOTH directions.** "No data" is not a small number: a campaign with no ROAS must
    not head the ascending list as the worst performer nor the descending one as the best.
    """
    source = _template()
    assert "const COLUMNS = [" in source
    columns = source[source.index("const COLUMNS = ["):]
    columns = columns[:columns.index("];")]
    for key in ("spend", "sales", "roas", "acos", "clicks", "orders", "targets", "daily_budget"):
        assert f'key: "{key}"' in columns, f"{key} is not sortable"

    assert "function compareRows(" in source
    body = source[source.index("function compareRows("):][:700]
    assert "xNull" in body and "return 1" in body, "nulls are not forced to the end"

    # One sort function shared by mouse and keyboard, and focus restored after the thead rebuild.
    assert "function applySort(" in source
    apply_body = source[source.index("function applySort("):][:800]
    assert ".focus()" in apply_body
    assert '$("table-area").addEventListener("keydown"' in source
    assert source.count("dir: -sort.dir") == 1, (
        "the sort toggle is written more than once, so mouse and keyboard paths can drift"
    )


def test_ad_groups_sort_within_their_parent_campaign():
    """A child row floating away from the campaign it belongs to would be meaningless."""
    source = _template()
    # Up to the NEXT function, not to the first `innerHTML` — that appears in the early-return
    # empty-state branch and would cut the extract off before the row loop.
    start = source.index("function renderTable(")
    body = source[start:source.index("function applySort(", start)]

    assert "groups.slice().sort(" in body, "ad groups are not sorted"
    # The sort happens INSIDE the per-campaign loop, so it cannot reorder across parents.
    assert body.index("groups.slice().sort(") > body.index("list.forEach("), (
        "ad groups are sorted outside the campaign loop, so a child could float away from its parent"
    )


def test_ticking_a_campaign_ticks_its_ad_groups_even_when_not_expanded():
    """Asked for: "on ticking say MF_SP_keywords all the ad groups under it should be ticked as well".

    **The ad groups may not be loaded yet**, because the campaign has never been expanded — so they
    are fetched before ticking. Without that, "select this campaign" would select the campaign and
    none of its ad groups, and the scope note would claim a selection the rule does not have.
    """
    source = _template()
    handler = source[source.index('$("table-area").addEventListener("change"'):]
    handler = handler[:handler.index('$("table-area").addEventListener("click"')]
    assert "fetchAdGroups(" in handler, (
        "ticking a campaign does not load its ad groups, so an unexpanded campaign selects none"
    )
    assert "selectedAdGroups.add" in handler and "selectedAdGroups.delete" in handler, (
        "the cascade does not work in both directions"
    )


def test_a_saved_rule_fills_the_bar_and_does_not_run_anything():
    """Asked for: "save a rule so next time I can just select it, select the campaigns and press run".

    **Loading a rule must not preview or apply.** A saved rule is a shortcut to the same two-click
    path, not a bypass of it — otherwise picking a name from a list becomes the click that moves
    bids.
    """
    source = _template()
    assert "function loadRule(" in source
    body = source[source.index("function loadRule("):]
    body = body[:body.index("async function saveRule(")]
    for forbidden in ("runPreview(", "applyPlan(", "/ads/apply"):
        assert forbidden not in body, (
            f"loading a saved rule reaches {forbidden} — picking a name would move bids"
        )
    # It REPLACES the conditions rather than merging: leftovers would run something never built.
    assert "conditions = (rule.conditions" in body
    # And it clears any plan on screen, which belonged to the previous rule.
    assert "plan = null" in body

    assert "function renderSaved(" in source
    assert 'id="save-rule-btn"' in source
    assert "data-load-rule=" in source


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


# ─── The preview controls ─────────────────────────────────────────────────────


def test_the_preview_has_apply_controls_at_the_top_as_well_as_the_bottom():
    """**A real rule matched 1,005 rows**, so the only Apply button sat a screenful below the list.

    Both, not one: the top bar is what gets used on a long list, and the bottom one serves someone who
    has read to the end. Rendered from ONE builder so the two cannot disagree about the count.
    """
    source = _code_only(_template())
    assert source.count("${applyBarHtml()}") == 2, (
        "the apply controls are not rendered both above and below the table"
    )
    assert source.count("function applyBarHtml(") == 1, (
        "the apply bar is built twice, so the two copies can drift"
    )


def test_the_apply_controls_use_attributes_rather_than_ids():
    """An id may appear once per document, and these are deliberately rendered twice.

    **This is also why `applyPlan` had to change.** It read `$("apply-btn").disabled`, which would be
    `null.disabled` once the id was gone — killing the only money-spending path in the app before it
    sent anything. Found by a fresh review of the plan, not by a test.
    """
    source = _code_only(_template())
    assert 'id="apply-btn"' not in source and 'id="apply-count"' not in source
    assert 'id="cancel-preview"' not in source, (
        "the post-apply card still reuses the old id, whose handler no longer exists"
    )

    # **Every attribute the script SELECTS on must be one the markup EMITS.**
    #
    # A weaker version of this test asserted only that `querySelectorAll("[data-apply]")` appeared,
    # and a mutation renaming it to `[data-apply-nope]` survived — applyPlan would silently disable
    # nothing and the send button would stay live during a send. The two halves have to be checked
    # against each other, because a selector is only correct relative to the markup.
    import re

    # The boundary is anything that is not a name character: a bare boolean attribute can be followed
    # by whitespace, `>`, `=`, or a `${...}` template expression — `data-apply${cond ? ... }` is how
    # the disabled state is set, and a `[\s=>]`-only boundary missed exactly that one.
    emitted = set(re.findall(r"\b(data-[a-z]+(?:-[a-z]+)*)(?![a-z-])", source))
    for attribute in ("data-apply", "data-apply-count", "data-toggle-all", "data-cancel-preview",
                      "data-apply-bar"):
        assert attribute in emitted, f"{attribute} is selected on but never rendered"

    # `emitted` is derived from the whole file, so an attribute that appears ONLY in a selector would
    # count itself as rendered. So the emitted set is narrowed to attributes that appear inside a tag,
    # which is the only place markup can declare one.
    in_markup = set(re.findall(r"<[a-z][^>]*?\b(data-[a-z]+(?:-[a-z]+)*)", source, flags=re.S))

    selected = set(re.findall(r'querySelectorAll\("\[(data-[a-z-]+)\]"\)', source))
    selected |= set(re.findall(r'closest\("\[(data-[a-z-]+)\]"\)', source))
    unknown = selected - in_markup
    assert not unknown, (
        f"the script selects on attribute(s) no tag ever renders: {sorted(unknown)} — the handler "
        f"would silently match nothing, and for [data-apply] that means the Apply buttons stay live "
        f"during a send"
    )
    assert "data-apply" in selected, "applyPlan does not find the buttons by attribute"


def test_the_preview_can_clear_every_tick_at_once():
    """1,005 rows arrive all ticked, so the real gesture is "clear all, then pick five".

    A TOGGLE rather than a lone Unselect button — having cleared them, the way back is the same
    control — with its label derived from the live count so it cannot claim the wrong action.
    """
    source = _code_only(_template())
    assert "data-toggle-all" in source, "there is no select/unselect-all control"
    assert "Unselect all" in source and "Select all" in source, (
        "the control does not name both directions, so it is a button rather than a toggle"
    )
    assert "approved.clear()" in source, "the toggle cannot clear the selection"


def test_the_preview_names_the_ad_group_not_only_the_campaign():
    """**The same keyword text exists in several ad groups at different bids.**

    So the campaign alone does not identify the row whose live bid is about to change.
    `attach_names` has always resolved `ad_group_name` in one query; the preview never rendered it.
    """
    body = _js_function(_code_only(_template()), "renderPreview")
    assert "ad_group_name" in body, "the preview does not show which ad group a row belongs to"


def test_an_unavailable_suggested_bid_says_why_rather_than_rendering_blank():
    """**Sponsored Brands has no suggested-bid endpoint — measured, three probed, all 404.**

    ~296 rows in a typical preview have none. A blank cell in a bid column reads as "no suggestion, so
    bid low"; the honest answer is that Amazon does not offer one. Same three-state discipline as the
    Portfolio tab's ACOS column.
    """
    body = _js_function(_code_only(_template()), "suggestedCell")
    assert "suggested_unavailable" in body, "the reason is not shown"
    assert "—" in body, "an unavailable suggestion does not render a dash"
    assert "suggested_low" in body, "the low-high range is dropped, so three bids became one number"


def test_the_suggested_bids_are_fetched_after_the_table_is_rendered():
    """The preview must not block on Amazon, and must survive Amazon refusing.

    A 1,005-row plan spans a few dozen ad groups. This is the safety mechanism for the only feature
    that spends money, so a context column cannot be allowed to delay or break it.
    """
    source = _code_only(_template())
    assert "function loadSuggestedBids(" in source
    assert "/ads/suggested-bids" in source
    # Not awaited inside the render, and guarded against a re-render loop.
    assert "if(!options || options.fetchSuggestions !== false) loadSuggestedBids();" in source, (
        "the suggestion fetch is not guarded, so rendering its own result would loop"
    )
    assert "suggestedToken" in source, (
        "a stale response can overwrite a newer preview's suggestions"
    )


def test_the_preview_shows_the_match_type_as_well_as_the_writer():
    """**EXACT, PHRASE and BROAD were one "keyword" tag**, so 1,418 broad rows looked exact.

    Both columns stay: Match is how the row competes, Type is which Amazon API its bid is written to.
    `TARGETING_EXPRESSION` exists under both ad products and routes differently, so a screen showing
    only one of them cannot reveal a misrouted write.
    """
    source = _code_only(_template())
    assert "function matchTag(" in source, "there is no match-type column"
    assert "function writerTag(" in source, "the writer column was replaced rather than joined"
    body = _js_function(source, "renderPreview")
    assert "matchTag(c)" in body and "writerTag(c)" in body, (
        "the preview renders one of the two columns but not both"
    )
    assert ">Match<" in body, "the Match column has no header"


def test_the_match_vocabulary_comes_from_the_server():
    """Hardcoding the labels in the template would let it drift from `logic.MATCH_LABELS`.

    A new match type would then render "?" on screen while the server knew its name.
    """
    source = _template()
    assert "match_labels | tojson" in source, "the labels are hardcoded in the template"

    from app.ads import logic as ads_logic

    main = (Path(__file__).parent.parent / "app" / "main.py").read_text(encoding="utf-8")
    assert "match_labels" in main, "the page route does not pass the vocabulary"
    # Every label the template can render must have a style, or a real match type shows unstyled.
    for label in ads_logic.MATCH_LABELS.values():
        assert f".tag.m-{label}{{" in source, f"the {label} tag has no style"


def test_the_preview_trusts_the_servers_default_selection():
    """**The guard is in the DATA, not the screen.**

    `plan_run` computes `approved_ids`, excluding rows already changed today, and `POST /ads/apply`
    re-checks the same rule. A template that ticked everything would put the compounding change one
    click away.
    """
    source = _code_only(_template())
    assert "body.approved_ids" in source, (
        "the screen ticks every row rather than honouring the server's selection"
    )


def test_a_row_changed_today_says_so_on_the_row():
    """The reason has to be ON the row: with 1,005 rows a summary line alone cannot explain why one
    is unticked."""
    body = _js_function(_code_only(_template()), "renderPreview")
    assert "changed_today" in body, "the badge is missing"
    assert "report_bid" in body, (
        "the stale report figure is hidden rather than shown, so the owner cannot see why the "
        "current bid differs from what the last preview said"
    )
    assert ".tag.changed{" in _template(), "the badge has no style"


def test_the_apply_summary_names_rows_refused_for_being_changed_today():
    """A shrunken count with no reason reads as rows silently going missing — the rule `moved` and
    `inactive` already follow."""
    source = _code_only(_template())
    assert "body.repeated" in source, "the refusal is never surfaced on screen"


async def test_apply_refuses_a_row_already_changed_today(auth_client, db):
    """**Re-checked server-side, because a preview can sit open while another run happens.**

    The screen is not a trust boundary and this is the only route in the app that spends money.
    Applying the same percentage to an already-moved bid compounds it: -10% twice is -19%.
    """
    from datetime import datetime

    from app.models import AdsMutation

    await _seed(db)
    db.add(AdsMutation(run_id="earlier", entity_id="111", entity_type="keyword", writer="keyword",
                       old_bid=18.75, new_bid=16.88, status="applied", rule_summary="earlier rule",
                       created_at=datetime.utcnow()))
    await db.commit()

    response = await auth_client.post("/ads/apply", json={
        "rule": "spend > 100 -> bid -10%",
        "changes": [{"entity_id": "111", "writer": "keyword", "text": "makhana",
                     "old_bid": 16.88, "new_bid": 15.19, "match_type": "PHRASE",
                     "campaign_id": "c1", "ad_group_id": "g1"}],
    })
    body = response.json()
    assert response.status_code == 200, body
    assert body.get("applied", 0) == 0, "a row already changed today was sent to Amazon"
    assert body.get("repeated"), "the refusal is not reported, so the row vanishes silently"
    assert "already changed today" in body["repeated"][0]["reason"]
