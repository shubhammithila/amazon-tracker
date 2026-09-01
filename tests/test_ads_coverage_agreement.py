"""The screen and the server must reach the same conclusion about a window.

Reported as the Ads tab showing Rs 0 with **both** a green *"inside the 2026-07-04 → 2026-08-31 daily
data — no fetch needed, summed instantly"* and an info banner reading *"Nothing fetched for
2026-08-25 → 2026-08-31"*, above five zeroed KPIs.

Measured cause, on production: the nightly run stored 482,578 Sponsored Products rows across 59 days
and Amazon then rate-limited the Sponsored Brands report, storing **0**. So `sp` held 59 days and `sb`
held 7, `daily_range_complete` correctly refused every window outside those 7 — and the template drew
its "instant" dot from `daily_coverage`, a span MERGED across products, which said 59 days for
everything:

     7d 2026-08-25..2026-08-31  dot=True  server=False
    14d 2026-08-18..2026-08-31  dot=True  server=False
    30d 2026-08-02..2026-08-31  dot=True  server=False

One rule computed in two places. These tests assert the INVARIANT — the two answers are the same
computation — rather than the symptom, because "the dot is right for a 7-day preset" would pass again
the next time a product falls behind.
"""
import pytest

from app.ads import repository

pytestmark = pytest.mark.regression


def _row(entity_id, day, *, spend=10.0, product="sp"):
    """One DAILY report row, in the shape Amazon returns for that ad product."""
    row = {
        "keywordId": entity_id, "date": day, "campaignId": "C1", "adGroupId": "A1",
        "keyword": f"kw-{entity_id}", "matchType": "EXACT", "keywordBid": 10.0,
        "impressions": 100, "clicks": 10, "cost": spend,
    }
    if product == "sp":
        row["sales7d"] = spend * 2
        row["purchases7d"] = 1
    else:
        row["sales"] = spend * 2
        row["purchases"] = 1
    return row


def _days(start_day, count):
    from datetime import date, timedelta
    first = date.fromisoformat(start_day)
    return [(first + timedelta(days=offset)).isoformat() for offset in range(count)]


async def _seed_production_shape(db):
    """The exact shape measured on the box: `sp` for 2026-07-04..2026-08-31, `sb` for 08-24..08-30."""
    for day in _days("2026-07-04", 59):
        await repository.save_daily(db, [_row("K1", day, product="sp")], ad_product="sp")
    for day in _days("2026-08-24", 7):
        await repository.save_daily(db, [_row("K2", day, product="sb")], ad_product="sb")


# ─── The invariant ───────────────────────────────────────────────────────────


async def test_the_screens_dot_and_the_servers_refusal_are_one_computation(db):
    """**The test that reproduces the screenshot.**

    `range_completeness` is what the screen now reads and what `daily_range_complete` returns, so
    there is no second implementation left to disagree. Asserted over the presets AND over the merged
    span the old code trusted, so the specific lie is pinned rather than only the general property.
    """
    await _seed_production_shape(db)

    merged = await repository.daily_coverage(db)
    assert merged == ("2026-07-04", "2026-08-31"), (
        "the merged span is what the template used to trust; it must still report the union, "
        "because the fix is that nothing GATES on it"
    )

    for start, end in [("2026-08-25", "2026-08-31"),      # the 7d preset from the screenshot
                       ("2026-08-18", "2026-08-31"),      # 14d
                       ("2026-08-02", "2026-08-31")]:     # 30d
        answer = await repository.range_completeness(db, start, end)
        gate = await repository.daily_range_complete(db, start, end)
        assert answer["complete"] == gate, f"{start}..{end}: two answers for one question"
        # The merged span covers all three, which is exactly why trusting it was wrong.
        assert merged[0] <= start and merged[1] >= end
        assert not answer["complete"], (
            f"{start}..{end} was called summable while Sponsored Brands holds only 7 days"
        )


async def test_the_answer_names_which_product_is_short_and_by_how_much(db):
    """A boolean sent the owner to press Refresh on a window whose Sponsored Products half was
    complete and current. The count is what makes it actionable — 25 missing days and 1 missing day
    call for different reactions."""
    await _seed_production_shape(db)

    answer = await repository.range_completeness(db, "2026-08-02", "2026-08-31")
    assert answer["missing_count"] == {"sb": 23}, (
        f"the gap is not attributed to Sponsored Brands: {answer['missing_count']}"
    )
    assert "sp" not in answer["missing_count"], "Sponsored Products is complete and must not be blamed"
    assert answer["held"]["sb"] == ["2026-08-24", "2026-08-30"], (
        "the answer cannot say what Sponsored Brands DOES hold, so the screen cannot either"
    )
    assert answer["held"]["sp"] == ["2026-07-04", "2026-08-31"]
    assert answer["products"] == ["sb", "sp"]


async def test_a_window_both_products_cover_is_complete(db):
    """The other half: the guard must still say yes when it should."""
    await _seed_production_shape(db)
    answer = await repository.range_completeness(db, "2026-08-24", "2026-08-30")
    assert answer["complete"], f"a window both products hold was refused: {answer}"
    assert answer["missing_count"] == {}
    assert await repository.daily_range_complete(db, "2026-08-24", "2026-08-30")


async def test_an_interior_gap_is_incomplete_even_though_the_span_covers_it(db):
    """**The case per-product SPANS could not see, which is why the answer is per-DAY.**

    A four-report night can lose the middle chunk rather than the end — Amazon caps a report at 31
    days, so 60 days is two chunks per product and either can throttle. A span-based check would call
    this complete and `sum_daily` would quietly understate the spend a bid rule then acts on.
    """
    for day in ["2026-08-01", "2026-08-02", "2026-08-05"]:
        await repository.save_daily(db, [_row("K1", day)], ad_product="sp")

    spans = await repository.daily_coverage_by_product(db)
    assert spans["sp"] == ("2026-08-01", "2026-08-05"), "the span covers the whole range"

    answer = await repository.range_completeness(db, "2026-08-01", "2026-08-05")
    assert not answer["complete"], "an interior gap was called summable"
    assert answer["missing_count"] == {"sp": 2}
    assert answer["missing"]["sp"] == ["2026-08-03", "2026-08-04"], "the gap is not named"


async def test_the_named_days_are_capped_but_the_count_stays_exact(db):
    """A 60-day range can be short 53 days. The screen needs a sentence, not a list — and a count
    that was capped along with the list would understate the gap."""
    await repository.save_daily(db, [_row("K1", "2026-08-31")], ad_product="sp")

    answer = await repository.range_completeness(db, "2026-07-03", "2026-08-31")
    assert answer["missing_count"]["sp"] == 59, "the count is not the true number of missing days"
    assert len(answer["missing"]["sp"]) == repository.MISSING_DAYS_SHOWN
    assert answer["missing"]["sp"][0] == "2026-07-03", "the named days are not the earliest ones"


async def test_an_empty_table_is_incomplete_rather_than_vacuously_complete(db):
    """"We hold every day for every product" is trivially true of no products, and a completeness
    check that answered yes would let `sum_daily` return nothing and the screen report Rs 0 as a
    fact rather than as an absence."""
    answer = await repository.range_completeness(db, "2026-08-01", "2026-08-07")
    assert not answer["complete"]
    assert answer["products"] == []
    assert not await repository.daily_range_complete(db, "2026-08-01", "2026-08-07")


async def test_a_third_ad_product_needs_no_change_here(db):
    """Sponsored Display is a plausible third, and the product list is derived from the data.

    Hardcoding `("sp", "sb")` would silently exclude it — a whole product's spend missing from a
    completeness check that reported yes.
    """
    for day in _days("2026-08-01", 3):
        await repository.save_daily(db, [_row("K1", day)], ad_product="sp")
    await repository.save_daily(db, [_row("K9", "2026-08-01", product="sb")], ad_product="sd")

    assert await repository.daily_products(db) == ["sd", "sp"]
    answer = await repository.range_completeness(db, "2026-08-01", "2026-08-03")
    assert answer["missing_count"] == {"sd": 2}, (
        f"a third ad product was not considered: {answer['missing_count']}"
    )


# ─── The run record, which must outlive the process ──────────────────────────


async def test_a_partial_night_is_recorded_as_partial_with_the_counts_apart(db):
    """**The exact state that read as Rs 0, stored so it can explain itself.**

    `sp_rows` and `sb_rows` are separate columns rather than one total, because `0 SB` beside
    `482,578 SP` IS the finding — a single `rows_stored` of 482,578 reads as a completely successful
    night, which is what the log line very nearly said.
    """
    await repository.record_refresh(
        db, window_start="2026-07-03", window_end="2026-08-31",
        sp_rows=482578, sb_rows=0, campaigns=29, ad_groups=1833,
        sb_error="Amazon is rate-limiting report creation for this report type (Sponsored Brands).",
    )

    stored = await repository.last_refresh(db)
    assert stored["status"] == "partial", (
        f"a throttled Sponsored Brands report was recorded as {stored['status']!r} — 'failed' would "
        f"be wrong when 482,578 Sponsored Products rows landed, and 'done' would hide it"
    )
    assert stored["sp_rows"] == 482578 and stored["sb_rows"] == 0
    assert "rate-limiting" in stored["sb_error"]
    assert stored["error"] == "", "the whole run did not fail, so `error` must stay empty"


async def test_a_whole_run_failing_is_failed_not_partial(db):
    """The distinction the screen renders differently: nothing moved, versus most things moved."""
    await repository.record_refresh(
        db, window_start="2026-08-01", window_end="2026-08-07", error="LWA invalid_client",
    )
    stored = await repository.last_refresh(db)
    assert stored["status"] == "failed"
    assert stored["sp_rows"] == 0


async def test_a_clean_run_is_done(db):
    await repository.record_refresh(
        db, window_start="2026-08-01", window_end="2026-08-07", sp_rows=100, sb_rows=50,
    )
    stored = await repository.last_refresh(db)
    assert stored["status"] == "done"
    assert stored["sb_error"] == ""


async def test_the_newest_run_is_the_one_reported(db):
    """A throttled night followed by a good morning must not still read as throttled."""
    await repository.record_refresh(db, window_start="2026-08-01", window_end="2026-08-07",
                                   sp_rows=1, sb_error="throttled")
    await repository.record_refresh(db, window_start="2026-08-02", window_end="2026-08-08",
                                   sp_rows=2, sb_rows=2)
    stored = await repository.last_refresh(db)
    assert stored["status"] == "done" and stored["window_start"] == "2026-08-02"


async def test_never_having_run_is_none_rather_than_an_empty_record(db):
    """So the screen can tell "never fetched" from "fetched and failed" — different sentences."""
    assert await repository.last_refresh(db) is None


async def test_the_record_is_json_safe(db):
    """A `datetime` reaching `JSONResponse` is a 500, and this project has shipped that twice."""
    import json

    await repository.record_refresh(db, window_start="2026-08-01", window_end="2026-08-07")
    json.dumps(await repository.last_refresh(db))     # raises if a datetime survived


def test_the_run_record_is_written_in_the_finally_block():
    """**A SOURCE assertion, and it guards the case that most needs explaining.**

    `except Exception` does not catch `asyncio.CancelledError`, and the refresh runs as
    fire-and-forget — exactly the thing cancelled at shutdown. A record written on the success path
    would be absent for precisely the runs someone later asks about. The same reasoning already makes
    `STATE["running"] = False` live in `finally`, where a mutation proved it load-bearing.
    """
    import inspect

    from app.ads import refresh

    source = inspect.getsource(refresh.run)
    finally_block = source[source.rindex("finally:"):]
    assert "record_refresh" in finally_block, (
        "the run record is written outside `finally`, so a cancelled or crashed refresh leaves "
        "nothing to explain itself"
    )


# ─── The template no longer computes it ──────────────────────────────────────


def test_the_merged_span_gates_nothing_on_screen():
    """**A SOURCE assertion, because no runtime test can watch a template read the wrong field.**

    The same honesty `test_ads_one_source.py` uses for `daily=True`: the bug was a client-side
    computation agreeing with itself, so the only durable guard is that the computation is gone and
    the dot comes from the server's answer.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from test_ads_api import _code_only

    source = Path("templates/ads.html").read_text(encoding="utf-8")

    assert "function insideDailyCoverage(" not in source, (
        "the client recomputes summability from a product-merged span again"
    )
    # **Comments stripped before asserting an absence.** The comment above `renderWindow` explains
    # this very bug and names `data.daily_coverage` in doing so; a raw substring check would fail on
    # the explanation and, worse, could be "fixed" by deleting the reasoning.
    code = _code_only(source)
    window_render = code[code.index("function renderWindow("):
                         code.index("function gapShort(")]
    assert "preset_completeness" in window_render, (
        "the preset dots are not driven by the server's own completeness answer"
    )
    assert "daily_coverage" not in window_render, (
        "the merged span is back in the code that decides the dot"
    )
    # The ranges come from the server too, so the browser clock cannot disagree about "last 7 days".
    assert "entry.start" in window_render and "entry.end" in window_render, (
        "the preset ranges are computed in the browser, which reintroduces the IST/UTC seam"
    )


def test_a_one_product_view_does_not_call_its_own_window_unsummable():
    """**Found in a browser, three lines from the fix for the same class of defect.**

    In a one-product view the server gates on that product's days alone, so `cached` can be true while
    the all-product `window_completeness.complete` is false. The note read the latter and printed
    "not summable — Sponsored Brands is missing 1 of these days" directly above a banner reading
    "Summed from the stored daily rows" and a table showing Rs 35,000 of real spend. Two claims about
    one window — exactly what this change exists to remove.

    Asserted on the source because the contradiction is between two renderers reading different
    fields, which no single runtime assertion observes.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from test_ads_api import _code_only, _js_function

    code = _code_only(Path("templates/ads.html").read_text(encoding="utf-8"))

    note = _js_function(code, "renderWindowNote")
    assert "data.cached" in note, (
        "the window note judges summability from the all-product answer, so a one-product view "
        "reads 'not summable' above figures that were summed"
    )

    banners = _js_function(code, "renderBanners")
    assert "!data.ad_product" in banners, (
        "the 'cannot be summed' banner is shown in a one-product view, beside the numbers it denies"
    )
