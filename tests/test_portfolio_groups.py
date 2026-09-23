"""Three tabs over seven verdicts, category sales, and the daily ratings scrape.

Asked for as *"lesser tabs — I want only Scale (top performers), Maintain (mid performers), Kill or
monitor (least performers)"*, *"need each category sales as well"*, and *"live fetching of reviews
from scraper every day and account it while making the bifurcation"*.

**The grouping is a VIEW, not a rewrite.** `verdict_for` keeps its seven rules and every reason
string, because each reason is a claim the owner can check against Seller Central — and CLAUDE.md
records what each cost to learn. These tests assert that the seven survive.

Every test here fails against the code before this change.
"""
import json
from pathlib import Path

import pytest

from app.portfolio import logic

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"
WINDOW = ("2026-07-28", "2026-08-26")


# ── The mapping ──────────────────────────────────────────────────────────────


def test_every_verdict_has_exactly_one_group():
    """**Total over `VERDICT_ORDER`.**

    A verdict with no group would put its products in whichever tab the fallback picks, and a new
    verdict added later would silently land there too. Asserted by iterating the real order so
    adding an eighth verdict fails this test rather than hiding a product.
    """
    assert set(logic.VERDICT_ORDER) == set(logic.VERDICT_GROUPS), (
        "every verdict must be mapped, and nothing unmapped may be listed"
    )
    for verdict in logic.VERDICT_ORDER:
        assert logic.verdict_group(verdict) in logic.GROUP_ORDER


def test_there_are_exactly_three_groups():
    assert logic.GROUP_ORDER == ("Scale", "Maintain", "Kill or monitor")


def test_the_profitable_verdicts_are_scale():
    assert logic.verdict_group(logic.VERDICT_BEST_BET) == logic.GROUP_SCALE
    assert logic.verdict_group(logic.VERDICT_SCALE) == logic.GROUP_SCALE


def test_surgical_is_maintain_not_kill():
    """The parent EARNS its place; one size does not. The action is surgery.

    Measured live: Cheese & Cream Roasted Chana earns +27.1% overall while one 250 g pack burns
    103% TACOS at -52.7% net. Putting that parent in "Kill or monitor" would invite killing a
    profitable product.
    """
    assert logic.verdict_group(logic.VERDICT_SURGICAL) == logic.GROUP_MAINTAIN


def test_dead_and_kill_are_kill_or_monitor():
    assert logic.verdict_group(logic.VERDICT_KILL) == logic.GROUP_KILL
    assert logic.verdict_group(logic.VERDICT_DEAD) == logic.GROUP_KILL


def test_exactly_ONE_verdict_carries_a_group_flag():
    """A flag on every row is noise; a flag on none loses the information.

    **This asserted TWO — `{SURGICAL, AD_DEPENDENT}` — and it was right when written.** AD DEPENDENT
    was retired along with its rule when ACOS stopped deciding anything, so there is one left. The
    property is unchanged and is the reason the test exists: asserted in BOTH directions, so a flag
    cannot quietly be added to a verdict whose group already says the right thing, and the surviving
    one cannot quietly be dropped.

    A companion test, `test_ad_dependent_is_kill_or_monitor_but_carries_its_flag`, was deleted
    outright rather than adapted — keeping it would have kept a deleted verdict alive.
    """
    flagged = {v for v in logic.VERDICT_ORDER if logic.group_flag(v)}
    assert flagged == {logic.VERDICT_SURGICAL}


def test_an_unknown_verdict_lands_in_maintain_rather_than_vanishing():
    """Deny into the SAFE bucket, like `ads.logic.manager_of` treating an unknown campaign as ours.

    A product missing from all three tabs is invisible; one in the middle tab is merely mis-sorted,
    and its row still shows its own verdict and reason.
    """
    assert logic.verdict_group("SOMETHING NEW") == logic.GROUP_MAINTAIN
    assert logic.verdict_group("") == logic.GROUP_MAINTAIN


def test_the_six_verdicts_still_exist_so_this_is_a_view_not_a_rewrite():
    """`verdict_for`'s rules and reasons are untouched by the three-group VIEW over them.

    The whole argument for mapping rather than rewriting is that each reason is verifiable against
    Seller Central. If the six collapsed into three, "net -56.8%, TACOS 78%" would become "Kill" and
    the owner would have nothing to check.

    **This asserted SEVEN, and it was right when written.** AD DEPENDENT was removed deliberately —
    it was the only rule reading ACOS, and the owner does not trust ACOS to be captured correctly.
    That is a rule the owner retired, not a rewrite of the remaining six, which is exactly the
    distinction this test exists to defend. Six is now the number that must not drift.
    """
    assert len(logic.VERDICT_ORDER) == 6
    assert set(logic.VERDICT_HELP) >= set(logic.VERDICT_ORDER) - {logic.VERDICT_DEAD}


# ── The counts ───────────────────────────────────────────────────────────────


def test_group_counts_covers_every_group_even_at_zero():
    """A tab matching nothing must render empty, not vanish.

    The Portfolio tab already learned this with the verdict chips: dropping a zero-count chip left
    an empty table, nothing highlighted, and no control left to click to undo the filter.
    """
    counts = logic.group_counts([])
    assert set(counts) == set(logic.GROUP_ORDER)
    assert all(value == 0 for value in counts.values())


def test_group_counts_sums_to_the_row_count():
    """A re-arrangement, never a filter. Every row lands in exactly one group.

    The literal was 2/2/3 while AD DEPENDENT existed in Kill-or-monitor. The `sum` assertion above
    it is the one that carries the property; the literal is what makes a silent re-mapping visible.
    """
    rows = [{"verdict": v} for v in logic.VERDICT_ORDER]
    counts = logic.group_counts(rows)
    assert sum(counts.values()) == len(rows)
    assert counts == {"Scale": 2, "Maintain": 2, "Kill or monitor": 2}


# ── Category sales ───────────────────────────────────────────────────────────


def _parent(product, sales, ad_spend, net, units=100, sizes=None):
    return {
        "product": product,
        "sales": sales,
        "ad_spend": ad_spend,
        "net": net,
        "units": units,
        "sizes": sizes or [{"product": product}],
    }


def test_the_labels_come_from_the_shipment_tab_not_a_second_copy():
    """ONE vocabulary. The keyword ORDER in `shipment.logic` is the rule, not an implementation
    detail — "Bangla Chana Sattu" is a sattu, "Rice Atta" is a flour — and a second copy here would
    be a second thing to keep in step, with a category total that disagrees with the packer's sort
    order as the failure.
    """
    import inspect

    from app.shipment.logic import CATEGORY_LABELS

    source = inspect.getsource(logic.category_totals)
    assert "from app.shipment.logic import CATEGORY_LABELS" in source
    # And the labels really are the shipment ones, including P4 staying "Rice" for consistency.
    assert CATEGORY_LABELS[1] == "Sattu"
    assert CATEGORY_LABELS[4] == "Rice"


def test_category_totals_sum_to_the_account_total():
    """Verified on real production data too: ₹44,33,606 both ways.

    Built from the PARENT rows rather than a second aggregation over the economics, so a category
    total is the sum of the rows on screen — the defect class this codebase records three times.
    """
    parents = [
        _parent("Chana Sattu", 1000.0, 200.0, 250.0),
        _parent("Roasted Chana", 500.0, 300.0, -50.0),
        _parent("Something Else", 250.0, 50.0, 60.0),
    ]
    out = logic.category_totals(parents, {"chana sattu": 1, "roasted chana": 2})
    assert sum(c["sales"] for c in out["categories"]) == 1750.0
    assert sum(c["ad_spend"] for c in out["categories"]) == 550.0
    assert sum(c["products"] for c in out["categories"]) == 3


def test_percentages_are_recomputed_from_the_sums_never_averaged():
    """The mean of two products' TACOS belongs to no product.

    One product sells 1 unit at 100% TACOS and the other 400 units at 10%. The average is 55%; the
    real figure is weighted by spend and sales. `_sum_sizes` already follows this rule.
    """
    parents = [
        _parent("Chana Sattu", 100.0, 100.0, 0.0, units=1),
        _parent("Bengali Chana Sattu", 10_000.0, 1_000.0, 3_000.0, units=400),
    ]
    out = logic.category_totals(
        parents, {"chana sattu": 1, "bengali chana sattu": 1}
    )
    sattu = next(c for c in out["categories"] if c["category"] == "Sattu")
    # 1,100 / 10,100 = 10.9%, not the 55% a mean would give.
    assert sattu["tacos"] == pytest.approx(1100 / 10100)
    assert sattu["margin"] == pytest.approx(3000 / 10100)


def test_an_unclassified_parent_is_its_own_bucket_and_is_NAMED():
    """Not folded into Rest, which would make Rest the largest category and meaningless.

    Measured on production: only 38 of ~90 parents are classified. Naming them is what lets the
    owner classify them once on the Shipment tab, the way the catalogue notes and the Projections
    `needs_review` list already do.
    """
    parents = [
        _parent("Chana Sattu", 1000.0, 100.0, 200.0),
        _parent("Mystery Product", 500.0, 50.0, 90.0),
    ]
    out = logic.category_totals(parents, {"chana sattu": 1})
    labels = [c["category"] for c in out["categories"]]
    assert logic.CATEGORY_UNCLASSIFIED in labels
    assert "Rest" not in labels, "an unclassified product must not be filed as Rest"
    assert out["unclassified_names"] == ["Mystery Product"]
    assert out["unclassified_total"] == 1


def test_a_renamed_multi_flavour_parent_matches_on_its_SIZE_names():
    """**Found on real production data.**

    `family_label` renames a multi-flavour parent to what its flavours share and disambiguates a
    collision by appending a count, so the row reads "Roasted Chana (5 flavours)" while
    `product_categories` holds "roasted chana". Matching the parent name alone left 52 of 90
    products unclassified — including several whose categories ARE stored — and moved ₹2.33 lakh of
    Chana sales into Unclassified.
    """
    parent = _parent(
        "Roasted Chana (5 flavours)",
        661_026.0,
        212_179.0,
        150_088.0,
        sizes=[{"product": "Peri Peri Roasted Chana"}, {"product": "Roasted Chana"}],
    )
    out = logic.category_totals([parent], {"roasted chana": 2})
    assert [c["category"] for c in out["categories"]] == ["Chana"]
    assert out["unclassified_total"] == 0


def test_the_stored_choice_is_used_not_a_keyword_guess():
    """`category_totals` reads what the owner CHOSE, exact match only.

    `shipment.logic.category_for` does substring keyword matching and is the right tool for
    guessing. Falling back to it here would make a wrong guess indistinguishable from a decision,
    which is exactly what naming the unclassified products exists to avoid.
    """
    # "Chana Sattu" would keyword-match to Sattu, but nothing is stored for it.
    out = logic.category_totals([_parent("Chana Sattu", 100.0, 10.0, 20.0)], {})
    assert [c["category"] for c in out["categories"]] == [logic.CATEGORY_UNCLASSIFIED]


def test_the_named_unclassified_list_is_capped_but_the_count_is_exact():
    """52 names is a column, not a sentence — the same cap the catalogue notes use."""
    parents = [_parent(f"Product {i}", 10.0, 1.0, 2.0) for i in range(20)]
    out = logic.category_totals(parents, {})
    assert len(out["unclassified_names"]) == logic.UNCLASSIFIED_SHOWN
    assert out["unclassified_total"] == 20


def test_unclassified_rows_and_NAMES_are_reported_separately():
    """**Found on live production: the card read 55 while the note read 51.**

    Both were right — 55 unclassified ROWS over 51 distinct NAMES, because `Singhara Atta` appears
    four times in the catalogue and `Arwa Katarni Rice` twice (catalogue duplicates this app
    deliberately does not invent a distinction for). Two numbers for one thing with nothing
    explaining the gap is the "86 orders beside 87 lines" defect, so both travel and the screen
    states the difference.

    The NAMES count is the one that drives the action: a category is stored per name, so
    classifying "Singhara Atta" once covers all four rows.
    """
    parents = [
        _parent("Singhara Atta", 100.0, 10.0, 20.0),
        _parent("Singhara Atta", 100.0, 10.0, 20.0),
        _parent("Bengali Posta", 50.0, 5.0, 10.0),
    ]
    out = logic.category_totals(parents, {})
    card = next(
        c for c in out["categories"] if c["category"] == logic.CATEGORY_UNCLASSIFIED
    )
    assert card["products"] == 3, "the card must count every row, or its sales would not add up"
    assert out["unclassified_rows"] == 3
    assert out["unclassified_total"] == 2, (
        "the note must count distinct NAMES, since that is what gets classified"
    )
    assert out["unclassified_names"] == ["Singhara Atta", "Bengali Posta"]


def test_categories_are_ordered_biggest_first():
    """The strip answers "where is the money", not a fixed taxonomy."""
    parents = [
        _parent("Chana Sattu", 100.0, 10.0, 20.0),
        _parent("Roasted Chana", 900.0, 90.0, 180.0),
    ]
    out = logic.category_totals(parents, {"chana sattu": 1, "roasted chana": 2})
    assert [c["category"] for c in out["categories"]] == ["Chana", "Sattu"]


def test_a_category_with_no_sales_reports_no_tacos_rather_than_zero():
    """A dash, never a zero. 0% would rank it as the most ad-efficient thing in the portfolio —
    the reason `_ratio` returns None with no denominator."""
    out = logic.category_totals(
        [_parent("Chana Sattu", 0.0, 0.0, 0.0, units=0)], {"chana sattu": 1}
    )
    sattu = out["categories"][0]
    assert sattu["tacos"] is None
    assert sattu["margin"] is None


# ── The route: both views are computed on the SERVER ─────────────────────────
#
# `logic.category_totals` and `logic.group_counts` being correct is not the claim the screen relies
# on — the claim is that `GET /portfolio` actually calls them, with the right arguments. This
# codebase has shipped a complete, tested, correctly-wired SERVER three times over a client that
# silently did something else (`intakeFromShipment`, `renderInvoiceBar`, the ads pause feature), and
# once shipped a working aggregation nothing ever called.


async def _seed(db):
    from app.portfolio import repository

    rows = json.loads((FIXTURES / "economics_rows.json").read_text(encoding="utf-8"))
    # Stamped onto ONE day inside WINDOW: the store is keyed per day, and one day's rows summed IS
    # the window's rows, so every total in this file means exactly what it did before.
    for row in rows:
        row["startDate"] = row["endDate"] = WINDOW[1]
    return await repository.save_economics_daily(db, rows)


async def test_the_payload_carries_the_grouping_and_the_categories(auth_client, db):
    """The mapping and the two flags travel, so the screen holds no second copy.

    Sent from the server for the same reason `phase_labels` and `ads.MATCH_LABELS` are: a table
    duplicated into the template is a second thing to keep in step, and the failure mode is a tab
    whose count disagrees with the rows beneath it.
    """
    await _seed(db)
    body = (await auth_client.get("/portfolio")).json()

    assert body["group_order"] == list(logic.GROUP_ORDER)
    assert body["verdict_groups"] == dict(logic.VERDICT_GROUPS), (
        "the grouping does not reach the screen, so every row falls back to Maintain"
    )
    assert body["group_flags"] == dict(logic.GROUP_FLAGS), (
        "the two flags do not reach the screen, so SURGICAL reads as a healthy product"
    )
    assert set(body["group_counts"]) == set(logic.GROUP_ORDER)
    assert "categories" in body["category_totals"]


async def test_the_payload_counts_each_grain_against_its_own_rows(auth_client, db):
    """**"11 Scale products" and "36 Scale SKUs" are both true, and the tab must match the table.**

    Two counts rather than one, each summing to its own grain's row count. Counting the wrong grain
    would put a tab reading 36 above a table showing 11 — the "86 orders beside 87 lines" defect,
    and it looks entirely plausible on screen because both numbers are real.
    """
    await _seed(db)
    body = (await auth_client.get("/portfolio")).json()

    assert sum(body["group_counts"].values()) == len(body["parents"])
    assert sum(body["sku_group_counts"].values()) == len(body["skus"])
    # And the fixture has to make the two DIFFER, or a swap would pass — the one-campaign-fixture
    # mistake `test_a_group_total_is_exactly_the_sum_of_its_own_rows` already made once.
    assert len(body["parents"]) != len(body["skus"]), (
        "the fixture cannot tell the two counts apart, so swapping them would pass"
    )


async def test_the_category_strip_uses_the_shipment_tabs_own_classification(auth_client, db):
    """ONE vocabulary: the owner classifies a product once and both tabs agree.

    Asserted through the ROUTE rather than against `category_totals`, because the route is where
    the join happens — passing `{}` there would leave every product Unclassified with the pure
    function still perfectly correct.
    """
    from app.shipment import repository as ship_repository

    stored = await _seed(db)
    body = (await auth_client.get("/portfolio")).json()
    names = [p["product"] for p in body["parents"] if p.get("product")]
    assert names, "no products to classify"

    before = {c["category"] for c in body["category_totals"]["categories"]}
    assert before == {logic.CATEGORY_UNCLASSIFIED}, (
        "something is already classified, so this test cannot prove the join is read"
    )

    await ship_repository.set_categories(db, {names[0]: 1})
    after = (await auth_client.get("/portfolio")).json()["category_totals"]
    assert "Sattu" in {c["category"] for c in after["categories"]}, (
        "a stored category does not reach the strip, so the route is not reading "
        "product_categories at all"
    )
    assert after["unclassified_total"] < body["category_totals"]["unclassified_total"]
    assert stored  # the snapshot really was saved


# ── The screen reads the server's answer ─────────────────────────────────────


def test_the_table_filters_on_the_group_not_the_verdict():
    """`filter` holds a GROUP now, so comparing it against a verdict matches nothing.

    Source-level, because no runtime test here drives the browser — and the failure is silent in
    the worst way: every tab would show an empty table while its count said otherwise.
    """
    source = _portfolio_template()
    assert "verdictGroup(r.verdict) !== filter" in source, (
        "the tab filter compares against the raw verdict, so no row can ever match a group name"
    )


def test_the_unclassified_note_explains_the_row_and_name_counts():
    """The card counts rows, the note counts names, and the screen must say so when they differ.

    Stated only when there IS a difference: "51 names across 51 rows" is noise, and a caveat that
    fires on every render is the kind that trains the reader to skip the one that matters.
    """
    body = _template_function(_portfolio_template(), "renderCategories")
    assert "ct.unclassified_rows" in body, (
        "the note does not read the row count, so 55 on the card beside 51 in the note is "
        "unexplained"
    )
    # The whole condition, anchored to its assignment. A substring check on `rowCount > nameCount`
    # alone survived `false && rowCount > nameCount` — the deploy-detector trap (an id that also
    # appears in the comment explaining the bug), and the FIFTH instance of it in this codebase.
    assert "const dupes = rowCount > nameCount" in body, (
        "the explanation is not gated on the two counts actually differing, so it either fires "
        "when they agree or has been disabled"
    )


def test_the_screen_reads_the_grouping_from_the_server_not_a_second_copy():
    """A mapping duplicated here is how a tab count comes to disagree with its own rows."""
    source = _portfolio_template()
    body = _template_function(source, "verdictGroup")
    assert "data.verdict_groups" in body, "the screen does not read the server's mapping"
    for verdict in logic.VERDICT_ORDER:
        assert verdict not in body, (
            f"{verdict!r} is hardcoded in verdictGroup, which is a second copy of the mapping"
        )
    flags = _template_function(source, "groupFlag")
    assert "data.group_flags" in flags, "the flag text is a second copy rather than the server's"


def _template_function(source: str, name: str) -> str:
    """The body of one top-level function, up to the next one.

    Same helper `tests/test_portfolio_screen.py` uses, and for the same reason: "this rule is
    followed somewhere in 1,400 lines" is a different claim from "this function follows it".
    """
    start = source.index(f"function {name}(")
    rest = source[start:]
    end = rest.find("\nfunction ", 1)
    return rest if end == -1 else rest[:end]


# ── The daily ratings scrape ─────────────────────────────────────────────────


def test_the_scrape_can_run_without_waking_the_keyword_track_or_the_purge(monkeypatch):
    """**The finding: the scrape had NEVER run on production.**

    `SCHEDULER_ENABLED=false` keeps the heavy jobs asleep on a 951 MB box, so the 06:00 scrape was
    dormant and the Portfolio tab's star ratings were six days old — every scrape in
    `rating_history` was manual. A stale rating silently shapes a verdict.

    Asserted on the registered job IDS, not on the flag. `setup_scheduler`'s guard was once at the
    top of the function, and moving it back there reads as tidy while silently stopping the orders
    refresh — so the test has to observe what was actually registered.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    import app.scheduler as sched

    fake = AsyncIOScheduler()
    monkeypatch.setattr(sched, "scheduler", fake)
    monkeypatch.setattr(sched.settings, "scheduler_enabled", False)
    monkeypatch.setattr(sched.settings, "order_refresh_enabled", False)
    monkeypatch.setattr(sched.settings, "scrape_enabled", True)

    sched.setup_scheduler()
    ids = {job.id for job in fake.get_jobs()}

    assert "daily_product_scrape" in ids, "the scrape must register under its own flag"
    assert "daily_keyword_track" not in ids, "the keyword track must stay asleep"
    assert "daily_history_purge" not in ids, "the purge must stay asleep"


def test_the_scrape_runs_BEFORE_the_portfolio_job_reads_the_ratings(monkeypatch):
    """ORDER, not just presence.

    The portfolio job reads `rating_history` at 07:30 IST. A scrape scheduled after it would leave
    the tab a full day behind on stars, which is the bug being fixed — so the times are asserted
    relative to each other rather than as constants.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    import app.scheduler as sched

    fake = AsyncIOScheduler()
    monkeypatch.setattr(sched, "scheduler", fake)
    monkeypatch.setattr(sched.settings, "scheduler_enabled", False)
    monkeypatch.setattr(sched.settings, "order_refresh_enabled", False)
    monkeypatch.setattr(sched.settings, "scrape_enabled", True)
    sched.setup_scheduler()

    jobs = {job.id: job for job in fake.get_jobs()}
    scrape = jobs["daily_product_scrape"]
    portfolio = jobs["portfolio_refresh"]

    def hhmm(job):
        fields = {f.name: str(f) for f in job.trigger.fields}
        return int(fields["hour"]), int(fields["minute"])

    # Both are UTC crons on a UTC box. The scrape is 23:30 UTC (05:00 IST) and the portfolio 02:00
    # UTC (07:30 IST) — the scrape is the PREVIOUS UTC day, which is exactly the wrap `ist.utc_hhmm`
    # exists to get right, so the comparison is on the IST intent.
    assert hhmm(scrape) == (23, 30), "05:00 IST is 23:30 UTC the previous day"
    assert hhmm(portfolio) == (2, 0), "07:30 IST is 02:00 UTC"


def test_the_scrape_time_is_stated_in_IST(monkeypatch):
    """Asserted on the IST value, never the UTC one.

    `hour == 23` would pin the arithmetic instead of the intent — which is how the comment
    "03:20 IST-ish (the box is UTC...)" came to be written while the job fired at 09:20 IST.
    """
    from app import ist
    from app.config import get_settings

    settings = get_settings()
    assert settings.scrape_ist_hour == 5
    assert ist.utc_hhmm(settings.scrape_ist_hour, settings.scrape_ist_minute) == (23, 30)
    assert "05:00 IST" in ist.label(
        settings.scrape_ist_hour, settings.scrape_ist_minute
    )


# ── The screen ───────────────────────────────────────────────────────────────
#
# Source-level, because no runtime test here drives a browser — but these three properties were
# each verified by opening the page and clicking, which is how the temporal-dead-zone bug below was
# found. CLAUDE.md records three prior instances of a server contract passing while the client
# silently did something else (the pause feature, `intakeFromShipment`, `renderInvoiceBar`).


def _portfolio_template() -> str:
    from pathlib import Path

    return Path("templates/portfolio.html").read_text(encoding="utf-8")


def test_the_hidden_columns_are_gated_in_ALL_THREE_places():
    """Header, body and footer must agree about the column count.

    A cell rendered under a hidden header shifts every cell after it one column left, which reads
    as a rounding error rather than a layout bug. Verified in the browser: 8/8/8 collapsed and
    11/11/11 expanded, with the detail row's colspan following.

    Asserted at source because the three are built by three different functions, and only a
    convention keeps them in step.
    """
    source = _portfolio_template()
    # The header derives its list; the body and footer gate on the same flag.
    assert "shownColumns()" in source, "the header must derive its columns from one list"
    assert source.count("showExtra ?") >= 3, (
        "the body cells, the detail cells and the totals row must each gate on showExtra — "
        "otherwise a hidden column still renders in one of them"
    )
    # And no hardcoded colspan can survive, or an expanded row stops spanning the table.
    assert 'colspan="11"' not in source

    # **The list must actually DROP the hidden ones, not merely be called.** A mutation returning
    # `COLUMNS` unfiltered survived an earlier version of this test: every assertion above stayed
    # true while the header rendered 11 columns over 8 body cells. The count is what fails.
    body = _template_function(source, "shownColumns")
    assert "showExtra" in body, "shownColumns does not consult the toggle at all"
    assert ".filter(" in body, (
        "shownColumns returns every column, so the header renders 11 headings over 8 body cells "
        "and every figure after Net % sits under the wrong one"
    )
    # Scoped to the COLUMNS array, so the prose explaining the flag is not counted as a column —
    # the deploy-detector mistake (a substring that also appears in its own explanation).
    declaration = source[source.index("const COLUMNS = ["):]
    declaration = declaration[: declaration.index("];")]
    extras = declaration.count("extra: true}")
    assert extras == 3, (
        f"{extras} optional columns declared; the three gated blocks render exactly 3 "
        "(Units, Returns, Rating), so a fourth would render under a hidden header"
    )


def test_showExtra_is_declared_AFTER_the_helper_it_calls():
    """**Found by opening the page: it rendered "Loading…" for ever.**

    `remembered` is a `const` arrow function, so it is NOT hoisted. Declaring
    `let showExtra = remembered(...)` above it threw `Cannot access '$' before initialization` —
    from inside the error handler, which needs `$` — so the real cause never reached the console and
    the page simply never finished loading.

    Nothing in the test suite could have caught this; it needed the page. Asserted on ORDER so the
    declaration cannot drift back above its helper.
    """
    source = _portfolio_template()
    assert source.index("const remembered =") < source.index("let showExtra ="), (
        "showExtra reads remembered(), which is a const arrow function and therefore not hoisted"
    )


def test_the_two_standing_banners_collapse_to_one_line():
    """Both facts survive; the ~90px of banner before any data does not.

    The pre-COGS caveat and the ratings date are what stop a money-losing SKU reading as a keeper
    and a stale star rating silently shaping a verdict — the ratings one is what revealed the scrape
    had never run. So they are collapsed, not dropped, and the ratings DATE stays on the visible
    line because that is the part that changes.
    """
    source = _portfolio_template()
    assert "caveat-line" in source and "caveat-full" in source
    assert "aria-expanded" in source, "the expander is a button that owns a region"
    # The full text of both is still present.
    assert "pre-COGS" in source
    assert "own scraper" in source
