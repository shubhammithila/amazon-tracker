"""Pausing and re-enabling keywords and targets from a rule.

The action that turns a keyword OFF. Every test here guards a decision recorded in
docs/superpowers/specs/2026-09-03-ads-pause-keywords-design.md.

Measured before any of it was built, on the live 30-day window: `spend>1000 AND roas<1` matches
**88 rows carrying Rs 1,40,751** (6.8% of spend) across 46 ad groups, and every one of those rows has
more than 10 clicks. That last figure is what makes this rule safe where the zero-ROAS *bid* warning
was rejected — there, 1,107 of 1,425 rows had zero clicks, so ROAS was an undefined ratio rather than
a measurement. The `spend>1000` floor is what does the discriminating.
"""
from __future__ import annotations

import pytest

from app.ads import logic


def test_archived_is_not_a_writable_state():
    """**The whole safety argument for this feature.**

    Amazon documents archiving as terminal — "permanent and can't be undone" — and on Sponsored
    Brands an archived negative can never be recreated. The ledger's safety model is an
    `old -> new` pair with a reversible undo chain, so an irreversible action has no undo and
    cannot honestly be offered through a rule that moves hundreds of rows in one click.

    Asserted on the CONSTANT rather than only on `state_error`, so a future reader who
    "completes" the enum for symmetry fails here with the reason in front of them.
    """
    assert logic.WRITABLE_STATES == (logic.STATE_PAUSED, logic.STATE_ENABLED)
    assert "ARCHIVED" not in logic.WRITABLE_STATES
    assert logic.state_error("ARCHIVED") is not None
    assert "permanent" in logic.state_error("ARCHIVED").lower()


def test_the_state_action_is_recognised_and_distinguishable_from_a_bid_action():
    """`is_state_action` is the branch every later task keys on, so it is pinned here."""
    assert logic.ACTION_SET_STATE in logic.ACTIONS
    assert logic.is_state_action(logic.ACTION_SET_STATE) is True
    for bid_action in (logic.ACTION_INCREASE_PCT, logic.ACTION_DECREASE_PCT,
                       logic.ACTION_INCREASE_ABS, logic.ACTION_DECREASE_ABS, logic.ACTION_SET):
        assert logic.is_state_action(bid_action) is False


@pytest.mark.parametrize("value", ["PAUSED", "ENABLED", "paused", " enabled "])
def test_a_legal_state_is_accepted_in_any_case_and_with_whitespace(value):
    """The screen sends a string from a <select>; a stray space must not read as an unknown state."""
    assert logic.state_error(value) is None


@pytest.mark.parametrize("value", ["", None, "PAUSE", "off", 0, "ARCHIVED", "ENABLING"])
def test_an_illegal_state_is_refused_with_a_reason(value):
    """Refusals carry prose, like `guardrail_error`, so the screen can say what is allowed."""
    problem = logic.state_error(value)
    assert problem, f"{value!r} should be refused"
    assert isinstance(problem, str)


def test_sb_states_are_lower_case_and_sp_states_are_upper():
    """**Wrong case is a per-row rejection inside a 207 whose HTTP status says success.**

    The same silent shape as SB's missing `adGroupId`. One function holds the rule so it is stated
    once rather than inlined at each of the three payload builders.
    """
    assert logic.normalise_state("PAUSED", logic.AD_PRODUCT_SP) == "PAUSED"
    assert logic.normalise_state("paused", logic.AD_PRODUCT_SP) == "PAUSED"
    assert logic.normalise_state("PAUSED", logic.AD_PRODUCT_SB) == "paused"
    assert logic.normalise_state(" enabled ", logic.AD_PRODUCT_SB) == "enabled"


def test_min_pause_spend_is_a_range_checked_guardrail():
    """The pause equivalent of `max_change_pct`.

    For a bid rule the dangerous typo is 10% written as 100%. For a pause there is no percentage —
    the dangerous typo is `spend>10` where `spend>1000` was meant, which `max_rows` cannot catch
    because 900 cheap rows fit under a 1,000-row ceiling.

    Range-checked on READ as well as write is the `good_rating: 99` lesson: a stored value nothing
    could ever reach silently zeroed a whole verdict on the Portfolio tab.
    """
    assert logic.DEFAULT_GUARDRAILS["min_pause_spend"] == 100.0
    assert "min_pause_spend" in logic.GUARDRAIL_RANGES
    assert logic.guardrail_error("min_pause_spend", 100) is None
    assert logic.guardrail_error("min_pause_spend", 0) is not None
    assert logic.guardrail_error("min_pause_spend", -5) is not None
    # Read path: an absurd stored value falls back to the default rather than being honoured.
    assert logic.guardrails_or_default({"min_pause_spend": 0})["min_pause_spend"] == 100.0


# ─── Planning a pause ────────────────────────────────────────────────────────


def _row(entity_id="1", *, spend=2000.0, roas=0.5, bid=12.0,
         writer=logic.WRITER_KEYWORD, ad_product="sp", campaign="MF_SP_keywords"):
    """One report row, already in `metrics_for` shape so `plan_run` uses it as given."""
    return {
        "entity_id": entity_id, "writer": writer, "ad_product": ad_product,
        "match_type": "PHRASE", "text": f"kw {entity_id}",
        # `manager` explicitly, not left to `manager_of(campaign_name)` to derive. The fallback
        # would classify this fixture as `us` anyway, but only because of the campaign name chosen
        # here — a test that passes by luck stops passing when someone renames the fixture.
        "manager": logic.MANAGER_US,
        "campaign_id": "c1", "campaign_name": campaign,
        "ad_group_id": "g1", "ad_group_name": "ag",
        "bid": bid, "spend": spend, "sales": spend * roas, "roas": roas,
        "clicks": 100, "impressions": 5000, "orders": 0, "acos": 2.0,
    }


def _pause(rows, *, amount="PAUSED", conditions=None, guardrails=None, applied_today=None):
    return logic.plan_run(
        rows,
        conditions=conditions if conditions is not None else [
            {"field": "spend", "op": "gt", "value": 1000},
        ],
        action=logic.ACTION_SET_STATE,
        amount=amount,
        guardrails=guardrails,
        applied_today=applied_today,
    )


def test_a_pause_plan_carries_a_state_and_no_new_bid():
    """A state row must not carry `new_bid`.

    Five sites downstream read the bid off a change. A state row that carried one — even a copy of
    the current bid — would let a pause be recorded in the ledger as a bid change, and then
    `last_applied_bids` would serve it as the true current bid.
    """
    plan = _pause([_row("1")])
    assert plan["blocked"] is None
    assert len(plan["changes"]) == 1
    change = plan["changes"][0]
    assert change["new_state"] == "PAUSED"
    # The live state is unknowable from the report — see the next test.
    assert change["old_state"] is None
    assert "new_bid" not in change
    assert plan["totals"]["pausing"] == 1


def test_the_report_cannot_supply_the_live_state_so_no_row_is_skipped_for_being_paused():
    """**There is deliberately no `SKIP_ALREADY_PAUSED` here, and this test is why.**

    `spTargeting` has no state column — none of its 15 columns carries one — so at preview time
    `plan_run` genuinely cannot know whether a row is already paused. The precondition is enforced
    at apply, where the live state is read anyway, and reported as `unchanged`.

    A skip constant added here for symmetry would be dead code that can never fire, and a later
    reader would "fix" it by inventing a state source that does not exist.
    """
    assert not any(name.startswith("SKIP_ALREADY") for name in dir(logic))
    plan = _pause([_row("1")])
    assert len(plan["changes"]) == 1


def test_a_row_below_the_pause_spend_floor_is_skipped_and_named():
    """The guard against `spend>10` typed where `spend>1000` was meant.

    Skipped and NAMED, never silently absent — a row missing from a preview is indistinguishable
    from a bug.
    """
    plan = _pause([_row("1", spend=50.0)], conditions=[{"field": "spend", "op": "gt", "value": 10}])
    assert plan["changes"] == []
    assert len(plan["skipped"]) == 1
    assert plan["skipped"][0]["reason"] == logic.SKIP_BELOW_PAUSE_SPEND
    assert plan["totals"]["below_pause_spend"] == 1


def test_a_pause_run_ignores_the_bid_guardrails_because_they_cannot_apply():
    """`max_bid`, `min_bid` and `max_change_pct` are about arithmetic a pause does not do.

    A row whose bid sits above the ceiling is still perfectly pausable — indeed it is likelier to
    need it. Applying the bid ceiling here would refuse to turn off the most expensive keywords in
    the account.
    """
    plan = _pause([_row("1", bid=900.0)], guardrails={"max_bid": 60.0, "min_bid": 1.0})
    assert plan["blocked"] is None
    assert len(plan["changes"]) == 1


@pytest.mark.parametrize("bad", ["ARCHIVED", "", "off"])
def test_an_illegal_state_blocks_the_whole_run_rather_than_skipping_rows(bad):
    """The rule itself is wrong, so this is a refusal and not a per-row exclusion.

    Same distinction the function already draws for a guardrail breach: `blocked` means the rule is
    wrong, `skipped` means a row is unsuitable. A preview of 88 rows the owner might approve must
    not be produced from an unusable rule.
    """
    plan = _pause([_row("1")], amount=bad)
    assert plan["changes"] == []
    assert plan["blocked"], f"{bad!r} should block the run"


def test_the_row_ceiling_still_applies_to_a_pause():
    """`max_rows` is the one bid guardrail that DOES transfer: it bounds surprise, not arithmetic."""
    rows = [_row(str(i)) for i in range(12)]
    plan = _pause(rows, guardrails={"max_rows": 5})
    assert plan["blocked"] is not None
    assert "12" in plan["blocked"]


def test_a_row_paused_today_arrives_unticked_rather_than_dropped():
    """The once-per-day guard, on its own basis.

    Its job differs from the bid guard's: a repeated bid change COMPOUNDS, a repeated pause is
    idempotent. What this prevents is a pause/enable flip-flop inside one day, ending wherever the
    last run happened to land.
    """
    import datetime as dt

    today = logic.ist_day(dt.datetime.utcnow())
    plan = _pause(
        [_row("1"), _row("2")],
        applied_today={"1": {"state": "PAUSED", "day": today, "at": "now", "rule": "earlier rule"}},
    )
    assert len(plan["changes"]) == 2, "still visible with its reason"
    assert plan["approved_ids"] == ["2"], "but not ticked"
    assert plan["totals"]["changed_today"] == 1


def test_a_pause_still_refuses_a_campaign_somebody_else_optimises():
    """M19's optimiser would simply re-enable it, so our run and theirs would fight.

    Measured: 0 of the real rule's 88 rows are in automated campaigns, so this costs nothing today —
    but the reason holds more strongly for a pause than for a bid.

    **`manager` is deleted rather than the campaign merely renamed**, so this exercises the real
    path: `plan_run` re-derives the classification from the campaign NAME whenever the field is
    absent, precisely so a row assembled by an older code path or a hand-built request cannot slip
    through unclassified. `_row` sets `manager` explicitly, which would otherwise mask that.
    """
    row = _row("1", campaign="m19 autopilot MF")
    del row["manager"]
    plan = _pause([row])
    assert plan["changes"] == []
    assert plan["skipped"][0]["reason"] == logic.SKIP_AUTOMATED
    assert plan["totals"]["automated"] == 1
