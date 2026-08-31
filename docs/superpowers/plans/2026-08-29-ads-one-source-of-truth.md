# Ads Tab: One Source of Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the Ads tab losing 28% of spend (₹1,26,328 of Sponsored Brands) on any window that was not fetched exactly, by making per-day rows the only source of truth — then extend history to 60 days and add four preview improvements.

**Architecture:** `refresh.run` currently stores one payload at two grains but writes Sponsored Brands only to the per-window table `ads_performance`. The read side prefers that table and falls back to summing `ads_performance_daily`, which holds SP rows alone — so a derived window silently drops SB. We write SB daily too, delete `ads_performance` entirely, and make every window a `GROUP BY` over daily rows. One code path means two figures cannot disagree.

**Tech Stack:** FastAPI (async), SQLAlchemy 2.0 async, aiosqlite, Alembic with `batch_alter_table`, APScheduler, pytest + pytest-asyncio, vanilla JS in Jinja2 templates.

**Spec:** `docs/superpowers/specs/2026-08-29-ads-one-source-of-truth-design.md`

## Global Constraints

- **Tests:** `venv/Scripts/python -m pytest -q` from the repo root. 1847 currently pass; every task must leave the whole suite green.
- **Never `pip install -r requirements.txt` on production.** Python 3.14 there has newer libraries than the pins; `-r` tries to downgrade pandas and OOM-kills the box.
- **Every new migration MUST add a newest-first branch to the baseline detector** in `deploy/update-ec2.sh` (~line 356), keyed on something the revision changes. A stale detector has stamped production backwards and failed two deploys. `tests/test_schema_migrations.py` runs that heredoc against a freshly-migrated database.
- **Upserts are SELECT-then-UPDATE-or-INSERT** everywhere except `repository.save_daily`, which is delete-then-bulk-insert for a measured 62× reason. No dialect-specific `ON CONFLICT` — the app must stay PostgreSQL-portable.
- **No inline event handlers in templates.** Keyword text and campaign names come from Amazon; use delegated listeners, as `templates/ads.html` already does.
- **`app/ads/logic.py` is pure** — no DB, no network. Keep it that way.
- **Nothing in the nightly job may apply a bid.** A test asserts the scheduler cannot reach `apply_bids`, `plan_run` or `open_run`.
- **Current Alembic head:** `e2b7d94c15af`. New revisions chain from it.
- **Timestamps** use `datetime.utcnow()` to match the existing code in these modules.

---

## File Structure

**Modify:**
- `app/ads/repository.py` — scope `save_daily`'s delete by `(day, ad_product)`; make `sum_daily` the only read path; delete `load_performance`, `purge_windows`, `windows_available`, `WINDOW_RETENTION_COUNT`; raise `DAILY_RETENTION_DAYS` to 60.
- `app/ads/refresh.py` — store SB daily; commit per chunk; report a throttled chunk without failing the run.
- `app/ads/reports.py` — add an `on_chunk` callback so a chunk can be stored as it lands.
- `app/ads/spapi_ads.py` — add `fetch_bid_recommendations`.
- `app/routers/ads.py` — read only via `sum_daily`; return `suggested_bid` on preview rows.
- `app/scheduler.py` — nightly window 7 → 60 days.
- `app/models.py` — drop the `AdsPerformance` model.
- `templates/ads.html` — top Apply/Cancel bar, select-all toggle, ad group column, suggested bid column.
- `deploy/update-ec2.sh` — detector branch; drop `ads_performance` from the required-tables check; `KEEP_BACKUPS` 5 → 3.
- `CLAUDE.md` — record the defect and the decisions.

**Create:**
- `alembic/versions/<rev>_drop_ads_performance.py`
- `tests/test_ads_one_source.py` — the invariants that were violated.
- `tests/test_ads_bid_recommendations.py`

**Delete:** the `ads_performance` table and every reference to it.

**Already correct — do NOT re-implement:**
- **Presets already end yesterday.** `templates/ads.html:402` (`presetRange`) subtracts a day, and
  `settledDate()` at :511 backs the custom picker. The spec's "presets end yesterday, today on
  demand" is satisfied; verified by reading the code, not assumed.
- **`localDate` is already used everywhere** in this template, and `tests/test_local_dates.py`
  enforces no `toISOString` across every template.
- **`daily_range_complete` already checks every day** in a range rather than the endpoints, which is
  what makes a partial nightly scrape safe. Task 3 depends on this; it needs no change.
- **`attach_names` already resolves `ad_group_name`** in one query. Task 5 renders it; the data has
  been there all along.
- **`save_daily` already accepts and stores `ad_product`.** Only its DELETE scope is wrong (Task 1),
  and only the SB call site is missing (Task 1).

---

## Task 1: SB daily rows, and the delete that would have destroyed SP

**Files:**
- Modify: `app/ads/repository.py:377-443` (`save_daily`)
- Modify: `app/ads/refresh.py:268-281` (the SB block)
- Test: `tests/test_ads_one_source.py` (create)

**Interfaces:**
- Consumes: `repository.save_daily(db, rows, *, ad_product="sp") -> int` (exists), `reports.fetch_targeting(start, end, *, daily=False, ad_product="sp", sleep=None, on_progress=None) -> list[dict]` (exists).
- Produces: `save_daily` scoped by `(day, ad_product)`; SB rows present in `ads_performance_daily`.

**Why this is first:** `save_daily` deletes by **day alone**. Calling it for SB after SP for the same days would delete every SP row just stored — the current bug inverted, and worse, since SP is 72% of spend. The scoping fix must land before anything calls it twice.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ads_one_source.py`:

```python
"""The invariants that were violated while every existing test passed.

Reported as "22-28 is showing 4.44 lakh spend and 22-29 is showing 3.3 lakh" — a superset reporting
LESS than its subset. Measured cause: `refresh.run` writes Sponsored Brands to the per-window table
only, so any window summed from daily rows drops SB entirely. Rs 1,26,328 vanished, and a bid rule
on such a window found 743 changes with 0 SB rows where the stored window found 1,005 with 296.

These tests assert INVARIANTS rather than descriptions. "SB is stored daily" would pass again the
next time a product is added; "a superset can never report less than its subset" would not.
"""
import pytest

from app.ads import repository

pytestmark = pytest.mark.regression


def _report_row(entity_id, day, *, spend, product="sp", match_type="EXACT", bid=10.0):
    """One DAILY report row in the shape Amazon returns.

    `cost` and `sales7d`/`sales` differ per ad product — `logic.metrics_for` reads both — so the
    keys here mirror what the real report carries for that product.
    """
    row = {
        "keywordId": entity_id,
        "date": day,
        "campaignId": "C1",
        "adGroupId": "A1",
        "keyword": f"kw-{entity_id}",
        "matchType": match_type,
        "keywordBid": bid,
        "impressions": 100,
        "clicks": 10,
        "cost": spend,
    }
    if product == "sp":
        row["sales7d"] = spend * 2
        row["purchases7d"] = 1
    else:
        row["sales"] = spend * 2
        row["purchases"] = 1
    return row


async def test_storing_sb_for_a_day_does_not_delete_that_days_sp_rows(db):
    """**`save_daily` deleted by DAY alone, and that would have destroyed the SP data.**

    It is delete-then-bulk-insert — the deliberate 62x deviation from the house upsert — so a second
    call for the same days wipes the first product's rows. Storing SB after SP would have left
    SB-only days: the reported bug inverted, and worse, because SP is 72% of spend.

    The function's own docstring already claims this property one dimension down ("scoped per DAY so
    refetching a 7-day window cannot disturb the other 23 days"); until now only one product ever
    reached it.
    """
    await repository.save_daily(db, [_report_row("SP1", "2026-08-22", spend=100.0)],
                                ad_product="sp")
    await repository.save_daily(db, [_report_row("SB1", "2026-08-22", spend=50.0, product="sb")],
                                ad_product="sb")

    rows = await repository.sum_daily(db, "2026-08-22", "2026-08-22")
    by_product = {r["ad_product"]: r for r in rows}
    assert "sp" in by_product, "storing SB deleted the SP rows for the same day"
    assert "sb" in by_product, "the SB rows were not stored"
    assert by_product["sp"]["spend"] == 100.0
    assert by_product["sb"]["spend"] == 50.0


async def test_refetching_one_product_replaces_only_its_own_rows(db):
    """The other half: a re-fetch must still REPLACE, not accumulate.

    Delete-then-insert exists because a day's rows are wholly superseded by a refetch. Scoping by
    product must not turn that into an append, or a second nightly run would double the day's spend.
    """
    await repository.save_daily(db, [_report_row("SP1", "2026-08-22", spend=100.0)],
                                ad_product="sp")
    await repository.save_daily(db, [_report_row("SP1", "2026-08-22", spend=140.0)],
                                ad_product="sp")

    rows = await repository.sum_daily(db, "2026-08-22", "2026-08-22")
    assert len(rows) == 1, f"the refetch accumulated instead of replacing: {rows}"
    assert rows[0]["spend"] == 140.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_ads_one_source.py -q -p no:randomly`

Expected: `test_storing_sb_for_a_day_does_not_delete_that_days_sp_rows` FAILS with
`AssertionError: storing SB deleted the SP rows for the same day`. The second test passes already
(it is the guard on the fix, not a demonstration of the bug).

- [ ] **Step 3: Scope the delete by day AND ad product**

In `app/ads/repository.py`, replace the delete inside `save_daily` (currently
`delete(AdsPerformanceDaily).where(AdsPerformanceDaily.day.in_(sorted(days)))`) with:

```python
    # Replace exactly the days present in this payload, FOR THIS AD PRODUCT ONLY.
    #
    # **`ad_product` in this scope is load-bearing, and omitting it destroys data.** This function
    # is delete-then-bulk-insert (the measured 62x deviation from the house upsert), so a second
    # call for the same days replaces whatever the first one wrote. Sponsored Products and
    # Sponsored Brands are two separate reports covering the SAME days, so scoped by day alone the
    # SB write would delete every SP row it had just stored — leaving SB-only days, which is the
    # "Rs 1,26,328 vanished" bug inverted and worse, because SP is 72% of spend.
    #
    # The docstring above already claims this property one dimension down ("scoped per DAY so
    # refetching a 7-day window cannot disturb the other 23 days"). Until Sponsored Brands started
    # being stored daily, only one product ever reached this line.
    await db.execute(
        delete(AdsPerformanceDaily).where(
            AdsPerformanceDaily.day.in_(sorted(days)),
            AdsPerformanceDaily.ad_product == ad_product,
        )
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_ads_one_source.py -q -p no:randomly`

Expected: 2 passed.

- [ ] **Step 5: Store SB daily in the refresh**

In `app/ads/refresh.py`, the SB block currently calls `reports.fetch_targeting(...)` without
`daily=True` and stores only via `save_performance`. Replace the body of the `try` with:

```python
        try:
            sb_rows = await reports.fetch_targeting(
                window_start, window_end, ad_product="sb", daily=True, sleep=sleep,
            )
            async with db_factory() as db:
                # **DAILY, like Sponsored Products.** Storing SB at the window grain only is what
                # made Rs 1,26,328 disappear from every window that was not fetched exactly: the
                # read side sums daily rows, and SB had none. The earlier comment here reasoned
                # from ROW COUNT ("2,914 against SP's 12,205") when the figure that matters is
                # SPEND SHARE — SB is 28% of the money.
                sb_stored = await repository.save_daily(db, sb_rows, ad_product="sb")
            STATE["sb_rows"] = sb_stored
            logger.info("ads refresh: %d Sponsored Brands daily row(s) stored", sb_stored)
        except AdsError as exc:
            # Isolated: the SP rows above are already committed and current. A throttled SB report
            # is "not now", not a failed refresh — `sbTargeting` has been measured returning 429
            # after 15 minutes of complete idleness because reports were created earlier that day.
            STATE["sb_error"] = f"The Sponsored Brands report failed: {exc}"
            logger.warning("ads refresh: SB report failed: %s", exc)
```

- [ ] **Step 6: Run the ads suite**

Run: `venv/Scripts/python -m pytest tests/test_ads_api.py tests/test_ads_sb.py tests/test_ads_logic.py -q`

Expected: all pass. If a test asserted SB is stored at the window grain, it encoded the defect —
rewrite it to assert the requirement ("SB figures are present in a derived window") and note in its
docstring that the assertion flipped, as CLAUDE.md records for the orders-window tests.

- [ ] **Step 7: Commit**

```bash
git add app/ads/repository.py app/ads/refresh.py tests/test_ads_one_source.py
git commit -m "fix(ads): store Sponsored Brands daily, and scope the daily delete by product

save_daily is delete-then-bulk-insert and its delete was scoped by DAY alone,
so storing SB after SP for the same days would have deleted every SP row just
written. Scoped by (day, ad_product) before anything calls it twice."
```

---

## Task 2: One read path — delete `ads_performance`

**Files:**
- Modify: `app/ads/repository.py` (delete `load_performance`, `purge_windows`, `windows_available`, `WINDOW_RETENTION_COUNT`, `save_performance`; `DAILY_RETENTION_DAYS` 30 → 60)
- Modify: `app/ads/refresh.py` (drop the `save_performance` and `purge_windows` calls)
- Modify: `app/routers/ads.py:160-176, 309-315, 381-397`
- Modify: `app/models.py` (drop the `AdsPerformance` model)
- Create: `alembic/versions/<rev>_drop_ads_performance.py`
- Modify: `deploy/update-ec2.sh:44-45, 356-358, 437`
- Test: `tests/test_ads_one_source.py`, `tests/test_schema_migrations.py`

**Interfaces:**
- Consumes: `repository.sum_daily(db, start, end, *, campaign_ids=None, ad_group_ids=None) -> list[dict]`, `repository.daily_range_complete(db, start, end) -> bool`, `repository.daily_coverage(db) -> tuple[str, str] | None`.
- Produces: `GET /ads` and `POST /ads/preview` read only `sum_daily`. `GET /ads` no longer returns `windows_available`; the screen uses `daily_coverage`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ads_one_source.py`:

```python
async def test_a_superset_window_never_reports_less_than_its_subset(db):
    """**The reported bug, as an invariant.**

    22-28 showed Rs 4,44,550 and 22-29 showed Rs 3,34,300 — adding a day REDUCED the total, because
    the first was a stored window (SP + SB) and the second was summed from daily rows (SP only).

    Stated as monotonicity rather than as "SB is stored daily": a test phrased the second way would
    pass again the next time a third ad product is added.
    """
    for day, spend in (("2026-08-22", 100.0), ("2026-08-23", 120.0)):
        await repository.save_daily(db, [_report_row("SP1", day, spend=spend)], ad_product="sp")
        await repository.save_daily(
            db, [_report_row("SB1", day, spend=spend / 2, product="sb")], ad_product="sb")

    subset = await repository.sum_daily(db, "2026-08-22", "2026-08-22")
    superset = await repository.sum_daily(db, "2026-08-22", "2026-08-23")

    subset_spend = sum(r["spend"] for r in subset)
    superset_spend = sum(r["spend"] for r in superset)
    assert superset_spend >= subset_spend, (
        f"a superset window reported LESS ({superset_spend}) than its subset ({subset_spend})"
    )
    assert superset_spend == pytest.approx(330.0), superset_spend


async def test_every_derived_window_carries_the_sponsored_brands_spend(db):
    """Rs 1,26,328 was 28% of the account and read as zero.

    Asserted on the summed rows because that is the one path left after this task — there is no
    longer a second path that could be the one that happens to be right.
    """
    await repository.save_daily(db, [_report_row("SP1", "2026-08-22", spend=318222.0)],
                                ad_product="sp")
    await repository.save_daily(
        db, [_report_row("SB1", "2026-08-22", spend=126328.0, product="sb")], ad_product="sb")

    rows = await repository.sum_daily(db, "2026-08-22", "2026-08-22")
    sb = [r for r in rows if r["ad_product"] == "sb"]
    assert sb, "the Sponsored Brands rows are missing from a derived window"
    assert sum(r["spend"] for r in sb) == pytest.approx(126328.0)
    assert sum(r["spend"] for r in rows) == pytest.approx(444550.0)


async def test_a_range_with_an_interior_gap_declines_to_answer(db):
    """**This is what makes a partial nightly scrape safe rather than silently wrong.**

    A 60-day scrape is four reports and `sbTargeting` throttles for hours, so a missing chunk is the
    expected case. `daily_range_complete` already checks EVERY day rather than the endpoints — a
    missing Tuesday must make the window refuse to answer, because a sum that is quietly short is
    what a bid rule would then act on.

    Pinned here because Task 3 depends on it and nothing else asserts the interior case: a test using
    only the endpoints would pass against a version that checks just `min` and `max`.
    """
    for day in ("2026-08-22", "2026-08-24"):          # 23rd deliberately absent
        await repository.save_daily(db, [_report_row("SP1", day, spend=10.0)], ad_product="sp")

    assert await repository.daily_range_complete(db, "2026-08-22", "2026-08-22") is True
    assert await repository.daily_range_complete(db, "2026-08-22", "2026-08-24") is False, (
        "a range with a missing interior day claimed to be complete, so its sum would be short"
    )


def test_the_window_grain_table_is_gone():
    """One source of truth, enforced structurally.

    While two tables answered the same question, WHICH ONE you got depended on whether somebody had
    fetched that exact range — and they disagreed by 28%. Deleting the model is what makes a
    regression impossible rather than merely unlikely.
    """
    import app.models as models

    assert not hasattr(models, "AdsPerformance"), (
        "the per-window table still exists, so two paths can answer the same question again"
    )
    assert hasattr(models, "AdsPerformanceDaily")
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_ads_one_source.py -q -p no:randomly`

Expected: `test_the_window_grain_table_is_gone` FAILS (`AdsPerformance` still exists). The two data
tests pass already — they document the invariant that Task 1 restored, and they must keep passing.

- [ ] **Step 3: Point the routes at `sum_daily` alone**

In `app/routers/ads.py`, replace the grain-selection block (lines ~160-176) with:

```python
    # **ONE source: the per-day rows.** There used to be two — a per-window table read when the
    # exact range had been fetched, and this sum otherwise — and they disagreed by 28% of spend,
    # because Sponsored Brands was only ever written to the window table. Which figure you got
    # depended on whether somebody had happened to fetch that range.
    complete = await repository.daily_range_complete(db, window_start, window_end)
    rows = []
    if complete:
        rows = await repository.sum_daily(
            db, window_start, window_end,
            campaign_ids=[campaign_id] if campaign_id else None,
        )
    if rows:
        rows = await repository.attach_names(db, rows)
    cached = complete
```

Delete `derived` and its key from the returned dict, and replace `windows_available` with nothing
(the screen already receives `daily_coverage`). In the return dict, remove these two lines:

```python
        "derived": derived,
        "windows_available": [list(w) for w in available],
```

and remove the now-unused `available = await repository.windows_available(db)` and
`exact = (window_start, window_end) in available` above.

Apply the same substitution at lines ~309-315 (`/ads/rows`) and ~381-397 (`/ads/preview`): both
call `load_performance` first, so both become a single `sum_daily` guarded by
`daily_range_complete`. In `/ads/preview` keep the existing 400 when there are no rows, with the
message unchanged.

- [ ] **Step 4: Delete the dead repository functions**

In `app/ads/repository.py` delete `save_performance`, `load_performance`, `purge_windows`,
`windows_available` and the `WINDOW_RETENTION_COUNT` constant. Change the retention constant to:

```python
#: How many days of per-day rows to keep. **60**, matching the nightly scrape's horizon.
#:
#: Measured at 8,384 rows/day in July (August is quieter at 6,107), so 60 days is ~503,000 rows and
#: ~93 MB. That fits: deleting the per-window table returns 17.1 MB, and `KEEP_BACKUPS` dropping
#: from 5 to 3 returns ~180 MB more on a box with 912 MB free.
DAILY_RETENTION_DAYS = 60
```

In `app/ads/refresh.py`, delete the `save_performance` call and the `purged += await
repository.purge_windows(db)` line, keeping `purge_daily`. Drop `STATE["rows"]` if nothing reads it,
or set it from `daily_stored`.

In `app/models.py` delete the `AdsPerformance` class.

- [ ] **Step 5: Write the migration**

Create `alembic/versions/<rev>_drop_ads_performance.py` (generate a hex revision id, e.g.
`a1c7e93f24b8`):

```python
"""Drop ads_performance: the per-window grain is gone, daily rows are the only source.

Revision ID: a1c7e93f24b8
Revises: e2b7d94c15af

**Nothing of value is lost.** Every row is reproducible from `ads_performance_daily` or a refetch,
and the table was the largest in the database (105,755 rows / 17.1 MB) purely to cache figures the
daily rows already hold. Keeping it is what let two code paths answer the same question differently:
Sponsored Brands was written HERE and not to the daily table, so any window nobody had fetched
exactly under-reported spend by 28%.

The downgrade recreates the table EMPTY. It cannot restore the rows, and pretending otherwise would
be worse than saying so: the data is refetchable, the schema is what a downgrade owes.
"""
from alembic import op
import sqlalchemy as sa

revision = "a1c7e93f24b8"
down_revision = "e2b7d94c15af"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("ads_performance")


def downgrade() -> None:
    op.create_table(
        "ads_performance",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("window_start", sa.String(length=10), nullable=False),
        sa.Column("window_end", sa.String(length=10), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=16), nullable=False),
        sa.Column("ad_product", sa.String(length=8), nullable=False, server_default="sp"),
        sa.Column("campaign_id", sa.String(length=64), nullable=True),
        sa.Column("ad_group_id", sa.String(length=64), nullable=True),
        sa.Column("text", sa.String(length=500), nullable=True),
        sa.Column("match_type", sa.String(length=64), nullable=True),
        sa.Column("reported_bid", sa.Numeric(10, 2), nullable=True),
        sa.Column("impressions", sa.Integer(), nullable=True),
        sa.Column("clicks", sa.Integer(), nullable=True),
        sa.Column("spend", sa.Numeric(12, 2), nullable=True),
        sa.Column("orders", sa.Integer(), nullable=True),
        sa.Column("sales", sa.Numeric(12, 2), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_ads_performance_window",
        "ads_performance",
        ["window_start", "window_end", "entity_id"],
    )
```

- [ ] **Step 6: Update the deploy script**

In `deploy/update-ec2.sh`:

Change line 45 to `KEEP_BACKUPS="${KEEP_BACKUPS:-3}"` and update the comment above it to say the
database roughly doubles with 60 days of daily ads rows, so five copies of it is more than the app.

Add the newest branch to the detector, **above** the `ads_entity` branch:

```sh
elif "ads_performance_daily" in tables and "ads_performance" not in tables:
    print("a1c7e93f24b8")                           # head: daily rows are the only ads grain
```

Remove `"ads_performance"` from the required-tables set at line ~437.

- [ ] **Step 7: Run the migration and the schema tests**

```bash
venv/Scripts/python -m alembic upgrade head
venv/Scripts/python -m pytest tests/test_schema_migrations.py tests/test_ads_one_source.py -q
```

Expected: migration applies; all tests pass, including the detector answering `a1c7e93f24b8`.

- [ ] **Step 8: Fix the FIVE template sites that read the deleted fields**

`templates/ads.html` reads `data.windows_available` and `data.derived`, both of which this task
removes from the payload. Left alone the window bar silently marks every range as uncached and the
"summed from the daily rows" banner never fires — a broken screen with a green test suite, because
no test asserts the banner.

Exact sites, verified by grep:

| Line | Reads | Becomes |
|---|---|---|
| 365 | `data.derived` | delete the branch — everything is derived now |
| 396 | `data.windows_available` | delete; `have` is no longer needed |
| 447 | `exactlyCached(from, to)` | delete the branch |
| 474-479 | `insideDailyCoverage` | keep, unchanged — it reads `daily_coverage` |
| 481-483 | `function exactlyCached` | delete the function |

At line 365, replace the `data.derived` branch with one that states the new single source:

```javascript
  } else if(data.cached){
    /* **One source now: the per-day rows.** There used to be two — a per-window cache and this sum
       — and they disagreed by 28% of spend, because Sponsored Brands was only ever written to the
       window table. Which figure you got depended on whether anyone had fetched that exact range. */
    out.push(`<div class="banner good">Summed from the <strong>stored daily rows</strong> for
      ${esc(data.window[0])} → ${esc(data.window[1])} — no Amazon call was needed.</div>`);
```

At line 396, delete the `have` line. In the preset button builder, replace `have.has(key)` with
`insideDailyCoverage(r.start, r.end)` so a preset is marked instant when the daily rows cover it —
which is now the only thing that makes a range instant.

At line 447, delete the `exactlyCached` branch, leaving `insideDailyCoverage` as the first check.
Delete the `exactlyCached` function at 481.

- [ ] **Step 9: Verify the JavaScript parses and nothing references the removed fields**

```bash
grep -n "windows_available\|exactlyCached\|data\.derived" templates/ads.html && echo "STILL REFERENCED" || echo "clean"
node -e "const fs=require('fs');const s=fs.readFileSync('templates/ads.html','utf8');const m=[...s.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(x=>x[1]).join('\n');fs.writeFileSync('/tmp/ads.js',m);" && node --check /tmp/ads.js && echo "JS OK"
```

Expected: `clean` then `JS OK`.

- [ ] **Step 10: Run the full suite**

Run: `venv/Scripts/python -m pytest -q`

Expected: all pass. Tests referencing `load_performance`, `save_performance` or `windows_available`
must be rewritten against `sum_daily`; where one asserted the two-path behaviour, say in its
docstring that the assertion flipped and why.

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "refactor(ads): delete ads_performance, daily rows are the only source

Two tables answered the same question and disagreed by 28% of spend, because
Sponsored Brands was written to one and not the other. Which figure you got
depended on whether anyone had fetched that exact window.

Deleting the table also removes purge_windows and the 6-window eviction
policy — a retention rule that existed only because the same numbers were
cached twice. Frees 17.1 MB, the largest table in the database."
```

---

## Task 3: 60-day nightly history, committed chunk by chunk

**Files:**
- Modify: `app/ads/reports.py:195-233` (`fetch_targeting` gains `on_chunk`)
- Modify: `app/ads/refresh.py` (store each chunk as it lands)
- Modify: `app/scheduler.py:287-307` (`days=7` → `days=60`)
- Test: `tests/test_ads_one_source.py`

**Interfaces:**
- Consumes: `reports.split_window(start, end) -> list[tuple[str, str]]` (from `app.portfolio.ads`, cap `MAX_REPORT_DAYS = 31`).
- Produces: `reports.fetch_targeting(..., on_chunk=None)`, where `on_chunk` is
  `async (rows: list[dict], chunk_start: str, chunk_end: str) -> None`, awaited after each chunk.
  When `on_chunk` is given, `fetch_targeting` still returns the full list.

**Measured:** one real 31-day daily SP report is **259,900 rows in 19.5 minutes**. 60 days is 2 SP
chunks + 2 SB chunks ≈ 50–60 min at 03:50.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ads_one_source.py`:

```python
async def test_a_failed_chunk_keeps_the_days_already_stored(db, monkeypatch):
    """**A 60-day scrape is 4 reports; losing the night because the last one failed is not tolerable.**

    Amazon caps a report at 31 days, so 60 days is 2 SP chunks + 2 SB chunks — and `sbTargeting` has
    been measured returning 429 after 15 minutes of complete idleness. So a chunk failing is the
    expected case, not the exceptional one, and each must commit as it lands.

    Asserted through `on_chunk` because that is the mechanism: `fetch_targeting` used to accumulate
    every chunk and return once, so a failure in the last one discarded the lot.
    """
    from app.ads import reports

    stored: list[str] = []

    async def fake_one_report(client, start, end, **kwargs):
        if start >= "2026-08-01":
            raise reports.AdsError("Amazon throttled this report (429).")
        return [_report_row("SP1", start, spend=10.0)]

    monkeypatch.setattr(reports, "_one_report", fake_one_report)

    async def on_chunk(rows, chunk_start, chunk_end):
        await repository.save_daily(db, rows, ad_product="sp")
        stored.append(chunk_start)

    with pytest.raises(reports.AdsError):
        await reports.fetch_targeting(
            "2026-07-02", "2026-08-30", daily=True, on_chunk=on_chunk,
            sleep=_no_sleep,
        )

    assert stored, "the first chunk was not committed before the second failed"
    rows = await repository.sum_daily(db, "2026-07-02", "2026-07-02")
    assert rows, "the successfully fetched chunk was discarded when a later one failed"
```

Add this helper near the top of the file, under `_report_row`:

```python
async def _no_sleep(_seconds):
    """Skip the 20-second report poll interval in tests."""
    return None
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/Scripts/python -m pytest tests/test_ads_one_source.py::test_a_failed_chunk_keeps_the_days_already_stored -q -p no:randomly`

Expected: FAIL with `TypeError: fetch_targeting() got an unexpected keyword argument 'on_chunk'`.

- [ ] **Step 3: Add `on_chunk` to `fetch_targeting`**

In `app/ads/reports.py`, add `on_chunk=None` to the signature and call it inside the chunk loop,
immediately after `_one_report` returns, before `raw.extend(...)`:

```python
            chunk_rows = await _one_report(
                client, chunk_start, chunk_end, daily=daily, ad_product=ad_product,
                sleep=sleep, on_progress=chunk_progress,
            )
            # **Hand each chunk over as it lands.** A 60-day window is two reports per ad product
            # and `sbTargeting` throttles for HOURS, so a later chunk failing is expected — and
            # accumulating everything before returning meant one 429 discarded up to 40 minutes of
            # successfully fetched data. The caller stores as it goes and a partial night leaves
            # real days stored; `daily_range_complete` then makes the incomplete range decline to
            # answer rather than sum short.
            if on_chunk is not None:
                await on_chunk(chunk_rows, chunk_start, chunk_end)
            raw.extend(chunk_rows)
```

Document the parameter in the docstring: `on_chunk` is awaited with
`(rows, chunk_start, chunk_end)` after each chunk, and an exception from it propagates.

- [ ] **Step 4: Run to verify it passes**

Run: `venv/Scripts/python -m pytest tests/test_ads_one_source.py -q -p no:randomly`

Expected: all pass.

- [ ] **Step 5: Use `on_chunk` in the refresh**

In `app/ads/refresh.py`, replace the single SP `fetch_targeting` call and its store block with a
version that stores per chunk:

```python
        async def store_chunk(rows, chunk_start, chunk_end):
            """Commit one chunk's days immediately. See `reports.fetch_targeting`'s `on_chunk`."""
            async with db_factory() as db:
                stored = await repository.save_daily(db, rows, ad_product="sp")
            STATE["daily_rows"] = STATE.get("daily_rows", 0) + stored
            logger.info(
                "ads refresh: stored %d daily row(s) for %s..%s", stored, chunk_start, chunk_end
            )

        STATE["daily_rows"] = 0
        await reports.fetch_targeting(
            window_start, window_end, daily=True, sleep=sleep,
            on_progress=on_report_progress, on_chunk=store_chunk,
        )
```

Do the same for the SB block, with `ad_product="sb"` in both the fetch and the `save_daily`, and its
own counter `STATE["sb_rows"]`.

- [ ] **Step 6: Raise the nightly window to 60 days**

In `app/scheduler.py`, change `await ads_refresh.run(days=7)` to `days=60` and replace the docstring
paragraph about the 7-day window with:

```
    **A 60-day window**, so that any range the tab offers is answerable from stored rows without a
    fresh report. Amazon caps one report at 31 days, so this is two chunks per ad product — four
    reports, measured at ~50-60 minutes total (one real 31-day daily report is 259,900 rows in 19.5
    minutes). Each chunk commits as it lands, so a throttled fourth report leaves the first three
    stored rather than losing the night.
```

- [ ] **Step 7: Run the suite**

Run: `venv/Scripts/python -m pytest -q`

Expected: all pass. `tests/test_retention_and_scheduler.py` asserts the registered job ids — the
scheduler must still register `ads_refresh` and must still be unable to reach `apply_bids`.

- [ ] **Step 8: Commit**

```bash
git add app/ads/reports.py app/ads/refresh.py app/scheduler.py tests/test_ads_one_source.py
git commit -m "feat(ads): 60-day nightly history, committed chunk by chunk

Measured: one real 31-day daily report is 259,900 rows in 19.5 min, so 60 days
is 2 SP + 2 SB chunks at ~50-60 min. fetch_targeting used to accumulate every
chunk before returning, so one 429 discarded up to 40 minutes of good data —
and sbTargeting has been measured throttling after 15 idle minutes, so that is
the expected case. Each chunk now commits as it lands."
```

---

## Task 4: Suggested bids from Amazon

**Files:**
- Modify: `app/ads/spapi_ads.py` (add `fetch_bid_recommendations`)
- Modify: `app/routers/ads.py:398-415` (attach `suggested_bid` to preview rows)
- Test: `tests/test_ads_bid_recommendations.py` (create)

**Interfaces:**
- Produces: `spapi_ads.fetch_bid_recommendations(client, rows) -> dict[str, dict]`, keyed by
  `entity_id`, each value `{"suggested_bid": float | None, "low": float | None,
  "high": float | None, "unavailable": str}`. `unavailable` is `""` when a bid was returned, else a
  short reason for the screen.

**Measured against the live account:**

| Endpoint | Result |
|---|---|
| `/v2/sp/adGroups/{id}/bidRecommendations` | 404 "Method Not Found" |
| `/v2/sp/keywords/{id}/bidRecommendations` | 404 |
| `/sp/keywords/bid/recommendations` | 403, twice, spurious SigV4 error |
| **`/sp/targets/bid/recommendations`** | **200 — real bids** |
| `/sb/recommendations/bids/keyword` | 404 |
| `/sb/recommendations/bids/targets` | 404 |
| `/sb/targets/bid/recommendations` | 404 |

Media type `application/vnd.spthemebasedbidrecommendation.v3+json`. Amazon returns **three** bids
per expression, e.g. `[10.68, 14.24, 17.80]`. Batched per ad group.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ads_bid_recommendations.py`:

```python
"""Amazon's suggested bid, and the three states it has.

**The endpoint had to be found by probing.** Four candidates were called against the live account:
the two documented v2 paths 404 ("Method Not Found"), `/sp/keywords/bid/recommendations` returns 403
with a spurious SigV4 error, and only `/sp/targets/bid/recommendations` answers 200 with real bids.

**Sponsored Brands has none.** Three SB candidates were probed and all three 404, so ~296 rows in a
typical preview have no suggestion — which must be SAID rather than left blank.
"""
import pytest

from app.ads import spapi_ads

pytestmark = pytest.mark.regression


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeClient:
    """Records requests and replays canned responses, keyed by the ad group in the body."""

    def __init__(self, by_ad_group):
        self.by_ad_group = by_ad_group
        self.calls = []

    async def request(self, method, url, json=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": json})
        ad_group = (json or {}).get("adGroupId")
        return FakeResponse(200, self.by_ad_group.get(ad_group, {"bidRecommendations": []}))

    async def post(self, url, json=None, headers=None):
        return await self.request("POST", url, json=json, headers=headers)


#: The real response shape, captured from the live account.
LIVE_SHAPE = {
    "bidRecommendations": [{
        "theme": "CONVERSION_OPPORTUNITIES",
        "bidRecommendationsForTargetingExpressions": [{
            "targetingExpression": {"type": "KEYWORD_EXACT_MATCH", "value": "usna chawal bihar"},
            "bidValues": [{"suggestedBid": 140.33}, {"suggestedBid": 182.83},
                          {"suggestedBid": 225.33}],
        }],
    }]
}


def _row(entity_id, text, *, product="sp", match_type="EXACT", ad_group="A1"):
    return {
        "entity_id": entity_id, "text": text, "ad_product": product,
        "match_type": match_type, "ad_group_id": ad_group, "campaign_id": "C1",
    }


async def test_the_middle_of_three_bids_is_the_suggestion(monkeypatch):
    """**Amazon returns THREE bids, not one:** [140.33, 182.83, 225.33].

    The middle value is the suggestion and the outer two are its range. Recording one number as
    "the suggested bid" without saying which would be a silent choice between three, so the range
    travels alongside it.
    """
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = FakeClient({"A1": LIVE_SHAPE})

    result = await spapi_ads.fetch_bid_recommendations(
        client, [_row("KW1", "usna chawal bihar")]
    )

    assert result["KW1"]["suggested_bid"] == pytest.approx(182.83)
    assert result["KW1"]["low"] == pytest.approx(140.33)
    assert result["KW1"]["high"] == pytest.approx(225.33)
    assert result["KW1"]["unavailable"] == ""


async def test_sponsored_brands_rows_are_reported_as_unavailable_not_blank(monkeypatch):
    """**SB has no bid-recommendation endpoint — three probed, all 404.**

    A blank cell in a bid column reads as "no suggestion, so bid low". The honest answer is that
    Amazon does not offer one here, which is the same three-state discipline the Portfolio tab's
    ACOS column follows. Critically it must never borrow an SP figure for an SB row.
    """
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = FakeClient({"A1": LIVE_SHAPE})

    result = await spapi_ads.fetch_bid_recommendations(
        client, [_row("SB1", "roast chana", product="sb")]
    )

    assert result["SB1"]["suggested_bid"] is None
    assert "Sponsored Brands" in result["SB1"]["unavailable"]
    assert not client.calls, "an SB row was sent to the Sponsored Products endpoint"


async def test_one_call_per_ad_group_not_per_row(monkeypatch):
    """The endpoint is batched per ad group, which is what makes a 1,005-row preview affordable.

    One call per row would be 1,005 calls; one per distinct ad group is a few dozen.
    """
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = FakeClient({"A1": LIVE_SHAPE, "A2": LIVE_SHAPE})

    rows = [_row(f"KW{i}", f"kw {i}", ad_group="A1") for i in range(20)]
    rows += [_row(f"KX{i}", f"kx {i}", ad_group="A2") for i in range(15)]
    await spapi_ads.fetch_bid_recommendations(client, rows)

    assert len(client.calls) == 2, f"expected one call per ad group, made {len(client.calls)}"


async def test_a_failed_recommendation_call_does_not_fail_the_preview(monkeypatch):
    """A suggestion is CONTEXT. Losing it must not lose the 1,005 bid changes beside it.

    The preview is the safety mechanism for the only feature that spends money; degrading it
    because a nice-to-have column errored would be the wrong trade.
    """
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)

    class Boom(FakeClient):
        async def request(self, *a, **k):
            raise RuntimeError("Amazon said no")

    result = await spapi_ads.fetch_bid_recommendations(Boom({}), [_row("KW1", "chana")])
    assert result["KW1"]["suggested_bid"] is None
    assert result["KW1"]["unavailable"], "a failure must carry a reason, not an empty string"


async def _fake_token(_client):
    return "token"
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_ads_bid_recommendations.py -q -p no:randomly`

Expected: FAIL with `AttributeError: module 'app.ads.spapi_ads' has no attribute
'fetch_bid_recommendations'`.

- [ ] **Step 3: Implement `fetch_bid_recommendations`**

Add to `app/ads/spapi_ads.py`, near `fetch_current_bids`:

```python
#: The ONE endpoint that answers, found by probing four candidates against the live account:
#: `/v2/sp/adGroups/{id}/bidRecommendations` and `/v2/sp/keywords/{id}/bidRecommendations` both 404
#: with "Method Not Found", and `/sp/keywords/bid/recommendations` returns 403 twice with a spurious
#: SigV4 error. Only this one returns real bids.
BID_RECS_PATH = "/sp/targets/bid/recommendations"
BID_RECS_VND = "application/vnd.spthemebasedbidrecommendation.v3+json"

#: Match type -> the `targetingExpression.type` the recommendation endpoint expects.
KEYWORD_EXPRESSION_TYPES = {
    "EXACT": "KEYWORD_EXACT_MATCH",
    "PHRASE": "KEYWORD_PHRASE_MATCH",
    "BROAD": "KEYWORD_BROAD_MATCH",
}

#: Why a row has no suggestion. Shown on screen — a blank cell in a bid column reads as
#: "no suggestion, bid low", which is a different claim from "Amazon does not offer one".
SB_UNAVAILABLE = "Amazon offers no suggested bid for Sponsored Brands"
NO_MATCH_UNAVAILABLE = "Amazon returned no suggestion for this target"


async def fetch_bid_recommendations(client, rows: Sequence[Mapping]) -> dict[str, dict]:
    """`{entity_id: {suggested_bid, low, high, unavailable}}` for the rows Amazon will answer for.

    **Batched per AD GROUP**, which is what makes this affordable: the owner's real rule matched
    1,005 rows across a few dozen ad groups, so this is a few dozen calls rather than 1,005.

    **Amazon returns THREE bids per expression** — measured `[140.33, 182.83, 225.33]`. The middle
    value is the suggestion and the outer two are its range; both travel, because picking one of
    three silently is exactly the kind of choice that later reads as a fact.

    **Sponsored Brands rows are excluded and LABELLED, never guessed.** Three SB endpoints were
    probed (`/sb/recommendations/bids/keyword`, `/sb/recommendations/bids/targets`,
    `/sb/targets/bid/recommendations`) and all three 404. Borrowing an SP figure for an SB row
    would put a number next to a bid that Amazon never suggested for it.

    Never raises. A suggestion is context beside the bid; losing it must not lose the preview that
    is the safety mechanism for the only feature in this app that spends money.
    """
    out: dict[str, dict] = {}
    by_ad_group: dict[str, list] = {}

    for row in rows:
        entity_id = str(row.get("entity_id") or "")
        if not entity_id:
            continue
        if (row.get("ad_product") or "sp") != AD_PRODUCT_SP:
            out[entity_id] = {"suggested_bid": None, "low": None, "high": None,
                              "unavailable": SB_UNAVAILABLE}
            continue
        expression = _expression_for(row)
        if expression is None:
            out[entity_id] = {"suggested_bid": None, "low": None, "high": None,
                              "unavailable": NO_MATCH_UNAVAILABLE}
            continue
        by_ad_group.setdefault(str(row.get("ad_group_id") or ""), []).append((row, expression))

    if not by_ad_group:
        return out

    try:
        token = await _access_token(client)
    except Exception as exc:                      # noqa: BLE001 - context, never fatal
        logger.warning("ads: no token for bid recommendations: %s", exc)
        for group in by_ad_group.values():
            for row, _ in group:
                out[str(row["entity_id"])] = {"suggested_bid": None, "low": None, "high": None,
                                               "unavailable": f"Could not ask Amazon: {exc}"}
        return out

    settings = get_settings()
    headers = {**_headers(token), "Content-Type": BID_RECS_VND, "Accept": BID_RECS_VND}

    for ad_group_id, group in by_ad_group.items():
        if not ad_group_id:
            for row, _ in group:
                out[str(row["entity_id"])] = {"suggested_bid": None, "low": None, "high": None,
                                               "unavailable": NO_MATCH_UNAVAILABLE}
            continue
        body = {
            "campaignId": str(group[0][0].get("campaign_id") or ""),
            "adGroupId": ad_group_id,
            "recommendationType": "BIDS_FOR_EXISTING_AD_GROUP",
            "targetingExpressions": [expression for _, expression in group],
        }
        try:
            response = await client.request(
                "POST", settings.ads_endpoint + BID_RECS_PATH, json=body, headers=headers,
            )
            payload = response.json() if response.status_code == 200 else {}
            reason = "" if response.status_code == 200 else (
                f"Amazon returned {response.status_code} for this ad group"
            )
        except Exception as exc:                  # noqa: BLE001 - context, never fatal
            payload, reason = {}, f"Could not ask Amazon: {exc}"

        suggestions = _parse_bid_recommendations(payload)
        for row, expression in group:
            key = (expression.get("value") or "").casefold()
            found = suggestions.get(key)
            out[str(row["entity_id"])] = found or {
                "suggested_bid": None, "low": None, "high": None,
                "unavailable": reason or NO_MATCH_UNAVAILABLE,
            }
    return out


def _expression_for(row: Mapping) -> dict | None:
    """The `targetingExpression` for one row, or None when Amazon has no expression type for it."""
    match_type = (row.get("match_type") or "").upper()
    text = (row.get("text") or "").strip()
    kind = KEYWORD_EXPRESSION_TYPES.get(match_type)
    if kind and text:
        return {"type": kind, "value": text}
    if match_type == "TARGETING_EXPRESSION_PREDEFINED" and text:
        # Auto targets carry their own type name in the report text ("close-match" etc).
        return {"type": text.replace("-", "_").upper(), "value": None}
    return None


def _parse_bid_recommendations(payload: Mapping) -> dict[str, dict]:
    """`{expression value casefolded: {suggested_bid, low, high, unavailable}}`.

    Amazon nests these three deep — themes, then expressions, then a LIST of bid values — and
    returns three bids per expression. The middle one is the suggestion.
    """
    out: dict[str, dict] = {}
    for theme in payload.get("bidRecommendations") or []:
        for entry in theme.get("bidRecommendationsForTargetingExpressions") or []:
            expression = entry.get("targetingExpression") or {}
            bids = [
                float(v["suggestedBid"])
                for v in (entry.get("bidValues") or [])
                if v.get("suggestedBid") is not None
            ]
            if not bids:
                continue
            bids.sort()
            middle = bids[len(bids) // 2]
            out[(expression.get("value") or "").casefold()] = {
                "suggested_bid": round(middle, 2),
                "low": round(bids[0], 2),
                "high": round(bids[-1], 2),
                "unavailable": "",
            }
    return out
```

Ensure `Sequence` and `Mapping` are imported from `collections.abc` at the top of the module (they
already are, for `fetch_current_bids`).

- [ ] **Step 4: Run to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_ads_bid_recommendations.py -q -p no:randomly`

Expected: 4 passed.

- [ ] **Step 5: Attach suggestions to preview rows**

In `app/routers/ads.py`, in the `preview` handler after `plan = logic.plan_run(...)` and before the
return, add:

```python
    # Amazon's own suggested bid, as CONTEXT beside each change. Fetched here rather than in
    # `logic.plan_run` because that module is pure — no network, no database — and this is a live
    # Amazon call. Never fatal: a missing suggestion must not cost the owner the preview.
    changes = plan.get("changes") or []
    if changes and get_settings().ads_configured:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=get_settings().ads_timeout) as bid_client:
                suggestions = await spapi_ads.fetch_bid_recommendations(bid_client, changes)
        except Exception as exc:                  # noqa: BLE001 - context, never fatal
            logger.warning("ads: suggested bids unavailable: %s", exc)
            suggestions = {}
        for change in changes:
            found = suggestions.get(str(change.get("entity_id"))) or {}
            change["suggested_bid"] = found.get("suggested_bid")
            change["suggested_low"] = found.get("low")
            change["suggested_high"] = found.get("high")
            change["suggested_unavailable"] = found.get("unavailable") or ""
```

- [ ] **Step 6: Run the ads route tests**

Run: `venv/Scripts/python -m pytest tests/test_ads_api.py tests/test_ads_bid_recommendations.py -q`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/ads/spapi_ads.py app/routers/ads.py tests/test_ads_bid_recommendations.py
git commit -m "feat(ads): show Amazon's suggested bid on preview rows

The endpoint had to be found by probing: the two documented v2 paths 404 and
/sp/keywords/bid/recommendations 403s with a spurious SigV4 error. Only
/sp/targets/bid/recommendations answers, batched per ad group, returning THREE
bids per expression — the middle is the suggestion, the outer two its range.

Sponsored Brands has no such endpoint (three probed, all 404), so those rows
say so rather than rendering blank. Never fatal: a suggestion is context, and
losing it must not cost the preview that guards the only feature that spends."
```

---

## Task 5: The four preview changes on screen

**Files:**
- Modify: `templates/ads.html:838-903` (`renderPreview`)
- Test: `tests/test_ads_api.py` (append template assertions)

**Interfaces:**
- Consumes: preview rows carrying `ad_group_name` (already set by `repository.attach_names`),
  `suggested_bid`, `suggested_low`, `suggested_high`, `suggested_unavailable` (Task 4).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ads_api.py`:

```python
def test_the_preview_has_apply_controls_at_the_top_as_well_as_the_bottom():
    """**A real rule matched 1,005 rows**, so the controls were below a screenful of scrolling.

    Both, not one: the top bar is what gets used on a long list, and the bottom one serves someone
    who has read to the end. Rendered from ONE builder so the two cannot disagree about the count.
    """
    source = _ads_template()
    assert source.count("data-apply-bar") >= 2, (
        "the apply controls are not rendered both above and below the table"
    )
    assert source.count("function applyBarHtml(") == 1, (
        "the apply bar is built twice, so the two copies can drift"
    )


def test_the_preview_can_clear_every_tick_at_once():
    """1,005 rows arrive all ticked, so the real gesture is "clear all, then pick five".

    A TOGGLE rather than a lone Unselect button, with its label derived from the live count so it
    cannot claim the wrong action.
    """
    source = _ads_template()
    assert "data-toggle-all" in source, "there is no select/unselect-all control"
    assert "Unselect all" in source and "Select all" in source, (
        "the control does not name both directions, so it is a button rather than a toggle"
    )


def test_the_preview_names_the_ad_group_not_only_the_campaign():
    """**The same keyword text exists in several ad groups at different bids.**

    So the campaign alone does not identify the row whose live bid is about to change.
    `attach_names` has always resolved `ad_group_name` in one query; the preview never rendered it.
    """
    source = _ads_template()
    body = source[source.index("function renderPreview("):]
    body = body[:body.index("\nfunction ")]
    assert "ad_group_name" in body, "the preview does not show which ad group a row belongs to"


def test_an_unavailable_suggested_bid_says_why_rather_than_rendering_blank():
    """**Sponsored Brands has no suggested-bid endpoint — measured, three probed, all 404.**

    ~296 rows in a typical preview have none. A blank cell in a bid column reads as "no suggestion,
    bid low"; the honest answer is that Amazon does not offer one. Same three-state discipline as
    the Portfolio tab's ACOS column.
    """
    source = _ads_template()
    body = source[source.index("function suggestedCell("):]
    body = body[:body.index("\nfunction ")]
    assert "suggested_unavailable" in body, "the reason is not shown"
    assert "—" in body, "an unavailable suggestion does not render a dash"
```

Add the template reader near the other helpers in that file if it is not already present:

```python
def _ads_template() -> str:
    return (Path(__file__).parent.parent / "templates" / "ads.html").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_ads_api.py -q -p no:randomly -k "preview_has_apply or clear_every_tick or names_the_ad_group or unavailable_suggested"`

Expected: 4 FAILED.

- [ ] **Step 3: Add the apply bar and suggested-bid cell builders**

In `templates/ads.html`, above `renderPreview`, add:

```javascript
/* ONE builder for the apply controls, rendered ABOVE and BELOW the table.

   A real rule on this account matched 1,005 rows, which put the only Apply button a screenful of
   scrolling below the thing it applies to. Both positions rather than a move: the top bar is what
   gets used on a long list, the bottom one serves someone who has read to the end. One function so
   the two cannot come to disagree about the count — the same reason `applySort` is shared by the
   mouse and keyboard paths on the Portfolio tab. */
function applyBarHtml(){
  const total = (plan.changes || []).length;
  const allTicked = approved.size === total && total > 0;
  return `<div class="controls" data-apply-bar style="margin:0 0 10px">
    <button class="btn-danger" data-apply>Apply <span class="apply-count">${approved.size}</span>
      change(s) to Amazon</button>
    <button class="btn-outline" data-toggle-all>${allTicked ? "Unselect all" : "Select all"}</button>
    <button class="btn-outline" data-cancel-preview>Cancel</button>
    <span class="dim" style="font-size:12px">Recorded with the previous bid, so it can be undone.</span>
  </div>`;
}

/* Amazon's suggested bid. THREE states, and they must not look alike — the same discipline the
   Portfolio tab's ACOS column follows:
     a real suggestion   -> the number, with its low-high range
     none offered        -> a dash plus the reason (Sponsored Brands has no endpoint at all)
     not asked for       -> a dash
   A blank cell in a bid column reads as "no suggestion, so bid low", which is a different claim. */
function suggestedCell(change){
  if(change.suggested_bid === null || change.suggested_bid === undefined){
    const why = change.suggested_unavailable || "";
    return `<span class="dim" title="${esc(why)}">—</span>`;
  }
  const range = (change.suggested_low !== null && change.suggested_low !== undefined)
    ? `<br/><span class="dim" style="font-size:10.5px">₹${change.suggested_low.toFixed(2)}–${
        change.suggested_high.toFixed(2)}</span>`
    : "";
  return `₹${change.suggested_bid.toFixed(2)}${range}`;
}
```

- [ ] **Step 4: Render both bars, the ad group, and the new column**

In `renderPreview`, change the row template to add the ad group under the campaign and a suggested
cell before the bid:

```javascript
      <td>${esc(c.text)}<br/><span class="mono">${esc(c.campaign_name || c.campaign_id)}</span>${
        c.ad_group_name ? `<br/><span class="mono dim">${esc(c.ad_group_name)}</span>` : ""}</td>
```

and after the ACOS cell:

```javascript
      <td class="num">${suggestedCell(c)}</td>
```

Add the header `<th scope="col" class="num">Suggested</th>` between ACOS and Bid. Then in the card
markup, put `${applyBarHtml()}` immediately before `<div class="table-wrap">` and replace the
existing bottom `<div class="controls">…</div>` block with `${applyBarHtml()}`.

- [ ] **Step 5: Wire the delegated listeners**

The existing listener block uses `#apply-btn` and `#cancel-preview` ids, which cannot be duplicated.
Replace those handlers with attribute-based delegation in the same `preview-area` listener:

```javascript
  const toggle = e.target.closest("[data-toggle-all]");
  if(toggle){
    const total = (plan.changes || []).length;
    if(approved.size === total){ approved.clear(); }
    else { approved = new Set((plan.changes || []).map(c => c.entity_id)); }
    renderPreview();
    return;
  }
  if(e.target.closest("[data-apply]")){ applyPlan(); return; }
  if(e.target.closest("[data-cancel-preview]")){ plan = null; renderPreview(); return; }
```

Update the per-row tick handler to refresh both counts by calling `renderPreview()` rather than
setting `#apply-count` directly, or update every `.apply-count` node.

- [ ] **Step 6: Run the tests**

Run: `venv/Scripts/python -m pytest tests/test_ads_api.py tests/test_template_render_targets.py tests/test_local_dates.py tests/test_theme.py -q`

Expected: all pass. `test_template_render_targets.py` checks every `getElementById` that is written
to has an element — the ids removed here must not be left referenced.

- [ ] **Step 7: Verify the JavaScript parses**

```bash
node -e "const fs=require('fs');const s=fs.readFileSync('templates/ads.html','utf8');const m=[...s.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(x=>x[1]).join('\n');fs.writeFileSync('/tmp/ads.js',m);" && node --check /tmp/ads.js && echo "JS OK"
```

Expected: `JS OK`. A template test cannot catch a syntax error; this can.

- [ ] **Step 8: Commit**

```bash
git add templates/ads.html tests/test_ads_api.py
git commit -m "feat(ads): apply controls at the top, select-all toggle, ad group, suggested bid

A real rule matched 1,005 rows, so the only Apply button sat a screenful below
the list. Both positions now, from one builder so the counts cannot disagree.

The ad group matters because the same keyword text exists in several ad groups
at different bids, so the campaign alone does not identify the row whose live
bid is about to change — and attach_names already resolved it."
```

---

## Task 6: Documentation and production verification

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-08-29-ads-one-source-of-truth-design.md` (mark shipped)

- [ ] **Step 1: Run the full suite and the mutations**

```bash
venv/Scripts/python -m pytest -q
```

Then verify each mutation is caught, restoring the file after each:

| Mutation | Must fail |
|---|---|
| `save_daily` delete scoped by day alone | `test_storing_sb_for_a_day_does_not_delete_that_days_sp_rows` |
| `refresh` SB fetch without `daily=True` | `test_every_derived_window_carries_the_sponsored_brands_spend` |
| `daily_range_complete` checks endpoints only | `test_a_range_with_an_interior_gap_declines_to_answer` |
| `_parse_bid_recommendations` takes `bids[0]` | `test_the_middle_of_three_bids_is_the_suggestion` |
| `fetch_bid_recommendations` sends SB rows to the SP endpoint | `test_sponsored_brands_rows_are_reported_as_unavailable_not_blank` |

- [ ] **Step 2: Record the defect and decisions in CLAUDE.md**

Under the Ads tab section, add a subsection covering: the ₹1,26,328 SB loss and the 1,005-vs-743
rule gap; that the original comment reasoned from row count rather than spend share; that
`ads_performance` is deleted so one path answers; the `save_daily` `(day, ad_product)` scope and why
omitting it destroys SP; the measured 19.5 min / 259,900 rows per 31-day report; the four probed
bid-recommendation endpoints and the three that 404; that SB has no suggested bid; `KEEP_BACKUPS`
5 → 3; and `DAILY_RETENTION_DAYS` 60. Update the test count on line 10.

- [ ] **Step 3: Commit and deploy**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-08-29-ads-one-source-of-truth-design.md
git commit -m "docs: record the Ads one-source-of-truth change and its measurements"
git push origin claude/stoic-allen-bb3a55
ssh -i "<key>" ubuntu@13.233.144.148 "cd /opt/amazon-tracker && bash deploy/update-ec2.sh"
```

- [ ] **Step 4: Verify on production**

Run the reported rule on both windows and confirm they now agree:

```bash
# spend > 100, roas < 2, decrease 10% — must report the SAME totals on both windows
for w in "2026-08-22 2026-08-28" "2026-08-22 2026-08-29"; do set -- $w; done
```

Expected: **both windows report 1,005 changes including 296 SB rows** (or whatever the current
figures are, but IDENTICAL between the two for their shared days), and the KPI strip no longer drops
₹1,26,328 when a day is added.

Then check: the nightly job at 03:50 stores 60 days across 4 chunks; a 7d default view loads
instantly; the preview shows Apply at the top, an ad group per row, and a suggested bid for SP rows
with a dash and a reason for SB rows.

- [ ] **Step 5: Confirm disk**

```bash
ssh -i "<key>" ubuntu@13.233.144.148 "df -h / | tail -1; du -sh /home/ubuntu/tracker-backups/"
```

Expected: free space at or above ~835 MB, backups directory smaller than before (3 copies, not 5).

---

## Out of scope

`impactMetrics` (Amazon's estimated clicks and orders per bid level); automatically applying a
suggested bid; growing the EBS volume; any change to the guardrails, the mutation ledger, or the four
writers.
