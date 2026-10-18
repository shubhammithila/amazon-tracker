"""Store and read the Analytics months.

**Every economics row is written through the Profit tab's own `logic.size_row`**, so fees are ex-GST
and Net is computed by the one rule the Profit tab uses. Read back, each month is rebuilt in Amazon's
nested shape (`portfolio.repository._summed_amazon_row`) so `logic.portfolio` can roll it up exactly
as it rolls up a Profit window: products, flavours, the Active flag and Net come for free and cannot
drift from the Profit tab.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import delete, func, insert, select

from app.models import (
    AnalyticsMonthly, AnalyticsRefresh, AnalyticsTrafficMonthly, EconomicsDaily,
)
from app.portfolio.repository import ASIN_GRAIN, _float, _json, _summed_amazon_row


def rows_from_economics(raw: list[dict]) -> list[dict]:
    """Amazon's MONTH rows -> column dicts. A row with no `startDate` is skipped, never guessed."""
    from app.portfolio import logic

    now = datetime.utcnow()
    out: dict[tuple[str, str], dict] = {}
    for r in raw:
        month = str(r.get("startDate") or "")[:7]
        if len(month) != 7:
            continue
        parsed = logic.size_row(r, {})
        if not parsed["asin"]:
            continue
        out[(month, parsed["asin"])] = {
            "month": month, "child_asin": parsed["asin"], "parent_asin": parsed["parent_asin"] or None,
            "ordered_sales": parsed["sales"], "refunded_sales": parsed["refunded"],
            "ad_spend": parsed["ad_spend"], "net_proceeds": parsed["net"],
            "units_ordered": parsed["units_ordered"], "units_refunded": parsed["units_refunded"],
            "net_units": parsed["units"], "fees_json": json.dumps(parsed["fees"]),
            "ads_json": json.dumps(parsed["ad_types"]), "fetched_at": now,
        }
    return list(out.values())


def rows_from_traffic(raw: list[dict], month: str) -> list[dict]:
    """One row per child ASIN, SUMMING the rows Amazon splits it into.

    **A child moved to a new parent mid-month comes back as one row per parent.** Measured on Aug 2026:
    249 rows for 129 ASINs (Chana Sattu 500g under its old parent and under B0DR322QYG). Keeping one
    row per ASIN halved the month's sessions. Counts are summed and Buy Box % is weighted by page
    views; the parent kept is the one with more page views (the current listing)."""
    now = datetime.utcnow()
    out: dict[str, dict] = {}
    weight: dict[str, list[float]] = {}
    for r in raw:
        asin = str(r.get("childAsin") or "").strip().upper()
        if not asin:
            continue
        traffic = r.get("traffic") or {}
        sales = r.get("sales") or {}
        views = int(traffic.get("pageViews") or 0)
        row = out.setdefault(asin, {"month": month, "child_asin": asin, "parent_asin": None, "sessions": 0,
                                    "page_views": 0, "units_ordered": 0, "buy_box_pct": None, "sales": 0.0,
                                    "fetched_at": now, "_lead": -1})
        if views > row["_lead"]:
            row["parent_asin"] = str(r.get("parentAsin") or "").strip().upper() or None
            row["_lead"] = views
        row["sessions"] += int(traffic.get("sessions") or 0)
        row["page_views"] += views
        row["units_ordered"] += int(sales.get("unitsOrdered") or 0)
        row["sales"] = round(row["sales"] + _float((sales.get("orderedProductSales") or {}).get("amount")), 2)
        bb = traffic.get("buyBoxPercentage")
        if bb is not None:
            w = weight.setdefault(asin, [0.0, 0.0])
            w[0] += float(bb) * max(views, 1)
            w[1] += max(views, 1)
    for asin, row in out.items():
        row.pop("_lead")
        if asin in weight and weight[asin][1]:
            row["buy_box_pct"] = round(weight[asin][0] / weight[asin][1], 2)
    return list(out.values())


async def save_economics(db, rows: list[dict], months: list[str]) -> int:
    """Replace the given months: a re-run corrects rather than doubles, and other months are kept."""
    if months:
        await db.execute(delete(AnalyticsMonthly).where(AnalyticsMonthly.month.in_(months)))
    keep = [r for r in rows if r["month"] in set(months)]
    if keep:
        await db.execute(insert(AnalyticsMonthly), keep)
    return len(keep)


async def save_traffic(db, rows: list[dict], month: str) -> int:
    await db.execute(delete(AnalyticsTrafficMonthly).where(AnalyticsTrafficMonthly.month == month))
    if rows:
        await db.execute(insert(AnalyticsTrafficMonthly), rows)
    return len(rows)


async def sb_by_month(db) -> dict[str, dict]:
    """Sponsored Brands spend the Profit tab attributed, per month: `{month: {"asins": {asin: x},
    "days": days with an attribution}}`. Only the days `economics_daily` still holds (~60-90)."""
    out: dict[str, dict] = {}
    rows = await db.execute(
        select(EconomicsDaily.day, EconomicsDaily.child_asin, EconomicsDaily.sb_spend, EconomicsDaily.sb_basis)
        .where(EconomicsDaily.seller_sku == ASIN_GRAIN))
    days: dict[str, set] = {}
    for day, asin, sb, basis in rows:
        month = day[:7]
        if basis:
            days.setdefault(month, set()).add(day)
        if _float(sb):
            bucket = out.setdefault(month, {"asins": {}, "days": 0})
            bucket["asins"][asin] = round(bucket["asins"].get(asin, 0.0) + _float(sb), 2)
    for month, held in days.items():
        out.setdefault(month, {"asins": {}, "days": 0})["days"] = len(held)
    return out


async def load_months(db) -> dict[str, list[dict]]:
    """`{month: [Amazon-shaped row]}`, Sponsored Brands added to ads and taken off Net where held."""
    sb = await sb_by_month(db)
    out: dict[str, list[dict]] = {}
    for row in (await db.execute(select(AnalyticsMonthly))).scalars():
        sb_amount = (sb.get(row.month) or {}).get("asins", {}).get(row.child_asin, 0.0)
        agg = {
            "parent_asin": row.parent_asin,
            "ordered_sales": _float(row.ordered_sales), "refunded_sales": _float(row.refunded_sales),
            "ad_spend": _float(row.ad_spend),
            # Amazon's netProceeds already nets the SP spend it reports; SB is ours, so it comes off here.
            "net_proceeds": _float(row.net_proceeds) - sb_amount,
            "units_ordered": int(row.units_ordered or 0), "units_refunded": int(row.units_refunded or 0),
            "net_units": int(row.net_units or 0),
            "fees": {k: _float(v) for k, v in _json(row.fees_json).items()},
            "ad_types": {k: _float(v) for k, v in _json(row.ads_json).items()},
            "sb_spend": sb_amount,
        }
        out.setdefault(row.month, []).append(_summed_amazon_row((row.child_asin, ""), agg, (row.month, row.month)))
    return out


async def load_traffic(db) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for row in (await db.execute(select(AnalyticsTrafficMonthly))).scalars():
        out.setdefault(row.month, {})[row.child_asin] = {
            "parent_asin": row.parent_asin, "sessions": int(row.sessions or 0),
            "page_views": int(row.page_views or 0), "units": int(row.units_ordered or 0),
            "buy_box_pct": None if row.buy_box_pct is None else float(row.buy_box_pct),
        }
    return out


async def months_held(db) -> dict[str, set[str]]:
    econ = {m for (m,) in await db.execute(select(AnalyticsMonthly.month).distinct())}
    traffic = {m for (m,) in await db.execute(select(AnalyticsTrafficMonthly.month).distinct())}
    return {"economics": econ, "traffic": traffic}


async def data_version(db) -> tuple:
    a = (await db.execute(select(func.max(AnalyticsMonthly.fetched_at), func.count(AnalyticsMonthly.id)))).one()
    t = (await db.execute(select(func.max(AnalyticsTrafficMonthly.fetched_at)))).scalar()
    sb = (await db.execute(select(func.max(EconomicsDaily.fetched_at)))).scalar()
    return (str(a[0]), a[1], str(t), str(sb))


async def start_run(db, source: str, months: list[str]) -> AnalyticsRefresh:
    run = AnalyticsRefresh(source=source, status="running", months=",".join(months),
                           started_at=datetime.utcnow())
    db.add(run)
    await db.commit()
    return run


async def last_run(db) -> dict | None:
    run = (await db.execute(select(AnalyticsRefresh).where(AnalyticsRefresh.status != "running")
                            .order_by(AnalyticsRefresh.id.desc()).limit(1))).scalar()
    if run is None:
        return None
    return {"status": run.status, "source": run.source, "months": run.months, "error": run.error,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None}
