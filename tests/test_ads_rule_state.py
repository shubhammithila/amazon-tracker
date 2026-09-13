"""Saving a PAUSE rule: the amount is a state string, not a number.

**Reported as "I just saved a rule but it is not showing".** `POST /ads/rules` returned 500 twice:

    ValueError: could not convert string to float: 'PAUSED'
    sqlalchemy.exc.StatementError: (builtins.ValueError) could not convert string to float: 'PAUSED'

`AdsRule.amount` is `Numeric(12, 2)`. A bid rule's amount is `10` (a percentage or a rupee figure); a
pause rule's is the string `"PAUSED"`, which that column cannot hold.

**This is the same class of gap as the "no usable bid" bug two days earlier**, and it is the third
time the pause feature has been shipped with one end of a path unconverted. When `set_state` was added
I updated `plan_run`, `apply_changes`, the ledger and the preview — and not the SAVED-RULES table,
because saving a rule is a different route from previewing one and no test crossed both.

The lesson recorded in CLAUDE.md for the earlier case was "20 passing tests verified the SERVER
contract and not one checked that the CLIENT honours it". This one is its sibling: the feature was
verified along the path it was designed for, and the neighbouring path that shares its vocabulary was
never walked.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.ads import logic, repository
from app.models import AdsRule

pytestmark = pytest.mark.regression


async def test_a_pause_rule_can_be_saved_and_read_back(db):
    """The bug, at the repository level: a state amount must survive a round trip."""
    saved = await repository.save_rule(db, "kill the losers", {
        "conditions": [{"field": "spend", "op": "gt", "value": 1000},
                       {"field": "roas", "op": "lt", "value": 1}],
        "action": logic.ACTION_SET_STATE,
        "amount": logic.STATE_PAUSED,
        "window_days": 30,
    })
    assert saved["amount"] == "PAUSED"

    rules = {r["name"]: r for r in await repository.load_rules(db)}
    assert "kill the losers" in rules, "the rule was not stored at all"
    assert rules["kill the losers"]["amount"] == "PAUSED", (
        "the state came back as something other than PAUSED — `_f()` would turn it into 0.0 or None, "
        "and the rule would then load into the screen with no state"
    )
    assert rules["kill the losers"]["action"] == logic.ACTION_SET_STATE


async def test_a_bid_rule_still_round_trips_as_a_number(db):
    """The regression guard. `amount` must stay numeric for the four bid actions.

    A rule stored as the STRING "10" would still preview correctly — `new_bid` coerces — so this is
    asserted on the type rather than only the value, or the fix could quietly turn every saved
    percentage into text.
    """
    await repository.save_rule(db, "trim the weak", {
        "conditions": [{"field": "spend", "op": "gt", "value": 100}],
        "action": logic.ACTION_DECREASE_PCT,
        "amount": 10,
        "window_days": 7,
    })
    rules = {r["name"]: r for r in await repository.load_rules(db)}
    amount = rules["trim the weak"]["amount"]
    assert amount == 10.0
    assert isinstance(amount, float), f"a bid amount must stay numeric, got {type(amount).__name__}"


async def test_the_two_kinds_are_stored_in_different_columns(db):
    """**A state rule must not leave a stray number in `amount`, nor a bid rule a stray state.**

    Otherwise `load_rules` has to guess which field to believe, and the screen would load a rule whose
    action says one thing and whose amount says another — the ambiguity that made `ads_mutation` need
    its own `action` column for exactly the same reason.
    """
    await repository.save_rule(db, "pause it", {
        "conditions": [{"field": "spend", "op": "gt", "value": 1000}],
        "action": logic.ACTION_SET_STATE, "amount": "PAUSED", "window_days": 30,
    })
    await repository.save_rule(db, "cut it", {
        "conditions": [{"field": "spend", "op": "gt", "value": 100}],
        "action": logic.ACTION_DECREASE_PCT, "amount": 10, "window_days": 7,
    })

    rows = {r.name: r for r in (await db.execute(select(AdsRule))).scalars().all()}
    assert rows["pause it"].amount is None, "a state rule should leave the numeric column empty"
    assert rows["pause it"].target_state == "PAUSED"
    assert rows["cut it"].target_state is None, "a bid rule should leave the state column empty"
    assert float(rows["cut it"].amount) == 10.0


@pytest.mark.parametrize("bad", ["ARCHIVED", "off", "", None])
async def test_an_unusable_state_is_refused_on_the_way_IN(db, bad):
    """Validated on save, not only on run.

    The `good_rating: 99` lesson: a value stored now and rejected later is a rule the owner has to
    debug at the moment they want to use it. `ARCHIVED` in particular is refused everywhere — it is
    terminal at Amazon and has no undo.
    """
    with pytest.raises(ValueError):
        await repository.save_rule(db, "bad state", {
            "conditions": [{"field": "spend", "op": "gt", "value": 1000}],
            "action": logic.ACTION_SET_STATE, "amount": bad, "window_days": 30,
        })


async def test_saving_a_pause_rule_through_the_route_returns_200(auth_client):
    """**End to end, because the 500 happened at the route and not in a helper.**

    The screen posts to `POST /ads/rules`; a repository-level test alone would not prove the route
    stopped returning 500. This posts exactly what `templates/ads.html` sends for a pause rule.
    """
    response = await auth_client.post("/ads/rules", json={
        "name": "spend>1000, ROAS<1, pause",
        "conditions": [{"field": "spend", "op": "gt", "value": 1000},
                       {"field": "roas", "op": "lt", "value": 1}],
        "action": "set_state",
        "amount": "PAUSED",
        "window_days": 30,
    })
    assert response.status_code == 200, (
        f"saving a pause rule returned {response.status_code}: {response.text[:200]}"
    )
    assert response.json()["saved"]["amount"] == "PAUSED", (
        "the route must echo back the state the screen sent, or the saved-rule chip renders blank"
    )

    # **The dashboard is where the screen reads its rules from** — there is no `GET /ads/rules`;
    # `templates/ads.html` uses `data.rules` off the `GET /ads` payload. Asserted through that path
    # rather than the one that seemed likely, since the report was that the rule did not SHOW.
    listed = await auth_client.get("/ads?days=7")
    assert listed.status_code == 200, listed.text[:200]
    saved_rules = {r["name"]: r for r in listed.json().get("rules", [])}
    assert "spend>1000, ROAS<1, pause" in saved_rules, (
        "the rule saved but does not appear on the dashboard — which is exactly what was reported"
    )
    assert saved_rules["spend>1000, ROAS<1, pause"]["amount"] == "PAUSED"
