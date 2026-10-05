"""Amazon posts fees LATE, and once returned a month of rows with two fee types missing.

Measured on production, 6 Oct, by re-fetching 5 Sep - 4 Oct and comparing with what was stored:
every day was ₹15,000-29,000 short on fees. Two causes, each fixed here.

* Fees arrive over days: 4 Oct's row held ₹92 of fees when first fetched, ₹29,379 a day later, and
  the nightly job fetched each day ONCE. It now re-reads the last 30 days of economics nightly.
* On 4 Oct Amazon's answer lacked FBA fulfilment and fixed closing fees for the whole month, and the
  Sunday Projections job wrote it over good rows. A fresh answer can no longer REPLACE settled days
  with much lower fees, and Projections stores only days that were missing.
"""
from datetime import date, timedelta

import pytest

from app.portfolio import refresh, repository

pytestmark = pytest.mark.regression

END = "2026-10-04"


def _row(day, fee):
    return {"startDate": day, "endDate": day, "childAsin": "B0AAA00001", "parentAsin": "B0P",
            "sales": {"orderedProductSales": {"amount": 1000}, "unitsOrdered": 5, "netUnitsSold": 5},
            "fees": [{"feeTypeName": "WeightBasedFee",
                      "charges": [{"aggregatedDetail": {"totalAmount": {"amount": fee}}}]}],
            "netProceeds": {"total": {"amount": 1000 - fee}}}


def _days(n_back_from, count):
    end = date.fromisoformat(END)
    return [(end - timedelta(days=n_back_from + i)).isoformat() for i in range(count)]


def test_THE_4_OCT_ANSWER_settled_days_with_much_lower_fees_are_kept():
    settled = _days(refresh.SETTLED_AFTER_DAYS + 1, 10)
    stored = {d: 20000.0 for d in settled}
    new = [_row(d, 500.0) for d in settled]          # two fee types missing: about 2% of what is held
    assert refresh.incomplete_settled_days(new, stored, END) == sorted(settled)


def test_fees_that_GREW_are_accepted_which_is_the_normal_late_posting_case():
    settled = _days(refresh.SETTLED_AFTER_DAYS + 1, 10)
    stored = {d: 20000.0 for d in settled}
    assert refresh.incomplete_settled_days([_row(d, 26000.0) for d in settled], stored, END) == []


def test_RECENT_days_are_never_held_back_even_when_their_fees_drop():
    """Within the settling window fees are still moving in both directions; only settled days are
    judged."""
    recent = _days(0, refresh.SETTLED_AFTER_DAYS)
    stored = {d: 20000.0 for d in recent}
    assert refresh.incomplete_settled_days([_row(d, 10.0) for d in recent], stored, END) == []


def test_one_dipping_day_among_many_is_a_reimbursement_not_an_incomplete_answer():
    settled = _days(refresh.SETTLED_AFTER_DAYS + 1, 10)
    stored = {d: 20000.0 for d in settled}
    new = [_row(d, 20000.0) for d in settled[1:]] + [_row(settled[0], 5000.0)]
    assert refresh.incomplete_settled_days(new, stored, END) == []


def test_a_first_fetch_with_nothing_stored_is_always_accepted():
    settled = _days(refresh.SETTLED_AFTER_DAYS + 1, 10)
    assert refresh.incomplete_settled_days([_row(d, 1.0) for d in settled], {}, END) == []


def test_the_guard_reads_fees_exactly_as_the_screen_does():
    from app.portfolio import logic

    row = _row(END, 1234.5)
    assert refresh._fees_of(row) == logic.size_row(row, {})["fees_total"] == 1234.5


async def test_run_keeps_the_held_figures_of_settled_days_when_amazon_returns_less(monkeypatch, db):
    from app.portfolio import economics

    settled = _days(refresh.SETTLED_AFTER_DAYS + 1, 5)
    recent = _days(0, 2)
    await repository.save_economics_daily(db, [_row(d, 20000.0) for d in settled + recent])

    async def fake_fetch(**kw):
        rows = [_row(d, 300.0) for d in settled] + [_row(d, 25000.0) for d in recent]
        return rows, [], min(settled), END

    monkeypatch.setattr(economics, "fetch_economics", fake_fetch)

    def factory():
        class Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *a):
                return False
        return Ctx()

    refresh.reset_state()
    result = await refresh.run(factory, econ_start=min(settled), econ_end=END, skip_ads=True)
    fees = await repository.fee_totals_by_day(db, min(settled), END)
    assert all(fees[d] == 20000.0 for d in settled), "settled days were overwritten by the low answer"
    assert all(fees[d] == 25000.0 for d in recent), "recent days must still take the fresh figures"
    assert result.get("econ_warning") and "previous figures" in result["econ_warning"]
    refresh.reset_state()


async def test_projections_stores_ONLY_the_missing_days(monkeypatch, db):
    from app.portfolio import economics
    from app.projections import refresh as proj

    today = date(2026, 10, 5)
    start, end = economics.window_for(today, 30)
    held = [(date.fromisoformat(start) + timedelta(days=i)).isoformat() for i in range(29)]
    await repository.save_economics_daily(db, [_row(d, 20000.0) for d in held])
    last = end

    async def fake_fetch(**kw):
        return [_row(d, 1.0) for d in held + [last]], [], start, end

    monkeypatch.setattr(economics, "fetch_economics", fake_fetch)
    await proj._ensure_window(db, today=today, days=30, sleep=None)
    fees = await repository.fee_totals_by_day(db, start, end)
    assert all(fees[d] == 20000.0 for d in held), "Projections overwrote days the Portfolio owns"
    assert last in fees, "the genuinely missing day was not stored"


def test_the_settling_window_covers_how_late_fees_were_actually_found_to_move():
    """Measured: on 6 Oct, days up to 30 days old had fees ₹15,000-20,000 short, and refunds keep
    arriving for weeks. A shorter window leaves exactly those days wrong, so the bound is a
    requirement, not the constant compared with itself — that version let `ECON_SETTLE_DAYS = 1`
    pass. Capped by what one economics query may ask for."""
    from app.portfolio import economics

    assert refresh.ECON_SETTLE_DAYS >= 30
    assert refresh.ECON_SETTLE_DAYS <= economics.MAX_WINDOW_DAYS
    assert refresh.SETTLED_AFTER_DAYS < refresh.ECON_SETTLE_DAYS, (
        "if no day in the re-read window counts as settled, the incomplete-answer guard never runs"
    )
