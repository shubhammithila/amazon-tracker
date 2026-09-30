"""Sponsored Brands spend, folded into each product's ad figure.

Asked as *"the ad expenses which you are taking includes all SP, SB, SD campaigns right?"* — and the
answer was **no**. Amazon's economics feed returns exactly one ad type name, `SponsoredProductFee`,
measured across all 9,074 stored rows that carry ad spend, so **28% of real ad spend was invisible**:
Rs 11,31,487 on screen against Rs 19,36,375 actually spent, over the reported 30-day window. TACOS was
understated and net margin overstated on every row and in all four KPI tiles.

Then: *"Add SB into the Portfolio figures — but no separate labelling of it in the portfolio tab.
just add to the main ad figures of each product."*

── Why attribution is needed at all ──

Amazon publishes no ASIN-level SB COST report. `sbAdvertisedProduct` does not exist; of the seven SB
report types exactly one carries an ASIN (`sbPurchasedProduct.purchasedAsin`), that is the ASIN
*bought* rather than the one *advertised*, and it has no `cost` column. Structurally an SB ad holds
an `asins` ARRAY plus a Brand-Store landing page, so one click incurs one cost with no single
advertised product to bill.

**But `/sb/v4/ads/list` declares `creative.asins` per ad group**, and SB spend is already stored per
ad group — so 97.9% of it attributes by a join against Amazon's own statement rather than a guess.
The rest is brand-level and is spread by sales share.

── The arithmetic that everything else depends on ──

`netProceeds` **already has the SP ad spend deducted**. Verified on real rows to the rupee:

    sales 3784  fees 1241  ads  805 | net 1604 | sales-fees-ads 1604  (delta -0)
    sales 6316  fees 2056  ads 2262 | net 1798 | sales-fees-ads 1798  (delta -0)

So adding SB to `ad_spend` without subtracting it from `net_proceeds` would fix TACOS and leave
margin exactly as overstated as before — and the two figures would then contradict each other on
screen, which is worse than the honest-but-incomplete state it replaced.
"""
import re
from pathlib import Path

import pytest

from app.portfolio import logic

pytestmark = pytest.mark.regression

TEMPLATE = Path(__file__).parent.parent / "templates" / "portfolio.html"

DAY = "2026-09-01"
NEXT = "2026-09-02"

#: The REAL ad group that settled the equal-vs-weighted decision: Rs 18,342 over 3 ASINs, one of
#: which sold nothing. Deliberately UNEQUAL sales, so equal-split and weighted-split give different
#: answers — a fixture with equal sales would pass under either basis and prove nothing, which is
#: the "one-campaign fixture" lesson this codebase already records.
REAL_GROUP = "406048686897409"
REAL_SPEND = 18342.0
REAL_SALES = {
    (DAY, "B0D11KTZ1X"): 64379.0,
    (DAY, "B0DF5PHH3K"): 22835.0,
    (DAY, "B0DXQBYB35"): 0.0,          # sold nothing that day
}


def test_a_size_that_sold_NOTHING_is_charged_no_SB_spend():
    """The decision, on the real numbers that prompted it.

    Equal split would bill the dead ASIN Rs 6,114 for advertising it never converted. Sales-weighted
    bills it Rs 0 and gives the money to the products that actually sold.
    """
    allocated, unattributable = logic.allocate_sb_spend(
        {(DAY, REAL_GROUP): REAL_SPEND},
        {REAL_GROUP: ["B0D11KTZ1X", "B0DF5PHH3K", "B0DXQBYB35"]},
        REAL_SALES,
    )
    assert allocated.get((DAY, "B0DXQBYB35"), 0.0) == 0.0, (
        "a product that sold nothing was charged for Sponsored Brands advertising"
    )
    # The weighted figures from the plan, to the rupee.
    assert round(allocated[(DAY, "B0D11KTZ1X")]) == 13540
    assert round(allocated[(DAY, "B0DF5PHH3K")]) == 4802
    # ...and NOT the equal split, so this cannot pass under the rejected basis.
    assert round(allocated[(DAY, "B0D11KTZ1X")]) != round(REAL_SPEND / 3)
    assert unattributable == 0.0


def test_the_allocation_conserves_every_rupee():
    """`sum(allocated) + unattributable == sum(spend)`, to the paisa.

    **The single most important property here.** An allocator that quietly loses or invents money
    produces a number that looks entirely plausible on screen — the same class of defect as a
    superset window reporting less than its subset, which cost Rs 1,26,328 of SB spend once already.
    So it is asserted directly rather than spot-checked, over all three shapes at once: a 7-ASIN
    group, a 1-ASIN group, and one naming no ASIN at all.
    """
    sales = {
        (DAY, "B0AAA"): 1000.0, (DAY, "B0BBB"): 3333.0, (DAY, "B0CCC"): 7.0,
        (DAY, "B0DDD"): 999.99, (DAY, "B0EEE"): 1.0, (DAY, "B0FFF"): 450.5,
        (DAY, "B0GGG"): 88.0, (DAY, "B0SOLO"): 12345.0,
    }
    spend = {
        (DAY, "seven"): 54210.37,     # 7 ASINs, awkward remainder
        (DAY, "solo"): 263958.0,      # 1 ASIN, no division at all
        (DAY, "brandstore"): 11114.0,  # names no ASIN
    }
    allocated, unattributable = logic.allocate_sb_spend(
        spend,
        {
            "seven": ["B0AAA", "B0BBB", "B0CCC", "B0DDD", "B0EEE", "B0FFF", "B0GGG"],
            "solo": ["B0SOLO"],
            "brandstore": [],
        },
        sales,
    )
    assert round(sum(allocated.values()) + unattributable, 2) == round(sum(spend.values()), 2), (
        "the allocation does not conserve the spend it was given"
    )
    assert unattributable == 11114.0, "the brand-store ad group should be wholly unattributable"


def test_an_ad_group_naming_no_ASIN_goes_to_the_SPREAD_bucket_not_to_one_product():
    """A Brand Store ad advertises the brand, so there is no product to charge it to.

    Measured at Rs 11,114 a month — 2.1% of SB spend. Charging it to one arbitrary product would
    make that product look far worse than it is, which is the opposite of the point.
    """
    allocated, unattributable = logic.allocate_sb_spend(
        {(DAY, "brandstore"): 11114.0},
        {"brandstore": []},
        {(DAY, "B0AAA"): 5000.0},
    )
    assert allocated == {}
    assert unattributable == 11114.0


def test_an_ASIN_the_economics_has_never_heard_of_is_NOT_silently_charged():
    """Its share goes to the spread bucket instead of inventing a row for it.

    All 24 ASINs currently receiving SB spend have an economics row, so **no live data exercises
    this** — which is precisely why the fixture constructs it, the same reasoning as the
    unknown-pack-weight case in `test_portfolio_weight.py`.
    """
    allocated, unattributable = logic.allocate_sb_spend(
        {(DAY, "g"): 900.0},
        {"g": ["B0NOTINECON"]},
        {(DAY, "B0AAA"): 5000.0},
    )
    assert allocated == {}, "spend was attributed to an ASIN with no economics row"
    assert unattributable == 900.0


def test_a_zero_sales_DAY_for_the_whole_group_does_not_divide_by_zero():
    """Sales-weighting has no denominator when nothing in the group sold that day.

    Those rupees are reported as unattributable rather than split equally, because "we cannot
    attribute this" is the honest reading — the same reason `_ratio` returns None rather than 0.0
    for a product with no sales.
    """
    allocated, unattributable = logic.allocate_sb_spend(
        {(DAY, "g"): 500.0},
        {"g": ["B0DEAD1", "B0DEAD2"]},
        {(DAY, "B0DEAD1"): 0.0, (DAY, "B0DEAD2"): 0.0},
    )
    assert allocated == {}
    assert unattributable == 500.0


def test_the_allocation_is_PER_DAY_so_a_sub_range_is_still_right():
    """Allocating three days then summing two must equal allocating those two alone.

    The store is keyed per day precisely so every sub-range is a sum rather than a refetch. An
    allocation computed over a WINDOW total would make a 7-day slice of a 30-day allocation wrong —
    and wrong in a way that only shows up when someone compares two windows.
    """
    third = "2026-09-03"
    asins = ["B0AAA", "B0BBB"]
    # Sales shares DIFFER per day, so a window-level allocation cannot coincidentally agree.
    sales = {
        (DAY, "B0AAA"): 100.0, (DAY, "B0BBB"): 900.0,
        (NEXT, "B0AAA"): 800.0, (NEXT, "B0BBB"): 200.0,
        (third, "B0AAA"): 500.0, (third, "B0BBB"): 500.0,
    }
    spend = {(DAY, "g"): 1000.0, (NEXT, "g"): 1000.0, (third, "g"): 1000.0}

    three, _ = logic.allocate_sb_spend(spend, {"g": asins}, sales)
    two, _ = logic.allocate_sb_spend(
        {(DAY, "g"): 1000.0, (NEXT, "g"): 1000.0}, {"g": asins}, sales
    )
    for asin in asins:
        summed = three[(DAY, asin)] + three[(NEXT, asin)]
        alone = two[(DAY, asin)] + two[(NEXT, asin)]
        assert round(summed, 2) == round(alone, 2), (
            f"{asin}: a 2-day slice of a 3-day allocation disagrees with allocating 2 days alone"
        )
    # And the per-day split really does follow that day's sales, not the window's.
    assert three[(DAY, "B0BBB")] > three[(DAY, "B0AAA")]
    assert three[(NEXT, "B0AAA")] > three[(NEXT, "B0BBB")]


def test_the_brand_level_remainder_is_spread_by_sales_and_conserved():
    """Asked for, so Portfolio's total reconciles EXACTLY against the Ads tab rather than 2% light."""
    sales = {(DAY, "B0AAA"): 1000.0, (DAY, "B0BBB"): 3000.0, (DAY, "B0CCC"): 0.0}
    spread = logic.spread_unattributable(11114.0, sales)
    assert round(sum(spread.values()), 2) == 11114.0, "the spread does not conserve"
    assert (DAY, "B0CCC") not in spread, "a product that sold nothing carries brand-level spend"
    assert spread[(DAY, "B0BBB")] > spread[(DAY, "B0AAA")], "the spread is not sales-weighted"


def test_spreading_with_nothing_sold_returns_nothing_rather_than_dividing_by_zero():
    assert logic.spread_unattributable(500.0, {(DAY, "B0AAA"): 0.0}) == {}
    assert logic.spread_unattributable(500.0, {}) == {}
    assert logic.spread_unattributable(0.0, {(DAY, "B0AAA"): 100.0}) == {}


# ── The read path: both figures move together, or neither should ──────────────────────────────


def _econ(day, asin, *, sales, net, sp):
    """One Amazon-shaped economics row for one day, with SP ad spend already netted — as Amazon does."""
    return {
        "startDate": day, "endDate": day, "parentAsin": "B0PARENT01", "childAsin": asin,
        "msku": None,
        "sales": {"orderedProductSales": {"amount": sales}, "refundedProductSales": {"amount": 0.0},
                  "unitsOrdered": 10, "unitsRefunded": 0, "netUnitsSold": 10},
        "fees": [],
        "ads": [{"adTypeName": "SponsoredProductFee", "charge": {"totalAmount": {"amount": sp}}}],
        "netProceeds": {"total": {"amount": net}},
    }


async def test_net_proceeds_drops_by_exactly_what_ad_spend_gains(db):
    """**The highest-value test here**, because getting it wrong is invisible in the right place.

    `netProceeds` already nets the SP ad spend — verified to the rupee on production rows. So SB
    must be both ADDED to ad spend and SUBTRACTED from net. Do only the first and TACOS becomes
    correct while margin stays exactly as overstated as before, and the two figures then contradict
    each other on the same row.

    **Driven through the database and `size_row`, not asserted on source.** The first version of
    this test grepped `load_snapshot` for `ad_spend +=` — and the obvious fix, bumping the summed
    `ad_spend` total, would have satisfied it while changing NO figure on screen: that total is
    summed and never emitted, because `size_row` reads ad spend from the rebuilt `ads` LIST.
    """
    from app.portfolio import repository

    await repository.save_economics_daily(db, [_econ(DAY, "B0AAA", sales=1000.0, net=400.0, sp=100.0)])
    before = logic.size_row((await repository.load_snapshot(db, (DAY, DAY)))[0], {})

    await repository.save_sb_spend(db, {DAY}, {(DAY, "B0AAA"): (250.0, logic.SB_BASIS_DECLARED)})
    after = logic.size_row((await repository.load_snapshot(db, (DAY, DAY)))[0], {})

    assert after["ad_spend"] == before["ad_spend"] + 250.0, (
        "SB spend did not reach the ad spend figure size_row reports, so TACOS stays understated"
    )
    assert after["net"] == before["net"] - 250.0, (
        "SB spend was not taken out of net proceeds, so margin stays OVERSTATED while TACOS reads "
        "correct — the two figures then disagree on the same row"
    )
    assert after["sales"] == before["sales"], "SB attribution must not touch sales"


async def test_storing_SB_twice_corrects_rather_than_doubles(db):
    """A second backfill or a re-run night must land on the same figure, not twice it."""
    from app.portfolio import repository

    await repository.save_economics_daily(db, [_econ(DAY, "B0AAA", sales=1000.0, net=400.0, sp=100.0)])
    for _ in range(2):
        await repository.save_sb_spend(db, {DAY}, {(DAY, "B0AAA"): (250.0, "declared")})
    row = logic.size_row((await repository.load_snapshot(db, (DAY, DAY)))[0], {})
    assert row["ad_spend"] == 350.0


async def test_an_ASIN_that_STOPPED_receiving_SB_loses_yesterdays_figure(db):
    """The reset is what makes a re-allocation honest: an absent key means zero, not "unchanged"."""
    from app.portfolio import repository

    await repository.save_economics_daily(db, [
        _econ(DAY, "B0AAA", sales=1000.0, net=400.0, sp=0.0),
        _econ(DAY, "B0BBB", sales=1000.0, net=400.0, sp=0.0),
    ])
    await repository.save_sb_spend(db, {DAY}, {(DAY, "B0AAA"): (100.0, "declared"),
                                               (DAY, "B0BBB"): (100.0, "declared")})
    await repository.save_sb_spend(db, {DAY}, {(DAY, "B0AAA"): (200.0, "declared")})
    rows = {r["childAsin"]: logic.size_row(r, {})
            for r in await repository.load_snapshot(db, (DAY, DAY))}
    assert rows["B0AAA"]["ad_spend"] == 200.0
    assert rows["B0BBB"]["ad_spend"] == 0.0, "a stale SB figure survived a re-allocation"


async def test_writing_one_day_leaves_the_other_days_SB_alone(db):
    """The nightly run allocates ONE day; a reset scoped wider would zero the other 89."""
    from app.portfolio import repository

    await repository.save_economics_daily(db, [
        _econ(DAY, "B0AAA", sales=1000.0, net=400.0, sp=0.0),
        _econ(NEXT, "B0AAA", sales=1000.0, net=400.0, sp=0.0),
    ])
    await repository.save_sb_spend(db, {DAY, NEXT}, {(DAY, "B0AAA"): (100.0, "declared"),
                                                     (NEXT, "B0AAA"): (100.0, "declared")})
    await repository.save_sb_spend(db, {NEXT}, {(NEXT, "B0AAA"): (300.0, "declared")})
    first = logic.size_row((await repository.load_snapshot(db, (DAY, DAY)))[0], {})
    assert first["ad_spend"] == 100.0, "allocating one day reset another day's SB spend"


async def test_re_storing_a_days_ECONOMICS_does_not_wipe_its_SB(db):
    """A manual re-fetch is delete-then-insert. If that night's SB report then throttles — an
    hours-long window for SB report creation, measured — the day would silently go SP-only. The
    carried figure is the last good one, and the SB phase overwrites it when it succeeds."""
    from app.portfolio import repository

    await repository.save_economics_daily(db, [_econ(DAY, "B0AAA", sales=1000.0, net=400.0, sp=100.0)])
    await repository.save_sb_spend(db, {DAY}, {(DAY, "B0AAA"): (250.0, "declared")})
    await repository.save_economics_daily(db, [_econ(DAY, "B0AAA", sales=1100.0, net=450.0, sp=100.0)])
    row = logic.size_row((await repository.load_snapshot(db, (DAY, DAY)))[0], {})
    assert row["ad_spend"] == 350.0, "re-storing the economics wiped the attributed SB spend"
    assert row["net"] == 200.0


async def test_the_STORED_ads_json_stays_Amazons_verbatim_answer(db):
    """SB exists only in the read-time shape, never written into Amazon's cached breakdown."""
    import json

    from sqlalchemy import select

    from app.models import EconomicsDaily
    from app.portfolio import repository

    await repository.save_economics_daily(db, [_econ(DAY, "B0AAA", sales=1000.0, net=400.0, sp=100.0)])
    await repository.save_sb_spend(db, {DAY}, {(DAY, "B0AAA"): (250.0, "declared")})
    stored = (await db.execute(select(EconomicsDaily.ads_json))).scalar_one()
    assert json.loads(stored) == {"SponsoredProductFee": 100.0}


def test_ACOS_is_NOT_affected_by_the_SB_allocation():
    """ACOS divides the Advertising API's own cost by that API's own attributed sales.

    Mixing an allocated SB figure into that numerator would be a ratio of two different things, and
    the three ACOS states (never advertised / spend with no attributed sales / a real percentage)
    exist precisely because that distinction matters. `size_row` must keep reading `ads_cost`.
    """
    source = (Path(__file__).parent.parent / "app" / "portfolio" / "logic.py").read_text(
        encoding="utf-8"
    )
    body = source[source.index("def size_row("):]
    body = body[: body.index("\ndef ", 1)]
    assert '"acos": _ratio(ads_cost' in body.replace("'", '"'), (
        "ACOS is no longer computed from the Advertising API's own cost"
    )
    assert "sb_spend" not in body, (
        "size_row reads the SB figure directly; it must arrive already folded into ad_spend by "
        "load_snapshot, or the fold happens in two places and they can disagree"
    )


def test_the_SB_figure_is_NOT_rendered_as_its_own_column_or_tile():
    """*"no separate labelling of it in the portfolio tab"* — explicit, so it is pinned.

    The obvious future edit is to add an "SB spend" column beside ad spend. This fails if anyone
    does, and the instruction is quoted in the message so the reason travels with the failure.
    """
    rendered = TEMPLATE.read_text(encoding="utf-8")
    for pattern in (r"/\*.*?\*/", r"\{#.*?#\}", r"<!--.*?-->"):
        rendered = re.sub(pattern, " ", rendered, flags=re.S)
    rendered = re.sub(r"(?m)(?<![:\w])//[^\n]*", " ", rendered)

    for token in ("sb_spend", "sb_basis", "Sponsored Brands"):
        assert token not in rendered, (
            f"{token!r} is rendered on the Portfolio tab. The instruction was 'no separate "
            "labelling of it in the portfolio tab. just add to the main ad figures of each product.'"
        )


def test_the_basis_constants_are_distinct_and_named():
    """`sb_basis` exists so "declared" and "spread" can be told apart without re-running the
    allocator. Two identical constants would silently make that impossible."""
    assert logic.SB_BASIS_DECLARED != logic.SB_BASIS_SPREAD
    assert logic.SB_BASIS_DECLARED and logic.SB_BASIS_SPREAD


# ── End to end: the Ads tab's stored SB rows -> the Portfolio rows ────────────────────────────


async def _sb_row(db, day, group, spend, entity="kw1"):
    from app.models import AdsPerformanceDaily
    db.add(AdsPerformanceDaily(day=day, entity_id=f"{entity}-{group}-{day}", entity_type="keyword",
                               ad_product="sb", campaign_id="c1", ad_group_id=group, spend=spend))
    await db.commit()


async def test_attribution_reconciles_EXACTLY_against_the_Ads_tabs_own_SB_total(db):
    """**The strongest check available**, because it compares two separate integrations.

    Whatever the Ads tab stored as SB spend for these days must reappear on the Portfolio rows,
    to the paisa — declared, spread, or named as lost. This is the check that would have caught
    the Rs 1,26,328 the Ads tab once lost between two caches of one figure.
    """
    from app.portfolio import repository, sb_attribution

    await repository.save_economics_daily(db, [
        _econ(DAY, "B0AAA", sales=3000.0, net=900.0, sp=100.0),
        _econ(DAY, "B0BBB", sales=1000.0, net=300.0, sp=50.0),
    ])
    await _sb_row(db, DAY, "single", 400.0)                       # names one ASIN
    await _sb_row(db, DAY, "multi", 1000.0)                       # names both, split 3:1
    await _sb_row(db, DAY, "multi", 200.0, entity="kw2")          # a second keyword, same group
    await _sb_row(db, DAY, "brandstore", 80.0)                    # names nothing -> spread

    summary = await sb_attribution.attribute_days(db, [DAY], ad_group_asins={
        "single": ["B0BBB"], "multi": ["B0AAA", "B0BBB"], "brandstore": [],
    })
    rows = {r["childAsin"]: logic.size_row(r, {})
            for r in await repository.load_snapshot(db, (DAY, DAY))}
    portfolio_sb = (rows["B0AAA"]["ad_spend"] - 100.0) + (rows["B0BBB"]["ad_spend"] - 50.0)

    assert round(portfolio_sb, 2) == 1680.0, "Portfolio SB spend does not equal the Ads tab's"
    assert summary == {"total": 1680.0, "declared": 1600.0, "spread": 80.0, "lost": 0.0, "days": 1}
    # The 3:1 sales split on the multi group, plus the 3:1 spread of the brand-store 80.
    assert round(rows["B0AAA"]["ad_spend"] - 100.0, 2) == 900.0 + 60.0
    assert round(rows["B0BBB"]["ad_spend"] - 50.0, 2) == 400.0 + 300.0 + 20.0


async def test_a_day_the_Ads_tab_has_NOT_fetched_keeps_its_last_good_figure(db):
    """No SB rows may mean "the report was throttled", not "nothing was spent". Zeroing the day on
    that evidence is the understatement this whole change exists to remove."""
    from app.portfolio import repository, sb_attribution

    await repository.save_economics_daily(db, [_econ(DAY, "B0AAA", sales=1000.0, net=400.0, sp=0.0)])
    await repository.save_sb_spend(db, {DAY}, {(DAY, "B0AAA"): (250.0, "declared")})

    summary = await sb_attribution.attribute_days(db, [DAY], ad_group_asins={})
    assert summary["days"] == 0
    row = logic.size_row((await repository.load_snapshot(db, (DAY, DAY)))[0], {})
    assert row["ad_spend"] == 250.0, "a day with no SB rows was zeroed"


async def test_a_day_with_SB_but_no_economics_yet_waits_rather_than_losing_the_spend(db):
    """Nightly order: Portfolio stores yesterday at 07:30, Ads stores yesterday's SB at 08:00.
    Whichever lands SECOND attributes — a day with no sales to weight by must not be attributed."""
    from app.portfolio import sb_attribution

    await _sb_row(db, DAY, "single", 400.0)
    summary = await sb_attribution.attribute_days(db, [DAY], ad_group_asins={"single": ["B0AAA"]})
    assert summary["days"] == 0


async def test_a_failed_ASIN_list_writes_NOTHING(db, monkeypatch):
    """An empty map would send 100% of SB spend to the spread bucket — plausible and wrong."""
    from app.portfolio import repository, sb_attribution

    await repository.save_economics_daily(db, [_econ(DAY, "B0AAA", sales=1000.0, net=400.0, sp=0.0)])
    await repository.save_sb_spend(db, {DAY}, {(DAY, "B0AAA"): (250.0, "declared")})
    await _sb_row(db, DAY, "single", 999.0)

    async def boom():
        raise RuntimeError("Amazon said no")
    monkeypatch.setattr(sb_attribution, "fetch_ad_group_asins", boom)

    with pytest.raises(RuntimeError):
        await sb_attribution.attribute_days(db, [DAY])
    row = logic.size_row((await repository.load_snapshot(db, (DAY, DAY)))[0], {})
    assert row["ad_spend"] == 250.0, "a failed fetch overwrote the last good SB figure"


def test_the_Portfolio_refresh_creates_NO_Sponsored_Brands_report():
    """**The design decision most likely to be "tidied" back, so it is pinned.** SB report creation
    is throttled over HOURS across every report created that day, and the Ads tab's nightly job
    needs one at 08:00 IST. A Portfolio report at 07:30 would spend that budget first and bring back
    "Sponsored Brands figures are stale" — the defect that took a week to find."""
    import ast
    import inspect

    from app.portfolio import refresh, sb_attribution

    for module in (refresh, sb_attribution):
        tree = ast.parse(inspect.getsource(module))
        names = {n.attr if isinstance(n, ast.Attribute) else getattr(n, "id", "")
                 for n in ast.walk(tree) if isinstance(n, (ast.Attribute, ast.Name))}
        assert "fetch_targeting" not in names, (
            f"{module.__name__} creates an SB report; read the Ads tab's stored rows instead"
        )


def test_the_Ads_refresh_hands_its_SB_days_to_the_Portfolio_and_cannot_be_failed_by_it():
    """The nightly path. Called from `store_sb_chunk`, whose exceptions propagate out of
    `fetch_targeting` — so the helper must swallow, or a Portfolio bug fails the Ads tab's SB report."""
    import inspect

    from app.ads import refresh as ads_refresh

    source = inspect.getsource(ads_refresh.run)
    chunk = source[source.index("async def store_sb_chunk"):]
    chunk = chunk[: chunk.index("try:")]
    assert "_attribute_sb_to_portfolio(" in chunk, "SB days are no longer handed to the Portfolio"

    helper = inspect.getsource(ads_refresh._attribute_sb_to_portfolio)
    assert "except Exception" in helper and "raise" not in helper.split("except Exception", 1)[1], (
        "a failure attributing SB to the Portfolio can now fail the Ads tab's own refresh"
    )


async def test_the_Ads_refresh_helper_really_swallows(monkeypatch):
    from app.ads import refresh as ads_refresh
    from app.portfolio import sb_attribution

    async def boom(*_a, **_k):
        raise RuntimeError("attribution exploded")
    monkeypatch.setattr(sb_attribution, "attribute_days", boom)

    class _Ctx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    await ads_refresh._attribute_sb_to_portfolio(lambda: _Ctx(), DAY, NEXT)   # must not raise


def test_a_days_brand_level_spend_is_spread_over_THAT_DAYS_sales_only():
    """Brand-store spend on the 1st must not land on the 2nd.

    Every earlier test used one day, so spreading over the WINDOW's sales was invisible — the
    mutation harness caught it surviving. Spread over the window, a 1-day slice of a 2-day
    allocation would carry spend from a day outside it: the per-DAY property the store exists for.
    """
    from app.portfolio import sb_attribution

    sales = {(DAY, "B0AAA"): 1000.0, (NEXT, "B0BBB"): 1000.0}
    allocations, summary = sb_attribution.combine(
        {(DAY, "brandstore"): 100.0}, {"brandstore": []}, sales
    )
    assert allocations == {(DAY, "B0AAA"): (100.0, logic.SB_BASIS_SPREAD)}, (
        "one day's brand-level SB spend was spread onto another day's sales"
    )
    assert summary["spread"] == 100.0 and summary["lost"] == 0.0
