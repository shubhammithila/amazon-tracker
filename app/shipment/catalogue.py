"""Which products are still active, read from the product master sheet.

Column **T ("Active", Y/N)** in the sheet is the owner's own record of what he
still sells. A discontinued SKU has no business appearing in a shipment plan: it
reaches the packer's morning sheet, the Amazon upload and potentially a GST
invoice, and every one of those is a wasted trip or a rejected line.

At the time of writing the live sheet has **152 inactive ASINs against 119
active**, so this is not a rounding error — more than half the catalogue would
otherwise be offered to the warehouse.

Three decisions worth stating, because each one is a failure mode if reversed:

**1. The sheet is read at generate time, not on a schedule.** The plan is built
from the sheet's answer at the moment the owner uploads, so a product
reactivated this morning appears in this morning's plan. A nightly sync would be
one day stale exactly when it mattered.

**2. A fetch failure falls back to the last good copy, and says so.** Google
being briefly unreachable, or the sheet's sharing changing, must not stop the
owner from building a plan — that would hand him a broken app on a Monday
morning for a reason he cannot fix. The cached list is used and the response
carries a warning naming its date. Only if there has never been a successful
fetch does generate proceed with everything, and it says that too.

**3. Unknown ASINs are treated as ACTIVE.** An ASIN absent from the sheet is
missing information, not a decision to discontinue — dropping it would silently
shrink the plan and the owner would have to notice the count. Currently all 205
catalogue ASINs are in the sheet, so this only guards a future gap.

The cache is a JSON file rather than a table: it is a copy of somebody else's
data with no history worth keeping, and a file survives a database reset, which
is the situation where you most want a fallback to exist.
"""
import csv
import io
import json
import logging
from datetime import datetime
from pathlib import Path

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent.parent / "invoice" / "active_products.json"

#: Column indexes in the master sheet, 0-based. 'I' is the ASIN, 'T' is Active.
#: Resolved by HEADER NAME where possible and only falling back to these — a
#: column inserted in the middle of the sheet would otherwise silently shift the
#: Active flag onto the Brand column and mark everything inactive.
ASIN_COLUMN_FALLBACK = 8    # I
ACTIVE_COLUMN_FALLBACK = 19  # T

#: Values in column T that mean "still selling". Anything else — including blank
#: — means inactive. Checked case-insensitively.
ACTIVE_VALUES = frozenset({"y", "yes", "active", "true", "1"})


def _sheet_csv_url() -> str:
    settings = get_settings()
    return (
        f"https://docs.google.com/spreadsheets/d/{settings.product_sheet_id}"
        f"/export?format=csv&gid={settings.product_sheet_gid}"
    )


def _column_indexes(header: list[str]) -> tuple[int, int]:
    """Locate the ASIN and Active columns by name, falling back to position.

    By name first because the sheet is edited by hand: inserting a column is a
    normal thing to do, and position-only lookup would then read the Active flag
    from whatever landed in slot T. That failure marks the whole catalogue
    inactive and produces an empty plan — loud, but for a baffling reason.
    """
    asin_index = active_index = None
    for index, raw in enumerate(header):
        name = (raw or "").strip().casefold()
        if name == "asin" and asin_index is None:
            asin_index = index
        elif name == "active" and active_index is None:
            active_index = index

    if asin_index is None:
        logger.warning("Product sheet has no 'ASIN' header; using column I")
        asin_index = ASIN_COLUMN_FALLBACK
    if active_index is None:
        logger.warning("Product sheet has no 'Active' header; using column T")
        active_index = ACTIVE_COLUMN_FALLBACK
    return asin_index, active_index


def parse_active_flags(csv_text: str) -> dict[str, bool]:
    """{ASIN: is_active} from the sheet's CSV export.

    Pure, so it is testable without touching the network. A duplicated ASIN
    resolves to active if ANY row says active: the same product legitimately
    appears more than once (different pack sizes share an ASIN row in places), and
    between "he still sells it" and "he doesn't", the answer that keeps a live
    product shippable is the safer one to be wrong about.
    """
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return {}

    asin_index, active_index = _column_indexes(rows[0])

    flags: dict[str, bool] = {}
    for row in rows[1:]:
        if len(row) <= asin_index:
            continue
        asin = (row[asin_index] or "").strip().upper()
        # B0 + 8 chars is the ASIN shape the scraper also enforces; it keeps
        # header repeats and note rows out.
        if not asin.startswith("B0") or len(asin) != 10:
            continue
        raw = (row[active_index] or "").strip().casefold() if len(row) > active_index else ""
        is_active = raw in ACTIVE_VALUES
        flags[asin] = flags.get(asin, False) or is_active
    return flags


def _read_cache() -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("flags"), dict):
            return data
    except Exception:
        logger.warning("active_products.json is unreadable; ignoring it", exc_info=True)
    return None


def _write_cache(flags: dict[str, bool]) -> None:
    """Best-effort. A failure here must not break generate.

    The cache is an optimisation for the next run, not part of this one's result —
    losing it costs a warning later, whereas raising would cost the plan now.
    """
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(
                {"fetched_at": datetime.utcnow().isoformat(timespec="seconds"),
                 "flags": flags},
                handle,
                indent=2,
                sort_keys=True,
            )
    except Exception:
        logger.warning("Could not write the active-product cache", exc_info=True)


async def load_active_flags() -> tuple[dict[str, bool], str | None]:
    """({ASIN: is_active}, warning or None).

    Never raises. The caller gets the best answer available and a warning
    describing what it had to settle for, so the owner can see on screen whether
    the filter used today's sheet or last week's.
    """
    try:
        async with httpx.AsyncClient(
            timeout=get_settings().product_sheet_timeout, follow_redirects=True
        ) as client:
            response = await client.get(_sheet_csv_url())
    except Exception as error:
        logger.warning("Could not fetch the product sheet: %s", type(error).__name__)
        response = None

    if response is not None and response.status_code == 200:
        flags = parse_active_flags(response.text)
        if flags:
            _write_cache(flags)
            return flags, None
        # A 200 with nothing usable in it — most often Google returning a login
        # page because sharing was changed. Falling through to the cache is right,
        # but the reason has to be distinguishable from a network failure.
        logger.warning("Product sheet fetched but contained no ASIN rows")

    cached = _read_cache()
    if cached:
        stamp = str(cached.get("fetched_at") or "")[:10] or "an earlier date"
        return (
            {str(k).upper(): bool(v) for k, v in cached["flags"].items()},
            f"Could not read the product sheet, so the active/inactive list from "
            f"{stamp} was used. Check the sheet is still shared if this repeats.",
        )

    # First ever run with no sheet and no cache. Proceed with everything rather
    # than block the owner, and be explicit that no filtering happened — a silent
    # full plan looks identical to a correctly filtered one.
    return {}, (
        "Could not read the product sheet and there is no saved copy, so "
        "discontinued products could not be filtered out. Every product is "
        "included — check the plan before finalising it."
    )


def is_active(flags: dict[str, bool], asin: str) -> bool:
    """Unknown ASINs count as active — missing data is not a decision.

    Dropping an ASIN the sheet has never heard of would shrink the plan silently
    and leave the owner to spot it in a row count.
    """
    return flags.get(str(asin or "").strip().upper(), True)
