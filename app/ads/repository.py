"""The only reader and writer of the ads cache, the saved rules and the mutation ledger.

SELECT-then-UPDATE-or-INSERT rather than a dialect-specific upsert, so the same code runs on SQLite
locally and PostgreSQL in production — the reasoning `shipment/repository.py` documents.

**Three kinds of row live here and the boundary is the design:**

* `AdsEntity` / `AdsPerformance` are a CACHE of Amazon's numbers. The refresh writes them, nothing
  edits them, and a wrong value is fixed by refreshing.
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
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ads import logic
from app.models import AdsEntity, AdsMutation, AdsPerformance, AdsRule, PortfolioSettings

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


async def load_campaigns(db: AsyncSession) -> list[dict]:
    """Every cached campaign, with its ad group count. 24 on this account, so no paging needed."""
    rows = (await db.execute(
        select(AdsEntity).where(AdsEntity.entity_type == "campaign").order_by(AdsEntity.name)
    )).scalars().all()

    counts = dict((await db.execute(
        select(AdsEntity.campaign_id, func.count())
        .where(AdsEntity.entity_type == "ad_group")
        .group_by(AdsEntity.campaign_id)
    )).all())

    return [
        {
            "campaign_id": r.entity_id,
            "name": r.name,
            "state": r.state,
            "daily_budget": _f(r.daily_budget),
            "ad_groups": int(counts.get(r.entity_id) or 0),
        }
        for r in rows
    ]


async def load_ad_groups(db: AsyncSession, campaign_id: str | None = None) -> list[dict]:
    """Cached ad groups, optionally for one campaign."""
    query = select(AdsEntity).where(AdsEntity.entity_type == "ad_group")
    if campaign_id:
        query = query.where(AdsEntity.campaign_id == str(campaign_id))
    rows = (await db.execute(query.order_by(AdsEntity.name))).scalars().all()
    return [
        {
            "ad_group_id": r.entity_id,
            "campaign_id": r.campaign_id,
            "name": r.name,
            "state": r.state,
            "default_bid": _f(r.default_bid),
        }
        for r in rows
    ]


# ─── Performance ─────────────────────────────────────────────────────────────


async def save_performance(
    db: AsyncSession, window_start: str, window_end: str, rows: list[dict]
) -> int:
    """Store report rows for one window. Keyed on `(window, entity_id)`.

    Takes RAW report rows and normalises through `logic.metrics_for`, so the stored shape and the
    rule engine's view of a row cannot disagree — one parser, not two.
    """
    if not rows:
        return 0

    written = 0
    for raw in rows:
        m = logic.metrics_for(raw)
        entity_id = m["entity_id"]
        if not entity_id:
            continue

        existing = (await db.execute(
            select(AdsPerformance).where(
                AdsPerformance.window_start == window_start,
                AdsPerformance.window_end == window_end,
                AdsPerformance.entity_id == entity_id,
            )
        )).scalar_one_or_none()

        values = {
            "entity_type": "keyword" if m["writer"] == logic.WRITER_KEYWORD else "target",
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
            "fetched_at": datetime.utcnow(),
        }
        if existing:
            for field, value in values.items():
                setattr(existing, field, value)
        else:
            db.add(AdsPerformance(
                window_start=window_start, window_end=window_end,
                entity_id=entity_id, **values,
            ))
        written += 1

    await db.commit()
    return written


async def load_performance(
    db: AsyncSession,
    window_start: str,
    window_end: str,
    *,
    campaign_ids: list[str] | None = None,
    ad_group_ids: list[str] | None = None,
) -> list[dict]:
    """Stored rows for one window, shaped exactly as `logic.metrics_for` returns them.

    **Returned in the rule engine's own shape, not the database's.** A rule preview and an apply
    both read this, so a mismatch between what is stored and what a rule expects would be a bug
    that only appears at apply time — with a live bid on the other end of it.
    """
    query = select(AdsPerformance).where(
        AdsPerformance.window_start == window_start,
        AdsPerformance.window_end == window_end,
    )
    if campaign_ids:
        query = query.where(AdsPerformance.campaign_id.in_([str(c) for c in campaign_ids]))
    if ad_group_ids:
        query = query.where(AdsPerformance.ad_group_id.in_([str(a) for a in ad_group_ids]))

    rows = (await db.execute(query.order_by(AdsPerformance.spend.desc()))).scalars().all()

    out = []
    for r in rows:
        spend = _f(r.spend) or 0.0
        sales = _f(r.sales) or 0.0
        out.append({
            "entity_id": r.entity_id,
            "writer": logic.writer_for(r.match_type),
            "match_type": r.match_type,
            "text": r.text or "",
            "campaign_id": r.campaign_id or "",
            "campaign_name": "",
            "ad_group_id": r.ad_group_id or "",
            "ad_group_name": "",
            "bid": _f(r.reported_bid),
            "spend": spend,
            "sales": sales,
            "clicks": int(r.clicks or 0),
            "impressions": int(r.impressions or 0),
            "orders": int(r.orders or 0),
            # Recomputed here rather than stored, so a ratio can never disagree with its own
            # numerator. `None` when there is no denominator — never 0.0.
            "roas": (sales / spend) if spend else None,
            "acos": (spend / sales) if sales else None,
            "ctr": (r.clicks / r.impressions) if r.impressions else None,
            "cvr": (r.orders / r.clicks) if r.clicks else None,
            "cpc": (spend / r.clicks) if r.clicks else None,
        })
    return out


async def windows_available(db: AsyncSession) -> list[tuple[str, str]]:
    """Cached windows, newest first, so the picker can mark which ranges load instantly.

    A cached window is immediate; an uncached one is a ~5.5-minute report per 31 days, and the cost
    of a click should be visible before clicking it.
    """
    rows = (await db.execute(
        select(AdsPerformance.window_start, AdsPerformance.window_end)
        .group_by(AdsPerformance.window_start, AdsPerformance.window_end)
        .order_by(AdsPerformance.window_end.desc(), AdsPerformance.window_start.desc())
    )).all()
    return [(r[0], r[1]) for r in rows]


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
        row["campaign_name"] = names.get(row.get("campaign_id")) or row.get("campaign_id") or ""
        row["ad_group_name"] = names.get(row.get("ad_group_id")) or ""
    return rows


# ─── The mutation ledger ─────────────────────────────────────────────────────


async def open_run(
    db: AsyncSession,
    changes: list[dict],
    *,
    rule_summary: str,
    reverts_run_id: str | None = None,
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
        db.add(AdsMutation(
            run_id=run_id,
            entity_id=str(change["entity_id"]),
            entity_type=("keyword" if change.get("writer") == logic.WRITER_KEYWORD else "target"),
            writer=change.get("writer") or logic.WRITER_KEYWORD,
            text=(change.get("text") or "")[:500],
            campaign_id=change.get("campaign_id") or None,
            ad_group_id=change.get("ad_group_id") or None,
            # The value BEFORE. Without this the run is not reversible.
            old_bid=change.get("old_bid"),
            new_bid=change.get("new_bid"),
            status="pending",
            rule_summary=(rule_summary or "")[:300],
            reverts_run_id=reverts_run_id,
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
            "status": r.status,
            "error": r.error or "",
        }
        for r in rows
    ]


async def build_undo(db: AsyncSession, run_id: str) -> list[dict]:
    """The change list that reverses a run: every APPLIED row, with old and new swapped.

    **Only `applied` rows.** A `failed` row never changed at Amazon, so "undoing" it would write
    `old_bid` over a bid that was never replaced — turning a refused edit into a real one, in the
    opposite direction, which is worse than the original failure.

    `pending` rows are excluded for the same reason but a different cause: their outcome is unknown,
    and guessing in either direction is a write nobody asked for. They are reported to the owner
    instead.
    """
    rows = (await db.execute(
        select(AdsMutation).where(
            AdsMutation.run_id == run_id,
            AdsMutation.status == "applied",
        )
    )).scalars().all()

    undo = []
    for r in rows:
        if r.old_bid is None:
            # Cannot restore what was never recorded. Should be impossible — `open_run` always
            # writes it — but a row from a future code path with a null old_bid must be skipped
            # rather than written as bid 0.
            continue
        undo.append({
            "entity_id": r.entity_id,
            "writer": r.writer,
            "text": r.text or "",
            "campaign_id": r.campaign_id or "",
            "ad_group_id": r.ad_group_id or "",
            "old_bid": _f(r.new_bid),      # what it is now
            "new_bid": _f(r.old_bid),      # what it was before the run
        })
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


async def save_rule(db: AsyncSession, name: str, rule: dict) -> dict:
    """Create or replace a saved rule by name. Validates before storing.

    Validated on the way IN as well as out, so a rule that could never match cannot be saved and
    then puzzled over — the `good_rating: 99` lesson from the Portfolio tab.
    """
    conditions = rule.get("conditions") or []
    for condition in conditions:
        problem = logic.condition_error(condition)
        if problem:
            raise ValueError(problem)
    if rule.get("action") not in logic.ACTIONS:
        raise ValueError(f"Unknown action {rule.get('action')!r}.")

    existing = (await db.execute(
        select(AdsRule).where(AdsRule.name == name)
    )).scalar_one_or_none()

    values = {
        "conditions_json": json.dumps(conditions),
        "action": rule.get("action"),
        "amount": rule.get("amount"),
        "window_days": int(rule.get("window_days") or 7),
    }
    if existing:
        for field, value in values.items():
            setattr(existing, field, value)
    else:
        db.add(AdsRule(name=name, created_at=datetime.utcnow(), **values))

    await db.commit()
    return {"name": name, **values, "conditions": conditions}


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
            "amount": _f(r.amount),
            "window_days": int(r.window_days or 7),
            "last_run_at": r.last_run_at.isoformat() if r.last_run_at else "",
        })
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
