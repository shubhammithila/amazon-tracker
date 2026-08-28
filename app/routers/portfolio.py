"""The Portfolio tab: which products earn their place, and which to churn.

Replaced a CSV upload. The previous version asked the owner to download a Business Report from
Seller Central and upload it, then wrote the aggregate to a JSON file at repo root — so the
analysis was stale the moment it was saved, could not be repeated without a human, and lived
outside the database like the shipment plan used to.

**Nothing here calls Amazon.** Every route reads stored rows, because a Data Kiosk query takes
one to two minutes and a page that called it would hang. ``POST /refresh`` starts the background
job and returns immediately; the screen polls ``/refresh-status``.

**Nothing here writes to Amazon, and no verdict is ever applied.** The tab reads economics, reads
our own review scraper, and records the owner's own decision. Turning ads off or delisting a
product stays a human action in Seller Central: a dashboard that could kill a SKU on a threshold
is a dashboard that kills a SKU on a bad data day.
"""
import io
import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import permissions
from app.database import get_db
from app.portfolio import economics, logic, refresh, repository
from app.routers.auth import require_area
from app.shipment import catalogue, documents

router = APIRouter(prefix="/portfolio")
logger = logging.getLogger(__name__)

#: Ratings older than this are called out on screen. Three days, because the scrape is daily by
#: design — anything older means it has been failing, and a stale star rating silently averaged
#: into a kill decision is exactly what should be visible instead. They were NINE days old when
#: this was written.
STALE_RATING_DAYS = 3


async def _dashboard(db: AsyncSession, window: tuple[str, str] | None = None) -> dict:
    """The portfolio, its verdicts and its provenance. ONE function behind screen and export.

    Reads local rows only. The catalogue is loaded live (with its own cache fallback) because it
    decides which products exist and what they are called — the stale
    ``app/invoice/product_families.json`` the old tab read had 205 ASINs against the sheet's 271.

    Five sources, each answering something the others cannot: the economics (margins), the ads
    report (ACOS), the per-SKU economics (the merchant/FBA split), our review scraper (ratings)
    and the owner's own decisions. All optional except the economics — without ads credentials the
    tab shows exactly what it showed before ACOS existed.
    """
    window = window or await repository.latest_window(db)
    econ_rows = await repository.load_snapshot(db, window)
    sku_rows = await repository.load_sku_snapshot(db, window)
    ads_by_asin, ads_by_sku = await repository.load_ads_snapshot(db, window)
    sheet_catalogue, catalogue_warning, source = await catalogue.load_catalogue()
    ratings = await repository.load_ratings(db)
    decisions = await repository.load_decisions(db)
    thresholds = await repository.load_settings(db)

    result = logic.portfolio(
        econ_rows, sheet_catalogue, ratings, decisions, date.today(),
        ads_by_asin=ads_by_asin,
        channels=logic.channel_split(sku_rows, ads_by_sku),
        thresholds=thresholds,
    )
    result["catalogue_source"] = source
    result["catalogue_warning"] = catalogue_warning
    result["last_refresh"] = await repository.last_refresh(db)
    result["window"] = window
    result["windows_available"] = await repository.windows_available(db)
    result["ratings_as_of"], result["ratings_stale"] = _rating_freshness(ratings)
    # Whether ACOS is available at all, so the screen can say "not configured" rather than
    # rendering a column of dashes with no explanation.
    result["acos_available"] = bool(ads_by_asin)
    result["verdict_help"] = {
        verdict: text.format(**thresholds)
        for verdict, text in logic.VERDICT_HELP.items()
    }
    result["verdict_order"] = list(logic.VERDICT_ORDER)
    # Sent from here so a phase renamed in `refresh` cannot render as a raw key on screen — the
    # template has no list of its own to fall out of step.
    result["phase_labels"] = dict(refresh.PHASE_LABELS)
    return result


def _rating_freshness(ratings: dict) -> tuple[str | None, bool]:
    """The newest rating timestamp, and whether it is too old to trust silently.

    Stated rather than assumed: the review scrape is what makes a rating-based verdict
    meaningful, and it is the one input to this dashboard that can quietly stop running.
    """
    stamps = [row.get("scraped_at") for row in ratings.values() if row.get("scraped_at")]
    if not stamps:
        return None, True
    newest = max(stamps)
    try:
        age = datetime.utcnow() - datetime.fromisoformat(newest)
    except (TypeError, ValueError):
        return newest, True
    return newest, age > timedelta(days=STALE_RATING_DAYS)


def _requested_window(start: str | None, end: str | None, days: int | None):
    """Turn query parameters into a window, or return an error message for a 400.

    Returns ``(window, error)``: exactly one is None. Validated here rather than deep in the
    fetch so a bad range is refused before any work, and the message reaches the screen verbatim.
    """
    if start and end:
        try:
            return economics.validate_window(start, end), None
        except ValueError as exc:
            return None, str(exc)
    if days:
        try:
            span = int(days)
        except (TypeError, ValueError):
            return None, "days must be a whole number."
        if span < 1 or span > economics.MAX_WINDOW_DAYS:
            return None, f"days must be between 1 and {economics.MAX_WINDOW_DAYS}."
        return economics.window_for(date.today(), span), None
    return None, None            # neither given: use whatever is stored


@router.get("")
async def get_portfolio(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    days: int | None = None,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.PORTFOLIO)),
):
    """The whole dashboard. Local rows only — no Amazon call.

    ``?days=30`` or ``?start=&end=`` selects a window; both are validated to at most 90 days and
    to end no later than yesterday. **A window with no stored rows returns empty rather than
    fetching**, because a fetch takes twelve minutes and a GET must not block on one — the screen
    offers a Fetch button instead.

    Carries ``last_refresh``, ``ratings_as_of`` and ``windows_available`` so the screen can say how
    old these numbers are and which ranges are instant — the things the CSV upload could never
    tell anyone.
    """
    window, error = _requested_window(start, end, days)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    data = await _dashboard(db, window)
    return JSONResponse({
        **data,
        "refresh": refresh.status(),
        "window_days": economics.WINDOW_DAYS,
        "max_window_days": economics.MAX_WINDOW_DAYS,
        # Every margin here is Amazon's net proceeds, which excludes what it costs to MAKE the
        # product. Flagged in the payload rather than only in the template, so an export cannot
        # present the number without the caveat.
        "pre_cogs": True,
    })


@router.post("/refresh")
async def start_refresh(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    days: int | None = None,
    grant=Depends(require_area(permissions.PORTFOLIO)),
):
    """Start the refresh and return at once. Optionally for a specific window.

    **Does not await the job.** The economics query takes ~30 seconds and the ad report ~12
    minutes, so awaiting would hold the request open and time out behind Caddy.

    A second call while one runs is refused by ``refresh.run`` itself rather than by a check
    here — the guard belongs with the state it protects, so every caller inherits it. The 409 is
    still returned early so the screen gets an immediate answer instead of a started-then-refused
    task.
    """
    import asyncio

    if refresh.status().get("running"):
        return JSONResponse(
            {"error": "A refresh is already running.", "refresh": refresh.status()},
            status_code=409,
        )

    window, error = _requested_window(start, end, days)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    # Fire and forget. The task holds its own session: the request's session closes when this
    # handler returns, so using it would fail once the response was sent.
    if window:
        asyncio.create_task(refresh.run(start=window[0], end=window[1]))
    else:
        asyncio.create_task(refresh.run())
    return JSONResponse({"started": True, "refresh": refresh.status()})


@router.get("/settings")
async def get_settings_route(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.PORTFOLIO)),
):
    """The effective verdict thresholds, plus the defaults and each rule in words.

    `defaults` travels alongside so the panel can offer a Reset without hardcoding the measured
    values in the template — one source of truth for numbers that are evidence.
    """
    thresholds = await repository.load_settings(db)
    return JSONResponse({
        "thresholds": thresholds,
        "defaults": logic.DEFAULT_THRESHOLDS,
        "help": {
            verdict: text.format(**thresholds)
            for verdict, text in logic.VERDICT_HELP.items()
        },
        "verdict_order": list(logic.VERDICT_ORDER),
    })


@router.post("/settings")
async def save_settings_route(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.PORTFOLIO)),
):
    """Save edited thresholds. `{"thresholds": {...}}`, or `{}` to reset to the measured defaults.

    **An unknown key is a 400, not a silent no-op.** A typo that appeared to save and changed
    nothing would be worse than an error: the owner would believe a rule had moved when it had
    not, and act on verdicts computed the old way.

    Verdicts recompute from stored rows on the next load, so no Amazon call is needed — changing a
    threshold is instant.
    """
    try:
        body = await request.json()
    except Exception:                       # noqa: BLE001 - a malformed body is a 400
        return JSONResponse({"error": "Expected a JSON body."}, status_code=400)

    values = (body or {}).get("thresholds")
    if values is None:
        values = {}
    if not isinstance(values, dict):
        return JSONResponse(
            {"error": "thresholds must be an object of {name: number}."}, status_code=400
        )

    unknown = sorted(set(values) - set(logic.DEFAULT_THRESHOLDS))
    if unknown:
        return JSONResponse(
            {"error": f"Unknown threshold(s): {', '.join(unknown)}. "
                      f"Valid names: {', '.join(sorted(logic.DEFAULT_THRESHOLDS))}."},
            status_code=400,
        )
    for key, value in values.items():
        try:
            float(value)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": f"{key} must be a number, got {value!r}."}, status_code=400
            )

    thresholds = await repository.save_settings(
        db, values, updated_by=getattr(grant, "username", "") or ""
    )
    logger.info("portfolio: thresholds saved (%d edited)", len(values))
    return JSONResponse({
        "status": "saved",
        "thresholds": thresholds,
        "help": {
            verdict: text.format(**thresholds)
            for verdict, text in logic.VERDICT_HELP.items()
        },
    })


@router.get("/refresh-status")
async def refresh_status(
    request: Request,
    grant=Depends(require_area(permissions.PORTFOLIO)),
):
    """Live progress for the banner. Cheap enough to poll every few seconds."""
    return JSONResponse(refresh.status())


@router.post("/decision")
async def save_decision(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.PORTFOLIO)),
):
    """Record a decision about one product. ``{"parent_asin", "decision", "note"}``.

    ``decision`` is one of kill / keep / watch, or empty to clear it. Validated against the
    tuple so a typo cannot create a fourth category the dashboard can neither filter nor count.

    **The figures at this moment are stored with it.** Revisiting a kill in three months
    otherwise means trusting memory about what the margin was, and being able to check whether a
    decision worked is the whole reason these are kept rather than a name in a set — which is all
    the ``discontinued_products.json`` this replaces ever held.

    Writes nothing to Amazon. This is a note to ourselves.
    """
    try:
        body = await request.json()
    except Exception:                       # noqa: BLE001 - a malformed body is a 400
        return JSONResponse({"error": "Expected a JSON body."}, status_code=400)

    parent_asin = str((body or {}).get("parent_asin") or "").strip().upper()
    decision = str((body or {}).get("decision") or "").strip().lower()
    note = str((body or {}).get("note") or "").strip()

    if not parent_asin:
        return JSONResponse({"error": "parent_asin is required."}, status_code=400)
    if decision and decision not in repository.DECISIONS:
        return JSONResponse(
            {"error": f"decision must be one of {', '.join(repository.DECISIONS)}, or empty."},
            status_code=400,
        )

    # The numbers as they stand right now, so the decision can be judged later against what was
    # actually on screen when it was taken.
    snapshot = None
    if decision:
        data = await _dashboard(db)
        parent = next(
            (p for p in data["parents"] if p["parent_asin"] == parent_asin), None
        )
        if parent:
            snapshot = {
                "verdict": parent["verdict"],
                "sales": parent["sales"],
                "ad_spend": parent["ad_spend"],
                "net": parent["net"],
                "net_pct": parent["net_pct"],
                "tacos": parent["tacos"],
                # ACOS travels with the decision too: "I killed this at 285% ACOS" is a more
                # specific claim than the margin alone, and it is the one that justifies
                # cutting ad spend rather than the product.
                "acos": parent.get("acos"),
                "units": parent["units"],
                "rating": parent["rating"],
                "window": data.get("window"),
            }

    decisions = await repository.save_decision(
        db, parent_asin, decision,
        note=note, snapshot=snapshot,
        decided_by=getattr(grant, "username", "") or "",
    )
    logger.info(
        "portfolio: decision %r recorded for %s", decision or "(cleared)", parent_asin
    )
    return JSONResponse({"status": "saved", "decisions": decisions})


@router.get("/download.xlsx")
async def download_portfolio(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    days: int | None = None,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.PORTFOLIO)),
):
    """The portfolio as Excel: parents with their sizes indented beneath.

    Built through the same ``_dashboard`` the screen uses, so the file and the monitor cannot
    disagree about a margin. Takes the same window parameters as the screen, so what is exported
    is what was being looked at.
    """
    window, error = _requested_window(start, end, days)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    data = await _dashboard(db, window)
    window = data.get("window")
    totals = data["totals"]

    rows = []
    for parent in data["parents"]:
        rows.append([
            parent["product"], parent["brand"], "",
            parent["verdict"],
            _pct(parent["net_pct"]), _pct(parent["tacos"]), _acos(parent),
            parent["sales"], parent["ad_spend"],
            parent.get("ad_attributed_sales") or 0,
            parent["net"], parent["units"],
            _stars(parent["rating"], parent["rating_count"]),
            parent["decision"] or "",
            parent["verdict_reason"],
        ])
        for size in parent["sizes"]:
            # Indented rather than a separate sheet: the pack-size detail is only meaningful
            # under its parent, and two sheets get read separately.
            rows.append([
                f"    {_size_name(size)}", _channel_note(size), size["asin"],
                "", _pct(size["net_pct"]), _pct(size["tacos"]), _acos(size),
                size["sales"], size["ad_spend"],
                size.get("ad_attributed_sales") or 0,
                size["net"], size["units"],
                "", "", "",
            ])

    subtitle = (
        f"{window[0]} to {window[1]} (IST) · " if window else ""
    ) + (
        f"{totals['parents']} products · {totals['units']} units · "
        f"net {_pct(totals['net_pct'])} of sales · TACOS {_pct(totals['tacos'])}"
        + (f" · ACOS {_pct(totals.get('acos'))}" if totals.get("acos") else "")
        + " · TACOS is ad spend over TOTAL sales; ACOS is over ad-ATTRIBUTED sales"
        + " · margins are PRE-COGS (they exclude what it costs to make the product)"
    )

    stream = documents.build_portfolio_xlsx(
        "Portfolio review",
        subtitle,
        ["Product", "Brand", "ASIN", "Verdict", "Net %", "TACOS", "ACOS",
         "Sales", "Ad spend", "Ad sales", "Net", "Units", "Rating", "Decision", "Why"],
        rows,
        [30, 16, 12, 10, 9, 8, 8, 12, 11, 12, 12, 8, 14, 10, 52],
    )
    filename = f"portfolio-{(window or ('', ''))[1] or 'latest'}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _pct(value) -> str:
    """A percentage for a spreadsheet cell, or a dash when there is no denominator.

    A dash rather than 0%: a product with no sales has no TACOS, and printing "0%" would rank it
    among the most ad-efficient products in the portfolio.
    """
    return "—" if value is None else f"{value * 100:.1f}%"


def _stars(rating, count) -> str:
    return "—" if rating is None else f"{rating:.1f} ({count or 0})"


def _acos(row: dict) -> str:
    """ACOS for a cell, distinguishing THREE states that must not look alike.

    * never advertised      -> em dash. No ACOS exists; 0% would rank it as the most efficient.
    * spend, no attribution -> "no sales" rather than a number. Measured: Rs 55,217 across 591
                               rows produced zero attributed sales, and a ratio cannot say that.
    * spend and attribution -> the percentage.
    """
    if row.get("acos_infinite"):
        return "spend, no sales"
    return _pct(row.get("acos"))


def _channel_note(size: dict) -> str:
    """"merchant + FBA" style note for a size row, or blank when the split is unknown.

    Shown because the split is decision-relevant: measured, one product's merchant SKU spent
    Rs 1,444 on ads for zero attributed sales while its FBA twin returned 36% ACOS.
    """
    channels = size.get("channels") or {}
    if not channels:
        return ""
    parts = []
    for name in ("merchant", "fba"):
        bucket = channels.get(name)
        if bucket:
            parts.append(f"{name} {int(round(bucket.get('sales') or 0)):,}")
    return " + ".join(parts)


def _size_name(size: dict) -> str:
    from app.shipment.logic import weight_label

    weight = float(size.get("weight") or 0)
    return weight_label(weight) if weight else (size.get("asin") or "")
