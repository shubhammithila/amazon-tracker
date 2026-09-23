"""Portfolio economics and ads stored PER DAY, so every window is instant.

Asked for as *"fetching of sales and ad data should be more dynamic and updated everyday atleast
once all 7d, 30d, 60d, 90d"*. Keyed per WINDOW, a range nobody had fetched had no row to read:
``GET /portfolio`` returned empty and offered a ~12 minute fetch. Keyed per DAY, any sub-range
inside the coverage is a ``GROUP BY``.

**Both granularities were measured on the live account before any of this was written**, because
the code recorded reasons NOT to use them that nobody had run:

    economics aggregateBy DAY : accepted. A 7-day DAY sum equals the RANGE query to the rupee on
                                sales, ads, net AND units. 30 days = 8,010 rows in 25 s, the same
                                cost as the RANGE query it replaces.
    ads timeUnit DAILY        : accepted WITH the `date` column. 7 real days returned cost
                                3,47,570.00 and attributed sales 3,81,534.93 — IDENTICAL to
                                SUMMARY, ACOS 91.10% both, because Amazon attributes each sale
                                back to the CLICK's day.

That last finding is what makes this safe: the fear was that one-day granularity would make the
14-day attribution smear 30x worse than the known chunked-report caveat. It does not.

Every test here fails against the code before this change.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.models import AdsDaily, EconomicsDaily
from app.portfolio import repository

pytestmark = pytest.mark.regression


def _econ(day: str, asin: str, *, sales: float, units: int, net: float,
          fees: dict | None = None, ads: dict | None = None, msku: str | None = None) -> dict:
    """One Amazon-shaped economics row for ONE day."""
    return {
        "startDate": day, "endDate": day,
        "parentAsin": "B0PARENT01", "childAsin": asin, "msku": msku,
        "sales": {
            "orderedProductSales": {"amount": sales},
            "refundedProductSales": {"amount": 0.0},
            "unitsOrdered": units, "unitsRefunded": 0, "netUnitsSold": units,
        },
        "fees": [
            {"feeTypeName": name,
             "charges": [{"aggregatedDetail": {"totalAmount": {"amount": amount}}}]}
            for name, amount in (fees or {}).items()
        ],
        "ads": [
            {"adTypeName": name, "charge": {"totalAmount": {"amount": amount}}}
            for name, amount in (ads or {}).items()
        ],
        "netProceeds": {"total": {"amount": net}},
    }


def _ad(day: str, asin: str, sku: str, *, cost: float, attributed: float) -> dict:
    return {"day": day, "child_asin": asin, "seller_sku": sku,
            "cost": cost, "attributed_sales": attributed,
            "purchases": 1, "clicks": 3, "impressions": 100}


DAYS = ["2026-09-10", "2026-09-11", "2026-09-12"]


# ── Summing days IS the window ────────────────────────────────────────────────


async def test_the_days_sum_to_the_window_figure(db):
    """**The claim the whole change rests on.**

    Verified against Amazon itself before this was built: a 7-day DAY query and the equivalent
    RANGE query returned the same sales, ads, net and units to the rupee. This asserts the local
    half — that the summing here reproduces the arithmetic rather than dropping or double-counting
    a day.
    """
    rows = [
        _econ(DAYS[0], "B0AAA00001", sales=100.0, units=5, net=30.0,
              fees={"ReferralFee": 10.0}, ads={"SponsoredProductFee": 4.0}),
        _econ(DAYS[1], "B0AAA00001", sales=150.0, units=7, net=45.0,
              fees={"ReferralFee": 15.0}, ads={"SponsoredProductFee": 6.0}),
        _econ(DAYS[2], "B0AAA00001", sales=200.0, units=9, net=60.0,
              fees={"ReferralFee": 20.0}, ads={"SponsoredProductFee": 8.0}),
    ]
    assert await repository.save_economics_daily(db, rows) == 3

    out = await repository.load_snapshot(db, (DAYS[0], DAYS[-1]))
    assert len(out) == 1, "the three days did not collapse to one row for the product"
    row = out[0]
    assert row["sales"]["orderedProductSales"]["amount"] == 450.0
    assert row["sales"]["unitsOrdered"] == 21
    assert row["netProceeds"]["total"]["amount"] == 135.0
    fees = {f["feeTypeName"]: f["charges"][0]["aggregatedDetail"]["totalAmount"]["amount"]
            for f in row["fees"]}
    assert fees == {"ReferralFee": 45.0}
    ads = {a["adTypeName"]: a["charge"]["totalAmount"]["amount"] for a in row["ads"]}
    assert ads == {"SponsoredProductFee": 18.0}


async def test_ANY_sub_range_is_answerable_without_a_fetch(db):
    """**The request, in one assertion.** A range nobody asked for in advance still answers.

    This is what the per-window cache structurally could not do: 10th-to-11th had never been
    "fetched" as a window, so it had no row and the screen offered a ~12 minute wait.
    """
    for index, day in enumerate(DAYS):
        await repository.save_economics_daily(
            db, [_econ(day, "B0AAA00001", sales=100.0 * (index + 1), units=1, net=10.0)]
        )

    whole = await repository.load_snapshot(db, (DAYS[0], DAYS[2]))
    first_two = await repository.load_snapshot(db, (DAYS[0], DAYS[1]))
    middle = await repository.load_snapshot(db, (DAYS[1], DAYS[1]))

    assert whole[0]["sales"]["orderedProductSales"]["amount"] == 600.0
    assert first_two[0]["sales"]["orderedProductSales"]["amount"] == 300.0
    assert middle[0]["sales"]["orderedProductSales"]["amount"] == 200.0


async def test_fees_merge_by_NAME_across_days_not_by_position(db):
    """Amazon returned 8 distinct fee types on this account and adds more.

    Two days can carry different sets — a removal fee appears only when something is removed — so a
    positional merge would add a removal fee to a referral fee and produce a plausible wrong number.
    """
    await repository.save_economics_daily(db, [
        _econ(DAYS[0], "B0AAA00001", sales=10.0, units=1, net=1.0,
              fees={"ReferralFee": 5.0}),
        _econ(DAYS[1], "B0AAA00001", sales=10.0, units=1, net=1.0,
              fees={"ReferralFee": 5.0, "RemovalFee": 2.0}),
    ])
    out = await repository.load_snapshot(db, (DAYS[0], DAYS[1]))
    fees = {f["feeTypeName"]: f["charges"][0]["aggregatedDetail"]["totalAmount"]["amount"]
            for f in out[0]["fees"]}
    assert fees == {"ReferralFee": 10.0, "RemovalFee": 2.0}


async def test_the_MSKU_rows_never_reach_a_total(db):
    """The `seller_sku` filter, ported to the per-day store.

    A product's merchant and FBA rows sum to its ASIN row, so including them would roughly DOUBLE
    every figure on the dashboard. Amazon's MSKU grain also loses a little to rows it cannot
    attribute to one SKU, which is why the ASIN rows stay authoritative.
    """
    await repository.save_economics_daily(
        db, [_econ(DAYS[0], "B0AAA00001", sales=100.0, units=4, net=20.0)]
    )
    before = (await repository.load_snapshot(db, (DAYS[0], DAYS[0])))[0]

    await repository.save_sku_snapshot(db, [
        _econ(DAYS[0], "B0AAA00001", sales=40.0, units=2, net=8.0, msku="aaa merchant"),
        _econ(DAYS[0], "B0AAA00001", sales=60.0, units=2, net=12.0, msku="aaa merchant FBA"),
    ])
    after = (await repository.load_snapshot(db, (DAYS[0], DAYS[0])))[0]

    assert after["sales"]["orderedProductSales"]["amount"] == \
        before["sales"]["orderedProductSales"]["amount"] == 100.0, (
        "the per-SKU rows leaked into the ASIN totals, so every dashboard figure would be inflated"
    )
    # ...and they ARE retrievable by their own loader, for the channel split.
    skus = await repository.load_sku_snapshot(db, (DAYS[0], DAYS[0]))
    assert {r["msku"] for r in skus} == {"aaa merchant", "aaa merchant FBA"}


async def test_storing_the_MSKU_grain_does_not_delete_the_ASIN_grain(db):
    """**The delete is scoped by (day, GRAIN), and the grain half is load-bearing.**

    Both grains share this table, so a delete scoped by day alone would make the MSKU write destroy
    the ASIN totals stored moments earlier — exactly the bug `ads.repository.save_daily`'s
    `(day, ad_product)` scope exists to prevent, where deleting by day alone would have wiped 72%
    of the spend.
    """
    await repository.save_economics_daily(
        db, [_econ(DAYS[0], "B0AAA00001", sales=100.0, units=4, net=20.0)]
    )
    await repository.save_sku_snapshot(
        db, [_econ(DAYS[0], "B0AAA00001", sales=40.0, units=2, net=8.0, msku="aaa merchant")]
    )
    assert await repository.load_snapshot(db, (DAYS[0], DAYS[0])), (
        "the ASIN-level rows were destroyed by the per-SKU write"
    )
    # And the reverse: re-saving the ASIN grain must not destroy the split.
    await repository.save_economics_daily(
        db, [_econ(DAYS[0], "B0AAA00001", sales=110.0, units=5, net=22.0)]
    )
    assert await repository.load_sku_snapshot(db, (DAYS[0], DAYS[0])), (
        "the per-SKU rows were destroyed by a re-save of the ASIN rows"
    )


async def test_refetching_one_day_leaves_the_other_days_alone(db):
    """The delete is scoped per DAY, so the nightly one-day run cannot disturb the other 89."""
    for day in DAYS:
        await repository.save_economics_daily(
            db, [_econ(day, "B0AAA00001", sales=100.0, units=1, net=10.0)]
        )
    # Yesterday is refetched with a corrected figure.
    await repository.save_economics_daily(
        db, [_econ(DAYS[2], "B0AAA00001", sales=999.0, units=1, net=10.0)]
    )
    out = await repository.load_snapshot(db, (DAYS[0], DAYS[2]))
    assert out[0]["sales"]["orderedProductSales"]["amount"] == 1199.0, (
        "refetching one day changed the others"
    )
    assert await repository.coverage(db) == {"first": DAYS[0], "last": DAYS[2], "days": 3}


async def test_the_same_day_twice_is_an_UPDATE_not_a_second_row(db):
    """`seller_sku` is `""` and NOT NULL, which is what makes the unique index bite.

    SQLite treats NULLs as DISTINCT in a unique index, so a nullable column would allow the same
    (day, asin) twice and silently double that day — which is what `economics_snapshot`'s index
    actually permitted.
    """
    from sqlalchemy import func, select

    for _ in range(3):
        await repository.save_economics_daily(
            db, [_econ(DAYS[0], "B0AAA00001", sales=100.0, units=1, net=10.0)]
        )
    count = (await db.execute(select(func.count()).select_from(EconomicsDaily))).scalar()
    assert count == 1, f"{count} rows for one (day, asin) — the day was doubled"


# ── A gap must refuse, not sum short ──────────────────────────────────────────


async def test_an_INTERIOR_gap_makes_the_range_incomplete(db):
    """**A span cannot see a hole, which is exactly how the Ads tab's version came to lie.**

    The endpoints are both held here, so any check based on min/max would call this complete and
    `load_snapshot` would quietly understate sales — and an understated total looks entirely
    plausible on a dashboard that decides which products to kill.
    """
    await repository.save_economics_daily(
        db, [_econ("2026-09-10", "B0AAA00001", sales=10.0, units=1, net=1.0)]
    )
    await repository.save_economics_daily(
        db, [_econ("2026-09-14", "B0AAA00001", sales=10.0, units=1, net=1.0)]
    )
    done = await repository.range_completeness(db, "2026-09-10", "2026-09-14")
    assert done["complete"] is False, "an interior gap was reported as summable"
    assert done["missing_count"] == 3
    assert done["missing"] == ["2026-09-11", "2026-09-12", "2026-09-13"]
    # The SPAN covers it, which is why the span gates nothing.
    assert await repository.coverage(db) == {
        "first": "2026-09-10", "last": "2026-09-14", "days": 2,
    }


async def test_an_INCOMPLETE_range_renders_NOTHING_rather_than_a_short_sum(auth_client, db):
    """**Found by driving the real screen, and it is the whole point of storing days.**

    A 90-day range over 40 stored days rendered 2 products and a total labelled "90 days" — a figure
    50 days short, on a dashboard whose purpose is deciding which products to stop selling. The
    banner correctly named the missing days while the grid showed a plausible wrong number right
    beside it, which is worse than either alone.

    Nothing in the mutation harness caught this, because every mutation there attacked the
    completeness CALCULATION and this was a missing consumer of its answer. The same rule
    `POST /ads/preview` follows: a partial window is refused, not summed.
    """
    await repository.save_economics_daily(
        db, [_econ("2026-09-10", "B0AAA00001", sales=1000.0, units=10, net=200.0)]
    )
    await repository.save_economics_daily(
        db, [_econ("2026-09-12", "B0AAA00001", sales=1000.0, units=10, net=200.0)]
    )

    body = (await auth_client.get("/portfolio?start=2026-09-10&end=2026-09-12")).json()
    assert body["completeness"]["complete"] is False
    assert body["parents"] == [], (
        "an incomplete range rendered rows, so the screen would show a total 1 day short and call "
        "it a 3-day figure"
    )
    assert body["totals"]["sales"] == 0
    # ...but the REASON still travels, so the screen can name the gap and offer the fetch.
    assert body["completeness"]["missing"] == ["2026-09-11"]
    assert body["coverage"]["days"] == 2


async def test_a_COMPLETE_range_does_render(auth_client, db):
    """The other half of the gate — it must not refuse everything.

    Without this, the assertion above is satisfied by a route that never returns rows at all.
    """
    for day in DAYS:
        await repository.save_economics_daily(
            db, [_econ(day, "B0AAA00001", sales=1000.0, units=10, net=200.0)]
        )
    body = (await auth_client.get(f"/portfolio?start={DAYS[0]}&end={DAYS[-1]}")).json()
    assert body["completeness"]["complete"] is True
    assert body["parents"], "a complete range rendered nothing"
    assert body["totals"]["sales"] == 3000.0


async def test_the_missing_list_is_capped_but_the_count_is_EXACT(db):
    """"Missing 25 days" and "missing 2 days" call for different actions, so the count cannot be
    capped — but 83 dates is a column rather than a sentence, so the list is."""
    await repository.save_economics_daily(
        db, [_econ("2026-09-30", "B0AAA00001", sales=10.0, units=1, net=1.0)]
    )
    done = await repository.range_completeness(db, "2026-09-01", "2026-09-30")
    assert done["missing_count"] == 29
    assert len(done["missing"]) == repository.MISSING_DAYS_SHOWN


async def test_a_day_holding_ONLY_sku_rows_does_not_count_as_held(db):
    """It carries no total, so summing it would silently drop every product Amazon could not
    attribute to a single SKU."""
    await repository.save_sku_snapshot(
        db, [_econ(DAYS[0], "B0AAA00001", sales=40.0, units=2, net=8.0, msku="aaa merchant")]
    )
    done = await repository.range_completeness(db, DAYS[0], DAYS[0])
    assert done["complete"] is False, (
        "a day with only per-SKU rows was treated as holding the day's figures"
    )


async def test_an_empty_store_is_incomplete_rather_than_complete(db):
    """Vacuous truth is the wrong answer here: "no days wanted are missing" must not read as
    "this range is summable" when nothing is stored at all."""
    done = await repository.range_completeness(db, DAYS[0], DAYS[-1])
    assert done["complete"] is False
    assert done["missing_count"] == 3
    assert done["held"] is None


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-09-12", "2026-09-10"),   # end BEFORE start
        ("not-a-date", "2026-09-10"),   # unparseable
        ("", ""),                       # blank
    ],
)
async def test_a_range_that_names_NO_days_is_not_complete(db, start, end):
    """**Vacuous truth, the other way round.**

    `expected_days` returns `[]` for a reversed or unparseable range, so "no wanted day is missing"
    is trivially true — and without the `bool(wanted)` guard the range would report itself summable
    and `load_snapshot` would return an empty list that renders as real zeros. A mutation removing
    that guard survived the first version of this file, because every case here tested a range that
    named real days.
    """
    await repository.save_economics_daily(
        db, [_econ(DAYS[0], "B0AAA00001", sales=10.0, units=1, net=1.0)]
    )
    done = await repository.range_completeness(db, start, end)
    assert done["complete"] is False, (
        f"{start}..{end} names no days at all, so it cannot be summable"
    )


# ── Ads, per day ──────────────────────────────────────────────────────────────


async def test_ad_days_sum_and_roll_up_to_the_asin(db):
    """Both grains ACCUMULATE, where the per-window version could assign.

    Several days carry the same (asin, sku), so an assignment would keep only the last day's spend —
    and it would look entirely plausible, because a single day's figures are a valid-looking number.
    """
    await repository.save_ads_daily(db, [
        _ad(DAYS[0], "B0AAA00001", "aaa FBA", cost=10.0, attributed=25.0),
        _ad(DAYS[1], "B0AAA00001", "aaa FBA", cost=10.0, attributed=25.0),
        _ad(DAYS[0], "B0AAA00001", "aaa", cost=5.0, attributed=0.0),
    ])
    # load_ads_snapshot falls back to latest_window, which reads the ECONOMICS days.
    await repository.save_economics_daily(
        db, [_econ(DAYS[0], "B0AAA00001", sales=1.0, units=1, net=1.0)]
    )

    by_asin, by_sku = await repository.load_ads_snapshot(db, (DAYS[0], DAYS[1]))
    assert by_asin["B0AAA00001"]["cost"] == 25.0, "the days were not summed"
    assert by_asin["B0AAA00001"]["attributed_sales"] == 50.0
    assert by_sku[("B0AAA00001", "aaa FBA")]["cost"] == 20.0
    assert by_sku[("B0AAA00001", "aaa")]["cost"] == 5.0


async def test_an_ECONOMICS_row_with_no_day_is_SKIPPED_not_defaulted(db):
    """A dateless row cannot be filed under a day, and a default would put one day's sales in
    another.

    The sibling of the ads case below. A mutation defaulting the day to `1970-01-01` survived the
    first version of this file, because every fixture here carries a date — so the skip was never
    exercised at all.
    """
    from sqlalchemy import func, select

    dateless = _econ(DAYS[0], "B0AAA00001", sales=100.0, units=1, net=10.0)
    dateless.pop("startDate")

    stored = await repository.save_economics_daily(db, [dateless])
    assert stored == 0, "a dateless row was stored under a guessed day"
    count = (await db.execute(select(func.count()).select_from(EconomicsDaily))).scalar()
    assert count == 0
    # And a defaulted row would show up as a bogus held day, making a range look answerable.
    assert await repository.days_held(db) == set()


async def test_an_ad_row_with_no_day_is_SKIPPED_not_defaulted(db):
    """Amazon accepts `timeUnit: DAILY` WITHOUT the `date` column (measured), and those rows carry
    no date at all. Filing one under a guessed day would put one day's spend into another."""
    from sqlalchemy import func, select

    stored = await repository.save_ads_daily(db, [
        {"child_asin": "B0AAA00001", "seller_sku": "s", "cost": 10.0,
         "attributed_sales": 0.0, "purchases": 0, "clicks": 0, "impressions": 0},
    ])
    assert stored == 0
    count = (await db.execute(select(func.count()).select_from(AdsDaily))).scalar()
    assert count == 0


async def test_refetching_one_ad_day_leaves_the_others(db):
    await repository.save_ads_daily(db, [
        _ad(DAYS[0], "B0AAA00001", "s", cost=10.0, attributed=0.0),
        _ad(DAYS[1], "B0AAA00001", "s", cost=10.0, attributed=0.0),
    ])
    await repository.save_ads_daily(db, [
        _ad(DAYS[1], "B0AAA00001", "s", cost=99.0, attributed=0.0),
    ])
    await repository.save_economics_daily(
        db, [_econ(DAYS[0], "B0AAA00001", sales=1.0, units=1, net=1.0)]
    )
    by_asin, _ = await repository.load_ads_snapshot(db, (DAYS[0], DAYS[1]))
    assert by_asin["B0AAA00001"]["cost"] == 109.0


# ── Retention ─────────────────────────────────────────────────────────────────


async def test_purge_keeps_the_retention_window_and_drops_older_days(db):
    """**Not optional housekeeping.** The per-window table this replaces had NO retention and grew
    by one window every night, reaching 32 windows / 21,698 rows — and every deploy copies the whole
    database, so an unbounded table eventually breaks the deploy as well as the writes.
    """
    today = date(2026, 9, 30)
    inside = (today - timedelta(days=5)).isoformat()
    outside = (today - timedelta(days=repository.DAILY_RETENTION_DAYS + 5)).isoformat()

    await repository.save_economics_daily(db, [
        _econ(inside, "B0AAA00001", sales=10.0, units=1, net=1.0),
        _econ(outside, "B0AAA00001", sales=10.0, units=1, net=1.0),
    ])
    await repository.save_ads_daily(db, [
        _ad(inside, "B0AAA00001", "s", cost=1.0, attributed=0.0),
        _ad(outside, "B0AAA00001", "s", cost=1.0, attributed=0.0),
    ])

    removed = await repository.purge_daily(db, today=today)
    assert removed == 2, f"{removed} rows purged; expected the two outside the window"
    assert await repository.days_held(db) == {inside}


async def test_the_retention_window_is_at_least_the_widest_range_offered(db):
    """90 days is the widest window the tab offers, so keeping fewer would make a range the picker
    presents unanswerable by construction."""
    from app.portfolio import economics

    assert repository.DAILY_RETENTION_DAYS >= economics.MAX_WINDOW_DAYS


async def test_the_purge_runs_even_when_the_REFRESH_CRASHES(monkeypatch, db):
    """**In the `finally`, not on the success path.**

    A sweep that only happens when a fetch succeeds is a side effect rather than a policy: a week of
    failed ad reports would leave the fastest-growing tables unpruned, which is how the per-window
    table reached 32 windows with no retention at all. The Ads tab duplicates its own nightly purge
    for the same reason.

    Asserted through a CRASHING refresh, because that is the path a success-only purge would miss —
    and a mutation disabling the purge survived the first version of this file, which only tested
    `purge_daily` directly.
    """
    from app.portfolio import refresh

    called = []

    async def _fake_purge(db_, **kwargs):
        called.append(True)
        return 0

    async def _boom(**kwargs):
        raise RuntimeError("Amazon fell over mid-fetch")

    monkeypatch.setattr(repository, "purge_daily", _fake_purge)
    monkeypatch.setattr(refresh.economics, "fetch_economics", _boom)
    refresh.reset_state()

    def _factory():
        class _Ctx:
            async def __aenter__(self_inner):
                return db

            async def __aexit__(self_inner, *a):
                return False
        return _Ctx()

    result = await refresh.run(_factory)
    assert result["error"], "the crash was swallowed, so this test proves nothing"
    assert called, (
        "the retention purge did not run after a failed refresh — a week of failures would leave "
        "the tables unpruned"
    )


# ── The incremental nightly path ──────────────────────────────────────────────


async def test_the_nightly_run_fetches_only_the_MISSING_days(monkeypatch, db):
    """~15 minutes a night instead of ~45.

    A rolling 90-day refetch would mean three ads reports every night on a box where the Ads tab's
    own job already runs for an hour. Per-day storage means a night only has to add yesterday.
    """
    from app.portfolio import refresh

    today = date(2026, 9, 30)
    # Everything up to the 28th is held; the 29th (yesterday) is not.
    for offset in range(2, 9):
        day = (today - timedelta(days=offset)).isoformat()
        await repository.save_economics_daily(
            db, [_econ(day, "B0AAA00001", sales=10.0, units=1, net=1.0)]
        )

    asked = {}

    async def _fake_run(db_factory=None, *, start=None, end=None, sleep=None, **kwargs):
        asked["start"], asked["end"] = start, end
        return {"rows": 1, "error": None}

    monkeypatch.setattr(refresh, "run", _fake_run)

    def _factory():
        class _Ctx:
            async def __aenter__(self_inner):
                return db

            async def __aexit__(self_inner, *a):
                return False
        return _Ctx()

    await refresh.run_incremental(_factory, today=today)
    assert asked["start"] == asked["end"] == "2026-09-29", (
        f"the nightly run asked for {asked} rather than just yesterday"
    )


async def test_the_nightly_run_is_a_NO_OP_when_the_day_is_already_held(monkeypatch, db):
    """A second run the same night must spend nothing, so the job is safe to retry."""
    from app.portfolio import refresh

    today = date(2026, 9, 30)
    for offset in range(1, 9):
        day = (today - timedelta(days=offset)).isoformat()
        await repository.save_economics_daily(
            db, [_econ(day, "B0AAA00001", sales=10.0, units=1, net=1.0)]
        )

    called = []

    async def _fake_run(*a, **k):
        called.append(True)
        return {}

    monkeypatch.setattr(refresh, "run", _fake_run)

    def _factory():
        class _Ctx:
            async def __aenter__(self_inner):
                return db

            async def __aexit__(self_inner, *a):
                return False
        return _Ctx()

    result = await refresh.run_incremental(_factory, today=today)
    assert result.get("skipped") is True
    assert not called, "a fetch was started for days that are already held"


async def test_a_long_gap_is_BOUNDED_rather_than_fetched_all_at_once(monkeypatch, db):
    """The app being off for a fortnight must not hold the nightly job open for hours.

    Later runs pick up the rest, and `range_completeness` refuses any range still touching a gap —
    so a partial catch-up is visible rather than quietly summing short.
    """
    from app.portfolio import refresh

    today = date(2026, 9, 30)
    asked = {}

    async def _fake_run(db_factory=None, *, start=None, end=None, sleep=None, **kwargs):
        asked["start"], asked["end"] = start, end
        return {}

    monkeypatch.setattr(refresh, "run", _fake_run)

    def _factory():
        class _Ctx:
            async def __aenter__(self_inner):
                return db

            async def __aexit__(self_inner, *a):
                return False
        return _Ctx()

    await refresh.run_incremental(_factory, today=today)
    span = (date.fromisoformat(asked["end"]) - date.fromisoformat(asked["start"])).days + 1
    assert span <= refresh.MAX_BACKFILL_DAYS, (
        f"an empty store made the catch-up ask for {span} days in one run"
    )
    # And never beyond yesterday: today's figures are still settling.
    assert asked["end"] == "2026-09-29"


async def test_the_backfill_cap_stays_inside_amazons_report_limit():
    """A wider span would silently become several ads reports and multiply a catch-up's cost."""
    from app.portfolio import ads, refresh

    assert refresh.MAX_BACKFILL_DAYS <= ads.MAX_REPORT_DAYS


async def test_the_nightly_portfolio_job_is_incremental(monkeypatch):
    """**The scheduler must call `run_incremental`, not `run`.**

    Source-asserted via a patched call rather than by reading the file, because the difference is
    invisible in any output: `run()` would work perfectly, refetch a whole 30-day window every
    night, and cost ~45 minutes of Amazon reports instead of ~15 — on a box where the Ads tab's own
    job already runs for an hour.
    """
    from app import scheduler
    from app.config import Settings, get_settings
    from app.portfolio import refresh

    class _Configured(Settings):
        sp_api_client_id: str = "amzn1.application-oa2-client.x"
        sp_api_client_secret: str = "secret"
        sp_api_refresh_token: str = "Atzr|x"

    get_settings.cache_clear()
    monkeypatch.setattr(scheduler, "get_settings", lambda: _Configured())

    called = []

    async def _fake_incremental(*a, **k):
        called.append("incremental")
        return {"skipped": True}

    async def _fake_run(*a, **k):
        called.append("full")
        return {}

    monkeypatch.setattr(refresh, "run_incremental", _fake_incremental)
    monkeypatch.setattr(refresh, "run", _fake_run)

    await scheduler.scheduled_portfolio_refresh()
    assert called == ["incremental"], (
        f"the nightly job called {called} — a full run refetches days it already holds"
    )


# ── The fetchers ask Amazon for days ──────────────────────────────────────────


def test_the_economics_query_asks_for_DAY():
    """`aggregateBy: { date: DAY }`, which the old docstring recorded a reason not to use that
    nobody had run. Measured: accepted, and a 7-day sum matches RANGE to the rupee."""
    from app.portfolio import economics

    q = economics.build_query("2026-09-10", "2026-09-11", "A21TJRUUN4KGV", by_day=True)
    assert "date: DAY" in q
    # And the flag really is a flag — RANGE is still reachable.
    assert "date: RANGE" in economics.build_query("2026-09-10", "2026-09-11", "A21TJRUUN4KGV")


def test_the_ads_report_asks_for_DAILY_and_the_date_column_TOGETHER():
    """**Both, or the rows carry no date and every one is skipped.**

    Measured: Amazon accepts `timeUnit: DAILY` without the `date` column, and `save_ads_daily`
    skips a dateless row by design — so the refresh would report success and store nothing.
    """
    from app.portfolio import ads

    body = ads.build_report_request("2026-09-10", "2026-09-11", daily=True)
    assert body["configuration"]["timeUnit"] == "DAILY"
    assert "date" in body["configuration"]["columns"], (
        "DAILY without the date column stores nothing at all"
    )
    # SUMMARY must NOT carry it: `date` is not a legal column under that time unit (a real 400).
    summary = ads.build_report_request("2026-09-10", "2026-09-11")
    assert summary["configuration"]["timeUnit"] == "SUMMARY"
    assert "date" not in summary["configuration"]["columns"]


def test_the_ads_aggregation_keys_on_the_DAY_under_daily():
    """Dropping the day here would collapse a 30-day report into window figures and silently defeat
    the whole store — the rows would still look correct and every sub-range would be wrong."""
    from app.portfolio import ads

    raw = [
        {"date": "2026-09-10", "advertisedAsin": "B0A", "advertisedSku": "s", "cost": "5",
         "attributedSalesSameSku14d": "20", "clicks": "2", "impressions": "10",
         "purchasesSameSku14d": "1"},
        {"date": "2026-09-10", "advertisedAsin": "B0A", "advertisedSku": "s", "cost": "3",
         "attributedSalesSameSku14d": "0", "clicks": "1", "impressions": "5",
         "purchasesSameSku14d": "0"},
        {"date": "2026-09-11", "advertisedAsin": "B0A", "advertisedSku": "s", "cost": "7",
         "attributedSalesSameSku14d": "10", "clicks": "3", "impressions": "20",
         "purchasesSameSku14d": "1"},
    ]
    out = ads.aggregate(raw, daily=True)
    assert len(out) == 2, "the days were merged, so every sub-range would be wrong"
    by_day = {r["day"]: r for r in out}
    # The campaign split still merges WITHIN a day.
    assert by_day["2026-09-10"]["cost"] == 8.0
    assert by_day["2026-09-11"]["cost"] == 7.0

    # And the SUMMARY path is unchanged: one row, no day.
    flat = ads.aggregate(raw)
    assert len(flat) == 1 and "day" not in flat[0]
    assert flat[0]["cost"] == 15.0


def test_the_ads_aggregation_DROPS_a_dateless_row_under_daily():
    """A dateless row must not reach the store with an empty day.

    `save_ads_daily` would skip it anyway, so this is belt-and-braces — but a row that survives
    aggregation with `day: ""` is a row that LOOKS storable, and the next reader of this code would
    reasonably assume the aggregation had already filtered. A mutation removing the guard survived
    the first version of this file because every fixture row carried a date.
    """
    from app.portfolio import ads

    out = ads.aggregate([
        {"advertisedAsin": "B0A", "advertisedSku": "s", "cost": "5"},               # no date
        {"date": "2026-09-10", "advertisedAsin": "B0A", "advertisedSku": "s", "cost": "7"},
    ], daily=True)
    assert len(out) == 1, "the dateless row survived aggregation"
    assert out[0]["day"] == "2026-09-10"
    assert all(r["day"] for r in out), "a row reached the caller with an empty day"


# ── The old per-window surface is really gone ────────────────────────────────


def test_the_per_window_tables_and_functions_are_GONE():
    """**Deleted rather than kept alongside, and that is the decision.**

    Two caches of one figure with a read side choosing between them is precisely the code that lost
    Rs 1,26,328 of Sponsored Brands spend on the Ads tab. And `economics_snapshot` had no retention
    at all — CLAUDE.md's own conclusion after deleting `ads_performance` was "the most reliable way
    to bound a table is not to have it".
    """
    from app import models

    assert not hasattr(models, "EconomicsSnapshot")
    assert not hasattr(models, "AdsSnapshot")
    assert not hasattr(repository, "windows_available")
    assert not hasattr(repository, "save_snapshot")
    assert not hasattr(repository, "save_ads_snapshot")


def test_the_migration_drops_both_per_window_tables():
    """Asserted at source, because a deploy whose migration drops a table while the OLD
    required-tables check is still in memory rolls the CODE back and leaves the SCHEMA forward —
    which is how a dropped table cost a rollback once already.
    """
    from pathlib import Path

    migration = Path("alembic/versions/e7b3f0c92a41_portfolio_daily.py").read_text(
        encoding="utf-8"
    )
    assert 'op.drop_table("economics_snapshot")' in migration
    assert 'op.drop_table("ads_snapshot")' in migration

    deploy = Path("deploy/update-ec2.sh").read_text(encoding="utf-8")
    assert '"economics_daily", "ads_daily"' in deploy, (
        "the new tables are not in the required-tables check"
    )
    assert "economics_snapshot" in deploy and "ads_snapshot" in deploy, (
        "the deploy does not verify the dropped tables are actually gone, so a skipped migration "
        "would leave two grains live"
    )
    assert '"economics_daily" in tables' in deploy, (
        "the baseline detector has no branch for this revision, so it would stamp production "
        "backwards"
    )
