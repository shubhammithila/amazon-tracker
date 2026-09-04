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
