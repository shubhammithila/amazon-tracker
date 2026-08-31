"""The bid change log: every individual change, searchable, with a 12-month retention.

A per-RUN history already existed (`load_runs`, the history panel, undo). This is the other question —
**"what has happened to THIS keyword"** — over the same `ads_mutation` rows, so there is no second
source of truth and nothing new to store.
"""
from datetime import datetime, timedelta

import pytest

from app.ads import repository
from app.models import AdsMutation

pytestmark = pytest.mark.regression


async def _change(db, entity_id, *, text, old, new, when, status="applied",
                  rule="spend > 100 -> bid -10%", run=None):
    """One ledger row.

    `run_id` defaults to something unique per (entity, timestamp), because
    `idx_ads_mutation_run_entity` is UNIQUE on `(run_id, entity_id)` — one row per entity per run, by
    design. Two changes to the same keyword are therefore two RUNS, which is exactly what happens in
    reality and what the bid-path test depends on.
    """
    db.add(AdsMutation(run_id=run or f"run-{entity_id}-{when.isoformat()}", entity_id=entity_id,
                       entity_type="keyword", writer="keyword",
                       text=text, old_bid=old, new_bid=new, status=status, rule_summary=rule,
                       campaign_id="c1", ad_group_id="g1", created_at=when))
    await db.commit()


async def test_the_log_returns_every_change_newest_first(db):
    now = datetime(2026, 8, 31, 12, 0)
    await _change(db, "K1", text="makhana", old=18.75, new=16.88, when=now - timedelta(hours=2))
    await _change(db, "K2", text="roasted chana", old=13.01, new=11.71, when=now)

    log = await repository.load_bid_log(db)
    assert log["total"] == 2
    assert [r["text"] for r in log["rows"]] == ["roasted chana", "makhana"]
    first = log["rows"][0]
    assert first["old_bid"] == 13.01 and first["new_bid"] == 11.71
    assert first["rule"] == "spend > 100 -> bid -10%"
    assert first["status"] == "applied"
    # The IST day travels, because that is the day the owner thinks in and the column stores UTC.
    assert first["day"] == "2026-08-31"


async def test_searching_by_keyword_text_finds_its_whole_history(db):
    """The "what happened to this keyword" question, which the per-run history cannot answer."""
    now = datetime(2026, 8, 31, 12, 0)
    await _change(db, "K1", text="makhana", old=18.75, new=16.88, when=now - timedelta(days=2))
    await _change(db, "K1", text="makhana", old=16.88, new=18.57, when=now)
    await _change(db, "K2", text="roasted chana", old=13.01, new=11.71, when=now)

    log = await repository.load_bid_log(db, search="makhana")
    assert log["total"] == 2
    assert all(r["text"] == "makhana" for r in log["rows"])
    # Case-insensitive and partial: the owner types what he remembers, not an exact string.
    assert (await repository.load_bid_log(db, search="MAKH"))["total"] == 2


async def test_searching_also_matches_an_entity_id(db):
    """A support question arrives as an id, not as a keyword — "why did 155301615480093 move"."""
    await _change(db, "155301615480093", text="govind bhog chawal", old=13.86, new=15.25,
                  when=datetime(2026, 8, 31, 12, 0))
    assert (await repository.load_bid_log(db, search="1553016"))["total"] == 1


async def test_filtering_by_date_range_and_status(db):
    now = datetime(2026, 8, 31, 12, 0)
    await _change(db, "K1", text="a", old=10.0, new=11.0, when=datetime(2026, 8, 20, 9, 0))
    await _change(db, "K2", text="b", old=10.0, new=11.0, when=now, status="failed")
    await _change(db, "K3", text="c", old=10.0, new=11.0, when=now)

    assert (await repository.load_bid_log(db, start="2026-08-31"))["total"] == 2
    assert (await repository.load_bid_log(db, end="2026-08-25"))["total"] == 1
    assert (await repository.load_bid_log(db, status="failed"))["total"] == 1
    assert (await repository.load_bid_log(db, status="applied"))["total"] == 2


async def test_the_end_of_the_range_includes_that_whole_day(db):
    """`end=2026-08-31` must include changes made DURING the 31st, not stop at its midnight.

    The obvious `created_at <= end` reads as inclusive and silently drops a whole day of rows — the
    kind of off-by-one that makes a log look like it is missing data.
    """
    await _change(db, "K1", text="a", old=10.0, new=11.0, when=datetime(2026, 8, 31, 16, 45))
    assert (await repository.load_bid_log(db, end="2026-08-31"))["total"] == 1


async def test_the_log_shows_the_bid_path_for_one_keyword(db):
    """**A compounding mistake is a SHAPE, not a row.**

    13.86 -> 15.25 -> 16.78 in one day is the thing the repeat guard exists to prevent, and it is only
    visible when the changes are read in sequence. Ascending for this view, unlike the newest-first
    list, because a path reads forwards.
    """
    base = datetime(2026, 8, 31, 9, 0)
    for index, (old, new) in enumerate([(13.86, 15.25), (15.25, 16.78), (16.78, 15.10)]):
        await _change(db, "K1", text="makhana", old=old, new=new,
                      when=base + timedelta(hours=index))

    log = await repository.load_bid_log(db, search="makhana", ascending=True)
    path = [(r["old_bid"], r["new_bid"]) for r in log["rows"]]
    assert path == [(13.86, 15.25), (15.25, 16.78), (16.78, 15.10)]
    # Each change starts where the previous one ended — the property that makes the path meaningful.
    for earlier, later in zip(path, path[1:]):
        assert earlier[1] == later[0], f"the path is broken at {earlier} -> {later}"


async def test_the_log_is_paged_because_a_year_of_runs_is_large(db):
    """At three 1,000-row runs a day this table holds a million rows a year. Unbounded SELECTs are how
    a page that used to load becomes a page that times out."""
    now = datetime(2026, 8, 31, 12, 0)
    for index in range(12):
        await _change(db, f"K{index}", text=f"kw{index}", old=10.0, new=11.0,
                      when=now - timedelta(minutes=index))

    page = await repository.load_bid_log(db, limit=5)
    assert len(page["rows"]) == 5
    assert page["total"] == 12, "the total must count ALL matches, not the page"
    second = await repository.load_bid_log(db, limit=5, offset=5)
    assert {r["entity_id"] for r in page["rows"]} & {r["entity_id"] for r in second["rows"]} == set()


async def test_a_failed_change_carries_amazons_own_message(db):
    """Amazon's refusals name the cause — they are how the bid floor and the 31-day cap were both
    found — so the log surfaces them verbatim rather than replacing them with "failed"."""
    db.add(AdsMutation(run_id="r1", entity_id="K1", entity_type="keyword", writer="keyword",
                       text="makhana", old_bid=10.0, new_bid=0.5, status="failed",
                       error="rangeError: bid is lower than the minimum allowed by the marketplace",
                       rule_summary="r", created_at=datetime(2026, 8, 31, 12, 0)))
    await db.commit()

    row = (await repository.load_bid_log(db))["rows"][0]
    assert row["status"] == "failed"
    assert "minimum allowed" in row["error"]


# ─── Retention ────────────────────────────────────────────────────────────────


async def test_the_ledger_keeps_twelve_months(db):
    """**12 months, not 1 — and that is a decision against the first instinct.**

    Measured: ~0.34 KB a row, so 37 MB/year at one 300-row run a day. This table is the undo chain and
    the audit trail, it is the source of the true current bid, and **unlike the daily report rows it is
    NOT refetchable** — Amazon will not say what we set a bid to in July. So it is kept long and
    bounded, rather than kept short.
    """
    assert repository.MUTATION_RETENTION_DAYS == 365

    now = datetime(2026, 8, 31, 12, 0)
    await _change(db, "OLD", text="old", old=10.0, new=11.0, when=now - timedelta(days=400))
    await _change(db, "NEW", text="new", old=10.0, new=11.0, when=now - timedelta(days=300))

    removed = await repository.purge_mutations(db, today=now.date())
    assert removed == 1, f"expected the 400-day-old row gone, removed {removed}"
    left = await repository.load_bid_log(db)
    assert [r["text"] for r in left["rows"]] == ["new"]


async def test_the_purge_keeps_a_row_on_the_boundary_day(db):
    """An off-by-one here silently deletes a day of audit trail every night."""
    now = datetime(2026, 8, 31, 12, 0)
    await _change(db, "EDGE", text="edge", old=10.0, new=11.0, when=now - timedelta(days=364))
    assert await repository.purge_mutations(db, today=now.date()) == 0
    assert (await repository.load_bid_log(db))["total"] == 1


def test_the_purge_runs_in_the_nightly_sweep():
    """A retention policy called only from a success path is a side effect, not a policy — the lesson
    `purge_daily` already records."""
    from pathlib import Path

    source = (Path(__file__).parent.parent / "app" / "scheduler.py").read_text(encoding="utf-8")
    assert "purge_mutations" in source, "the ledger is never purged, so it grows without bound"


# ─── The routes ───────────────────────────────────────────────────────────────


async def test_the_route_returns_the_log_and_its_retention(auth_client, db):
    """`retention_days` travels so the screen can say how far back it goes rather than implying
    for ever."""
    await _change(db, "K1", text="makhana", old=18.75, new=16.88,
                  when=datetime(2026, 8, 31, 12, 0))
    body = (await auth_client.get("/ads/bid-log")).json()
    assert body["total"] == 1
    assert body["rows"][0]["text"] == "makhana"
    assert body["retention_days"] == 365


async def test_a_malformed_date_is_a_400_not_a_500(auth_client, db):
    """The dates arrive from a query string, so a typo must be refused with a reason."""
    response = await auth_client.get("/ads/bid-log?start=not-a-date")
    assert response.status_code == 400
    assert "date" in response.json()["error"].lower()


async def test_the_log_downloads_as_a_spreadsheet(auth_client, db):
    """**`build_portfolio_xlsx`, not `build_simple_xlsx`.**

    The simple builder appends a totals row that `int()`s every trailing column, and this log's
    trailing columns hold a status word, a rule sentence and Amazon's error text — so it would raise
    `ValueError`. Summing bids would be meaningless anyway: a bid is a rate, not a quantity.
    """
    await _change(db, "K1", text="makhana", old=18.75, new=16.88,
                  when=datetime(2026, 8, 31, 12, 0))
    response = await auth_client.get("/ads/bid-log.xlsx")
    assert response.status_code == 200
    assert "spreadsheetml" in response.headers["content-type"]
    assert response.content[:2] == b"PK", "not a real xlsx"
    assert len(response.content) > 4000


async def test_the_log_is_gated_like_every_other_ads_route(client, db):
    """It shows what was spent and on which keyword. `ads` is the area that can spend money."""
    for path in ("/ads/bid-log", "/ads/bid-log.xlsx"):
        response = await client.get(path, follow_redirects=False)
        assert response.status_code in (302, 303, 401, 403), path


# ─── The panel ────────────────────────────────────────────────────────────────


def _template() -> str:
    from pathlib import Path

    return (Path(__file__).parent.parent / "templates" / "ads.html").read_text(encoding="utf-8")


def test_the_log_panel_offers_all_four_things_asked_for():
    """Search, date and status filters, the bid path, and an Excel download."""
    source = _template()
    assert 'id="bidlog-panel"' in source, "there is no log panel"
    assert 'id="bidlog-btn"' in source, "there is no way to open it"
    assert "function renderBidLog(" in source
    for control in ("bidlog-search", "bidlog-from", "bidlog-to", "bidlog-status"):
        assert f'id="{control}"' in source, f"{control} is missing"
    assert "data-bidpath" in source, "a keyword cannot be clicked for its bid path"
    assert "/ads/bid-log.xlsx" in source, "there is no Excel download"


def test_the_bid_path_reads_forwards():
    """**A compounding mistake is a SHAPE**, and a shape only reads in order.

    The list is newest-first, which is right for "what happened lately"; clicking one keyword switches
    to ascending, which is right for 13.86 -> 15.25 -> 16.78.
    """
    source = _template()
    assert "bidLogAscending" in source, "there is no ascending view"
    assert 'query.set("ascending", "true")' in source, "the flag never reaches the server"


def test_the_panel_uses_delegated_listeners_and_escapes_the_keyword():
    """Keyword text comes from Amazon, so an inline handler built out of it is an injection.

    The rule the whole template follows, and the log is the one panel that renders that text into a
    clickable control.
    """
    source = _template()
    assert '$("bidlog-panel").addEventListener' in source, "the panel is not wired up"
    assert 'data-bidpath="${esc(' in source, "the keyword reaches an attribute unescaped"
    # No inline handler anywhere in the panel's markup.
    start = source.index("function renderBidLog(")
    body = source[start:source.index("async function renderHistory(")]
    assert "onclick=" not in body and "onchange=" not in body


def test_the_panel_says_how_far_back_the_log_goes():
    """Otherwise it implies for ever, and it is purged at 12 months."""
    source = _template()
    assert "retention_days" in source, "the screen does not say what the retention is"


def test_the_log_is_a_button_not_a_span():
    """It does something, so it must be reachable from the keyboard and announced as pressable."""
    source = _template()
    assert '<button class="link-btn"' in source
    assert ".link-btn{" in source, "the control has no style"


def test_the_bid_path_search_term_is_passed_not_written_into_the_input():
    """**Found in a browser.** Clicking a keyword wrote the term into the input and let the render read
    it back — which works only because the render happens to rebuild that input afterwards.

    That sequence reads as a coincidence and breaks the moment anything else re-renders in between, so
    the term is passed as an argument and the input is rendered WITH it. The screen and the query then
    cannot disagree about what is being shown.
    """
    source = _template()
    assert "renderBidLog({search: path.dataset.bidpath})" in source, (
        "the search term is written into the input and read back rather than passed"
    )
    assert "options.search !== undefined" in source, (
        "renderBidLog cannot accept a search term, so the caller has to mutate the DOM first"
    )
