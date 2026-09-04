"""Source-level guards for the pause action's screen.

**Source-level because no runtime test can watch a template read the wrong field** — the same honesty
`tests/test_ads_one_source.py` already uses for `daily=True` and `tests/test_ads_sb.py` for the poll
loops' header dicts.

The bug these mostly guard is silent rather than loud: `Number("PAUSED")` is `NaN`, `JSON.stringify`
writes `NaN` as `null`, and the server then receives a rule with no state at all. Nothing throws,
nothing appears in a log, and the run either 400s for the wrong reason or does the wrong thing.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ADS_HTML = pathlib.Path("templates/ads.html")


@pytest.fixture(scope="module")
def markup():
    return ADS_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script(markup):
    """Just the <script> bodies, so prose elsewhere in the page cannot satisfy a code assertion."""
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", markup, re.S))


def test_the_action_dropdown_offers_a_state_change(markup):
    assert 'value="set_state"' in markup
    assert 'id="state-amount"' in markup
    assert "PAUSED" in markup and "ENABLED" in markup


def test_archived_is_not_offered_as_a_choice(markup):
    """The screen must not offer what the server refuses.

    Amazon's archive is terminal and has no undo, so it is absent from `WRITABLE_STATES`. A control
    that produced a 400 every time would read as a broken app.
    """
    options = re.findall(r'<option value="([^"]*)"', markup)
    assert "ARCHIVED" not in options
    assert "archived" not in options


def test_the_state_value_is_never_coerced_to_a_number(script):
    """**`Number("PAUSED")` is NaN, which JSON.stringify writes as null.**

    So the server would receive no state at all. Asserted on SOURCE because the failure is a silently
    absent field rather than an exception: the request succeeds and does the wrong thing.
    """
    bare = re.findall(r"amount:\s*Number\(", script)
    assert not bare, (
        f"{len(bare)} payload site(s) still coerce the amount to a number unconditionally; a state "
        f"action sends a string, and Number('PAUSED') is NaN -> null"
    )
    assert "function ruleAmount(" in script, "one helper must decide the amount for every call site"


def test_both_payload_sites_go_through_the_one_amount_helper(script):
    """Two copies of this decision is how one path starts sending null.

    The save-rule path and the preview path each read the amount box, and a saved rule that stored
    `null` would preview as having no amount for ever after.
    """
    assert len(re.findall(r"amount:\s*ruleAmount\(\)", script)) >= 2


def test_a_state_run_renders_the_state_pair_and_not_a_bid(script):
    """`old_bid.toFixed()` would THROW on a state row, which carries no bid at all.

    So this is not only a labelling question: the bid branch must not run for a state row.
    """
    assert "isStateRun" in script, "the renderer must know which kind of run it is drawing"
    assert "c.new_state" in script, "a state run renders the state pair"
    # The bid cells must be defensive even on the bid branch, since a malformed row is possible.
    assert "c.old_bid.toFixed" not in script, (
        "an unguarded .toFixed on old_bid throws for a state row, which has no bid"
    )


def test_the_apply_bar_says_a_pause_stops_delivery_and_is_reversible(markup):
    """A bid nudge and stopping delivery cannot share one confirmation sentence."""
    lowered = markup.lower()
    assert "stop serving" in lowered
    assert "reversible" in lowered


def test_the_already_in_that_state_bucket_is_reported(script):
    """Excluded and NAMED, the standing rule in this feature.

    A count quietly smaller than the table on screen reads as the rule not working.
    """
    assert "body.unchanged" in script
    assert "already in that state" in script


def test_loading_a_saved_state_rule_restores_its_control(script):
    """A state string in the NUMBER box renders an empty box, so the rule reads as having no amount.

    The one case a runtime test covers poorly, and where the loader bug would actually show.
    """
    assert 'isStateRun(rule.action)' in script
    assert '$("state-amount").value = rule.amount' in script
