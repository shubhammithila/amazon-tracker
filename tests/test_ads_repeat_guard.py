"""The bid shown after a rule has run, and refusing to move it twice in a day.

**Both halves are one change, and the order is why.** The preview computes a new bid from the report
figure, which does not update when we change a bid — measured on production, the report held 13.86
while Amazon held the 15.25 we had just set. That staleness accidentally made a repeat run idempotent
(13.86 x 1.10 = 15.25 twice). Showing the true bid CREATES the compounding: 15.25 x 1.10 = 16.78, so a
-10% rule applied twice is -19%. The guard therefore ships with the fix, not after it.

The true bid needs no Amazon call: `ads_mutation` already records what we set it to.
"""
from datetime import datetime

import pytest

from app.ads import logic, repository

pytestmark = pytest.mark.regression


def _row(entity_id, *, bid, spend=500.0, sales=1500.0, match_type="EXACT", campaign="MF_SP_kw"):
    return {
        "keywordId": str(entity_id), "matchType": match_type, "keyword": f"kw{entity_id}",
        "campaignName": campaign, "campaignId": "c1", "adGroupId": "g1",
        "cost": spend, "sales7d": sales, "keywordBid": bid,
    }


RULE = dict(conditions=[{"field": "spend", "op": "gt", "value": 100}],
            action=logic.ACTION_INCREASE_PCT, amount=10)


# ─── The IST day ─────────────────────────────────────────────────────────────


def test_the_ist_day_is_not_the_utc_day_for_five_and_a_half_hours():
    """**CLAUDE.md records FOUR separate bugs in this codebase from this exact boundary**, including
    one that back-dated a GST invoice.

    The ledger stores `datetime.utcnow()`, and "not twice on the same day" is a decision taken in IST.
    A change applied at 04:00 IST must count as TODAY: in UTC that is 22:30 the previous day, so a
    UTC-day guard would allow a second run that morning.
    """
    # 22:30 UTC on the 30th is 04:00 IST on the 31st.
    assert logic.ist_day(datetime(2026, 8, 30, 22, 30)) == "2026-08-31"
    # 18:29 UTC on the 31st is 23:59 IST the same day — the last minute of the IST day.
    assert logic.ist_day(datetime(2026, 8, 31, 18, 29)) == "2026-08-31"
    # 18:30 UTC is 00:00 IST the NEXT day.
    assert logic.ist_day(datetime(2026, 8, 31, 18, 30)) == "2026-09-01"
    assert logic.ist_day(None) == ""


# ─── The true current bid ────────────────────────────────────────────────────


def test_the_bid_comes_from_the_ledger_not_the_stale_report():
    """Measured on production: the report said 13.86 for a keyword Amazon held at 15.25.

    The ledger knows, so no fetch is needed. `report_bid` travels too, because "the report is stale"
    is a fact worth showing rather than silently correcting.
    """
    plan = logic.plan_run(
        [_row("K1", bid=13.86)], **RULE,
        applied_today={"K1": {"bid": 15.25, "at": "2026-08-31T14:28:02",
                              "rule": "ROAS >= 5 -> bid increase 10%", "day": "2026-08-31"}},
        today="2026-08-31",
    )
    change = plan["changes"][0]
    assert change["old_bid"] == 15.25, "the preview still shows the stale report bid"
    assert change["report_bid"] == 13.86, "the stale figure is hidden rather than shown"
    assert change["changed_today"] is True
    assert change["changed_at"] == "2026-08-31T14:28:02"
    assert "increase" in change["changed_rule"]


def test_the_new_bid_is_computed_from_the_true_bid():
    """The consequence that matters: a percentage applied to the wrong base is the wrong bid."""
    plan = logic.plan_run(
        [_row("K1", bid=13.86)], **RULE,
        applied_today={"K1": {"bid": 15.25, "at": "x", "rule": "r", "day": "2026-08-31"}},
        today="2026-08-31",
    )
    # 15.25 * 1.10, not 13.86 * 1.10 (which would be 15.25 — the accidental idempotence).
    assert plan["changes"][0]["new_bid"] == pytest.approx(16.78, abs=0.01)


def test_a_row_never_changed_keeps_the_report_bid():
    """The common case must be untouched: most rows have no ledger entry at all."""
    plan = logic.plan_run([_row("K1", bid=13.86)], **RULE, applied_today={}, today="2026-08-31")
    change = plan["changes"][0]
    assert change["old_bid"] == 13.86
    assert change["changed_today"] is False
    assert change["new_bid"] == pytest.approx(15.25, abs=0.01)


def test_a_row_already_at_its_target_is_no_change_not_a_change():
    """**`SKIP_NO_CHANGE` must compare against the TRUE bid**, and this is the quiet failure.

    Computed from the true bid but compared against the stale one, a row already sitting at the value
    the rule wants would be reported as changing — and then sent to Amazon as a no-op write. Found by
    reading the five guards downstream of the substitution rather than by running anything.
    """
    plan = logic.plan_run(
        [_row("K1", bid=10.0)],
        conditions=[{"field": "spend", "op": "gt", "value": 100}],
        action=logic.ACTION_SET, amount=15.25,
        applied_today={"K1": {"bid": 15.25, "at": "x", "rule": "r", "day": "2026-08-31"}},
        today="2026-08-31",
    )
    assert plan["changes"] == [], "a row already at the target was reported as changing"
    assert plan["skipped"][0]["reason"] == logic.SKIP_NO_CHANGE


# ─── Once per day ────────────────────────────────────────────────────────────


def test_a_row_changed_today_is_flagged_and_counted_but_not_dropped():
    """**Unticked and visible, not hidden.**

    A row silently missing from a 1,005-row preview is indistinguishable from a bug — the rule this
    whole screen follows. It stays in the table with its reason so the owner can deliberately re-tick
    it; there are legitimate reasons to move a bid twice.
    """
    plan = logic.plan_run(
        [_row("K1", bid=15.25), _row("K2", bid=10.0)], **RULE,
        applied_today={"K1": {"bid": 15.25, "at": "2026-08-31T14:28", "rule": "r",
                              "day": "2026-08-31"}},
        today="2026-08-31",
    )
    assert len(plan["changes"]) == 2, "the row was dropped instead of flagged"
    by_id = {c["entity_id"]: c for c in plan["changes"]}
    assert by_id["K1"]["changed_today"] is True
    assert by_id["K2"]["changed_today"] is False
    assert plan["totals"]["changed_today"] == 1


def test_the_default_selection_excludes_rows_changed_today():
    """`approved_ids` is what the screen ticks by default, so the guard is in the DATA.

    Computed here rather than in the template, because `POST /ads/apply` must be able to make the same
    judgement — a screen-level untick leaves the route re-appliable from a hand-built request, and
    this is the only route in the app that spends money.
    """
    plan = logic.plan_run(
        [_row("K1", bid=15.25), _row("K2", bid=10.0)], **RULE,
        applied_today={"K1": {"bid": 15.25, "at": "x", "rule": "r", "day": "2026-08-31"}},
        today="2026-08-31",
    )
    assert plan["approved_ids"] == ["K2"], (
        f"a row already changed today is ticked by default: {plan['approved_ids']}"
    )


def test_compounding_is_what_the_guard_prevents():
    """The number that justifies the feature.

    A -10% rule applied twice to the same live bid is **-19%**, not -10%. Built as a decrease because
    that is the direction that quietly loses impressions — an over-increase shows up as spend.
    """
    first = logic.plan_run(
        [_row("K1", bid=18.75)],
        conditions=[{"field": "spend", "op": "gt", "value": 100}],
        action=logic.ACTION_DECREASE_PCT, amount=10, applied_today={}, today="2026-08-31",
    )
    assert first["changes"][0]["new_bid"] == pytest.approx(16.88, abs=0.01)

    # A second run the same day, now seeing the true bid.
    second = logic.plan_run(
        [_row("K1", bid=18.75)],
        conditions=[{"field": "spend", "op": "gt", "value": 100}],
        action=logic.ACTION_DECREASE_PCT, amount=10,
        applied_today={"K1": {"bid": 16.88, "at": "x", "rule": "r", "day": "2026-08-31"}},
        today="2026-08-31",
    )
    proposed = second["changes"][0]["new_bid"]
    assert proposed == pytest.approx(15.19, abs=0.01), "not computed from the true bid"
    assert second["approved_ids"] == [], "the compounding change was ticked by default"
    assert round((15.19 / 18.75 - 1) * 100) == -19, "the arithmetic this guard exists to prevent"


# ─── The repository query ────────────────────────────────────────────────────


async def _apply(db, entity_id, *, old, new, when, status="applied", rule="r"):
    from app.models import AdsMutation

    db.add(AdsMutation(run_id=f"run-{entity_id}-{when.isoformat()}", entity_id=entity_id,
                       entity_type="keyword", writer="keyword", old_bid=old, new_bid=new,
                       status=status, rule_summary=rule, created_at=when))
    await db.commit()


async def test_only_applied_rows_count_as_a_change(db):
    """A `failed` row never changed anything at Amazon, and a `pending` one is unknown.

    Treating either as the current bid would compute the next change from a value Amazon never held —
    the same reasoning `build_undo` follows when it reverses only `applied` rows.
    """
    now = datetime(2026, 8, 31, 9, 0)
    await _apply(db, "K1", old=10.0, new=11.0, when=now, status="failed")
    await _apply(db, "K2", old=10.0, new=11.0, when=now, status="pending")
    await _apply(db, "K3", old=10.0, new=11.0, when=now, status="applied")

    found = await repository.last_applied_bids(db, ["K1", "K2", "K3"])
    assert set(found) == {"K3"}, f"a non-applied row was treated as the current bid: {found}"
    assert found["K3"]["bid"] == 11.0


async def test_the_newest_applied_row_wins(db):
    """Two changes in a day means the LAST one is the current bid."""
    await _apply(db, "K1", old=10.0, new=11.0, when=datetime(2026, 8, 31, 9, 0))
    await _apply(db, "K1", old=11.0, new=12.5, when=datetime(2026, 8, 31, 15, 0))

    found = await repository.last_applied_bids(db, ["K1"])
    assert found["K1"]["bid"] == 12.5


async def test_a_change_from_a_previous_day_gives_the_bid_but_not_the_guard(db):
    """**The two facts are separate, and this is the case that proves it.**

    Yesterday's change is still the true current bid — the report is stale for as long as nobody
    refetches. But it must NOT block a run today, or a rule could never touch the same keyword twice
    in its life.
    """
    await _apply(db, "K1", old=10.0, new=11.0, when=datetime(2026, 8, 29, 9, 0))
    found = await repository.last_applied_bids(db, ["K1"])
    assert found["K1"]["bid"] == 11.0
    assert found["K1"]["day"] == "2026-08-29"

    plan = logic.plan_run([_row("K1", bid=10.0)], **RULE, applied_today=found,
                          today="2026-08-31")
    change = plan["changes"][0]
    assert change["old_bid"] == 11.0, "yesterday's true bid was ignored"
    assert change["changed_today"] is False, "yesterday's change blocked a run today"
    assert plan["approved_ids"] == ["K1"]


async def test_an_empty_id_list_asks_the_database_nothing(db):
    """The preview calls this on every run, including ones that matched nothing."""
    assert await repository.last_applied_bids(db, []) == {}
    assert await repository.last_applied_bids(db, [None, ""]) == {}


# ─── The row limit measures what will actually be sent ───────────────────────


def test_the_row_limit_counts_only_rows_that_can_actually_be_sent():
    """**A rule was blocked for a size it would never send.**

    Measured on the owner's real rule: 109 changing, 105 already changed today, 4 appliable. Scaled up,
    1,100 matches with 1,050 already moved today would be refused for exceeding a 1,000-row limit while
    only 50 rows could go to Amazon.

    The limit exists to bound what reaches Amazon — `/ads/apply` enforces it again on what is actually
    approved — so counting rows the guard has already unticked measures the wrong thing.
    """
    ledger = {}
    rows = []
    for index in range(30):
        rows.append(_row(f"K{index}", bid=10.0))
        if index >= 5:                       # 25 of the 30 already moved today
            ledger[f"K{index}"] = {"bid": 10.0, "at": "x", "rule": "r", "day": "2026-08-31"}

    plan = logic.plan_run(
        rows, **RULE, applied_today=ledger, today="2026-08-31",
        guardrails={"max_rows": 10, "max_bid": 60.0, "min_bid": 1.0, "max_change_pct": 25.0},
    )
    assert plan["blocked"] is None, (
        f"blocked at a 10-row limit while only 5 rows are appliable: {plan['blocked']}"
    )
    assert plan["totals"]["changing"] == 30, "the full match count must still be reported"
    assert len(plan["approved_ids"]) == 5


def test_the_row_limit_still_blocks_a_genuinely_broad_rule():
    """The other half: the guard must still fire when the rows really would be sent."""
    rows = [_row(f"K{index}", bid=10.0) for index in range(30)]
    plan = logic.plan_run(
        rows, **RULE, applied_today={}, today="2026-08-31",
        guardrails={"max_rows": 10, "max_bid": 60.0, "min_bid": 1.0, "max_change_pct": 25.0},
    )
    assert plan["blocked"], "a 30-row run under a 10-row limit was allowed"
    assert "30" in plan["blocked"], "the message does not name the size that was refused"
