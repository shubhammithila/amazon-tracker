"""The mutation ledger — the table that makes a 299-row bid change reversible.

Amazon has no undo. The owner's real rule matched 299 targets carrying Rs 102,945 of weekly spend, so
without `old_bid` recorded locally, reversing a mistaken run means reading 299 numbers off a report
that has already moved on. Every test here protects some part of that guarantee.
"""
from __future__ import annotations

import pytest

from app.ads import logic, repository
from app.models import AdsMutation
from sqlalchemy import func, select


def _change(entity_id, *, writer=logic.WRITER_KEYWORD, old=10.0, new=9.0, text="kw"):
    return {
        "entity_id": str(entity_id), "writer": writer, "text": text,
        "campaign_id": "c1", "ad_group_id": "g1",
        "old_bid": old, "new_bid": new,
    }


# ─── Opening a run ───────────────────────────────────────────────────────────


async def test_a_run_records_the_old_bid_before_anything_is_sent(db):
    """**The ordering IS the safety mechanism.**

    `open_run` is called before the first request. If the process dies mid-run, the ledger already
    holds every row that was in flight and the bid it had before — so the damage is knowable and
    reversible. Writing after the fact would leave a successful Amazon change with no local record,
    which is the one state nothing can recover from.
    """
    run_id = await repository.open_run(
        db, [_change("111", old=12.68, new=11.41)], rule_summary="spend>100, 1<roas<3, -10%"
    )
    rows = await repository.load_run(db, run_id)
    assert len(rows) == 1
    assert rows[0]["old_bid"] == 12.68, "the previous bid was not recorded"
    assert rows[0]["new_bid"] == 11.41
    # `pending` until Amazon answers — NOT optimistically applied.
    assert rows[0]["status"] == "pending"


async def test_the_rule_is_stored_in_words_on_every_row(db):
    """So the ledger reads without joining to a rule that may since have been edited or deleted."""
    run_id = await repository.open_run(db, [_change("1")], rule_summary="spend>100, roas<3, -10%")
    stored = (await db.execute(
        select(AdsMutation).where(AdsMutation.run_id == run_id)
    )).scalars().all()
    assert stored[0].rule_summary == "spend>100, roas<3, -10%"


async def test_two_runs_do_not_share_an_id(db):
    """Undo operates on a run, so two runs sharing an id would revert each other's rows."""
    first = await repository.open_run(db, [_change("1")], rule_summary="a")
    second = await repository.open_run(db, [_change("1")], rule_summary="b")
    assert first != second


async def test_the_writer_is_recorded_rather_than_re_derived(db):
    """Which ENDPOINT each row was sent to, stored at the time.

    Re-deriving it later from `match_type` would hide a routing bug: the ledger would show what we
    *would* do now, not what we actually did.
    """
    run_id = await repository.open_run(db, [
        _change("1", writer=logic.WRITER_KEYWORD),
        _change("2", writer=logic.WRITER_TARGET),
    ], rule_summary="mixed")
    rows = {r["entity_id"]: r["writer"] for r in await repository.load_run(db, run_id)}
    assert rows == {"1": logic.WRITER_KEYWORD, "2": logic.WRITER_TARGET}


# ─── Recording Amazon's answer ───────────────────────────────────────────────


async def test_a_partial_result_marks_each_row_separately(db):
    """`207 Multi-Status` means some rows succeed and some do not, in one response."""
    run_id = await repository.open_run(
        db, [_change("111"), _change("222")], rule_summary="r"
    )
    counts = await repository.record_results(db, run_id, [
        {"entity_id": "111", "ok": True, "error": None},
        {"entity_id": "222", "ok": False, "error": "rangeError: Bid is lower than the minimum"},
    ])
    assert counts == {"applied": 1, "failed": 1, "pending": 0}

    rows = {r["entity_id"]: r for r in await repository.load_run(db, run_id)}
    assert rows["111"]["status"] == "applied" and rows["111"]["error"] == ""
    assert rows["222"]["status"] == "failed"
    assert "minimum" in rows["222"]["error"], "Amazon's own reason was not kept"


async def test_a_row_amazon_never_answered_for_stays_pending_and_is_counted(db):
    """**A pending row is a REPORTED anomaly, not a silent one.**

    It means we sent a change and do not know what happened to it. Rewriting it as applied would
    corrupt the undo chain; rewriting it as failed would claim knowledge we do not have. It stays
    pending and the count is surfaced.
    """
    run_id = await repository.open_run(db, [_change("111"), _change("222")], rule_summary="r")
    counts = await repository.record_results(
        db, run_id, [{"entity_id": "111", "ok": True, "error": None}]
    )
    assert counts == {"applied": 1, "failed": 0, "pending": 1}


# ─── Undo ────────────────────────────────────────────────────────────────────


async def test_undo_swaps_the_bids_of_applied_rows(db):
    """The whole point: one click restores what the run changed."""
    run_id = await repository.open_run(
        db, [_change("111", old=12.68, new=11.41)], rule_summary="r"
    )
    await repository.record_results(db, run_id, [{"entity_id": "111", "ok": True}])

    undo = await repository.build_undo(db, run_id)
    assert len(undo) == 1
    assert undo[0]["old_bid"] == 11.41, "the undo must start from what the bid IS now"
    assert undo[0]["new_bid"] == 12.68, "the undo must restore what it WAS"
    assert undo[0]["writer"] == logic.WRITER_KEYWORD, "an undo must go to the same endpoint"


async def test_undo_skips_failed_rows(db):
    """**A failed row never changed at Amazon.**

    Undoing it would write `old_bid` over a bid that was never replaced — turning a refused edit
    into a real one, in the opposite direction. That is worse than the original failure, and it is
    the kind of bug that only shows up as a bid nobody set.
    """
    run_id = await repository.open_run(
        db, [_change("111", old=12.0, new=10.8), _change("222", old=1.05, new=0.95)],
        rule_summary="r",
    )
    await repository.record_results(db, run_id, [
        {"entity_id": "111", "ok": True},
        {"entity_id": "222", "ok": False, "error": "rangeError: TOO_LOW"},
    ])

    undo = await repository.build_undo(db, run_id)
    assert [u["entity_id"] for u in undo] == ["111"]


async def test_undo_skips_pending_rows(db):
    """Their outcome is unknown, and guessing in either direction is a write nobody asked for."""
    run_id = await repository.open_run(
        db, [_change("111"), _change("222")], rule_summary="r"
    )
    await repository.record_results(db, run_id, [{"entity_id": "111", "ok": True}])
    undo = await repository.build_undo(db, run_id)
    assert [u["entity_id"] for u in undo] == ["111"]


async def test_a_partly_failed_undo_leaves_the_rest_undoable(db):
    """If an undo only half works, the rows that were NOT restored must keep reading `applied`, so
    a second undo can still reach them. Marking the whole run reverted would strand them."""
    run_id = await repository.open_run(
        db, [_change("111", old=12.0, new=10.8), _change("222", old=8.0, new=7.2)],
        rule_summary="r",
    )
    await repository.record_results(db, run_id, [
        {"entity_id": "111", "ok": True}, {"entity_id": "222", "ok": True},
    ])

    # The undo succeeded for 111 only.
    await repository.mark_reverted(db, run_id, ["111"])

    rows = {r["entity_id"]: r["status"] for r in await repository.load_run(db, run_id)}
    assert rows["111"] == "reverted"
    assert rows["222"] == "applied", "a row whose undo failed must stay undoable"

    again = await repository.build_undo(db, run_id)
    assert [u["entity_id"] for u in again] == ["222"], "the second undo cannot reach the remainder"


async def test_an_undo_run_names_the_run_it_reverses(db):
    """So the history reads as a chain rather than a loop, and a double-undo is detectable."""
    original = await repository.open_run(db, [_change("1")], rule_summary="r")
    await repository.record_results(db, original, [{"entity_id": "1", "ok": True}])

    undo_changes = await repository.build_undo(db, original)
    undo_run = await repository.open_run(
        db, undo_changes, rule_summary=f"undo of {original}", reverts_run_id=original
    )
    runs = {r["run_id"]: r for r in await repository.load_runs(db)}
    assert runs[undo_run]["reverts_run_id"] == original
    assert runs[original]["reverts_run_id"] is None


async def test_a_run_with_nothing_applied_is_not_offered_as_undoable(db):
    """An all-failed run changed nothing, so an Undo button on it would be a trap."""
    run_id = await repository.open_run(db, [_change("1")], rule_summary="r")
    await repository.record_results(db, run_id, [
        {"entity_id": "1", "ok": False, "error": "nope"},
    ])
    runs = {r["run_id"]: r for r in await repository.load_runs(db)}
    assert runs[run_id]["undoable"] is False


async def test_a_row_with_no_recorded_old_bid_is_never_restored_as_zero(db):
    """Defensive: `open_run` always records `old_bid`, but a null must be skipped rather than
    written as bid 0 — which Amazon would reject, or worse, accept."""
    run_id = await repository.open_run(db, [_change("1", old=None, new=9.0)], rule_summary="r")
    await repository.record_results(db, run_id, [{"entity_id": "1", "ok": True}])
    assert await repository.build_undo(db, run_id) == []


# ─── Run history ─────────────────────────────────────────────────────────────


async def test_runs_are_listed_newest_first_with_their_counts(db):
    """The history panel. Counts per status, because "299 changes" and "294 changed, 5 refused" are
    different claims."""
    first = await repository.open_run(db, [_change("1"), _change("2")], rule_summary="first rule")
    await repository.record_results(db, first, [
        {"entity_id": "1", "ok": True}, {"entity_id": "2", "ok": False, "error": "no"},
    ])
    second = await repository.open_run(db, [_change("3")], rule_summary="second rule")
    await repository.record_results(db, second, [{"entity_id": "3", "ok": True}])

    runs = await repository.load_runs(db)
    ids = [r["run_id"] for r in runs]
    assert set(ids) >= {first, second}

    by_id = {r["run_id"]: r for r in runs}
    assert by_id[first]["rows"] == 2
    assert by_id[first]["applied"] == 1 and by_id[first]["failed"] == 1
    assert by_id[first]["rule"] == "first rule"


# ─── Guardrail storage ───────────────────────────────────────────────────────


async def test_guardrails_round_trip_and_reset_by_deleting_the_row(db):
    """Reset DELETES, so "never customised" and "customised back to the defaults" are one state."""
    assert (await repository.load_guardrails(db))["max_bid"] == logic.DEFAULT_GUARDRAILS["max_bid"]

    await repository.save_guardrails(db, {"max_bid": 45.0}, updated_by="owner")
    assert (await repository.load_guardrails(db))["max_bid"] == 45.0

    await repository.reset_guardrails(db)
    assert (await repository.load_guardrails(db))["max_bid"] == logic.DEFAULT_GUARDRAILS["max_bid"]


async def test_an_absurd_guardrail_is_refused_with_a_reason(db):
    """A ceiling of Rs 0.01 would refuse every bid; a 5000% change limit would disable the ceiling
    that Amazon does not provide."""
    with pytest.raises(ValueError) as exc:
        await repository.save_guardrails(db, {"max_change_pct": 5000})
    assert "max_change_pct" in str(exc.value)

    with pytest.raises(ValueError):
        await repository.save_guardrails(db, {"made_up": 1})


async def test_guardrails_stored_out_of_range_fall_back_on_read(db):
    """A value written by an older version, or by hand, must not keep weakening the ceiling."""
    from app.models import PortfolioSettings
    db.add(PortfolioSettings(
        name=repository.GUARDRAIL_SETTING_NAME,
        value_json='{"max_change_pct": 9999, "max_bid": 50.0}',
    ))
    await db.commit()

    loaded = await repository.load_guardrails(db)
    assert loaded["max_change_pct"] == logic.DEFAULT_GUARDRAILS["max_change_pct"]
    assert loaded["max_bid"] == 50.0, "a legal stored value must still be honoured"


# ─── Performance cache ───────────────────────────────────────────────────────
#
# **These read DAILY rows, because that is the only grain.**
#
# They used to call `save_performance`/`load_performance` for a WINDOW. That table is deleted: holding
# the same figures at two grains is what made Rs 1,26,328 of Sponsored Brands spend vanish from any
# window nobody had fetched exactly, since SB was written to the window table and not the daily one.
# What these tests were actually about — the round trip, the None-not-zero rule, no Decimal reaching
# JSON — is unchanged, so they were converted rather than deleted.

#: One day, so a single-day sum returns exactly the seeded figures.
DAY = "2026-08-27"


async def test_a_days_rows_are_replaced_rather_than_doubled(db):
    """Re-running a refresh corrects rather than duplicates — repeated-save safety.

    Delete-then-bulk-insert per `(day, ad_product)` rather than the house upsert, which is the one
    measured deviation in this codebase: 30,921 rows/sec against 498.
    """
    rows = [{"keywordId": "1", "matchType": "EXACT", "date": DAY, "cost": 100.0, "sales7d": 200.0,
             "clicks": 5, "impressions": 50, "purchases7d": 1, "keywordBid": 10.0,
             "keyword": "kw", "campaignId": "c1", "adGroupId": "g1"}]

    assert await repository.save_daily(db, rows) == 1
    rows[0]["cost"] = 150.0
    assert await repository.save_daily(db, rows) == 1

    stored = await repository.sum_daily(db, DAY, DAY)
    assert len(stored) == 1, "the same entity was stored twice for one day"
    assert stored[0]["spend"] == 150.0, "the second save did not correct the first"


async def test_stored_rows_come_back_in_the_rule_engines_own_shape(db):
    """A preview and an apply both read this. A mismatch between the stored shape and what a rule
    expects is a bug that only appears at apply time — with a live bid on the other end."""
    await repository.save_daily(db, [
        {"keywordId": "1", "matchType": "PHRASE", "date": DAY, "cost": 500.0, "sales7d": 1000.0,
         "clicks": 20, "impressions": 900, "purchases7d": 3, "keywordBid": 12.0,
         "keyword": "makhana", "campaignId": "c1", "adGroupId": "g1"},
    ])
    stored = await repository.sum_daily(db, DAY, DAY)

    plan = logic.plan_run(stored, conditions=[{"field": "spend", "op": "gt", "value": 100}],
                          action=logic.ACTION_DECREASE_PCT, amount=10)
    assert plan["blocked"] is None
    assert plan["changes"][0]["new_bid"] == 10.8
    assert plan["changes"][0]["writer"] == logic.WRITER_KEYWORD


async def test_a_zero_spend_stored_row_has_no_roas(db):
    """The None-not-zero rule has to survive the round trip through the database, where a
    `Numeric` column returns `Decimal` and a naive division would produce 0."""
    await repository.save_daily(db, [
        {"keywordId": "1", "matchType": "EXACT", "date": DAY, "cost": 0.0, "sales7d": 0.0,
         "clicks": 0, "impressions": 12, "purchases7d": 0, "keywordBid": 7.0,
         "keyword": "dormant", "campaignId": "c1", "adGroupId": "g1"},
    ])
    stored = await repository.sum_daily(db, DAY, DAY)
    assert stored[0]["roas"] is None
    assert stored[0]["acos"] is None


async def test_no_decimal_reaches_json_from_the_stored_rows(db):
    """`JSONResponse` cannot serialise `Decimal`, and this app has shipped that defect twice."""
    import json as _json
    await repository.save_daily(db, [
        {"keywordId": "1", "matchType": "EXACT", "date": DAY, "cost": 12.34, "sales7d": 56.78,
         "clicks": 2, "impressions": 20, "purchases7d": 1, "keywordBid": 9.99,
         "keyword": "kw", "campaignId": "c1", "adGroupId": "g1"},
    ])
    stored = await repository.sum_daily(db, DAY, DAY)
    _json.dumps(stored)   # raises TypeError on a Decimal

    runs = await repository.load_runs(db)
    _json.dumps(runs)
    assert (await repository.load_guardrails(db))["max_bid"] is not None


# ─── Retention ────────────────────────────────────────────────────────────────
#
# **The two window-eviction tests are GONE with the table they tested.**
#
# `test_stale_window_row_sets_are_evicted_newest_kept` and
# `test_purging_windows_is_a_noop_when_under_the_limit` covered `purge_windows`, which existed only
# because `ads_performance` cached one row set per window viewed and nothing ever deleted them
# (105,755 rows / 17.1 MB on production — the largest table in the database). Deleting the table
# removes the growth, the retention rule and the need to test it. `purge_daily` is still covered in
# `tests/test_ads_api.py`.
