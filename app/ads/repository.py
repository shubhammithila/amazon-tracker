"""The only reader and writer of the ads cache, the saved rules and the mutation ledger.

SELECT-then-UPDATE-or-INSERT rather than a dialect-specific upsert, so the same code runs on SQLite
locally and PostgreSQL in production — the reasoning `shipment/repository.py` documents.

**Three kinds of row live here and the boundary is the design:**

* `AdsEntity` / `AdsPerformanceDaily` are a CACHE of Amazon's numbers. The refresh writes them,
  nothing edits them, and a wrong value is fixed by refreshing. **Per-day rows are the ONLY grain**
  — there used to be a per-window table beside them and the two disagreed by 28% of spend, because
  Sponsored Brands was written to one and not the other.
* `AdsRule` is the owner's saved rule. Amazon has no opinion about it.
* **`AdsMutation` is the audit trail and the undo**, and it is the only table here that must never
  be treated as disposable. It records what we asked Amazon to change and what the value was
  BEFORE, which is the only thing that makes a 299-row bid change reversible.

**Every Decimal is cast to float on the way out.** SQLAlchemy returns `Decimal` for `Numeric` and
`JSONResponse` cannot serialise it. Done here rather than per route, because this app has shipped
that exact defect twice — once with datetimes on the orders payload, once with `raw_kg` on the
purchasing view — and both were found in a browser on production rather than by a test.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ads import logic
from app.models import (
    AdsCoverageIgnore,
    AdsEntity,
    AdsMutation,
    AdsPerformanceDaily,
    AdsProductDaily,
    AdsRefresh,
    AdsRule,
    PortfolioSettings,
)

logger = logging.getLogger(__name__)

#: The guardrails live in `portfolio_settings` under their own name rather than in a new table.
#: One JSON row, the same pattern as the verdict thresholds, so adding a guardrail needs no
#: migration — and `logic.guardrail_error` validates every key on the way in AND out.
GUARDRAIL_SETTING_NAME = "ads_guardrails"


def _f(value) -> float | None:
    """`Decimal` -> `float`, preserving None. None is not 0.0 anywhere in this feature: a target
    with no bid inherits the ad group default, which is a different fact from bidding nothing."""
    return None if value is None else float(value)


# ─── The entity cache ────────────────────────────────────────────────────────


async def save_entities(db: AsyncSession, rows: list[dict]) -> int:
    """Upsert campaigns, ad groups, keywords or targets. Returns the number written.

    Keyed on `(entity_type, entity_id)`, so re-running a refresh updates rather than doubling — the
    same repeated-save safety the shipment plan needed for a flaky warehouse phone.
    """
    if not rows:
        return 0

    written = 0
    for row in rows:
        entity_id = str(row.get("entity_id") or "")
        entity_type = row.get("entity_type") or ""
        if not entity_id or not entity_type:
            continue

        existing = (await db.execute(
            select(AdsEntity).where(
                AdsEntity.entity_type == entity_type,
                AdsEntity.entity_id == entity_id,
            )
        )).scalar_one_or_none()

        values = {
            "ad_product": (row.get("ad_product") or "sp"),
            "parent_id": row.get("parent_id"),
            "campaign_id": row.get("campaign_id"),
            "name": (row.get("name") or "")[:500],
            "state": row.get("state"),
            "match_type": row.get("match_type"),
            "bid": row.get("bid"),
            "default_bid": row.get("default_bid"),
            "daily_budget": row.get("daily_budget"),
            "fetched_at": datetime.utcnow(),
        }
        if existing:
            for field, value in values.items():
                setattr(existing, field, value)
        else:
            db.add(AdsEntity(entity_type=entity_type, entity_id=entity_id, **values))
        written += 1

    await db.commit()
    return written


async def load_campaigns(db: AsyncSession, *, include_paused: bool = True) -> list[dict]:
    """Every cached campaign, with its ad group count. 24 on this account, so no paging needed.

    `include_paused=False` returns ENABLED only. **Verified on the live account: all 11 paused
    campaigns carry exactly Rs 0 of spend**, so hiding them cannot conceal money — which is why this
    is safe as the default view. It stays a filter rather than a hard exclusion because a campaign
    paused *today* may have spent earlier in the window, and that spend must remain findable.

    The ad group count is the ENABLED count when paused are hidden, so the number beside a campaign
    matches what expanding it will show. A count of 12 that opens to 3 rows reads as a bug.
    """
    query = select(AdsEntity).where(AdsEntity.entity_type == "campaign")
    if not include_paused:
        query = query.where(AdsEntity.state == "ENABLED")
    rows = (await db.execute(query.order_by(AdsEntity.name))).scalars().all()

    count_query = (
        select(AdsEntity.campaign_id, func.count())
        .where(AdsEntity.entity_type == "ad_group")
        .group_by(AdsEntity.campaign_id)
    )
    if not include_paused:
        count_query = count_query.where(AdsEntity.state == "ENABLED")
    counts = dict((await db.execute(count_query)).all())

    return [
        {
            "campaign_id": r.entity_id,
            "name": r.name,
            "state": r.state,
            "ad_product": r.ad_product or "sp",
            # Who optimises it. Derived from the NAME on every read, never stored — a rename would
            # otherwise leave a rule editing bids it was told to leave alone.
            "manager": logic.manager_of(r.name),
            "automated": logic.is_automated(r.name),
            "daily_budget": _f(r.daily_budget),
            "ad_groups": int(counts.get(r.entity_id) or 0),
        }
        for r in rows
    ]


async def load_ad_groups(
    db: AsyncSession,
    campaign_id: str | None = None,
    *,
    window: tuple[str, str] | None = None,
    include_paused: bool = True,
) -> list[dict]:
    """Cached ad groups, optionally for one campaign, optionally with their performance.

    **`window` rolls spend and sales up from the target rows rather than storing them.** An ad
    group's spend is by definition the sum of its keywords and targets, so a stored figure could
    disagree with the rows shown directly beneath it — the same reason the Portfolio tab computes a
    parent as the sum of its sizes and the Orders tab's "86 orders beside 87 lines" was a bug.

    Two queries regardless of how many ad groups there are: one for the entities, one for the
    rollup. 619 ad groups carry rows in a 7-day window, so a per-row query would be 619 round trips.
    """
    query = select(AdsEntity).where(AdsEntity.entity_type == "ad_group")
    if campaign_id:
        query = query.where(AdsEntity.campaign_id == str(campaign_id))
    if not include_paused:
        # ENABLED only. Verified on the live account: no paused ad group carries any spend, so this
        # cannot hide money — but it is a filter rather than a permanent exclusion, because a
        # paused-today group may have spent earlier in the window.
        query = query.where(AdsEntity.state == "ENABLED")
    rows = (await db.execute(query.order_by(AdsEntity.name))).scalars().all()

    totals: dict[str, dict] = {}
    if window:
        # **Summed from the per-day rows, the one source.**
        #
        # This used to query the per-window table first and fall back to these daily rows only when
        # that exact window had never been fetched — the SAME two-path arrangement that made
        # Rs 1,26,328 of Sponsored Brands vanish one level up, and with the identical failure mode:
        # the window table held SB rows and the daily table did not, so an ad group's spend depended
        # on which branch ran. Now there is one branch, so a campaign row and the ad groups beneath
        # it are summed from the same rows by construction.
        grouped = (await db.execute(
            select(
                AdsPerformanceDaily.ad_group_id,
                func.count(func.distinct(AdsPerformanceDaily.entity_id)).label("targets"),
                func.sum(AdsPerformanceDaily.spend).label("spend"),
                func.sum(AdsPerformanceDaily.sales).label("sales"),
                func.sum(AdsPerformanceDaily.clicks).label("clicks"),
                func.sum(AdsPerformanceDaily.impressions).label("impressions"),
                func.sum(AdsPerformanceDaily.orders).label("orders"),
            )
            .where(
                AdsPerformanceDaily.day >= window[0],
                AdsPerformanceDaily.day <= window[1],
            )
            .group_by(AdsPerformanceDaily.ad_group_id)
        )).all()

        for row in grouped:
            totals[str(row[0] or "")] = {
                "targets": int(row[1] or 0),
                "spend": round(float(row[2] or 0), 2),
                "sales": round(float(row[3] or 0), 2),
                "clicks": int(row[4] or 0),
                "impressions": int(row[5] or 0),
                "orders": int(row[6] or 0),
            }

    out = []
    for r in rows:
        figures = totals.get(r.entity_id) or {}
        spend = figures.get("spend", 0.0)
        sales = figures.get("sales", 0.0)
        out.append({
            "ad_group_id": r.entity_id,
            "campaign_id": r.campaign_id,
            "name": r.name,
            "state": r.state,
            "default_bid": _f(r.default_bid),
            "targets": figures.get("targets", 0),
            "spend": spend,
            "sales": sales,
            "clicks": figures.get("clicks", 0),
            "impressions": figures.get("impressions", 0),
            "orders": figures.get("orders", 0),
            # None, not 0 — an ad group with no spend has no ROAS, and 0 would sort it beside the
            # genuinely terrible ones.
            "roas": (sales / spend) if spend else None,
            "acos": (spend / sales) if sales else None,
        })
    return out


# ─── Performance ─────────────────────────────────────────────────────────────


#: How many days of per-day rows to keep. **60, matching what the nightly scrape fetches**, so every
#: range the tab offers is answerable from stored rows without an Amazon call.
#:
#: Measured at 8,384 rows/day in July (August is quieter at 6,107), so 60 days is ~503,000 rows and
#: ~93 MB. That fits on a box with 912 MB free because two other things were given back in the same
#: change: deleting the per-window table returned 17.1 MB, and `KEEP_BACKUPS` dropping from 5 to 3
#: returned ~180 MB. The bound exists at all because `update-ec2.sh` copies the whole database before
#: every deploy, so an unbounded table breaks the deploy before it breaks a query.
#:
#: It was 30, chosen as "the longest range Amazon answers in one report". That is no longer the
#: binding constraint: `split_window` chunks anything longer, so the number is now simply how much
#: history the owner asked to keep.
DAILY_RETENTION_DAYS = 60


async def save_daily(db: AsyncSession, rows: list[dict], *, ad_product: str = "sp") -> int:
    """Store per-day report rows. **Delete-then-bulk-insert per day, not the house upsert.**

    This is the one place in the codebase that deviates from SELECT-then-UPDATE-or-INSERT, and the
    reason is measured rather than stylistic:

        per-row upsert     498 rows/sec  ->  6.5 MINUTES for 30 days of data
        bulk insert     30,921 rows/sec  ->  6 SECONDS for the same data

    62x. And nothing is lost by replacing rather than merging: a day's rows are wholly superseded by
    a refetch of that day, so there is no earlier value an upsert would preserve. Scoped per DAY so
    refetching a 7-day window cannot disturb the other 23 days already stored.

    Portable: `delete()` + `insert()` through the ORM, no dialect-specific `ON CONFLICT`, so this
    still runs on PostgreSQL — the same constraint every other repository in this app respects.
    """
    if not rows:
        return 0

    mapped: list[dict] = []
    days: set[str] = set()
    now = datetime.utcnow()

    for raw in rows:
        m = logic.metrics_for(raw, ad_product)
        if not m["entity_id"]:
            continue
        day = str(raw.get("date") or "")[:10]
        if not day:
            # A DAILY report row without a date cannot be filed under a day, and guessing one would
            # put another day's spend into this one. Skipped rather than defaulted.
            continue
        days.add(day)
        mapped.append({
            "day": day,
            "entity_id": m["entity_id"],
            "entity_type": "target" if m["writer"] == logic.WRITER_TARGET else "keyword",
            "ad_product": m["ad_product"],
            "campaign_id": m["campaign_id"] or None,
            "ad_group_id": m["ad_group_id"] or None,
            "text": (m["text"] or "")[:500],
            "match_type": m["match_type"],
            "reported_bid": m["bid"],
            "impressions": m["impressions"],
            "clicks": m["clicks"],
            "spend": m["spend"],
            "orders": m["orders"],
            "sales": m["sales"],
            "fetched_at": now,
        })

    if not mapped:
        return 0

    # Replace exactly the days present in this payload, FOR THIS AD PRODUCT ONLY.
    #
    # **`ad_product` in this scope is load-bearing, and omitting it destroys data.** This function is
    # delete-then-bulk-insert (the measured 62x deviation from the house upsert), so a second call
    # for the same days replaces whatever the first one wrote. Sponsored Products and Sponsored
    # Brands are two separate reports covering the SAME days, so scoped by day alone the SB write
    # would delete every SP row it had just stored — leaving SB-only days, which is the
    # "Rs 1,26,328 vanished" bug inverted and worse, because SP is 72% of the spend.
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
    CHUNK = 5000
    for start in range(0, len(mapped), CHUNK):
        await db.execute(insert(AdsPerformanceDaily), mapped[start:start + CHUNK])
    await db.commit()

    logger.info("ads: stored %d daily row(s) across %d day(s)", len(mapped), len(days))
    return len(mapped)


async def daily_coverage(db: AsyncSession) -> tuple[str, str] | None:
    """The span of per-day rows held, as `(first_day, last_day)`, or None if empty.

    **Reports the SPAN, not a set of days.** A gap in the middle would make a sub-range sum silently
    short, so `daily_range_complete` checks each requested day rather than trusting this.
    """
    row = (await db.execute(
        select(func.min(AdsPerformanceDaily.day), func.max(AdsPerformanceDaily.day))
    )).first()
    if not row or not row[0]:
        return None
    return (row[0], row[1])


async def daily_days_held(db: AsyncSession, *, ad_product: str | None = None) -> set[str]:
    """Exactly which days we hold, optionally for one ad product.

    `ad_product` matters because a night is FOUR reports (two per product, since Amazon caps one at
    31 days) and any of them can be throttled — `sbTargeting` has been measured returning 429 after
    15 minutes of complete idleness. So "we hold this day" is not one fact, it is one per product.
    """
    query = select(AdsPerformanceDaily.day).group_by(AdsPerformanceDaily.day)
    if ad_product:
        query = query.where(AdsPerformanceDaily.ad_product == ad_product)
    rows = (await db.execute(query)).all()
    return {r[0] for r in rows}


def expected_days(start: str, end: str) -> list[str]:
    """Every calendar day in an inclusive range."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    out, cursor = [], first
    while cursor <= last:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


#: How many missing days a completeness answer names individually. A 60-day range can be short 53
#: days and the screen needs a sentence rather than a list; `missing_count` stays exact.
MISSING_DAYS_SHOWN = 5


async def daily_products(db: AsyncSession) -> list[str]:
    """Every ad product that has ever reported rows, sorted.

    **Derived from the data, never hardcoded `("sp", "sb")`.** Sponsored Display is a plausible third
    and adding it should be a fetch plus a writer, not an edit here.
    """
    return sorted(
        row[0] for row in (await db.execute(
            select(AdsPerformanceDaily.ad_product).group_by(AdsPerformanceDaily.ad_product)
        )).all() if row[0]
    )


async def held_days_by_product(db: AsyncSession) -> dict[str, set[str]]:
    """`{ad_product: {day, ...}}` for every product that has rows — ONE query.

    The Ads tab asks "which days are held" for the window on screen, for each of three presets and for
    two coverage spans on every click. Asked separately that was ~10 scans of a ~600,000-row table,
    about a second per click on production; read once here and passed to `range_completeness(held=…)`,
    every answer comes from the same reading, so they also cannot disagree with each other.
    """
    rows = (await db.execute(
        select(AdsPerformanceDaily.ad_product, AdsPerformanceDaily.day)
        .group_by(AdsPerformanceDaily.ad_product, AdsPerformanceDaily.day)
    )).all()
    out: dict[str, set[str]] = {}
    for product, day in rows:
        if product:
            out.setdefault(product, set()).add(day)
    return out


async def range_completeness(db: AsyncSession, start: str, end: str, *,
                             held: dict[str, set[str]] | None = None) -> dict:
    """Is this range summable, and if not, **which product is missing which days**?

    ``{"complete": bool, "products": [...], "missing": {product: [day, ...]},
       "missing_count": {product: int}, "held": {product: [first, last]}}``

    Two dimensions, and each was a real defect:

    * **Every DAY, not merely the endpoints.** A missing Tuesday would make the sum quietly
      understate spend, and an understated spend is what a bid rule would then act on.
    * **Every PRODUCT, not merely the union of days.** A night is four reports — Amazon caps one at
      31 days, so 60 days is two chunks per product — and any of them can be throttled. If the
      Sponsored Products chunk fails while Sponsored Brands lands, those days exist with SB rows
      only; asking "do we hold this day" without naming the product answers yes, `sum_daily` then
      reports 28% of the spend, and a rule previews against it. That is the Rs 1,26,328 failure.

    **A boolean was not enough, and that cost a morning.** On 1 Sep the nightly run stored 482,578
    Sponsored Products rows across 59 days and then Amazon rate-limited the Sponsored Brands report,
    storing **0** — so `sb` held 7 days against `sp`'s 59, every window outside those 7 days was
    correctly refused, and the screen could only say "nothing fetched" while half a million current
    rows sat in the table. Naming the product and the gap is the difference between a dashboard that
    is empty and one that is empty *for a reason the owner can act on*.

    `missing` is capped at `MISSING_DAYS_SHOWN`; `missing_count` is always exact, because "missing 25
    days" and "missing 5 days" call for different actions.
    """
    if held is None:
        held = await held_days_by_product(db)
    products = sorted(held)
    answer: dict = {
        "complete": bool(products),
        "products": products,
        "missing": {},
        "missing_count": {},
        "held": {},
    }
    if not products:
        return answer

    wanted = expected_days(start, end)
    for product in products:
        days = held[product]
        if days:
            answer["held"][product] = [min(days), max(days)]
        absent = [day for day in wanted if day not in days]
        if absent:
            answer["complete"] = False
            answer["missing_count"][product] = len(absent)
            answer["missing"][product] = absent[:MISSING_DAYS_SHOWN]
    return answer


async def daily_coverage_by_product(db: AsyncSession) -> dict[str, tuple[str, str]]:
    """Per-product spans: `{"sp": (first, last), "sb": (first, last)}`.

    **For PROSE only — this must never gate a decision.** A span cannot see an interior gap, which is
    exactly how the merged `daily_coverage` came to promise "summed instantly" for windows the server
    refused. What it is for is a sentence the owner can act on: "Sponsored Brands holds 2026-08-24 →
    2026-08-30" says which report is behind and by how much, where one merged span says nothing.
    `range_completeness` answers whether a range is summable.
    """
    rows = (await db.execute(
        select(
            AdsPerformanceDaily.ad_product,
            func.min(AdsPerformanceDaily.day),
            func.max(AdsPerformanceDaily.day),
        ).group_by(AdsPerformanceDaily.ad_product)
    )).all()
    return {r[0]: (r[1], r[2]) for r in rows if r[0] and r[1]}


async def daily_range_complete(db: AsyncSession, start: str, end: str) -> bool:
    """Do we hold every day in this range, for every ad product we advertise on?

    **A thin wrapper over `range_completeness`, deliberately.** The screen has to reach the same
    conclusion this returns — it marks each preset instant-or-not — and while the two were computed
    separately they disagreed: the template trusted `daily_coverage`, a span MERGED across products,
    so all three presets rendered "inside the daily data, summed instantly" beside a "Nothing
    fetched" banner and five zeroed KPIs. Measured on production, 1 Sep:

        7d 2026-08-25..2026-08-31  dot=True  server=False
       14d 2026-08-18..2026-08-31  dot=True  server=False
       30d 2026-08-02..2026-08-31  dot=True  server=False

    One rule computed twice is the defect class this codebase already records twice over (the Orders
    tab's "86 orders beside 87 lines"; the Portfolio parent rows that exist to prevent it). Keeping
    this name leaves its three callers untouched; moving the body is what makes the screen and the
    server one computation.
    """
    return (await range_completeness(db, start, end))["complete"]


async def sum_daily(
    db: AsyncSession,
    start: str,
    end: str,
    *,
    campaign_ids: list[str] | None = None,
    ad_group_ids: list[str] | None = None,
    ad_product: str | None = None,
) -> list[dict]:
    """Sum per-day rows into one row per entity for an arbitrary range. **No Amazon call.**

    This is the answer to "I already have 60 days — why must I refetch to see 20 of them?", and since
    the per-window table was deleted it is the ONLY way any figure on the tab is produced.

    **`ad_product` narrows to one product, and it is an opt-in for LOOKING, never for a rule.** It
    exists because a throttled Sponsored Brands report left `sb` 24 days behind `sp` while 482,578
    current Sponsored Products rows sat unreadable — the guard was right to refuse the mixed window,
    and there was no way to see the half that was fine. Every caller that passes it must label the
    result as excluding the other product's spend (28% of this account's), and `POST /ads/preview`
    must not accept it at all: a bid rule blind to a quarter of the spend is the exact failure the
    guard exists to prevent, on the one route in this app that spends money.

    **Grouped by `(entity_id, ad_product)`, not by `entity_id` alone**, and that is a correctness
    requirement rather than tidiness. Sponsored Products and Sponsored Brands are two separate APIs
    with two separate id spaces; nothing guarantees they never collide. Grouped by id alone, a
    colliding pair would be merged into one row whose product came from `max(ad_product)` — and
    `max('sb', 'sp')` is `'sp'`, so the row would be silently relabelled Sponsored Products and
    `logic.writer_for` would route a live bid change to `/sp/keywords` for a Sponsored Brands
    keyword. Measured today: 0 collisions across 29,360 ids, which is luck rather than a guarantee,
    and the cost of not relying on it is one column in a GROUP BY.

    `reported_bid` takes the LATEST day's value rather than a sum — adding bids across days would
    produce a number that means nothing.
    """
    query = (
        select(
            AdsPerformanceDaily.entity_id,
            func.max(AdsPerformanceDaily.entity_type),
            func.max(AdsPerformanceDaily.campaign_id),
            func.max(AdsPerformanceDaily.ad_group_id),
            func.max(AdsPerformanceDaily.text),
            func.max(AdsPerformanceDaily.match_type),
            func.sum(AdsPerformanceDaily.impressions),
            func.sum(AdsPerformanceDaily.clicks),
            func.sum(AdsPerformanceDaily.spend),
            func.sum(AdsPerformanceDaily.orders),
            func.sum(AdsPerformanceDaily.sales),
            func.max(AdsPerformanceDaily.day),
            AdsPerformanceDaily.ad_product,
        )
        .where(AdsPerformanceDaily.day >= start, AdsPerformanceDaily.day <= end)
        .group_by(AdsPerformanceDaily.entity_id, AdsPerformanceDaily.ad_product)
    )
    if campaign_ids:
        query = query.where(AdsPerformanceDaily.campaign_id.in_([str(c) for c in campaign_ids]))
    if ad_group_ids:
        query = query.where(AdsPerformanceDaily.ad_group_id.in_([str(a) for a in ad_group_ids]))
    if ad_product:
        query = query.where(AdsPerformanceDaily.ad_product == ad_product)

    grouped = (await db.execute(query)).all()
    if not grouped:
        return []

    # The bid as at the last day each entity appears — one extra query for the whole set rather
    # than one per entity.
    #
    # **Keyed `(entity_id, ad_product)` to match the grouping above.** Keyed on the id alone, a
    # colliding pair from the two id spaces would take whichever row happened to sort last and hand
    # one product's bid to the other — and this value becomes `old_bid` in the mutation ledger, which
    # is what an undo restores. A wrong bid here is a wrong bid written back to Amazon later.
    latest = {
        (row[0], row[1]): row[2]
        for row in (await db.execute(
            select(
                AdsPerformanceDaily.entity_id,
                AdsPerformanceDaily.ad_product,
                AdsPerformanceDaily.reported_bid,
            )
            .where(
                AdsPerformanceDaily.day >= start,
                AdsPerformanceDaily.day <= end,
                AdsPerformanceDaily.reported_bid.is_not(None),
            )
            .order_by(AdsPerformanceDaily.day)
        )).all()
    }

    out = []
    for row in grouped:
        spend = round(float(row[8] or 0), 2)
        sales = round(float(row[10] or 0), 2)
        clicks = int(row[7] or 0)
        impressions = int(row[6] or 0)
        orders = int(row[9] or 0)
        product = row[12] if len(row) > 12 and row[12] else "sp"
        out.append({
            "entity_id": row[0],
            "ad_product": product,
            "writer": logic.writer_for(row[5], product),
            "match_type": row[5],
            "text": row[4] or "",
            "campaign_id": row[2] or "",
            "campaign_name": "",
            "ad_group_id": row[3] or "",
            "ad_group_name": "",
            "bid": _f(latest.get((row[0], product))),
            "spend": spend,
            "sales": sales,
            "clicks": clicks,
            "impressions": impressions,
            "orders": orders,
            "roas": (sales / spend) if spend else None,
            "acos": (spend / sales) if sales else None,
            "ctr": (clicks / impressions) if impressions else None,
            "cvr": (orders / clicks) if clicks else None,
            "cpc": (spend / clicks) if clicks else None,
        })
    out.sort(key=lambda r: -r["spend"])
    return out


# ─── Product ads (SKU rules) ─────────────────────────────────────────────────
#
# **A separate table from `ads_performance_daily`, and the separation is the point.** A product ad's
# spend is the same money as its ad group's keyword spend; summed together, every campaign total on
# the tab would double. Nothing here reads or writes the keyword table.

#: How a product-ad range names itself in a completeness answer.
PRODUCT_GRAIN = "product"


async def save_product_daily(db: AsyncSession, rows: list[dict]) -> int:
    """Store `spAdvertisedProduct` DAILY rows, delete-then-insert per day like `save_daily`.

    Rows for the same `(day, adId)` are SUMMED before inserting rather than trusted to be unique —
    `spAdvertisedProduct` is the report the Portfolio tab measured arriving split by campaign.
    """
    merged: dict[tuple[str, str], dict] = {}
    now = datetime.utcnow()
    for raw in rows or ():
        day = str(raw.get("date") or "")[:10]
        ad_id = str(raw.get("adId") or "")
        if not day or not ad_id:
            continue
        row = merged.setdefault((day, ad_id), {
            "day": day, "ad_id": ad_id,
            "campaign_id": str(raw.get("campaignId") or "") or None,
            "ad_group_id": str(raw.get("adGroupId") or "") or None,
            "asin": (raw.get("advertisedAsin") or "")[:10] or None,
            "sku": (raw.get("advertisedSku") or "")[:80] or None,
            "impressions": 0, "clicks": 0, "spend": 0.0, "orders": 0, "sales": 0.0,
            "fetched_at": now,
        })
        row["impressions"] += logic._as_int(raw.get("impressions"))
        row["clicks"] += logic._as_int(raw.get("clicks"))
        row["orders"] += logic._as_int(raw.get("purchases7d"))
        row["spend"] = round(row["spend"] + logic._as_float(raw.get("cost")), 2)
        row["sales"] = round(row["sales"] + logic._as_float(raw.get("sales7d")), 2)
    if not merged:
        return 0
    days = sorted({k[0] for k in merged})
    await db.execute(delete(AdsProductDaily).where(AdsProductDaily.day.in_(days)))
    values = list(merged.values())
    for start in range(0, len(values), 5000):
        await db.execute(insert(AdsProductDaily), values[start:start + 5000])
    await db.commit()
    logger.info("ads: stored %d product-ad daily row(s) across %d day(s)", len(values), len(days))
    return len(values)


async def product_days_held(db: AsyncSession) -> set[str]:
    rows = (await db.execute(select(AdsProductDaily.day).group_by(AdsProductDaily.day))).all()
    return {r[0] for r in rows}


async def product_range_completeness(db: AsyncSession, start: str, end: str) -> dict:
    """`range_completeness`'s answer for the product-ad table: every DAY must be held."""
    held = await product_days_held(db)
    absent = [d for d in expected_days(start, end) if d not in held]
    answer: dict = {"complete": bool(held) and not absent, "products": [PRODUCT_GRAIN],
                    "missing": {}, "missing_count": {}, "held": {}}
    if held:
        answer["held"][PRODUCT_GRAIN] = [min(held), max(held)]
    if absent or not held:
        answer["missing_count"][PRODUCT_GRAIN] = len(absent)
        answer["missing"][PRODUCT_GRAIN] = absent[:MISSING_DAYS_SHOWN]
    return answer


async def sum_product_daily(
    db: AsyncSession,
    start: str,
    end: str,
    *,
    campaign_ids: list[str] | None = None,
    ad_group_ids: list[str] | None = None,
) -> list[dict]:
    """One row per product ad for an arbitrary range, summed from the per-day rows. No Amazon call.

    The SKU and ASIN are the ad's own (they cannot change for an `adId`), taken as `max` so a day
    with a blank one cannot blank the row.
    """
    query = (
        select(
            AdsProductDaily.ad_id,
            func.max(AdsProductDaily.campaign_id),
            func.max(AdsProductDaily.ad_group_id),
            func.max(AdsProductDaily.asin),
            func.max(AdsProductDaily.sku),
            func.sum(AdsProductDaily.impressions),
            func.sum(AdsProductDaily.clicks),
            func.sum(AdsProductDaily.spend),
            func.sum(AdsProductDaily.orders),
            func.sum(AdsProductDaily.sales),
        )
        .where(AdsProductDaily.day >= start, AdsProductDaily.day <= end)
        .group_by(AdsProductDaily.ad_id)
    )
    if campaign_ids:
        query = query.where(AdsProductDaily.campaign_id.in_([str(c) for c in campaign_ids]))
    if ad_group_ids:
        query = query.where(AdsProductDaily.ad_group_id.in_([str(a) for a in ad_group_ids]))
    out = []
    for row in (await db.execute(query)).all():
        out.append({
            "ad_id": row[0], "entity_id": row[0],
            "campaign_id": row[1] or "", "ad_group_id": row[2] or "",
            "asin": row[3] or "", "sku": row[4] or "",
            "impressions": int(row[5] or 0), "clicks": int(row[6] or 0),
            "spend": round(float(row[7] or 0), 2), "orders": int(row[8] or 0),
            "sales": round(float(row[9] or 0), 2),
        })
    out.sort(key=lambda r: -r["spend"])
    return out


async def replace_product_ads(db: AsyncSession, rows: list[dict]) -> int:
    """Replace the cached product-ad list (`ads_entity`, `entity_type="product_ad"`) wholesale.

    Bulk rather than `save_entities`' per-row upsert: 6,404 rows, and the list is always fetched
    whole. **An empty list writes nothing**, so a failed or empty fetch cannot wipe the states the
    last-active guard reads.
    """
    rows = [r for r in rows or () if r.get("entity_id")]
    if not rows:
        return 0
    now = datetime.utcnow()
    await db.execute(delete(AdsEntity).where(AdsEntity.entity_type == "product_ad"))
    values = [{
        "entity_type": "product_ad", "ad_product": "sp", "entity_id": str(r["entity_id"]),
        "parent_id": r.get("parent_id") or None, "campaign_id": r.get("campaign_id") or None,
        "name": (r.get("name") or "")[:500], "state": r.get("state") or None,
        "asin": (r.get("asin") or "")[:10] or None, "fetched_at": now,
    } for r in rows]
    for start in range(0, len(values), 5000):
        await db.execute(insert(AdsEntity), values[start:start + 5000])
    await db.commit()
    return len(values)


async def add_product_ads_to_cache(db: AsyncSession, rows: list[dict]) -> int:
    """Put product ads the SKU-coverage Add just created into the cached list, so the coverage panel
    stops naming them at once rather than at the next morning's refresh. Skips ids already held."""
    rows = [r for r in rows or () if r.get("entity_id")]
    if not rows:
        return 0
    held = set((await db.execute(
        select(AdsEntity.entity_id).where(
            AdsEntity.entity_type == "product_ad",
            AdsEntity.entity_id.in_([str(r["entity_id"]) for r in rows]))
    )).scalars().all())
    now = datetime.utcnow()
    added = 0
    for r in rows:
        if str(r["entity_id"]) in held:
            continue
        db.add(AdsEntity(
            entity_type="product_ad", ad_product="sp", entity_id=str(r["entity_id"]),
            parent_id=r.get("parent_id") or None, campaign_id=r.get("campaign_id") or None,
            name=(r.get("name") or "")[:500], state=r.get("state") or "ENABLED",
            asin=(r.get("asin") or "")[:10] or None, fetched_at=now))
        added += 1
    await db.commit()
    return added


async def load_coverage_ignores(db: AsyncSession) -> set[tuple[str, str]]:
    return {(i.ad_group_id, i.product_key)
            for i in (await db.execute(select(AdsCoverageIgnore))).scalars().all()}


async def set_coverage_ignore(db: AsyncSession, ad_group_id: str, product_key: str,
                              ignored: bool) -> None:
    """Mark (or unmark) one product of an ad group — or the whole group, `product_key="*"` — as
    deliberately not holding every SKU. Unmarking DELETES the row, so absence is "check it"."""
    ad_group_id, product_key = str(ad_group_id), str(product_key)
    row = (await db.execute(select(AdsCoverageIgnore).where(
        AdsCoverageIgnore.ad_group_id == ad_group_id,
        AdsCoverageIgnore.product_key == product_key))).scalar_one_or_none()
    if ignored and row is None:
        db.add(AdsCoverageIgnore(ad_group_id=ad_group_id, product_key=product_key))
    elif not ignored and row is not None:
        await db.delete(row)
    await db.commit()


async def enabled_product_ads(db: AsyncSession, ad_group_ids) -> dict[str, set[str]]:
    """`{ad_group_id: {enabled ad_id}}` for the preview's last-active guard.

    The cached list is from the morning refresh, so a pause or enable WE applied since is laid over
    it from the ledger (newest applied state, when newer than the cached reading). An ad group the
    cache does not hold is simply absent — `logic.last_active_refusals` then keeps one row on.
    """
    groups = sorted({str(g) for g in ad_group_ids or () if g})
    if not groups:
        return {}
    cached: list = []
    for start in range(0, len(groups), 500):
        cached += (await db.execute(
            select(AdsEntity.entity_id, AdsEntity.parent_id, AdsEntity.state, AdsEntity.fetched_at)
            .where(AdsEntity.entity_type == "product_ad",
                   AdsEntity.parent_id.in_(groups[start:start + 500]))
        )).all()
    ledger = await last_applied_states(db, [c[0] for c in cached])
    out: dict[str, set[str]] = {}
    for ad_id, group, state, fetched_at in cached:
        entry = ledger.get(ad_id)
        if entry and entry.get("at") and (fetched_at is None
                                          or entry["at"] > fetched_at.isoformat()):
            state = entry["state"]
        out.setdefault(group, set())
        if str(state or "").upper() == logic.STATE_ENABLED:
            out[group].add(ad_id)
    return out


async def campaign_totals(
    db: AsyncSession,
    start: str,
    end: str,
    *,
    ad_product: str | None = None,
) -> dict[str, dict]:
    """`{campaign_id: {spend, sales, clicks, impressions, orders, rows}}` for a range, summed IN SQL.

    **The campaign list needs only these totals**, and building them by summing every keyword row in
    Python (`sum_daily`) cost 2.4 s for 30 days on production: 27,728 rows fetched to make 29. Same
    per-day rows, same window, so the figures are the ones `sum_daily` would give — a test asserts it.
    `rows` counts distinct keywords and targets, which is what the old roll-up counted.
    """
    query = (
        select(
            AdsPerformanceDaily.campaign_id,
            func.sum(AdsPerformanceDaily.spend),
            func.sum(AdsPerformanceDaily.sales),
            func.sum(AdsPerformanceDaily.clicks),
            func.sum(AdsPerformanceDaily.impressions),
            func.sum(AdsPerformanceDaily.orders),
            func.count(func.distinct(AdsPerformanceDaily.entity_id + ":" + AdsPerformanceDaily.ad_product)),
        )
        .where(AdsPerformanceDaily.day >= start, AdsPerformanceDaily.day <= end)
        .group_by(AdsPerformanceDaily.campaign_id)
    )
    if ad_product:
        query = query.where(AdsPerformanceDaily.ad_product == ad_product)
    out: dict[str, dict] = {}
    for row in (await db.execute(query)).all():
        out[row[0] or ""] = {
            "spend": float(row[1] or 0), "sales": float(row[2] or 0), "clicks": int(row[3] or 0),
            "impressions": int(row[4] or 0), "orders": int(row[5] or 0), "rows": int(row[6] or 0),
        }
    return out


async def reclaim_space(db: AsyncSession) -> None:
    """`VACUUM` — actually return freed pages to the filesystem.

    **A `DELETE` does not shrink the file.** SQLite marks the pages free for REUSE inside the
    database, so a purge that removes 40,000 rows shows no change in `df` and the disk stays as full
    as it was. Measured: a purge plus VACUUM on production took the file from 46 MB to 43 MB, where
    the purge alone moved it not at all.

    Called after the nightly sweep rather than after every write: VACUUM rewrites the whole file, so
    it needs free space equal to the database size while it runs and briefly locks it. Once a night on
    an idle box is the right place; inside a request would be the wrong one.

    Best-effort by design — a VACUUM that cannot run (no room, or a concurrent write) must not fail
    the retention sweep that just succeeded.
    """
    from sqlalchemy import text

    try:
        # Outside a transaction: SQLite refuses VACUUM inside one.
        await db.commit()
        await db.execute(text("VACUUM"))
        logger.info("ads: VACUUM complete, freed pages returned to the filesystem")
    except Exception as exc:  # noqa: BLE001 - housekeeping must never break the sweep
        logger.warning("ads: VACUUM skipped (%s)", exc)


#: How long the bid-change ledger is kept. **365 days, and this is deliberately long.**
#:
#: Measured: ~0.34 KB per row, so 37 MB/year at one 300-row run a day and ~365 MB/year at three
#: 1,000-row runs. Monthly deletion was the first instinct and is wrong here on three counts — this
#: table is the **undo chain**, it is the **audit trail** for the only feature in this app that spends
#: money, and it is now the source of the **true current bid**. Unlike `ads_performance_daily` it is
#: also **not refetchable**: Amazon will not tell us what we set a bid to in July.
#:
#: So it is kept long and bounded rather than kept short. The bound exists at all because a box that
#: has sat at 89% disk copies the whole database before every deploy.
MUTATION_RETENTION_DAYS = 365


async def load_bid_log(
    db: AsyncSession,
    *,
    search: str | None = None,
    start: str | None = None,
    end: str | None = None,
    status: str | None = None,
    ascending: bool = False,
    limit: int = 500,
    offset: int = 0,
) -> dict:
    """Individual bid changes for the log view: `{"rows": [...], "total": int}`.

    **A sibling of `load_runs`, not a replacement.** That one answers "what did that run do"; this one
    answers **"what has happened to this keyword"**, which needs the rows ungrouped.

    `ascending=True` reads the BID PATH forwards — 13.86 -> 15.25 -> 16.78 — because a compounding
    mistake or an oscillation is a shape rather than a row, and a shape only reads in order.

    Paged because at three 1,000-row runs a day this table holds a million rows a year, and `total`
    counts every match rather than the page so the screen can say what it is showing part of.
    """
    query = select(AdsMutation)
    count_query = select(func.count()).select_from(AdsMutation)

    filters = []
    if search:
        like = f"%{search.strip()}%"
        # Text OR entity id: the owner searches for a keyword he remembers, but a support question
        # arrives as an id ("why did 155301615480093 move?").
        #
        # `ilike` rather than `like`, and it is portable — verified, SQLAlchemy compiles it to
        # `lower(col) LIKE lower(?)` on SQLite, so it is genuinely case-insensitive on both dialects
        # rather than relying on SQLite's ASCII-only LIKE.
        filters.append(AdsMutation.text.ilike(like) | AdsMutation.entity_id.ilike(like))
    if status:
        filters.append(AdsMutation.status == status)
    if start:
        filters.append(AdsMutation.created_at >= datetime.fromisoformat(start))
    if end:
        # **Inclusive of the whole END day.** The obvious `<= end` reads as inclusive and silently
        # drops every change made during that day, which makes the log look like it is missing data.
        filters.append(
            AdsMutation.created_at < datetime.fromisoformat(end) + timedelta(days=1)
        )
    for condition in filters:
        query = query.where(condition)
        count_query = count_query.where(condition)

    if ascending:
        query = query.order_by(AdsMutation.created_at.asc(), AdsMutation.id.asc())
    else:
        query = query.order_by(AdsMutation.created_at.desc(), AdsMutation.id.desc())

    rows = (await db.execute(
        query.limit(max(1, min(limit, 5000))).offset(max(0, offset))
    )).scalars().all()
    total = int((await db.execute(count_query)).scalar() or 0)

    return {
        "total": total,
        "rows": [
            {
                "entity_id": r.entity_id,
                "text": r.text or "",
                "writer": r.writer,
                "ad_product": r.ad_product or "sp",
                "campaign_id": r.campaign_id or "",
                "ad_group_id": r.ad_group_id or "",
                "old_bid": _f(r.old_bid),
                "new_bid": _f(r.new_bid),
                "status": r.status,
                # Amazon's own refusal, verbatim: their messages name the cause, and they are how the
                # bid floor and the 31-day report cap were both found.
                "error": r.error or "",
                "rule": r.rule_summary or "",
                "run_id": r.run_id,
                "reverts_run_id": r.reverts_run_id or "",
                "at": r.created_at.isoformat() if r.created_at else "",
                # The IST day, because that is the day the owner thinks in and the column stores UTC.
                "day": logic.ist_day(r.created_at),
            }
            for r in rows
        ],
    }


async def purge_mutations(db: AsyncSession, *, keep_days: int = MUTATION_RETENTION_DAYS,
                          today: date | None = None) -> int:
    """Delete ledger rows older than the retention window. Returns the number removed.

    **Bounded but long** — see `MUTATION_RETENTION_DAYS` for why 12 months rather than 1.

    The cutoff is a whole day back, not a timestamp: an off-by-one here silently deletes a day of
    audit trail every night, and the boundary day is pinned by a test.
    """
    cutoff = (today or date.today()) - timedelta(days=keep_days)
    result = await db.execute(
        delete(AdsMutation).where(
            AdsMutation.created_at < datetime.combine(cutoff, datetime.min.time())
        )
    )
    await db.commit()
    removed = int(result.rowcount or 0)
    if removed:
        logger.info("ads: purged %d ledger row(s) older than %s", removed, cutoff.isoformat())
    return removed


async def purge_daily(db: AsyncSession, *, keep_days: int = DAILY_RETENTION_DAYS,
                      today: date | None = None) -> int:
    """Delete per-day rows older than the retention window. Returns the number removed.

    **Not optional housekeeping.** Production is at 91% disk and every deploy copies the whole
    database; without this the daily table grows without bound and eventually breaks both SQLite
    writes and the deploy itself.
    """
    cutoff = ((today or date.today()) - timedelta(days=keep_days - 1)).isoformat()
    result = await db.execute(
        delete(AdsPerformanceDaily).where(AdsPerformanceDaily.day < cutoff)
    )
    # The product-ad rows keep the same 60 days: a SKU rule offers the same windows.
    products = await db.execute(delete(AdsProductDaily).where(AdsProductDaily.day < cutoff))
    # The search-term rows keep the same 60 days: a negative rule offers the same windows.
    from app.models import AdsSearchTermDaily
    terms = await db.execute(delete(AdsSearchTermDaily).where(AdsSearchTermDaily.day < cutoff))
    await db.commit()
    removed = int(result.rowcount or 0) + int(products.rowcount or 0) + int(terms.rowcount or 0)
    if removed:
        logger.info("ads: purged %d daily row(s) older than %s", removed, cutoff)
    return removed


async def attach_names(db: AsyncSession, rows: list[dict]) -> list[dict]:
    """Fill campaign and ad group NAMES onto performance rows, in two queries rather than per row.

    The report carries names but the stored rows deliberately do not duplicate them — a renamed
    campaign would leave stale text on every one of its 12,854 rows. Resolved from the entity cache
    at read time instead, which is one query per level regardless of row count.
    """
    if not rows:
        return rows

    names = dict((await db.execute(
        select(AdsEntity.entity_id, AdsEntity.name)
        .where(AdsEntity.entity_type.in_(("campaign", "ad_group")))
    )).all())

    for row in rows:
        campaign_name = names.get(row.get("campaign_id")) or row.get("campaign_id") or ""
        row["campaign_name"] = campaign_name
        row["ad_group_name"] = names.get(row.get("ad_group_id")) or ""
        # **Set HERE, where the campaign name is resolved.** `plan_run` recomputes it if absent, but
        # attaching it at the one point the name becomes known means the preview can show which rows
        # M19 or Amazon manages without every caller remembering to classify.
        row["manager"] = logic.manager_of(campaign_name)
    return rows


# ─── The mutation ledger ─────────────────────────────────────────────────────


async def open_run(
    db: AsyncSession,
    changes: list[dict],
    *,
    rule_summary: str,
    reverts_run_id: str | None = None,
    rule_window: str | None = None,
) -> str:
    """Write every intended change as `pending` and return the run id. **Called BEFORE any request.**

    This ordering is the whole safety mechanism. If the process dies mid-run, the ledger already
    holds every row that was in flight together with the bid it had before — so the damage is
    knowable and reversible. Writing after the fact would leave a successful Amazon change with no
    local record, which is the one state that cannot be recovered from.

    A `uuid4` rather than an autoincrement, because the id must exist before the first row is
    written and it travels in the URL of an undo.
    """
    run_id = str(uuid.uuid4())
    now = datetime.utcnow()

    for change in changes:
        # **`ad_product` is passed explicitly, and it was not before.** Measured on production: 304
        # Sponsored Brands rows sat in this table labelled `sp`, because the column default won
        # whenever the field was omitted — which was always. Harmless in effect, since `writer` is
        # what `split_by_writer` routes on, but the column exists precisely so the audit trail can
        # name the API that was written to. `entity_type` had the same fault in the same expression:
        # every SB keyword was recorded as a `target`.
        db.add(AdsMutation(
            run_id=run_id,
            entity_id=str(change["entity_id"]),
            entity_type=("keyword" if change.get("writer") in (
                logic.WRITER_KEYWORD, logic.WRITER_SB_KEYWORD)
                else "product_ad" if change.get("writer") == logic.WRITER_PRODUCT_AD
                else "neg_keyword" if str(change.get("writer") or "").endswith("_kw")
                else "neg_target" if str(change.get("writer") or "").startswith("neg_")
                else "target"),
            ad_product=change.get("ad_product") or logic.AD_PRODUCT_SP,
            writer=change.get("writer") or logic.WRITER_KEYWORD,
            text=(change.get("text") or "")[:500],
            campaign_id=change.get("campaign_id") or None,
            ad_group_id=change.get("ad_group_id") or None,
            # **Which KIND of change, derived from what the plan actually carries.** Keyed on
            # `new_state` rather than on a passed-in action, so a caller cannot label a state change
            # as a bid change; the two column pairs are then mutually exclusive by construction.
            # `create` is a product ad the SKU-coverage Add is about to make: its `entity_id` is a
            # provisional `new-…` id until Amazon answers with the real `adId`
            # (`record_create_results`).
            action=("negate" if change.get("negate") else
                    "create" if change.get("create") else
                    "state" if change.get("new_state") else "bid"),
            # The value BEFORE. Without this the run is not reversible.
            old_bid=change.get("old_bid"),
            new_bid=change.get("new_bid"),
            old_state=change.get("old_state"),
            new_state=change.get("new_state"),
            status="pending",
            rule_summary=(rule_summary or "")[:300],
            reverts_run_id=reverts_run_id,
            rule_window=(rule_window or "")[:23] or None,
            created_at=now,
        ))

    await db.commit()
    logger.info("ads: run %s opened with %d pending mutation(s)", run_id, len(changes))
    return run_id


async def record_results(db: AsyncSession, run_id: str, results: list[dict]) -> dict:
    """Mark each row of a run `applied` or `failed` from Amazon's per-row outcome.

    Returns `{"applied": n, "failed": n, "pending": n}`. **`pending` should be 0** — a non-zero
    count means Amazon did not report on a row we sent, which is surfaced rather than assumed
    successful.
    """
    by_id = {str(r["entity_id"]): r for r in results}
    now = datetime.utcnow()

    rows = (await db.execute(
        select(AdsMutation).where(AdsMutation.run_id == run_id)
    )).scalars().all()

    for row in rows:
        result = by_id.get(row.entity_id)
        if result is None:
            continue
        row.status = "applied" if result.get("ok") else "failed"
        row.error = None if result.get("ok") else (result.get("error") or "")[:2000]
        row.sent_at = now

    await db.commit()

    counts = {"applied": 0, "failed": 0, "pending": 0}
    for row in rows:
        if row.status in counts:
            counts[row.status] += 1
    logger.info("ads: run %s -> %s", run_id, counts)
    return counts


async def record_create_results(db: AsyncSession, run_id: str, results: list[dict]) -> dict:
    """`record_results` for a run of CREATED product ads: each row Amazon made takes its real `adId`.

    The provisional `new-…` id stays on a refused or unanswered row, which can never be undone (there
    is nothing at Amazon to pause). Returns the counts, like `record_results`.
    """
    by_id = {str(r["entity_id"]): r for r in results}
    now = datetime.utcnow()
    rows = (await db.execute(select(AdsMutation).where(AdsMutation.run_id == run_id))).scalars().all()
    for row in rows:
        result = by_id.get(row.entity_id)
        if result is None:
            continue
        ok = bool(result.get("ok") and result.get("ad_id"))
        row.status = "applied" if ok else "failed"
        row.error = None if ok else (result.get("error") or "Amazon did not return the new ad's id.")[:2000]
        if ok:
            row.entity_id = str(result["ad_id"])
        row.sent_at = now
    await db.commit()
    counts = {"applied": 0, "failed": 0, "pending": 0}
    for row in rows:
        if row.status in counts:
            counts[row.status] += 1
    logger.info("ads: create run %s -> %s", run_id, counts)
    return counts


async def last_applied_bids(db: AsyncSession, entity_ids: Sequence[str]) -> dict[str, dict]:
    """`{entity_id: {"bid", "at", "rule", "day"}}` — the newest APPLIED bid change per entity.

    **This is where the true current bid comes from, and it needs no Amazon call.** A performance
    report does not re-issue because we changed a bid, so the figure a preview shows is stale the
    moment a rule runs — measured on production, the report held 13.86 for a keyword we had just set
    to 15.25. The ledger already records what we set it to.

    **Only `applied` rows.** A `failed` row never changed anything at Amazon and a `pending` one is
    unknown; treating either as the current bid would compute the next change from a value Amazon
    never held. The same rule `build_undo` follows when it reverses only applied rows.

    `day` is the IST calendar day (`logic.ist_day`), because "not twice on the same day" is an IST
    decision while this column stores UTC.

    Chunked, because a real rule matched 1,005 rows and SQLite caps the length of an `IN (...)` list.
    """
    wanted = [str(e) for e in entity_ids if e]
    if not wanted:
        return {}

    out: dict[str, dict] = {}
    CHUNK = 500
    for start in range(0, len(wanted), CHUNK):
        rows = (await db.execute(
            select(AdsMutation)
            .where(
                AdsMutation.entity_id.in_(wanted[start:start + CHUNK]),
                AdsMutation.status == "applied",
                AdsMutation.new_bid.is_not(None),
            )
            # ASCENDING, so the last write per entity wins as the dict is filled. Descending with a
            # `setdefault` would work too; this way the newest row simply overwrites, which is one
            # fewer thing to get backwards.
            .order_by(AdsMutation.created_at, AdsMutation.id)
        )).scalars().all()
        for row in rows:
            out[row.entity_id] = {
                "bid": _f(row.new_bid),
                "at": row.created_at.isoformat() if row.created_at else "",
                "rule": row.rule_summary or "",
                "day": logic.ist_day(row.created_at),
            }
    return out


async def last_applied_states(db: AsyncSession, entity_ids: Sequence[str]) -> dict[str, dict]:
    """`{entity_id: {"state", "at", "rule", "day"}}` — the newest APPLIED state change per entity.

    **A sibling of `last_applied_bids` rather than a parameter on it, because that one CANNOT do this
    job.** It filters `new_bid IS NOT NULL`, which is what correctly stops a paused row being served as
    the true current bid — and is exactly what makes it blind to a state row. Reusing it as the state
    guard's basis would leave the guard silently doing nothing: the screen would still render the
    guarded-row machinery while every row arrived ticked.

    **What the guard prevents here differs from the bid version.** A repeated bid change COMPOUNDS
    (15.25 x 1.10 = 16.78, so -10% twice is -19%). A repeated pause is idempotent. What this stops is a
    pause/enable FLIP-FLOP inside one day — a keyword turned off by one rule and back on by the next,
    ending wherever the last run happened to land.

    **Only `applied` rows**, like the bid version: a `failed` row never changed anything at Amazon and
    a `pending` one is unknown, so neither may gate a later run.

    Chunked for the same reason — a real rule matched 1,005 rows and SQLite caps an `IN (...)` list.
    """
    wanted = [str(e) for e in entity_ids if e]
    if not wanted:
        return {}

    out: dict[str, dict] = {}
    CHUNK = 500
    for start in range(0, len(wanted), CHUNK):
        rows = (await db.execute(
            select(AdsMutation)
            .where(
                AdsMutation.entity_id.in_(wanted[start:start + CHUNK]),
                AdsMutation.status == "applied",
                AdsMutation.new_state.is_not(None),
            )
            # ASCENDING, so the last write per entity simply overwrites as the dict is filled — one
            # fewer thing to get backwards than a descending scan with `setdefault`.
            .order_by(AdsMutation.created_at, AdsMutation.id)
        )).scalars().all()
        for row in rows:
            out[row.entity_id] = {
                "state": row.new_state,
                "at": row.created_at.isoformat() if row.created_at else "",
                "rule": row.rule_summary or "",
                "day": logic.ist_day(row.created_at),
            }
    return out


async def changes_today(db: AsyncSession, entity_ids: Sequence[str], day: str) -> dict[str, list[dict]]:
    """`{entity_id: [{"rule", "at", "bid_from", "bid_to", "state"}]}` — every APPLIED change on `day`.

    **The basis of the per-RULE once-a-day guard.** `last_applied_bids` gives only the newest row per
    entity, which cannot say whether THIS rule ran today once a second rule has run after it. Oldest
    first, so the note on a preview row reads in the order things happened.

    **`day` is the IST calendar day.** The column stores UTC, so the query takes a window wide enough
    to hold the IST day and the rows are then kept by `logic.ist_day` — one definition of "today",
    never a hand-computed offset. Only `applied` rows: an undone (`reverted`) change put the keyword
    back where it was, so it must not stop the rule running again.
    """
    wanted = [str(e) for e in entity_ids if e]
    if not wanted:
        return {}
    start = datetime.fromisoformat(day) - timedelta(days=1)
    end = datetime.fromisoformat(day) + timedelta(days=1)
    out: dict[str, list[dict]] = {}
    CHUNK = 500
    for i in range(0, len(wanted), CHUNK):
        rows = (await db.execute(
            select(AdsMutation)
            .where(
                AdsMutation.entity_id.in_(wanted[i:i + CHUNK]),
                AdsMutation.status == "applied",
                AdsMutation.created_at >= start,
                AdsMutation.created_at < end,
            )
            .order_by(AdsMutation.created_at, AdsMutation.id)
        )).scalars().all()
        for row in rows:
            if logic.ist_day(row.created_at) != day:
                continue
            out.setdefault(row.entity_id, []).append({
                "rule": row.rule_summary or "",
                "at": row.created_at.isoformat() if row.created_at else "",
                "bid_from": _f(row.old_bid),
                "bid_to": _f(row.new_bid),
                "state": row.new_state or "",
            })
    return out


async def load_runs(db: AsyncSession, limit: int = 25) -> list[dict]:
    """Recent runs, newest first, each with its counts — the history panel and the undo list."""
    run_ids = (await db.execute(
        select(AdsMutation.run_id, func.max(AdsMutation.created_at).label("at"))
        .group_by(AdsMutation.run_id)
        .order_by(func.max(AdsMutation.created_at).desc())
        .limit(limit)
    )).all()

    out = []
    for run_id, _ in run_ids:
        rows = (await db.execute(
            select(AdsMutation).where(AdsMutation.run_id == run_id)
        )).scalars().all()
        if not rows:
            continue
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        first = rows[0]
        out.append({
            "run_id": run_id,
            "rule": first.rule_summary or "",
            "at": first.created_at.isoformat() if first.created_at else "",
            "rows": len(rows),
            "applied": counts.get("applied", 0),
            "failed": counts.get("failed", 0),
            "pending": counts.get("pending", 0),
            "reverted": counts.get("reverted", 0),
            "reverts_run_id": first.reverts_run_id,
            # Only an APPLIED row can be undone: a failed row never changed at Amazon, and undoing
            # it would write a bid that was never replaced.
            "undoable": counts.get("applied", 0) > 0,
        })
    return out


async def load_run(db: AsyncSession, run_id: str) -> list[dict]:
    """Every row of one run, for the detail view and for building an undo."""
    rows = (await db.execute(
        select(AdsMutation)
        .where(AdsMutation.run_id == run_id)
        .order_by(AdsMutation.id)
    )).scalars().all()
    return [
        {
            "entity_id": r.entity_id,
            "writer": r.writer,
            "text": r.text or "",
            "campaign_id": r.campaign_id or "",
            "ad_group_id": r.ad_group_id or "",
            "old_bid": _f(r.old_bid),
            "new_bid": _f(r.new_bid),
            "old_state": r.old_state or "",
            "new_state": r.new_state or "",
            "action": r.action or "bid",
            "ad_product": r.ad_product or logic.AD_PRODUCT_SP,
            "rule": r.rule_summary or "",
            "at": r.created_at.isoformat() if r.created_at else "",
            "status": r.status,
            "error": r.error or "",
        }
        for r in rows
    ]


async def entity_names(db: AsyncSession, ids) -> dict[str, str]:
    """`{entity_id: name}` for campaigns and ad groups, in one query — for documents that name them."""
    wanted = sorted({str(i) for i in ids if i})
    if not wanted:
        return {}
    out: dict[str, str] = {}
    for start in range(0, len(wanted), 500):
        out.update(dict((await db.execute(
            select(AdsEntity.entity_id, AdsEntity.name)
            .where(AdsEntity.entity_type.in_(("campaign", "ad_group")),
                   AdsEntity.entity_id.in_(wanted[start:start + 500]))
        )).all()))
    return out


async def build_undo(db: AsyncSession, run_id: str) -> list[dict]:
    """The change list that reverses a run: every APPLIED row, with old and new swapped.

    **Only `applied` rows.** A `failed` row never changed at Amazon, so "undoing" it would write
    `old_bid` over a bid that was never replaced — turning a refused edit into a real one, in the
    opposite direction, which is worse than the original failure.

    `pending` rows are excluded for the same reason but a different cause: their outcome is unknown,
    and guessing in either direction is a write nobody asked for. They are reported to the owner
    instead.

    **A state row is reversed by swapping the state pair, so the undo of a pause IS an enable** — a
    forward `set_state` through the same writer, with no reverse-specific code. `old_state` is the
    value read live from Amazon at apply time, never a value taken from the report.
    """
    rows = (await db.execute(
        select(AdsMutation).where(
            AdsMutation.run_id == run_id,
            AdsMutation.status == "applied",
        )
    )).scalars().all()

    undo = []
    for r in rows:
        common = {
            "entity_id": r.entity_id,
            "writer": r.writer,
            # Carried so an SB payload can be rebuilt: `adGroupId` is required on every SB write and
            # `campaignId` on an SB keyword state write. Dropping them here is a per-row refusal
            # inside a 207 whose HTTP status says success.
            "ad_product": r.ad_product or logic.AD_PRODUCT_SP,
            "text": r.text or "",
            "campaign_id": r.campaign_id or "",
            "ad_group_id": r.ad_group_id or "",
        }

        # **Branch on `action`, because the null-check below is per-KIND.**
        #
        # This used to skip any row with a null `old_bid`, under a comment saying that was impossible.
        # It was, when written. On a state row a null bid is NORMAL — so left as a blanket check,
        # undoing an 88-row pause run would reverse nothing and report success. Exactly the shape of
        # `delete_draft_plans`, whose docstring asserted an invariant that a later feature invalidated,
        # and which destroyed 400 units of packed stock on production.
        if r.action == "negate":
            # A negative a search-term rule added. Undo REMOVES it (SP); a Sponsored Brands negative
            # cannot be removed (Amazon's archive is permanent), and a provisional id never reached
            # Amazon — both are skipped, and the undo route says how many.
            if r.entity_id.startswith("neg-") or (r.writer or "").startswith("neg_sb"):
                continue
            undo.append({**common, "old_state": logic.STATE_ENABLED, "new_state": "ARCHIVED",
                         "negate_undo": True})
            continue

        if r.action == "create":
            # A product ad the coverage Add made. Amazon has no delete, so the undo PAUSES it. A row
            # still on its provisional id never reached Amazon and has nothing to pause.
            if r.entity_id.startswith("new-"):
                continue
            undo.append({**common, "old_state": logic.STATE_ENABLED,
                         "new_state": logic.STATE_PAUSED})
            continue

        if r.action == "state":
            if not r.old_state:
                # Nothing measured to restore. `/ads/apply` always records the live state, so this
                # means a row from a path that did not — skipped rather than guessed, because guessing
                # writes a state Amazon may never have held.
                continue
            undo.append({**common,
                         "old_state": r.new_state,     # what it is now
                         "new_state": r.old_state})    # what it was before the run
            continue

        if r.old_bid is None:
            # Cannot restore what was never recorded. Should be impossible for a bid row — `open_run`
            # always writes it — but a row from a future code path with a null old_bid must be skipped
            # rather than written as bid 0.
            continue
        undo.append({**common,
                     "old_bid": _f(r.new_bid),      # what it is now
                     "new_bid": _f(r.old_bid)})     # what it was before the run
    return undo


async def mark_reverted(db: AsyncSession, run_id: str, entity_ids: list[str]) -> int:
    """Flag the rows of an original run whose undo Amazon accepted.

    Only those: if an undo partially fails, the rows that were not restored must keep reading
    `applied` so a second undo can still reach them.
    """
    if not entity_ids:
        return 0
    rows = (await db.execute(
        select(AdsMutation).where(
            AdsMutation.run_id == run_id,
            AdsMutation.entity_id.in_([str(i) for i in entity_ids]),
        )
    )).scalars().all()
    for row in rows:
        row.status = "reverted"
    await db.commit()
    return len(rows)


# ─── Saved rules ─────────────────────────────────────────────────────────────


class RuleNameTaken(ValueError):
    """The name belongs to another saved rule. A 409, so the screen can offer to replace it."""


class RuleNotFound(ValueError):
    """`id` names no saved rule. A 404."""


async def save_rule(db: AsyncSession, name: str, rule: dict) -> dict:
    """Create, update or replace a saved rule. Validates before storing.

    * `rule["id"]` set: update THAT rule, rename allowed. Another rule already holding the new name is
      refused (`RuleNameTaken`) rather than silently overwritten.
    * no `id`, name free: create.
    * no `id`, name taken: refused unless `rule["replace"]` — saving a new rule over an existing one is
      a decision, not something a typo should do.

    Validated on the way IN as well as out, so a rule that could never match cannot be saved and
    then puzzled over — the `good_rating: 99` lesson from the Portfolio tab.
    """
    level = rule.get("level") or logic.LEVEL_KEYWORD
    if level not in logic.LEVELS:
        raise ValueError(f"Unknown rule level {level!r}.")
    conditions = rule.get("conditions") or []
    for condition in conditions:
        problem = logic.condition_error(condition, level)
        if problem:
            raise ValueError(problem)
    action = rule.get("action")
    if action not in logic.ACTIONS:
        raise ValueError(f"Unknown action {action!r}.")
    if level == logic.LEVEL_PRODUCT and not logic.is_state_action(action):
        raise ValueError("A product rule can only turn a SKU off or on.")
    if (level == logic.LEVEL_SEARCH) != (action == logic.ACTION_NEGATE):
        raise ValueError("Only a search-term rule adds negatives, and it does nothing else.")
    if action == logic.ACTION_NEGATE:
        rule = {**rule, "amount": None}

    # **A state rule's amount is a WORD, and it goes in its own column.**
    #
    # `amount` is `Numeric(12, 2)`, so storing "PAUSED" there raised
    # `could not convert string to float: 'PAUSED'` and `POST /ads/rules` returned 500 — the rule
    # never saved, which is what "I just saved a rule but it is not showing" was.
    #
    # Validated here rather than only at run time, the `good_rating: 99` lesson: a value accepted now
    # and refused later is a rule the owner has to debug at the moment they want to use it. `ARCHIVED`
    # is refused by name, because it is terminal at Amazon and has no undo.
    amount, target_state = rule.get("amount"), None
    if logic.is_state_action(action):
        problem = logic.state_error(amount)
        if problem:
            raise ValueError(problem)
        amount, target_state = None, str(amount).strip().upper()

    named = (await db.execute(
        select(AdsRule).where(AdsRule.name == name)
    )).scalar_one_or_none()
    if rule.get("id") is not None:
        existing = await db.get(AdsRule, int(rule["id"]))
        if existing is None:
            raise RuleNotFound("That saved rule no longer exists.")
        if named is not None and named.id != existing.id:
            raise RuleNameTaken(f"Another rule is already called {name!r}.")
        existing.name = name
    else:
        existing = named
        if existing is not None and not rule.get("replace"):
            raise RuleNameTaken(f"A rule called {name!r} already exists.")

    values = {
        "conditions_json": json.dumps(conditions),
        "action": action,
        "amount": amount,
        "target_state": target_state,
        "window_days": int(rule.get("window_days") or 7),
        "level": level,
    }
    if existing:
        for field, value in values.items():
            setattr(existing, field, value)
    else:
        existing = AdsRule(name=name, created_at=datetime.utcnow(), **values)
        db.add(existing)

    await db.commit()
    # `amount` is echoed back as the caller SENT it — the state for a state rule, the number otherwise
    # — so the screen can re-render the saved rule without knowing which column it landed in.
    return {"id": existing.id, "name": name, **values, "amount": target_state or amount,
            "conditions": conditions}


async def mark_rules_run(db: AsyncSession, rule_ids: Sequence[int]) -> None:
    """Stamp `last_run_at` on the saved rules a run was applied from, so the panel can say when."""
    if not rule_ids:
        return
    now = datetime.utcnow()
    for rule in (await db.execute(select(AdsRule).where(AdsRule.id.in_(list(rule_ids))))).scalars():
        rule.last_run_at = now
    await db.commit()


async def load_rules(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(select(AdsRule).order_by(AdsRule.name))).scalars().all()
    out = []
    for r in rows:
        try:
            conditions = json.loads(r.conditions_json or "[]")
        except json.JSONDecodeError:
            conditions = []
        out.append({
            "id": r.id,
            "name": r.name,
            "conditions": conditions,
            "action": r.action,
            # **The state wins where there is one.** `_f()` on "PAUSED" would give 0.0 or None, so a
            # saved pause rule would load into the screen with no state and silently become a
            # different rule from the one that was saved.
            "amount": r.target_state if r.target_state else _f(r.amount),
            "target_state": r.target_state or "",
            "window_days": int(r.window_days or 7),
            "level": r.level or logic.LEVEL_KEYWORD,
            "last_run_at": r.last_run_at.isoformat() if r.last_run_at else "",
            # Which column of the saved-rules panel: grow (raise spend) or cut (lower it).
            "impact": logic.rule_impact(r.action, r.target_state),
        })
    return out


async def ran_today_by_rule(db: AsyncSession, day: str) -> dict[str, dict[str, str]]:
    """`{rule sentence: {rule_window: newest applied-at ISO}}` for the IST ``day``.

    What greys a saved rule: it ran TODAY on THE WINDOW ON SCREEN. Asked for as *"when we change the
    time period… the rules which were set grey shows again normally since it has not been run on that
    time period"*. Only `applied` rows count (an undone run is `reverted`).
    """
    start = datetime.fromisoformat(day) - timedelta(days=1)
    end = datetime.fromisoformat(day) + timedelta(days=1)
    out: dict[str, dict[str, str]] = {}
    for summary, window, created in (await db.execute(
            select(AdsMutation.rule_summary, AdsMutation.rule_window, AdsMutation.created_at)
            .where(AdsMutation.status == "applied", AdsMutation.created_at >= start,
                   AdsMutation.created_at < end, AdsMutation.reverts_run_id.is_(None)))).all():
        if not summary or logic.ist_day(created) != day:
            continue
        at = created.isoformat()
        slot = out.setdefault(summary, {})
        key = window or ""
        if at > slot.get(key, ""):
            slot[key] = at
    return out


async def delete_rule(db: AsyncSession, name: str) -> bool:
    existing = (await db.execute(
        select(AdsRule).where(AdsRule.name == name)
    )).scalar_one_or_none()
    if not existing:
        return False
    await db.delete(existing)
    await db.commit()
    return True


# ─── Guardrails ──────────────────────────────────────────────────────────────


async def load_guardrails(db: AsyncSession) -> dict:
    """The guardrails, merged over the defaults and range-checked on READ.

    Validated on the way out as well as in: a value already stored, or hand-edited, would otherwise
    keep weakening the only bid ceiling that exists — Amazon does not enforce one.
    """
    row = (await db.execute(
        select(PortfolioSettings).where(PortfolioSettings.name == GUARDRAIL_SETTING_NAME)
    )).scalar_one_or_none()
    stored = {}
    if row and row.value_json:
        try:
            stored = json.loads(row.value_json) or {}
        except json.JSONDecodeError:
            logger.warning("ads: stored guardrails are not valid JSON; using the defaults")
    return logic.guardrails_or_default(stored)


async def save_guardrails(db: AsyncSession, values: dict, *, updated_by: str = "") -> dict:
    """Validate and store the guardrails. Raises `ValueError` naming the first problem.

    Refuses an unknown key rather than ignoring it: silently dropping a setting the owner believes
    he changed is how a ceiling ends up higher than he thinks.
    """
    for key, value in (values or {}).items():
        problem = logic.guardrail_error(key, value)
        if problem:
            raise ValueError(problem)

    merged = logic.guardrails_or_default(values)
    row = (await db.execute(
        select(PortfolioSettings).where(PortfolioSettings.name == GUARDRAIL_SETTING_NAME)
    )).scalar_one_or_none()
    if row:
        row.value_json = json.dumps(merged)
        row.updated_by = updated_by or row.updated_by
    else:
        db.add(PortfolioSettings(
            name=GUARDRAIL_SETTING_NAME,
            value_json=json.dumps(merged),
            updated_by=updated_by,
        ))
    await db.commit()
    return merged


async def reset_guardrails(db: AsyncSession) -> dict:
    """Delete the stored row so the measured defaults apply again.

    Deleting rather than writing the defaults, so "never customised" and "customised back to the
    defaults" are the same state — the same reasoning as clearing a product decision.
    """
    row = (await db.execute(
        select(PortfolioSettings).where(PortfolioSettings.name == GUARDRAIL_SETTING_NAME)
    )).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return dict(logic.DEFAULT_GUARDRAILS)


# ─── Refresh history ─────────────────────────────────────────────────────────
#
# **This section exists because the equivalent state was in memory and a deploy erased it.**
# On 1 Sep the nightly run stored 482,578 Sponsored Products rows and Amazon throttled the Sponsored
# Brands report, storing 0. `refresh.STATE` recorded that faithfully — and the app restarted, so the
# Ads tab said "nothing fetched" with no way to learn which half was missing or why.


async def record_refresh(
    db: AsyncSession,
    *,
    window_start: str | None,
    window_end: str | None,
    sp_rows: int = 0,
    sb_rows: int = 0,
    campaigns: int = 0,
    ad_groups: int = 0,
    error: str | None = None,
    sb_error: str | None = None,
    started_at: datetime | None = None,
    product_rows: int = 0,
    product_error: str | None = None,
    search_rows: int = 0,
    search_error: str | None = None,
) -> None:
    """Log one refresh attempt: succeeded, partly succeeded, or failed.

    **A FAILED run is recorded too**, which is the point — a tab showing four-day-old figures should
    be able to say "the last three refreshes were throttled" rather than merely looking stale. Named
    after `portfolio.repository.record_refresh`, which does the same job for the Economics feed.

    `status` is derived here rather than at the call site so every writer agrees what "partial" means:
    a run that stored something but could not store everything.
    """
    if error:
        status = "failed"
    elif sb_error or product_error or search_error:
        status = "partial"
    else:
        status = "done"
    db.add(AdsRefresh(
        window_start=window_start,
        window_end=window_end,
        status=status,
        sp_rows=int(sp_rows or 0),
        sb_rows=int(sb_rows or 0),
        campaigns=int(campaigns or 0),
        ad_groups=int(ad_groups or 0),
        error=error,
        sb_error=sb_error,
        product_rows=int(product_rows or 0),
        product_error=product_error,
        search_rows=int(search_rows or 0),
        search_error=search_error,
        started_at=started_at or datetime.utcnow(),
        finished_at=datetime.utcnow(),
    ))
    await db.commit()


async def last_refresh(db: AsyncSession) -> dict | None:
    """The newest refresh attempt, JSON-safe, or None if it has never run.

    What makes the Ads tab able to explain an empty window **after a restart**, which is the entire
    reason this table exists.
    """
    row = (await db.execute(
        select(AdsRefresh)
        .order_by(AdsRefresh.started_at.desc(), AdsRefresh.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    if row is None:
        return None
    return {
        "window_start": row.window_start,
        "window_end": row.window_end,
        "status": row.status or "done",
        "sp_rows": int(row.sp_rows or 0),
        "sb_rows": int(row.sb_rows or 0),
        "campaigns": int(row.campaigns or 0),
        "ad_groups": int(row.ad_groups or 0),
        "error": row.error or "",
        "sb_error": row.sb_error or "",
        "product_rows": int(row.product_rows or 0),
        "product_error": row.product_error or "",
        "search_rows": int(row.search_rows or 0),
        "search_error": row.search_error or "",
        # isoformat HERE, not in the route: a datetime reaching JSONResponse is a 500, and that exact
        # defect has already shipped once on this project.
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }
