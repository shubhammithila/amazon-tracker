"""The product catalogue, read live from the MRP master sheet.

**The sheet decides which products exist**, not just which are still sold. That is
the whole point of this module and it is a change made for a concrete reason:
Triphala Sattu was in the sheet, marked Active, in two pack sizes — and it never
appeared in a shipment plan, because the plan built its rows from
``app/invoice/product_families.json``, a static file frozen at 205 ASINs that
Triphala had never been added to. The sheet was consulted only for the yes/no
Active flag, so a genuinely new product was invisible no matter what the sheet said.

The sheet now supplies the ASIN, product name, pack size (Net Weight), brand and
the Active flag. Its 271 ASINs are a strict superset of the old file's 205 — checked
— so nothing is lost by preferring it.

Columns used, resolved BY HEADER NAME with positional fallback:

    A  Name         product name, and the sort key
    B  Net Weight   pack size in kg; drives sort order and the printed sheet
    I  ASIN         the join key to sales, stock and everything else
    S  Brand Name   Mithila Foods / Howrah Foods, drives brand_rank
    T  Active       Y/N — the owner's own record of what he still sells

The merchant SKU is deliberately NOT taken from here. Column M ("Amazon FBA SKU")
is blank for all 108 active rows, and the real value already arrives in the uploaded
stock CSV, which is Amazon's own export and therefore authoritative. Reading the
sheet's empty column instead would blank the SKU on every row and Amazon rejects
those lines.

Four decisions, each a failure mode if reversed:

**1. Read at generate time, not on a schedule.** The plan reflects the sheet at the
moment the owner uploads, so a product added or reactivated this morning is in this
morning's plan. That immediacy is what was asked for; a nightly sync would be one
day stale exactly when it mattered.

**2. A fetch failure falls back to the last good copy, and says so.** Google being
briefly unreachable, or the sheet's sharing changing, must not stop the owner
building a plan — that hands him a broken app on a Monday morning for a reason he
cannot fix. The cache is used and the response names its date. If there has never
been a successful fetch, the static file is used as a last resort, and that is
stated too.

**3. Inactive means excluded; unknown means included.** A discontinued SKU on the
packer's morning sheet is a wasted trip and a rejected Amazon line. But an ASIN the
sheet has never heard of is missing information rather than a decision, so it is
kept — dropping it would shrink the plan silently.

**4. A row with an unusable weight is kept, not skipped.** Weight only affects sort
order; a product missing from the plan affects a shipment. Losing a row over a typo
in one cell is the wrong trade, so the weight falls back to the static file and then
to 0.

The cache is a JSON file rather than a table: it is a copy of somebody else's data
with no history worth keeping, and a file survives a database reset — which is
exactly when you most want a fallback to exist.
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

#: Column indexes in the master sheet, 0-based. Resolved by HEADER NAME where
#: possible and only falling back to these — a column inserted in the middle of the
#: sheet would otherwise silently shift the Active flag onto the Brand column and
#: mark the entire catalogue inactive.
ASIN_COLUMN_FALLBACK = 8     # I
ACTIVE_COLUMN_FALLBACK = 19  # T
NAME_COLUMN_FALLBACK = 0     # A
WEIGHT_COLUMN_FALLBACK = 1   # B
BRAND_COLUMN_FALLBACK = 18   # S

#: Header name -> (attribute, positional fallback). Kept as one table so adding a
#: column means one line here rather than edits in three functions.
_COLUMNS = {
    "asin": ("asin", ASIN_COLUMN_FALLBACK),
    "active": ("active", ACTIVE_COLUMN_FALLBACK),
    "name": ("name", NAME_COLUMN_FALLBACK),
    "net weight": ("weight", WEIGHT_COLUMN_FALLBACK),
    "brand name": ("brand", BRAND_COLUMN_FALLBACK),
}

#: Values in column T that mean "still selling". Anything else — including blank
#: — means inactive. Checked case-insensitively.
ACTIVE_VALUES = frozenset({"y", "yes", "active", "true", "1"})


def _sheet_csv_url() -> str:
    settings = get_settings()
    return (
        f"https://docs.google.com/spreadsheets/d/{settings.product_sheet_id}"
        f"/export?format=csv&gid={settings.product_sheet_gid}"
    )


def _column_indexes(header: list[str]) -> dict[str, int]:
    """Locate each column by header name, falling back to position.

    By name first because the sheet is edited by hand: inserting a column is a normal
    thing to do, and position-only lookup would then read the Active flag from
    whatever landed in slot T. That failure marks the whole catalogue inactive and
    produces an empty plan — loud, but for a baffling reason.
    """
    found: dict[str, int] = {}
    for index, raw in enumerate(header):
        name = (raw or "").strip().casefold()
        if name in _COLUMNS:
            attribute, _fallback = _COLUMNS[name]
            found.setdefault(attribute, index)

    for _header_name, (attribute, fallback) in _COLUMNS.items():
        if attribute not in found:
            logger.warning(
                "Product sheet has no %r header; falling back to column index %d",
                _header_name, fallback,
            )
            found[attribute] = fallback
    return found


def _cell(row: list[str], index: int) -> str:
    """One cell, or "" when the row is short. Ragged rows are normal in a CSV export:
    trailing empty cells are simply omitted, so indexing blindly raises IndexError on
    perfectly ordinary rows."""
    return (row[index] or "").strip() if len(row) > index else ""


def parse_catalogue(csv_text: str) -> dict[str, dict]:
    """{ASIN: {name, weight, brand, active}} from the sheet's CSV export.

    Pure, so it is testable without touching the network.

    A duplicated ASIN resolves to active if ANY row says active, and keeps the first
    row's details. The same product legitimately appears more than once, and between
    "he still sells it" and "he doesn't", the answer that keeps a live product
    shippable is the safer one to be wrong about.

    ``weight`` is None when the cell is blank or unparseable, so the caller can tell
    "the sheet does not say" from "the sheet says zero" and fall back accordingly. A
    row is never dropped for a bad weight: weight only affects sort order, while a
    missing row affects a shipment.
    """
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return {}

    columns = _column_indexes(rows[0])
    out: dict[str, dict] = {}

    for row in rows[1:]:
        asin = _cell(row, columns["asin"]).upper()
        # B0 + 8 chars is the ASIN shape the scraper also enforces; it keeps header
        # repeats, blank spacer rows and hand-written notes out.
        if not asin.startswith("B0") or len(asin) != 10:
            continue

        raw_weight = _cell(row, columns["weight"])
        try:
            weight = float(raw_weight) if raw_weight else None
        except ValueError:
            # A typo in one cell must not cost the row. Logged at debug because a
            # handful of these across 271 rows is not worth a warning every generate.
            logger.debug("Unparseable Net Weight %r for %s", raw_weight, asin)
            weight = None

        is_active = _cell(row, columns["active"]).casefold() in ACTIVE_VALUES

        existing = out.get(asin)
        if existing is None:
            out[asin] = {
                "asin": asin,
                "name": _cell(row, columns["name"]),
                "weight": weight,
                "brand": _cell(row, columns["brand"]),
                "active": is_active,
            }
        else:
            existing["active"] = existing["active"] or is_active
            # Fill gaps from a later duplicate row, but never overwrite a value the
            # first row already supplied.
            for key, value in (("name", _cell(row, columns["name"])),
                               ("brand", _cell(row, columns["brand"]))):
                if not existing.get(key) and value:
                    existing[key] = value
            if existing.get("weight") is None and weight is not None:
                existing["weight"] = weight

    return out


def parse_active_flags(csv_text: str) -> dict[str, bool]:
    """{ASIN: is_active}. Kept as the narrow view over ``parse_catalogue``.

    Still used by the fallback path and by tests that only care about the flag, and
    it means there is exactly one CSV parser rather than two that must agree.
    """
    return {asin: bool(row["active"]) for asin, row in parse_catalogue(csv_text).items()}


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


def _write_cache(flags: dict[str, bool], products: dict[str, dict] | None = None) -> None:
    """Best-effort. A failure here must not break generate.

    The cache is an optimisation for the next run, not part of this one's result —
    losing it costs a warning later, whereas raising would cost the plan now.

    ``flags`` is still written alongside ``products`` so a cache created by this
    version stays readable by the previous one, and vice versa. That matters on a
    rollback: the deploy script reverts the code but not this file.
    """
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": datetime.utcnow().isoformat(timespec="seconds"),
            "flags": flags,
        }
        if products:
            payload["products"] = products
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
    except Exception:
        logger.warning("Could not write the active-product cache", exc_info=True)


async def _fetch_sheet_text() -> str | None:
    """The sheet's CSV, or None. Never raises.

    A 200 containing no ASIN rows is treated as a failure by the caller, because that
    is what Google returns when sharing has been turned off: a login page, status 200.
    Distinguishing it from a network error matters only for the log message; both fall
    back to the cache.
    """
    try:
        async with httpx.AsyncClient(
            timeout=get_settings().product_sheet_timeout, follow_redirects=True
        ) as client:
            response = await client.get(_sheet_csv_url())
    except Exception as error:
        logger.warning("Could not fetch the product sheet: %s", type(error).__name__)
        return None

    if response.status_code != 200:
        logger.warning("Product sheet returned HTTP %s", response.status_code)
        return None
    return response.text


async def load_catalogue() -> tuple[dict[str, dict], str | None, str]:
    """({ASIN: record}, warning or None, source).

    ``source`` is one of ``"sheet"``, ``"cache"`` or ``"none"``, so the caller can say
    on screen where the plan's product list came from. A plan built from last week's
    cached copy looks identical to one built from today's sheet, and the owner should
    not have to guess which he is holding.

    Never raises. The caller gets the best answer available plus a warning describing
    what it had to settle for.
    """
    text = await _fetch_sheet_text()
    if text:
        products = parse_catalogue(text)
        if products:
            _write_cache(
                {a: bool(r["active"]) for a, r in products.items()}, products
            )
            return products, None, "sheet"
        logger.warning("Product sheet fetched but contained no ASIN rows")

    cached = _read_cache()
    if cached and isinstance(cached.get("products"), dict) and cached["products"]:
        stamp = str(cached.get("fetched_at") or "")[:10] or "an earlier date"
        products = {
            str(a).upper(): {
                "asin": str(a).upper(),
                "name": r.get("name") or "",
                "weight": r.get("weight"),
                "brand": r.get("brand") or "",
                "active": bool(r.get("active")),
            }
            for a, r in cached["products"].items()
        }
        return products, (
            f"Could not read the MRP sheet, so the saved product list from {stamp} was "
            "used. A product added to the sheet since then will not appear. Check the "
            "sheet is still shared if this repeats."
        ), "cache"

    # An older cache holds only flags, with no names or weights. Those are still worth
    # using for the active/inactive decision — the caller falls back to the static file
    # for the product details.
    if cached and isinstance(cached.get("flags"), dict) and cached["flags"]:
        stamp = str(cached.get("fetched_at") or "")[:10] or "an earlier date"
        products = {
            str(a).upper(): {"asin": str(a).upper(), "name": "", "weight": None,
                             "brand": "", "active": bool(v)}
            for a, v in cached["flags"].items()
        }
        return products, (
            f"Could not read the MRP sheet. An older saved copy from {stamp} was used "
            "for the active/inactive list, and product details came from the built-in "
            "list. A newly added product will not appear."
        ), "cache"

    return {}, (
        "Could not read the MRP sheet and there is no saved copy, so the plan was "
        "built from the built-in product list and discontinued items could not be "
        "filtered out. Check the plan before finalising it."
    ), "none"


async def load_active_flags() -> tuple[dict[str, bool], str | None]:
    """({ASIN: is_active}, warning or None). The narrow view, for callers that only
    need the flag. Kept so there is one fetch-and-fallback path, not two."""
    products, warning, _source = await load_catalogue()
    return {a: bool(r["active"]) for a, r in products.items()}, warning


def is_active(flags: dict[str, bool], asin: str) -> bool:
    """Unknown ASINs count as active — missing data is not a decision.

    Dropping an ASIN the sheet has never heard of would shrink the plan silently
    and leave the owner to spot it in a row count.
    """
    return flags.get(str(asin or "").strip().upper(), True)
