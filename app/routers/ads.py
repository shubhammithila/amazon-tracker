"""The Ads tab: campaign performance, and bulk bid edits by rule.

**The only router in this app that changes something at Amazon.** Every other feature reads Amazon
and writes our own records. `POST /ads/apply` sends bids, so the shape of this module is dictated by
one requirement: nothing reaches Amazon that the owner has not seen and approved.

That is why there are two endpoints rather than one. `POST /ads/preview` computes a plan and stores
nothing; `POST /ads/apply` takes the plan's approved rows and sends them. Merging them into a single
"run this rule" call would mean the first click both computed and applied — and the owner's real rule
matches 299 rows carrying Rs 102,945 of weekly spend.

**No route here calls Amazon for READ data.** The targeting report takes ~5.5 minutes to generate, so
every read serves stored rows and `POST /ads/refresh` starts the background job. The exceptions are
`apply` and `undo`, which necessarily talk to Amazon — and even they read the LIVE bid first, because
applying a percentage to a stale number silently undoes someone's manual edit.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import permissions
from app.ads import logic, refresh, repository, spapi_ads
from app.config import get_settings
from app.database import get_db
from app.portfolio.ads import AdsError, AdsNotConfigured
from app.routers.auth import require_area

router = APIRouter(prefix="/ads")
logger = logging.getLogger(__name__)

#: The presets the window bar offers. 7/14/30 are SINGLE reports and therefore attribution-exact;
#: above 31 days Amazon needs several reports and the sum is slightly conservative for attributed
#: sales. Those three are what a bid decision should be taken on.
PRESET_DAYS = (7, 14, 30)

#: The hard ceiling on a custom range. 60 because "data of last 60 days is valid for advertisement
#: optimisation" — the owner's requirement — and because a 90-day window would be three chained
#: reports for a number that is less exact than the 7-day one.
MAX_WINDOW_DAYS = 60


def _window(start: str | None, end: str | None, days: int | None,
            *, today: date | None = None) -> tuple[str, str]:
    """Resolve and VALIDATE a requested window. Raises `ValueError` with a readable reason.

    Two bounds, for different reasons:

    * **at most 60 days**, the owner's stated horizon for optimisation data.
    * **ending yesterday at the latest.** Today's figures are still settling — a click costs
      immediately while its attributed sale can arrive hours later — so a window including today
      shows a punishing ROAS every morning that recovers by evening. A bid rule acting on that
      would cut bids on a measurement artefact.
    """
    yesterday = (today or date.today()) - timedelta(days=1)

    if start and end:
        try:
            first = date.fromisoformat(start)
            last = date.fromisoformat(end)
        except ValueError:
            raise ValueError("Dates must be YYYY-MM-DD.")
        if first > last:
            raise ValueError(f"The window starts ({start}) after it ends ({end}).")
        if last > yesterday:
            raise ValueError(
                f"The window may not include today — advertising figures are still settling, "
                f"so the latest usable day is {yesterday.isoformat()}."
            )
        span = (last - first).days + 1
        if span > MAX_WINDOW_DAYS:
            raise ValueError(
                f"That window is {span} days; this tab reads at most {MAX_WINDOW_DAYS}."
            )
        return start, end

    requested = int(days or 7)
    if requested < 1 or requested > MAX_WINDOW_DAYS:
        raise ValueError(f"Pick between 1 and {MAX_WINDOW_DAYS} days.")
    return refresh.default_window(requested, today=today)


def _rule_summary(conditions, action: str, amount) -> str:
    """The rule in words, stored on every ledger row.

    So the history reads without joining to a rule that may since have been edited or deleted —
    "spend>100, 1<roas<3, decrease 10%" is what the owner recognises three weeks later.
    """
    parts = []
    for condition in conditions or ():
        field = logic.FIELDS.get(condition.get("field"), {}).get("label", condition.get("field"))
        operator = logic.OPERATORS.get(condition.get("op"), condition.get("op"))
        parts.append(f"{field} {operator} {condition.get('value')}")
    verb = {
        logic.ACTION_INCREASE_PCT: f"increase {amount}%",
        logic.ACTION_DECREASE_PCT: f"decrease {amount}%",
        logic.ACTION_INCREASE_ABS: f"increase by {amount}",
        logic.ACTION_DECREASE_ABS: f"decrease by {amount}",
        logic.ACTION_SET: f"set to {amount}",
    }.get(action, action)
    return f"{', '.join(parts)} -> bid {verb}"


# ─── Reads ───────────────────────────────────────────────────────────────────


@router.get("")
async def ads_dashboard(
    request: Request,
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
    campaign_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    """The campaign list with per-campaign performance for one window. Reads stored rows only.

    **An uncached window returns EMPTY rather than fetching**, with `cached: false` so the screen can
    offer the button. A GET that started a 6-minute report would hold the connection open, and a
    second page load would start a second report.
    """
    try:
        window_start, window_end = _window(start, end, days)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    settings = get_settings()
    available = await repository.windows_available(db)
    cached = (window_start, window_end) in available

    rows = []
    if cached:
        rows = await repository.load_performance(
            db, window_start, window_end,
            campaign_ids=[campaign_id] if campaign_id else None,
        )
        rows = await repository.attach_names(db, rows)

    campaigns = await repository.load_campaigns(db)

    # Roll the report rows up per campaign. Summed from the SAME rows the table shows, never a
    # second query — the Orders tab's "86 orders beside 87 lines" was exactly that mistake.
    per_campaign: dict[str, dict] = {}
    for row in rows:
        bucket = per_campaign.setdefault(row["campaign_id"], {
            "spend": 0.0, "sales": 0.0, "clicks": 0, "impressions": 0, "orders": 0, "rows": 0,
        })
        bucket["spend"] += row["spend"]
        bucket["sales"] += row["sales"]
        bucket["clicks"] += row["clicks"]
        bucket["impressions"] += row["impressions"]
        bucket["orders"] += row["orders"]
        bucket["rows"] += 1

    for campaign in campaigns:
        totals = per_campaign.get(campaign["campaign_id"]) or {}
        spend = round(totals.get("spend") or 0.0, 2)
        sales = round(totals.get("sales") or 0.0, 2)
        campaign.update({
            "spend": spend,
            "sales": sales,
            "clicks": totals.get("clicks") or 0,
            "impressions": totals.get("impressions") or 0,
            "orders": totals.get("orders") or 0,
            "targets": totals.get("rows") or 0,
            # None, not 0 — a campaign with no spend has no ROAS, and 0 would sort it alongside the
            # genuinely terrible ones.
            "roas": (sales / spend) if spend else None,
            "acos": (spend / sales) if sales else None,
        })

    total_spend = round(sum(c["spend"] for c in campaigns), 2)
    total_sales = round(sum(c["sales"] for c in campaigns), 2)

    return {
        "window": [window_start, window_end],
        "cached": cached,
        "windows_available": [list(w) for w in available],
        "preset_days": list(PRESET_DAYS),
        "max_window_days": MAX_WINDOW_DAYS,
        "configured": settings.ads_configured,
        "campaigns": campaigns,
        "rows": rows if campaign_id else [],
        "totals": {
            "spend": total_spend,
            "sales": total_sales,
            "roas": (total_sales / total_spend) if total_spend else None,
            "acos": (total_spend / total_sales) if total_sales else None,
            "campaigns": len(campaigns),
            "targets": len(rows),
        },
        "fields": logic.FIELDS,
        "operators": logic.OPERATORS,
        "actions": list(logic.ACTIONS),
        "guardrails": await repository.load_guardrails(db),
        "rules": await repository.load_rules(db),
        "runs": await repository.load_runs(db, limit=10),
        "refresh": refresh.status(),
        "grant": grant.areas if hasattr(grant, "areas") else [],
    }


@router.get("/ad-groups")
async def ad_groups(
    campaign_id: str,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    """Ad groups for one campaign — the middle level of the hierarchy."""
    return {"campaign_id": campaign_id, "ad_groups": await repository.load_ad_groups(db, campaign_id)}


@router.get("/targets")
async def targets(
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
    campaign_id: str | None = None,
    ad_group_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    """Keyword and target rows for one campaign or ad group, for the deepest level."""
    try:
        window_start, window_end = _window(start, end, days)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    rows = await repository.load_performance(
        db, window_start, window_end,
        campaign_ids=[campaign_id] if campaign_id else None,
        ad_group_ids=[ad_group_id] if ad_group_id else None,
    )
    rows = await repository.attach_names(db, rows)
    return {"window": [window_start, window_end], "rows": rows, "count": len(rows)}


@router.get("/refresh-status")
async def refresh_status(grant=Depends(require_area(permissions.ADS))):
    """Progress for the banner. Polled every few seconds while a refresh runs."""
    return refresh.status()


@router.post("/refresh")
async def start_refresh(
    request: Request,
    grant=Depends(require_area(permissions.ADS)),
):
    """Start the background refresh for a window. Returns immediately.

    **Refuses rather than queueing** when one is already running: two concurrent reports double the
    wait and race on the same rows.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an empty body is a valid "refresh the default window"
        body = {}

    try:
        window_start, window_end = _window(body.get("start"), body.get("end"), body.get("days"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    started = refresh.start(start=window_start, end=window_end)
    if not started:
        return JSONResponse(
            {"error": "A refresh is already running.", "status": refresh.status()},
            status_code=409,
        )
    return {"started": True, "window": [window_start, window_end], "status": refresh.status()}


# ─── Rules: preview and apply ────────────────────────────────────────────────


@router.post("/preview")
async def preview(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    """Compute what a rule WOULD change. **Sends nothing to Amazon and stores nothing.**

    Returns the full change list so the screen can show every `old -> new` and let the owner
    deselect rows. `blocked` refuses the whole rule (a guardrail breach); `skipped` lists individual
    rows that cannot be written, each with its reason — a row silently missing from a 299-row run is
    indistinguishable from a bug.
    """
    body = await request.json()

    try:
        window_start, window_end = _window(body.get("start"), body.get("end"), body.get("days"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    rows = await repository.load_performance(
        db, window_start, window_end,
        campaign_ids=body.get("campaign_ids") or None,
        ad_group_ids=body.get("ad_group_ids") or None,
    )
    if not rows:
        return JSONResponse(
            {"error": f"No performance data for {window_start}..{window_end}. "
                      f"Refresh that window first."},
            status_code=400,
        )
    rows = await repository.attach_names(db, rows)

    plan = logic.plan_run(
        rows,
        conditions=body.get("conditions") or [],
        action=body.get("action") or "",
        amount=body.get("amount"),
        guardrails=await repository.load_guardrails(db),
        scope_campaign_ids=body.get("campaign_ids") or None,
        scope_ad_group_ids=body.get("ad_group_ids") or None,
    )

    return {
        "window": [window_start, window_end],
        "rule": _rule_summary(body.get("conditions"), body.get("action"), body.get("amount")),
        **plan,
    }


@router.post("/apply")
async def apply(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    """Send approved bid changes to Amazon. **The only route in this app that mutates the account.**

    The sequence is deliberate and each step exists because of a measured failure mode:

    1. **Validate against the guardrails FIRST**, before anything else — including before the
       credentials check. The order matters: a request that breaches the bid ceiling must be refused
       *for that reason*, not incidentally rejected by whatever check happens to run first. A test
       caught this the wrong way round, where an over-ceiling bid returned "credentials are not
       configured" and the ceiling was never consulted at all.
    2. **Re-read the LIVE bid** for every row. The plan came from a report that may be hours old; a
       bid edited in Seller Central since would otherwise be overwritten with a percentage of a
       number that no longer exists.
    3. **Drop rows whose current bid has moved**, reporting them. Silently applying the stale figure
       is the bug that step exists to prevent.
    4. **Write the ledger as `pending`, with `old_bid`, BEFORE sending.** A crash mid-run then
       leaves a knowable, reversible state.
    5. **Split by writer.** Keywords and targeting clauses are different endpoints; the report calls
       both ids `keywordId`, and a misroute returns 207 with the failure buried in an error array.
    6. **Record each row's own outcome.** 207 Multi-Status makes partial failure normal.
    """
    body = await request.json()
    approved = body.get("changes") or []
    if not approved:
        return JSONResponse({"error": "No changes were approved."}, status_code=400)

    guardrails = await repository.load_guardrails(db)

    # Re-validate the approved rows against the guardrails, BEFORE the credentials check and before
    # any Amazon call. The preview already did this, but the browser sends the list back and a client
    # is not a trust boundary — a hand-edited request must not be able to exceed the ceiling, and it
    # must be told which limit it broke.
    # The row count first: it is the cheapest check, and a 600-row request should be refused for
    # being 600 rows rather than reported through whichever of its bids happens to be first.
    if len(approved) > guardrails["max_rows"]:
        return JSONResponse(
            {"error": f"{len(approved)} rows exceeds the {guardrails['max_rows']}-row limit."},
            status_code=400,
        )

    for change in approved:
        try:
            new_bid = float(change.get("new_bid"))
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": f"Row {change.get('entity_id')} has no usable bid."}, status_code=400
            )
        if new_bid > guardrails["max_bid"]:
            return JSONResponse(
                {"error": f"A bid of {new_bid} exceeds the ceiling of {guardrails['max_bid']}."},
                status_code=400,
            )
        if new_bid < guardrails["min_bid"]:
            return JSONResponse(
                {"error": f"A bid of {new_bid} is below the floor of {guardrails['min_bid']}."},
                status_code=400,
            )

    # Only now: the request is well-formed and within every limit, so a missing credential is the
    # real obstacle rather than a coincidence.
    settings = get_settings()
    if not settings.ads_configured:
        return JSONResponse(
            {"error": "Advertising credentials are not configured."}, status_code=400
        )

    rule_summary = body.get("rule") or "manual bid edit"

    try:
        async with httpx.AsyncClient(timeout=settings.ads_timeout) as client:
            # 1-2. The stale-bid guard.
            live = await spapi_ads.fetch_current_bids(client, approved)
            to_send, moved = [], []
            for change in approved:
                identifier = str(change["entity_id"])
                current = live.get(identifier)
                expected = change.get("old_bid")
                if current is None:
                    moved.append({**change, "live_bid": None,
                                  "reason": "Amazon no longer reports a bid for this row."})
                    continue
                if expected is not None and round(float(expected), 2) != round(current, 2):
                    moved.append({**change, "live_bid": current,
                                  "reason": f"The bid is now {current}, not {expected} — someone "
                                            f"changed it since this window was fetched."})
                    continue
                to_send.append(change)

            if not to_send:
                return {
                    "run_id": None, "applied": 0, "failed": 0, "pending": 0,
                    "moved": moved,
                    "note": "Every approved row had already changed at Amazon, so nothing was sent.",
                }

            # 3. The ledger, before the wire.
            run_id = await repository.open_run(db, to_send, rule_summary=rule_summary)

            # 4-5. Two endpoints, per-row outcomes.
            grouped = logic.split_by_writer(to_send)
            results: list[dict] = []
            for writer, rows in grouped.items():
                if rows:
                    results.extend(await spapi_ads.apply_bids(client, rows, writer=writer))

        counts = await repository.record_results(db, run_id, results)

    except AdsNotConfigured as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except AdsError as exc:
        logger.warning("ads apply failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)

    logger.info("ads: run %s applied %s", run_id, counts)
    return {
        "run_id": run_id,
        **counts,
        "moved": moved,
        "results": results,
    }


@router.post("/undo/{run_id}")
async def undo(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    """Restore the bids a run changed. Itself a bulk write, recorded as a new run.

    **Only `applied` rows are reversed** — a failed row never changed at Amazon, so restoring
    `old_bid` over it would turn a refused edit into a real one in the opposite direction.
    """
    changes = await repository.build_undo(db, run_id)
    if not changes:
        return JSONResponse(
            {"error": "Nothing in that run can be undone — no row was successfully applied."},
            status_code=400,
        )

    settings = get_settings()
    if not settings.ads_configured:
        return JSONResponse({"error": "Advertising credentials are not configured."},
                            status_code=400)

    try:
        async with httpx.AsyncClient(timeout=settings.ads_timeout) as client:
            undo_run = await repository.open_run(
                db, changes, rule_summary=f"undo of run {run_id}", reverts_run_id=run_id,
            )
            grouped = logic.split_by_writer(changes)
            results: list[dict] = []
            for writer, rows in grouped.items():
                if rows:
                    results.extend(await spapi_ads.apply_bids(client, rows, writer=writer))

        counts = await repository.record_results(db, undo_run, results)
        # Only the rows Amazon actually restored. A partly-failed undo must leave the rest
        # `applied`, so a second attempt can still reach them.
        restored = [r["entity_id"] for r in results if r.get("ok")]
        await repository.mark_reverted(db, run_id, restored)

    except AdsError as exc:
        logger.warning("ads undo failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=502)

    return {"run_id": undo_run, "reverts": run_id, **counts}


@router.get("/runs")
async def runs(
    limit: int = 25,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    """Recent runs with their counts — the history panel."""
    return {"runs": await repository.load_runs(db, limit=min(max(limit, 1), 100))}


@router.get("/runs/{run_id}")
async def run_detail(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    """Every row of one run, so a failure can be read rather than guessed at."""
    rows = await repository.load_run(db, run_id)
    if not rows:
        return JSONResponse({"error": "No such run."}, status_code=404)
    return {"run_id": run_id, "rows": rows, "count": len(rows)}


# ─── Saved rules and guardrails ──────────────────────────────────────────────


@router.post("/rules")
async def save_rule(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    """Save a rule by name. Validated on the way in, so an unusable rule cannot be stored."""
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "A rule needs a name."}, status_code=400)
    try:
        saved = await repository.save_rule(db, name, body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"saved": saved}


@router.delete("/rules/{name}")
async def delete_rule(
    name: str,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    if not await repository.delete_rule(db, name):
        return JSONResponse({"error": "No such rule."}, status_code=404)
    return {"deleted": name}


@router.get("/guardrails")
async def get_guardrails(
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    """The limits, and what each one is for — shown in the settings panel."""
    return {
        "guardrails": await repository.load_guardrails(db),
        "defaults": dict(logic.DEFAULT_GUARDRAILS),
        "ranges": {k: list(v) for k, v in logic.GUARDRAIL_RANGES.items()},
        "help": {
            "max_bid": "No resulting bid may exceed this. Amazon accepted a Rs 1,000 bid in "
                       "testing on an account whose median is Rs 6.39, so this ceiling is the "
                       "only one that exists.",
            "min_bid": "Amazon refuses bids below the marketplace minimum. Holding our own floor "
                       "means those rows are excluded up front rather than failing after the run.",
            "max_change_pct": "The largest proportional move one run may make. Catches 10 typed "
                              "as 100.",
            "max_rows": "A run touching more than this needs narrowing first.",
        },
    }


@router.post("/guardrails")
async def set_guardrails(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    """Edit the limits, or reset to the measured defaults."""
    body = await request.json()
    if body.get("reset"):
        return {"guardrails": await repository.reset_guardrails(db), "status": "reset"}
    try:
        saved = await repository.save_guardrails(
            db, body.get("guardrails") or {},
            updated_by=getattr(getattr(request, "state", None), "username", "") or "",
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"guardrails": saved, "status": "saved"}
