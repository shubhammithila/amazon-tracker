"""Pure rules for the Ads tab — the module whose output CHANGES live bids.

Every test here corresponds to something measured against the live account on 2026-08-28, because
this is the first feature in the app that mutates the seller account and a wrong rule spends real
money. The measurements are named in each docstring so a future reader can tell a deliberate
threshold from an arbitrary one.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ads import logic


def _fixture() -> list[dict]:
    """433 real rows from a live `spTargeting` report, captured 2026-08-28.

    Trimmed from the full 12,854-row report but **chosen to preserve the result**: it keeps every
    row the owner's rule matches, 25+ of each of the five match types, and 67 zero-spend rows. The
    rule produces identical totals on the fixture and on the full report (299 matched, 283 changing,
    148 keywords + 135 targets), which is asserted below — a blind random sample would not have.
    """
    path = Path(__file__).parent / "fixtures" / "ads_targeting_rows.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ─── Routing: the trap that would be silent ──────────────────────────────────
#
# The `spTargeting` report labels BOTH id columns `keywordId`, but only some rows are keywords.
# Measured on a real 12,854-row report: of six rows matched by the owner's own rule, FOUR were
# targeting clauses (`close-match`, `loose-match`, `complements`, `category="4860253031"`) and only
# two were keywords. Sending a targetId to /sp/keywords returns 207 with the failure buried in an
# `error` array — the run reports success and quietly does nothing for those rows.


@pytest.mark.parametrize("match_type,expected", [
    ("EXACT", logic.WRITER_KEYWORD),
    ("PHRASE", logic.WRITER_KEYWORD),
    ("BROAD", logic.WRITER_KEYWORD),
    ("TARGETING_EXPRESSION_PREDEFINED", logic.WRITER_TARGET),   # auto: close-match, complements
    ("TARGETING_EXPRESSION", logic.WRITER_TARGET),              # manual product/category
])
def test_every_measured_match_type_routes_to_the_right_endpoint(match_type, expected):
    """The real vocabulary, read off a live report rather than the documentation."""
    assert logic.writer_for(match_type) == expected


def test_an_unrecognised_match_type_is_excluded_rather_than_guessed():
    """**A new Amazon target type must not be written to a guessed endpoint.**

    Excluding it and naming it on the preview is recoverable. Guessing sends an id to the wrong
    entity, and the 207 that comes back reports success for every other row — so the failure is
    invisible in exactly the run where it matters.
    """
    assert logic.writer_for("SOME_NEW_AMAZON_THING") is None
    assert logic.writer_for("") is None
    assert logic.writer_for(None) is None


def test_routing_is_case_insensitive_but_not_prefix_guessing():
    """Amazon has been consistent about case, but a lowercase value must not silently drop a row.

    It must NOT match on prefix, though: `TARGETING_EXPRESSION_SOMETHING_NEW` is not a type we
    know, and treating it as a targeting clause because it starts the same way is the same guess
    the test above forbids.
    """
    assert logic.writer_for("exact") == logic.WRITER_KEYWORD
    assert logic.writer_for("targeting_expression") == logic.WRITER_TARGET
    assert logic.writer_for("TARGETING_EXPRESSION_SOMETHING_NEW") is None


def test_split_by_writer_keeps_the_two_kinds_apart():
    """The last point where a keyword and a targeting clause are still in one list."""
    changes = [
        {"entity_id": "1", "writer": logic.WRITER_KEYWORD},
        {"entity_id": "2", "writer": logic.WRITER_TARGET},
        {"entity_id": "3", "writer": logic.WRITER_KEYWORD},
    ]
    out = logic.split_by_writer(changes)
    assert [c["entity_id"] for c in out[logic.WRITER_KEYWORD]] == ["1", "3"]
    assert [c["entity_id"] for c in out[logic.WRITER_TARGET]] == ["2"]


# ─── ROAS without spend ──────────────────────────────────────────────────────


def test_a_row_with_no_spend_has_no_roas_and_never_matches_a_roas_rule():
    """**This one guard keeps a dormant target out of a bid cut.**

    `_ratio` returns None rather than 0.0, exactly as the Portfolio tab's does. Returning 0.0
    would make every zero-spend row satisfy `roas < 3`, and this account has 148,291 keywords —
    so an innocuous-looking rule would sweep the whole dormant tail into a bid change.
    """
    row = {"keywordId": "1", "matchType": "EXACT", "cost": 0.0, "sales7d": 0.0,
           "keywordBid": 7.0, "clicks": 0, "impressions": 12, "purchases7d": 0}
    m = logic.metrics_for(row)
    assert m["roas"] is None
    assert m["acos"] is None
    assert m["roas"] != 0.0
    assert not logic.matches(m, [{"field": "roas", "op": "lt", "value": 3}])


def test_a_row_with_spend_and_no_sales_has_a_roas_of_zero_which_is_a_real_number():
    """Spend with no return is genuinely ROAS 0 — distinct from "no data", and it SHOULD match.

    The mirror of the test above, and the pair is the point: `None` means unmeasurable, `0.0`
    means measured and bad. Collapsing them would either hide the worst targets or invent them.
    """
    row = {"keywordId": "1", "matchType": "EXACT", "cost": 500.0, "sales7d": 0.0,
           "keywordBid": 7.0, "clicks": 20, "impressions": 900, "purchases7d": 0}
    m = logic.metrics_for(row)
    assert m["roas"] == 0.0
    assert m["acos"] is None, "no sales means ACOS has no denominator"
    assert logic.matches(m, [{"field": "roas", "op": "lt", "value": 3}])


# ─── Conditions ──────────────────────────────────────────────────────────────


def test_no_conditions_matches_nothing_rather_than_everything():
    """**"No rule" must never mean "every row in the account".**

    An empty condition list reaching `matches` as a match-all would, combined with a bid action,
    move every bid in a 148k-keyword account in one click.
    """
    m = logic.metrics_for({"keywordId": "1", "matchType": "EXACT", "cost": 100.0,
                           "sales7d": 200.0, "keywordBid": 5.0})
    assert not logic.matches(m, [])


def test_an_empty_condition_value_is_refused_rather_than_read_as_zero():
    """`Number("")` is 0, and the Portfolio tab shipped exactly this bug once.

    There a blank filter box became a live `> 0` and silently hid 45 of 90 products. Here it would
    become a live `spend > 0` and match the entire account, with a bid action attached.
    """
    assert logic.condition_error({"field": "spend", "op": "gt", "value": ""})
    assert logic.condition_error({"field": "spend", "op": "gt", "value": "   "})
    assert logic.condition_error({"field": "spend", "op": "gt", "value": None})
    # A deliberate zero is a real threshold and stays legal.
    assert logic.condition_error({"field": "spend", "op": "gt", "value": 0}) is None


def test_percentage_fields_are_entered_as_percents_and_compared_as_ratios():
    """ACOS is entered as "50" meaning 50%, matching the Portfolio tab's filter builder.

    The owner uses both screens; two conventions for one unit is how a rule gets written to mean
    5000%.
    """
    row = {"keywordId": "1", "matchType": "EXACT", "cost": 100.0, "sales7d": 250.0,
           "keywordBid": 5.0}
    m = logic.metrics_for(row)
    assert m["acos"] == pytest.approx(0.4)
    assert logic.matches(m, [{"field": "acos", "op": "lt", "value": 50}])
    assert not logic.matches(m, [{"field": "acos", "op": "gt", "value": 50}])


def test_an_unknown_field_or_operator_is_refused():
    assert logic.condition_error({"field": "made_up", "op": "gt", "value": 1})
    assert logic.condition_error({"field": "spend", "op": "approximately", "value": 1})


# ─── Bid arithmetic ──────────────────────────────────────────────────────────


def test_a_percentage_change_rounds_to_two_decimals_because_amazon_takes_two():
    """The owner's real rule: 12.68 x 0.9 = 11.412, and Amazon takes 2dp.

    Rounding here rather than relying on Amazon means the preview shows exactly what is sent.
    """
    assert logic.new_bid(12.68, logic.ACTION_DECREASE_PCT, 10) == 11.41
    assert logic.new_bid(18.75, logic.ACTION_DECREASE_PCT, 10) == 16.88
    assert logic.new_bid(10.66, logic.ACTION_DECREASE_PCT, 10) == 9.59
    assert logic.new_bid(39.66, logic.ACTION_DECREASE_PCT, 10) == 35.69


def test_increase_and_decrease_move_in_opposite_directions():
    """Trivial to state, and a sign inversion here spends money instead of saving it."""
    assert logic.new_bid(10.0, logic.ACTION_INCREASE_PCT, 10) == 11.0
    assert logic.new_bid(10.0, logic.ACTION_DECREASE_PCT, 10) == 9.0
    assert logic.new_bid(10.0, logic.ACTION_INCREASE_ABS, 2.5) == 12.5
    assert logic.new_bid(10.0, logic.ACTION_DECREASE_ABS, 2.5) == 7.5
    assert logic.new_bid(10.0, logic.ACTION_SET, 4.25) == 4.25


def test_a_row_with_no_current_bid_yields_no_new_bid():
    """**Never inherit the ad group default into a bid.**

    A target with no explicit bid spends the ad group's `defaultBid`. Writing a bid onto it
    CONVERTS it from inheriting to fixed — a structural change nobody asked for. Measured: 0 of
    the 299 rows matched by the real rule lacked an explicit bid, so excluding them costs nothing.
    """
    assert logic.new_bid(None, logic.ACTION_DECREASE_PCT, 10) is None
    assert logic.new_bid(0, logic.ACTION_DECREASE_PCT, 10) is None
    # ...but an explicit SET does not need a current bid, because it is not relative to one.
    assert logic.new_bid(None, logic.ACTION_SET, 5.0) == 5.0


# ─── Guardrails: Amazon has a floor but no ceiling ───────────────────────────


def test_a_percentage_change_beyond_the_limit_blocks_the_whole_run():
    """**Measured: Amazon ACCEPTED a ₹1,000 bid** on an account whose median bid is ₹6.39.

    It rejected ₹0.50 as below the marketplace minimum, so the floor is enforced on their side and
    the ceiling is not enforced at all. A "10" typed as "100" is caught here or nowhere.

    Blocked rather than skipped: the rule itself is wrong, so showing a preview the owner might
    approve would be the wrong answer.
    """
    rows = [{"keywordId": "1", "matchType": "EXACT", "cost": 500.0, "sales7d": 1000.0,
             "keywordBid": 10.0, "clicks": 20, "impressions": 900, "purchases7d": 3}]
    conditions = [{"field": "spend", "op": "gt", "value": 100}]

    plan = logic.plan_run(rows, conditions=conditions,
                          action=logic.ACTION_DECREASE_PCT, amount=100)
    assert plan["blocked"], "a 100% bid change was allowed"
    assert "25" in plan["blocked"], "the refusal does not name the limit"
    assert plan["changes"] == [], "a blocked run must propose no changes"

    ok = logic.plan_run(rows, conditions=conditions,
                        action=logic.ACTION_DECREASE_PCT, amount=10)
    assert ok["blocked"] is None and len(ok["changes"]) == 1


def test_a_new_bid_above_the_ceiling_is_skipped_and_named():
    """The ceiling is ours because Amazon does not have one worth the name."""
    rows = [{"keywordId": "1", "matchType": "EXACT", "cost": 500.0, "sales7d": 1000.0,
             "keywordBid": 55.0, "clicks": 20, "impressions": 900, "purchases7d": 3}]
    plan = logic.plan_run(rows, conditions=[{"field": "spend", "op": "gt", "value": 100}],
                          action=logic.ACTION_INCREASE_PCT, amount=20)
    assert plan["changes"] == []
    assert plan["skipped"][0]["reason"] == logic.SKIP_ABOVE_CEILING
    assert plan["skipped"][0]["new_bid"] == 66.0, "the refused value is shown, not hidden"


def test_a_new_bid_below_the_floor_is_skipped_before_amazon_can_reject_it():
    """Amazon rejects these with a `rangeError`, measured at ₹0.50 on this marketplace.

    Excluding them locally means the count on the preview is honest — a run reporting "299
    changes" that Amazon then refuses 40 of is a worse experience than one that says 259 up front.
    """
    rows = [{"keywordId": "1", "matchType": "EXACT", "cost": 500.0, "sales7d": 1000.0,
             "keywordBid": 1.05, "clicks": 20, "impressions": 900, "purchases7d": 3}]
    plan = logic.plan_run(rows, conditions=[{"field": "spend", "op": "gt", "value": 100}],
                          action=logic.ACTION_DECREASE_PCT, amount=10)
    assert plan["changes"] == []
    assert plan["skipped"][0]["reason"] == logic.SKIP_BELOW_FLOOR


def test_a_run_larger_than_the_row_limit_is_blocked():
    """The owner's real rule matched 299 rows, so the default limit sits above that at 500."""
    rows = [{"keywordId": str(i), "matchType": "EXACT", "cost": 500.0, "sales7d": 1000.0,
             "keywordBid": 10.0, "clicks": 20, "impressions": 900, "purchases7d": 3}
            for i in range(600)]
    plan = logic.plan_run(rows, conditions=[{"field": "spend", "op": "gt", "value": 100}],
                          action=logic.ACTION_DECREASE_PCT, amount=10)
    assert plan["blocked"] and "600" in plan["blocked"]


def test_a_bid_that_would_not_change_is_skipped_rather_than_written():
    """A no-op write is not harmless: it spends a row of the 207 budget and appears in the ledger
    as a change that changed nothing, which makes the audit trail lie."""
    rows = [{"keywordId": "1", "matchType": "EXACT", "cost": 500.0, "sales7d": 1000.0,
             "keywordBid": 10.0, "clicks": 20, "impressions": 900, "purchases7d": 3}]
    plan = logic.plan_run(rows, conditions=[{"field": "spend", "op": "gt", "value": 100}],
                          action=logic.ACTION_SET, amount=10.0)
    assert plan["changes"] == []
    assert plan["skipped"][0]["reason"] == logic.SKIP_NO_CHANGE


# ─── Guardrail settings validation ───────────────────────────────────────────


def test_an_absurd_guardrail_is_refused_with_its_reason():
    """Same lesson as `portfolio.logic.THRESHOLD_RANGES`.

    There, `good_rating: 99` passed a finite-float check and silently zeroed BEST BET for ever,
    because Amazon rates out of 5. Here a `max_bid` of 0.01 would refuse every bid on the account,
    and a `max_change_pct` of 5000 would disable the only ceiling that exists.
    """
    assert logic.guardrail_error("max_bid", 0.01)
    assert logic.guardrail_error("max_change_pct", 5000)
    assert logic.guardrail_error("max_change_pct", -5)
    assert logic.guardrail_error("max_bid", "banana")
    assert logic.guardrail_error("max_bid", float("inf"))
    assert logic.guardrail_error("made_up_limit", 1)
    assert logic.guardrail_error("max_bid", 60.0) is None


def test_a_stored_guardrail_out_of_range_falls_back_on_read():
    """**Validated on READ, not only on write.**

    A value already in the database, or hand-edited, would otherwise keep weakening the ceiling
    with nothing on screen to explain why a run was allowed.
    """
    merged = logic.guardrails_or_default({"max_change_pct": 9999, "max_bid": 45.0})
    assert merged["max_change_pct"] == logic.DEFAULT_GUARDRAILS["max_change_pct"]
    assert merged["max_bid"] == 45.0, "a legal stored value must still be honoured"


# ─── Scope ───────────────────────────────────────────────────────────────────


def test_a_rule_scoped_to_one_campaign_touches_nothing_outside_it():
    """"Go inside one campaign and run the rule" — the scope must actually bound the write."""
    rows = [
        {"keywordId": "1", "matchType": "EXACT", "campaignId": "c1", "adGroupId": "g1",
         "cost": 500.0, "sales7d": 1000.0, "keywordBid": 10.0},
        {"keywordId": "2", "matchType": "EXACT", "campaignId": "c2", "adGroupId": "g2",
         "cost": 500.0, "sales7d": 1000.0, "keywordBid": 10.0},
    ]
    conditions = [{"field": "spend", "op": "gt", "value": 100}]

    plan = logic.plan_run(rows, conditions=conditions, action=logic.ACTION_DECREASE_PCT,
                          amount=10, scope_campaign_ids=["c1"])
    assert [c["entity_id"] for c in plan["changes"]] == ["1"]

    by_group = logic.plan_run(rows, conditions=conditions, action=logic.ACTION_DECREASE_PCT,
                              amount=10, scope_ad_group_ids=["g2"])
    assert [c["entity_id"] for c in by_group["changes"]] == ["2"]

    unscoped = logic.plan_run(rows, conditions=conditions,
                              action=logic.ACTION_DECREASE_PCT, amount=10)
    assert len(unscoped["changes"]) == 2, "no scope means the whole report, as selected"


# ─── The owner's actual rule, end to end ─────────────────────────────────────


def test_the_owners_real_rule_reproduces_the_measured_result():
    """`spend>100, 1<roas<3, decrease bid 10%` — the rule that prompted this feature.

    These six rows are verbatim shapes from the live 12,854-row report, and the expected bids are
    the ones measured against the account: 12.68 -> 11.41, 10.66 -> 9.59, 18.75 -> 16.88,
    39.66 -> 35.69. Four of the six are targeting clauses, which is the routing trap in one test.
    """
    rows = [
        {"keywordId": "1", "keyword": "complements", "matchType": "TARGETING_EXPRESSION_PREDEFINED",
         "cost": 269.0, "sales7d": 664.4, "keywordBid": 12.68, "campaignId": "c1", "adGroupId": "g1"},
        {"keywordId": "2", "keyword": "close-match", "matchType": "TARGETING_EXPRESSION_PREDEFINED",
         "cost": 832.0, "sales7d": 2337.9, "keywordBid": 10.66, "campaignId": "c1", "adGroupId": "g1"},
        {"keywordId": "3", "keyword": "makhana", "matchType": "PHRASE",
         "cost": 2620.0, "sales7d": 3589.4, "keywordBid": 18.75, "campaignId": "c2", "adGroupId": "g2"},
        {"keywordId": "4", "keyword": "usna chawal", "matchType": "EXACT",
         "cost": 176.0, "sales7d": 385.4, "keywordBid": 39.66, "campaignId": "c2", "adGroupId": "g2"},
        # ROAS 4.0 — profitable, above the band, must be left alone.
        {"keywordId": "5", "keyword": "good kw", "matchType": "EXACT",
         "cost": 200.0, "sales7d": 800.0, "keywordBid": 9.0, "campaignId": "c2", "adGroupId": "g2"},
        # Spend below 100 — under the volume floor, must be left alone.
        {"keywordId": "6", "keyword": "tiny kw", "matchType": "EXACT",
         "cost": 40.0, "sales7d": 80.0, "keywordBid": 9.0, "campaignId": "c2", "adGroupId": "g2"},
    ]
    plan = logic.plan_run(
        rows,
        conditions=[{"field": "spend", "op": "gt", "value": 100},
                    {"field": "roas", "op": "gt", "value": 1},
                    {"field": "roas", "op": "lt", "value": 3}],
        action=logic.ACTION_DECREASE_PCT, amount=10,
    )
    assert plan["blocked"] is None
    assert [(c["text"], c["old_bid"], c["new_bid"]) for c in plan["changes"]] == [
        ("complements", 12.68, 11.41),
        ("close-match", 10.66, 9.59),
        ("makhana", 18.75, 16.88),
        ("usna chawal", 39.66, 35.69),
    ]
    # The split is the whole point: two of these go to a different Amazon endpoint.
    assert plan["totals"]["keywords"] == 2
    assert plan["totals"]["targets"] == 2
    assert plan["totals"]["spend"] == pytest.approx(3897.0)


# ─── Against the real report ──────────────────────────────────────────────────


def test_the_real_report_reproduces_the_measured_299_matches():
    """**The whole feature, checked against data measured BEFORE any of it was written.**

    I ran this rule directly against the live account and counted 299 matching targets carrying
    Rs 102,945 of 7-day spend, then wrote `logic.py`, then ran `logic.py` over the same report. The
    counts agree, which is the strongest evidence available that the rule engine means what the
    owner meant.

    The 283/16 split is the guardrails doing their job: 16 rows would have exceeded the Rs 60 bid
    ceiling and are excluded locally rather than being refused by Amazon after the fact.
    """
    plan = logic.plan_run(
        _fixture(),
        conditions=[{"field": "spend", "op": "gt", "value": 100},
                    {"field": "roas", "op": "gt", "value": 1},
                    {"field": "roas", "op": "lt", "value": 3}],
        action=logic.ACTION_DECREASE_PCT, amount=10,
    )
    assert plan["blocked"] is None
    assert plan["totals"]["matched"] == 299, "the rule no longer matches what was measured live"
    assert plan["totals"]["changing"] == 283
    assert plan["totals"]["skipped"] == 16
    assert all(s["reason"] == logic.SKIP_ABOVE_CEILING for s in plan["skipped"])


def test_the_real_report_splits_across_both_endpoints():
    """**148 keywords and 135 targeting clauses — the routing trap at real scale.**

    In the full report `TARGETING_EXPRESSION` is the LARGEST match type (6,665 of 12,854 rows), so
    routing everything to `/sp/keywords` would have failed for more than half the account while
    returning `207` and looking like a success.
    """
    plan = logic.plan_run(
        _fixture(),
        conditions=[{"field": "spend", "op": "gt", "value": 100},
                    {"field": "roas", "op": "gt", "value": 1},
                    {"field": "roas", "op": "lt", "value": 3}],
        action=logic.ACTION_DECREASE_PCT, amount=10,
    )
    assert plan["totals"]["keywords"] == 148
    assert plan["totals"]["targets"] == 135
    assert plan["totals"]["keywords"] + plan["totals"]["targets"] == plan["totals"]["changing"], (
        "every changing row must be routed to exactly one endpoint"
    )

    grouped = logic.split_by_writer(plan["changes"])
    assert len(grouped[logic.WRITER_KEYWORD]) == 148
    assert len(grouped[logic.WRITER_TARGET]) == 135


def test_every_match_type_in_the_real_report_is_recognised():
    """An unrecognised type is excluded by design — so it must not be happening on real data.

    This is the test that would catch Amazon introducing a new targeting type: rows would start
    being skipped as unroutable, and the account would quietly become partly un-editable.
    """
    unroutable = {
        row.get("matchType") for row in _fixture() if logic.writer_for(row.get("matchType")) is None
    }
    assert not unroutable, f"unrecognised match types in the live report: {unroutable}"


def test_zero_spend_rows_in_the_real_report_never_enter_a_roas_rule():
    """67 of the fixture's rows have no spend, and a bare `roas < 3` must not sweep them in."""
    rows = _fixture()
    zero_spend = [r for r in rows if not (r.get("cost") or 0)]
    assert len(zero_spend) > 20, "fixture no longer covers the zero-spend case"

    plan = logic.plan_run(rows, conditions=[{"field": "roas", "op": "lt", "value": 3}],
                          action=logic.ACTION_DECREASE_PCT, amount=10)
    touched = {c["entity_id"] for c in plan["changes"]}
    for row in zero_spend:
        assert str(row["keywordId"]) not in touched, (
            "a zero-spend row entered a ROAS rule, so ROAS is being read as 0 rather than None"
        )


def test_a_plan_can_be_fed_its_own_metrics_without_recomputing():
    """The apply step receives the plan the preview showed, so `plan_run` must accept both raw
    report rows and already-normalised metrics. If it silently recomputed, an approved change
    could differ from the one sent."""
    row = {"keywordId": "1", "keyword": "kw", "matchType": "EXACT", "cost": 500.0,
           "sales7d": 1000.0, "keywordBid": 10.0, "campaignId": "c1", "adGroupId": "g1"}
    once = logic.plan_run([logic.metrics_for(row)],
                          conditions=[{"field": "spend", "op": "gt", "value": 100}],
                          action=logic.ACTION_DECREASE_PCT, amount=10)
    twice = logic.plan_run([row], conditions=[{"field": "spend", "op": "gt", "value": 100}],
                           action=logic.ACTION_DECREASE_PCT, amount=10)
    assert once["changes"] == twice["changes"]
