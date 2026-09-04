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

from sqlalchemy import select

from app.ads import logic, repository, spapi_ads
from app.models import AdsMutation


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


# ─── The ledger ──────────────────────────────────────────────────────────────


async def test_open_run_records_a_state_change_and_its_action(db):
    """The ledger must be able to express a pause, or undo cannot reverse one."""
    run_id = await repository.open_run(db, [{
        "entity_id": "111", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
        "text": "kw", "campaign_id": "c1", "ad_group_id": "g1",
        "old_state": "ENABLED", "new_state": "PAUSED",
    }], rule_summary="spend>1000, roas<1 -> PAUSED")

    row = (await db.execute(
        select(AdsMutation).where(AdsMutation.run_id == run_id)
    )).scalars().one()
    assert row.action == "state"
    assert row.old_state == "ENABLED"
    assert row.new_state == "PAUSED"
    assert row.old_bid is None and row.new_bid is None
    assert row.status == "pending", "written BEFORE the wire"


async def test_open_run_records_a_bid_change_as_action_bid(db):
    """The default must stay `bid`, so a year of existing rows keep their meaning."""
    run_id = await repository.open_run(db, [{
        "entity_id": "222", "writer": logic.WRITER_TARGET, "ad_product": "sp",
        "old_bid": 12.0, "new_bid": 13.2,
    }], rule_summary="+10%")
    row = (await db.execute(
        select(AdsMutation).where(AdsMutation.run_id == run_id)
    )).scalars().one()
    assert row.action == "bid"
    assert row.new_state is None


async def test_open_run_records_the_ad_product_it_was_given(db):
    """**A pre-existing bug, fixed with this change rather than around it.**

    Measured on production: 304 Sponsored Brands rows were stored as `ad_product="sp"` because
    `open_run` never passed the field and the column default won. Harmless so far — `writer` carries
    the routing — but the column exists so the audit trail can name the API that was written to, and
    it was naming the wrong one.
    """
    run_id = await repository.open_run(db, [{
        "entity_id": "333", "writer": logic.WRITER_SB_KEYWORD, "ad_product": "sb",
        "old_bid": 20.0, "new_bid": 18.0,
    }], rule_summary="-10%")
    row = (await db.execute(
        select(AdsMutation).where(AdsMutation.run_id == run_id)
    )).scalars().one()
    assert row.ad_product == "sb", "an SB row must not be recorded as Sponsored Products"
    assert row.entity_type == "keyword", "sb_keyword is a keyword, not a target"


# ─── The write ───────────────────────────────────────────────────────────────


async def _fake_token(client):
    """`_access_token` stubbed out — no test may authenticate against LWA."""
    return "token"


@pytest.fixture
def ads_endpoint(monkeypatch):
    """A fake endpoint, so the writer builds a URL without reaching Amazon."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "ads_endpoint", "https://ads.test", raising=False)
    return settings


class _Recorder:
    """A fake httpx client that records what was PUT. Mirrors the fakes in test_ads_writes.py."""

    def __init__(self, body=None, status=207):
        self.sent = []
        self._body = body if body is not None else {"keywords": {"success": [], "error": []}}
        self._status = status

    async def put(self, url, json=None, headers=None):
        self.sent.append({"url": url, "json": json, "headers": headers or {}})
        body, status = self._body, self._status

        class _Response:
            status_code = status
            text = ""

            def json(self):
                return body

        return _Response()


async def test_an_sp_pause_sends_state_and_no_bid(monkeypatch, ads_endpoint):
    """The SP payload must carry `state` INSTEAD of `bid`, not alongside it.

    Amazon's update schemas require only the id and treat everything else as a partial update, so
    sending both would apply a bid change nobody previewed.
    """
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = _Recorder({"keywords": {"success": [{"index": 0, "keywordId": "111"}], "error": []}})

    await spapi_ads.apply_changes(client, [{
        "entity_id": "111", "ad_group_id": "g1", "campaign_id": "c1",
        "ad_product": "sp", "new_state": "PAUSED",
    }], writer=logic.WRITER_KEYWORD)

    row = client.sent[0]["json"]["keywords"][0]
    assert row == {"keywordId": "111", "state": "PAUSED"}
    assert "bid" not in row


async def test_an_sb_pause_is_lower_case_and_carries_its_parent_ids(monkeypatch, ads_endpoint):
    """**Three SB requirements, each of which fails inside a 207 whose status says success.**

    Lower-case state, `adGroupId`, and — for a keyword STATE write specifically — `campaignId`,
    which a bid write does not need.
    """
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = _Recorder([{"code": "SUCCESS", "keywordId": 111}])

    await spapi_ads.apply_changes(client, [{
        "entity_id": "111", "ad_group_id": "222", "campaign_id": "333",
        "ad_product": "sb", "new_state": "PAUSED",
    }], writer=logic.WRITER_SB_KEYWORD)

    row = client.sent[0]["json"][0]
    assert row["state"] == "paused", "SB rejects upper case"
    assert row["adGroupId"] == 222
    assert row["campaignId"] == 333, "required for an SB keyword STATE write"
    assert "bid" not in row


async def test_an_sb_target_pause_is_lower_case_under_its_targets_key(monkeypatch, ads_endpoint):
    """SB targets use a dict under `targets`, not SB keywords' bare list."""
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = _Recorder({"updateTargetSuccessResults": [
        {"targetRequestIndex": 0, "targetId": 444}], "updateTargetErrorResults": []})

    await spapi_ads.apply_changes(client, [{
        "entity_id": "444", "ad_group_id": "222", "campaign_id": "333",
        "ad_product": "sb", "new_state": "PAUSED",
    }], writer=logic.WRITER_SB_TARGET)

    row = client.sent[0]["json"]["targets"][0]
    assert row["state"] == "paused"
    assert row["targetId"] == 444
    assert "bid" not in row


async def test_a_bid_write_is_unchanged_by_the_rename(monkeypatch, ads_endpoint):
    """The regression guard. A bid payload must not gain a state key."""
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = _Recorder()
    await spapi_ads.apply_changes(client, [{
        "entity_id": "111", "ad_product": "sp", "old_bid": 12.0, "new_bid": 13.2,
    }], writer=logic.WRITER_KEYWORD)
    assert client.sent[0]["json"]["keywords"][0] == {"keywordId": "111", "bid": 13.2}


def test_apply_bids_is_gone_and_the_scheduler_guard_names_the_new_function():
    """**The rename would otherwise silently retire a safety assertion.**

    `tests/test_retention_and_scheduler.py` proves the nightly job cannot write to Amazon by grepping
    source for literal names. After a rename the old literal appears nowhere, so that loop passes
    VACUOUSLY — a green test on the guard that stops a scheduled job moving live bids. Same trap
    CLAUDE.md records for the deploy detector, where grepping for a revision id passed with the
    branch deleted because the id also appeared in a comment.
    """
    import pathlib

    assert not hasattr(spapi_ads, "apply_bids"), "no alias: both callers must be updated"
    assert hasattr(spapi_ads, "apply_changes")
    text = pathlib.Path("tests/test_retention_and_scheduler.py").read_text(encoding="utf-8")
    assert "apply_changes" in text, "the scheduler guard must search for the CURRENT name"


# ─── The once-per-day guard's own basis ──────────────────────────────────────


async def test_last_applied_bids_ignores_state_rows(db):
    """**Pins an INCIDENTAL protection, which is why it needs a test rather than an edit.**

    `last_applied_bids` filters `new_bid IS NOT NULL`, so a state row is already excluded with no code
    change. But it holds by accident of a filter written for another purpose: widen it and a paused
    row's null bid becomes the "true current bid", so the next percentage rule computes from a null.
    Stated as a requirement so the filter cannot later be removed as redundant.
    """
    run_id = await repository.open_run(db, [{
        "entity_id": "555", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
        "old_state": "ENABLED", "new_state": "PAUSED",
    }], rule_summary="pause")
    await repository.record_results(db, run_id, [{"entity_id": "555", "ok": True}])

    assert await repository.last_applied_bids(db, ["555"]) == {}, (
        "a pause is not a bid change and must never be served as the current bid"
    )


async def test_last_applied_states_reports_a_row_paused_today(db):
    """The day guard's own basis. Only `applied` rows count, like the bid version."""
    import datetime as dt

    run_id = await repository.open_run(db, [{
        "entity_id": "666", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
        "old_state": "ENABLED", "new_state": "PAUSED",
    }], rule_summary="spend>1000 -> PAUSED")
    await repository.record_results(db, run_id, [{"entity_id": "666", "ok": True}])

    found = await repository.last_applied_states(db, ["666"])
    assert found["666"]["state"] == "PAUSED"
    assert found["666"]["day"] == logic.ist_day(dt.datetime.utcnow())
    assert found["666"]["rule"] == "spend>1000 -> PAUSED"


async def test_last_applied_states_excludes_a_failed_row(db):
    """A failed row never changed anything at Amazon, so it must not gate a later run.

    Same rule `build_undo` follows for the same reason: treating a refusal as a real change is how a
    guard starts blocking work that was never done.
    """
    run_id = await repository.open_run(db, [{
        "entity_id": "777", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
        "old_state": "ENABLED", "new_state": "PAUSED",
    }], rule_summary="pause")
    await repository.record_results(
        db, run_id, [{"entity_id": "777", "ok": False, "error": "refused"}])

    assert await repository.last_applied_states(db, ["777"]) == {}


async def test_last_applied_states_ignores_bid_rows(db):
    """The mirror image of the first test: a bid change is not a state change."""
    run_id = await repository.open_run(db, [{
        "entity_id": "888", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
        "old_bid": 10.0, "new_bid": 11.0,
    }], rule_summary="+10%")
    await repository.record_results(db, run_id, [{"entity_id": "888", "ok": True}])

    assert await repository.last_applied_states(db, ["888"]) == {}
