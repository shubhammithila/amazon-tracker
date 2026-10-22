"""The once-a-day limit is per RULE, and saved rules can be edited (22 Oct 2026).

Asked for as *"Multiple rules can be applied to a keyword but the same rule cannot be applied to a
keyword more than once in a day."* A rule is identified by what it does — the sentence
`routers.ads._rule_summary` writes and every ledger row already stores — so two saved rules with the
same contents are one rule, and editing a number makes a different one.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.ads import logic, repository, spapi_ads

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "templates" / "ads.html").read_text(encoding="utf-8")
TODAY = "2026-08-31"
RULE_A = "Spend > 100 -> bid decrease 10%"
RULE_B = "ROAS < 1 -> bid decrease 20%"


def _row(entity_id="K1", bid=10.0):
    return {"keywordId": entity_id, "matchType": "EXACT", "keyword": f"kw{entity_id}",
            "campaignName": "MF_SP_kw", "campaignId": "c1", "adGroupId": "g1",
            "cost": 500.0, "sales7d": 1500.0, "keywordBid": bid}


ARGS = dict(conditions=[{"field": "spend", "op": "gt", "value": 100}],
            action=logic.ACTION_DECREASE_PCT, amount=10)


def _today(rule, *, bid_from=10.0, bid_to=9.0, state=""):
    return {"K1": [{"rule": rule, "at": f"{TODAY}T05:12:00", "bid_from": bid_from,
                    "bid_to": bid_to, "state": state}]}


# ─── The plan ────────────────────────────────────────────────────────────────


def test_the_same_rule_is_held_back_on_a_keyword_it_changed_today():
    plan = logic.plan_run([_row()], **ARGS, rule=RULE_A, today_changes=_today(RULE_A), today=TODAY)
    change = plan["changes"][0]
    assert change["changed_today"] is True and plan["approved_ids"] == []
    assert change["changed_rule"] == RULE_A and change["other_today"] == []


def test_a_different_rule_may_change_it_and_says_what_happened_earlier():
    plan = logic.plan_run([_row()], **ARGS, rule=RULE_A, today_changes=_today(RULE_B), today=TODAY)
    change = plan["changes"][0]
    assert change["changed_today"] is False and plan["approved_ids"] == ["K1"]
    note = change["other_today"][0]
    assert note["rule"] == RULE_B and note["what"] == "bid ₹10.00 → ₹9.00"


def test_a_pause_earlier_today_is_noted_on_an_enable_rule():
    plan = logic.plan_run([_row()], conditions=ARGS["conditions"], action=logic.ACTION_SET_STATE,
                          amount="ENABLED", rule="Spend > 100 -> turn ON (enable)",
                          today_changes=_today("Spend > 1000 -> turn OFF (pause)", state="PAUSED"),
                          today=TODAY)
    change = plan["changes"][0]
    assert change["changed_today"] is False
    assert change["other_today"][0]["what"] == "turned OFF"


def test_without_per_rule_data_any_change_today_still_holds_a_row_back():
    """Callers that do not pass `today_changes` keep the old meaning."""
    plan = logic.plan_run([_row()], **ARGS, today=TODAY, applied_today={
        "K1": {"bid": 9.0, "at": "x", "rule": RULE_B, "day": TODAY}})
    assert plan["changes"][0]["changed_today"] is True


def test_the_same_rule_always_writes_the_same_sentence():
    """The sentence is the rule's identity, so a saved 10.0 must match the screen's 10."""
    from app.routers.ads import _rule_summary
    cond = [{"field": "spend", "op": "gt", "value": 100}]
    assert _rule_summary(cond, logic.ACTION_DECREASE_PCT, 10.0) == _rule_summary(cond, logic.ACTION_DECREASE_PCT, 10)
    assert _rule_summary([{**cond[0], "value": 100.0}], logic.ACTION_DECREASE_PCT, 10) ==         _rule_summary(cond, logic.ACTION_DECREASE_PCT, 10)
    assert _rule_summary(cond, logic.ACTION_DECREASE_PCT, 2.5) != _rule_summary(cond, logic.ACTION_DECREASE_PCT, 25)


# ─── The ledger read ─────────────────────────────────────────────────────────


async def _ledger(db, entity_id, *, rule, when, status="applied", old=10.0, new=9.0, state=None):
    from app.models import AdsMutation
    db.add(AdsMutation(run_id=f"r-{entity_id}-{when.isoformat()}-{rule}", entity_id=entity_id,
                       entity_type="keyword", writer="keyword", old_bid=old if state is None else None,
                       new_bid=new if state is None else None, new_state=state,
                       action="state" if state else "bid", status=status, rule_summary=rule,
                       created_at=when))
    await db.commit()


async def test_changes_today_is_the_ist_day_and_applied_rows_only(db):
    # 22:30 UTC on the 30th is 04:00 IST on the 31st: today. 18:00 UTC on the 30th is the 30th IST.
    await _ledger(db, "K1", rule=RULE_A, when=datetime(2026, 8, 30, 22, 30))
    await _ledger(db, "K1", rule=RULE_B, when=datetime(2026, 8, 30, 18, 0))
    await _ledger(db, "K1", rule="failed one", when=datetime(2026, 8, 31, 5, 0), status="failed")
    await _ledger(db, "K1", rule="undone one", when=datetime(2026, 8, 31, 6, 0), status="reverted")
    found = await repository.changes_today(db, ["K1"], TODAY)
    assert [e["rule"] for e in found["K1"]] == [RULE_A]
    assert found["K1"][0]["bid_from"] == 10.0 and found["K1"][0]["bid_to"] == 9.0
    assert await repository.changes_today(db, [], TODAY) == {}


# ─── Apply re-checks ─────────────────────────────────────────────────────────


@pytest.fixture
def amazon(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ads_endpoint", "https://ads.test", raising=False)
    sent = []

    async def live(_client, changes):
        return {str(c["entity_id"]): {"bid": c.get("old_bid"), "state": "ENABLED"} for c in changes}

    async def push(_client, rows, *, writer):
        sent.extend(rows)
        return [{"entity_id": r["entity_id"], "ok": True} for r in rows]

    monkeypatch.setattr(spapi_ads, "fetch_current_bids", live)
    monkeypatch.setattr(spapi_ads, "apply_changes", push)
    return sent


def _change():
    return {"entity_id": "111", "writer": logic.WRITER_KEYWORD, "ad_product": "sp", "text": "kw",
            "old_bid": 9.0, "new_bid": 8.1, "match_type": "EXACT", "campaign_id": "c1", "ad_group_id": "g1"}


async def test_apply_refuses_the_same_rule_twice_but_lets_another_rule_through(auth_client, db, amazon):
    await _ledger(db, "111", rule=RULE_A, when=datetime.utcnow())
    same = (await auth_client.post("/ads/apply", json={"rule": RULE_A, "changes": [_change()]})).json()
    assert same.get("applied", 0) == 0 and same["repeated"], same
    assert "this rule already" in same["repeated"][0]["reason"]
    other = (await auth_client.post("/ads/apply", json={"rule": RULE_B, "changes": [_change()]})).json()
    assert other["applied"] == 1 and other["repeated"] == [], other
    assert len(amazon) == 1


async def test_a_change_that_was_undone_does_not_block_the_rule(auth_client, db, amazon):
    await _ledger(db, "111", rule=RULE_A, when=datetime.utcnow(), status="reverted")
    body = (await auth_client.post("/ads/apply", json={"rule": RULE_A, "changes": [_change()]})).json()
    assert body["applied"] == 1, body


async def test_applying_a_saved_rule_stamps_when_it_last_ran(auth_client, db, amazon):
    await repository.save_rule(db, "cut", {"conditions": [{"field": "spend", "op": "gt", "value": 100}],
                                          "action": logic.ACTION_DECREASE_PCT, "amount": 10})
    from app.routers.ads import _rule_summary
    sentence = _rule_summary([{"field": "spend", "op": "gt", "value": 100}], logic.ACTION_DECREASE_PCT, 10)
    await auth_client.post("/ads/apply", json={"rule": sentence, "changes": [_change()]})
    rules = await repository.load_rules(db)
    assert rules[0]["last_run_at"], "the saved rule's last run was not recorded"


# ─── Saved rules: edit, rename, clash ────────────────────────────────────────


RULE_BODY = {"conditions": [{"field": "spend", "op": "gt", "value": 100}],
             "action": logic.ACTION_DECREASE_PCT, "amount": 10}


async def test_a_rule_is_renamed_by_id_without_making_a_second_one(auth_client):
    first = (await auth_client.post("/ads/rules", json={"name": "old", **RULE_BODY})).json()["saved"]
    r = await auth_client.post("/ads/rules", json={"id": first["id"], "name": "new", **RULE_BODY, "amount": 15})
    assert r.status_code == 200, r.text
    rules = (await auth_client.get("/ads?days=7")).json().get("rules")
    names = [x["name"] for x in rules]
    assert names == ["new"] and rules[0]["amount"] == 15


async def test_a_taken_name_is_refused_unless_replacing(auth_client):
    await auth_client.post("/ads/rules", json={"name": "cut", **RULE_BODY})
    clash = await auth_client.post("/ads/rules", json={"name": "cut", **RULE_BODY, "amount": 20})
    assert clash.status_code == 409
    ok = await auth_client.post("/ads/rules", json={"name": "cut", **RULE_BODY, "amount": 20, "replace": True})
    assert ok.status_code == 200


async def test_renaming_onto_another_rules_name_is_refused(auth_client):
    a = (await auth_client.post("/ads/rules", json={"name": "a", **RULE_BODY})).json()["saved"]
    await auth_client.post("/ads/rules", json={"name": "b", **RULE_BODY})
    r = await auth_client.post("/ads/rules", json={"id": a["id"], "name": "b", **RULE_BODY})
    assert r.status_code == 409


async def test_an_unknown_rule_id_is_404(auth_client):
    r = await auth_client.post("/ads/rules", json={"id": 999, "name": "x", **RULE_BODY})
    assert r.status_code == 404


# ─── The page ────────────────────────────────────────────────────────────────


def test_the_saved_rules_panel_can_use_edit_copy_and_delete():
    for hook in ("data-use", "data-edit", "data-copy", "data-delete", 'id="rule-name"',
                 'id="save-area"', "Save changes", "Save as new"):
        assert hook in PAGE, hook
    assert "window.prompt(" not in PAGE and 'id="delete-rule"' not in PAGE


def test_the_preview_shows_what_another_rule_did_today():
    assert "other_today" in PAGE
