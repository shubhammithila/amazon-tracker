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


async def _dashboard(db: AsyncSession) -> dict:
    """The portfolio, its verdicts and its provenance. ONE function behind screen and export.

    Reads local rows only. The catalogue is loaded live (with its own cache fallback) because it
    decides which products exist and what they are called — the stale
    ``app/invoice/product_families.json`` the old tab read had 205 ASINs against the sheet's 271.
    """
    econ_rows = await repository.load_snapshot(db)
    sheet_catalogue, catalogue_warning, source = await catalogue.load_catalogue()
    ratings = await repository.load_ratings(db)
    decisions = await repository.load_decisions(db)

    result = logic.portfolio(econ_rows, sheet_catalogue, ratings, decisions, date.today())
    result["catalogue_source"] = source
    result["catalogue_warning"] = catalogue_warning
    result["last_refresh"] = await repository.last_refresh(db)
    result["window"] = await repository.latest_window(db)
    result["ratings_as_of"], result["ratings_stale"] = _rating_freshness(ratings)
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


@router.get("")
async def get_portfolio(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.PORTFOLIO)),
):
    """The whole dashboard. Local rows only — no Amazon call.

    Carries ``last_refresh`` and ``ratings_as_of`` so the screen can say how old these numbers
    are, which is the thing the CSV upload could never tell anyone.
    """
    data = await _dashboard(db)
    return JSONResponse({
        **data,
        "refresh": refresh.status(),
        "window_days": economics.WINDOW_DAYS,
        # Every margin here is Amazon's net proceeds, which excludes what it costs to MAKE the
        # product. Flagged in the payload rather than only in the template, so an export cannot
        # present the number without the caveat.
        "pre_cogs": True,
    })


@router.post("/refresh")
async def start_refresh(
    request: Request,
    grant=Depends(require_area(permissions.PORTFOLIO)),
):
    """Start the economics refresh and return at once.

    **Does not await the job.** A Data Kiosk query takes one to two minutes, so awaiting it would
    hold the request open and time out behind Caddy.

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

    # Fire and forget. The task holds its own session: the request's session closes when this
    # handler returns, so using it would fail once the response was sent.
    asyncio.create_task(refresh.run())
    return JSONResponse({"started": True, "refresh": refresh.status()})


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
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.PORTFOLIO)),
):
    """The portfolio as Excel: parents with their sizes indented beneath.

    Built through the same ``_dashboard`` the screen uses, so the file and the monitor cannot
    disagree about a margin. Uses the existing ``documents.build_simple_xlsx`` so this looks like
    every other document the app produces.
    """
    data = await _dashboard(db)
    window = data.get("window")
    totals = data["totals"]

    rows = []
    for parent in data["parents"]:
        rows.append([
            parent["product"], parent["brand"], "",
            parent["verdict"],
            _pct(parent["net_pct"]), _pct(parent["tacos"]),
            parent["sales"], parent["ad_spend"], parent["net"], parent["units"],
            _stars(parent["rating"], parent["rating_count"]),
            parent["decision"] or "",
            parent["verdict_reason"],
        ])
        for size in parent["sizes"]:
            # Indented rather than a separate sheet: the pack-size detail is only meaningful
            # under its parent, and two sheets get read separately.
            rows.append([
                f"    {_size_name(size)}", "", size["asin"],
                "", _pct(size["net_pct"]), _pct(size["tacos"]),
                size["sales"], size["ad_spend"], size["net"], size["units"],
                "", "", "",
            ])

    subtitle = (
        f"{window[0]} to {window[1]} (IST) · " if window else ""
    ) + (
        f"{totals['parents']} products · {totals['units']} units · "
        f"net {_pct(totals['net_pct'])} of sales · TACOS {_pct(totals['tacos'])} · "
        "margins are PRE-COGS (they exclude what it costs to make the product)"
    )

    stream = documents.build_portfolio_xlsx(
        "Portfolio review",
        subtitle,
        ["Product", "Brand", "ASIN", "Verdict", "Net %", "TACOS",
         "Sales", "Ad spend", "Net", "Units", "Rating", "Decision", "Why"],
        rows,
        [30, 12, 12, 10, 9, 8, 12, 11, 12, 8, 14, 10, 52],
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


def _size_name(size: dict) -> str:
    from app.shipment.logic import weight_label

    weight = float(size.get("weight") or 0)
    return weight_label(weight) if weight else (size.get("asin") or "")
