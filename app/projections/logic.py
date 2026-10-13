"""Pure rules for the Projections tab: how much stock to hold. No database, no network.

The owner's formula (12 Oct 2026):

    daily          = 60% x (last 7 days kg / 7) + 40% x (last 30 days kg / 30)
    projection     = daily x 30 x seasonal x (1 + company growth)          next 30 days, kg
    lead total     = supplier->WH + packing + WH->IXD + IXD->FBA           days
    WH buffer      = supplier->WH x 40% + packing x 30% + WH->IXD x 50% + IXD->FBA x 30%
    ideal FBA      = projection / 30 x lead total x markup
    ideal WH       = projection / 30 x WH buffer  x markup
    total to hold  = ideal FBA + ideal WH

**Ideal FBA covers the whole lead time**: shipping daily, every unit should sit at Amazon and the
warehouse should hold nothing but a safety buffer. **The WH buffer is the slips added up**: how
many days each leg of the lead time can run late, summed (worst case, all late at once), never a
hand-typed number. The percentages are one account-wide set; each product's own lead times still
make its buffer its own.

**Sales are weight ORDERED** (`units_ordered x pack weight`), the Profit tab's "Weight ordered":
demand, not demand net of refunds, which can even go negative on a refund-heavy week.

**A missing 7-day window is not a zero week.** No 7-day figure means the 30-day rate alone; a 7-day
figure of 0 kg is a real quiet week and IS blended.

**`/ 30`, not `/ 31`**: the projection is a 30-day figure, so a day of it is a thirtieth.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

#: The four editable lead-time legs, each paired with the setting holding how far it can slip.
LEGS = (
    ("supplier_to_wh", "slip_supplier_to_wh"),
    ("packing", "slip_packing"),
    ("wh_to_ixd", "slip_wh_to_ixd"),
    ("ixd_to_fba", "slip_ixd_to_fba"),
)
#: Every per-product field the owner may edit. Sales and everything computed are read-only.
EDITABLE = ("seasonal_impact",) + tuple(leg for leg, _ in LEGS)

#: Per-product values when nothing is stored and `projection_defaults.json` has no entry.
GLOBAL_DEFAULTS = {"seasonal_impact": 1.0, "supplier_to_wh": 5, "packing": 2,
                   "wh_to_ixd": 10, "ixd_to_fba": 5}

#: Bounds for the per-product edits. A lead time is whole days; seasonal is a multiplier.
EDIT_RANGES = {"seasonal_impact": (0.0, 10.0), "supplier_to_wh": (0, 365), "packing": (0, 365),
               "wh_to_ixd": (0, 365), "ixd_to_fba": (0, 365)}

#: The account-wide settings, saved under `portfolio_settings.name = "projection_blend"`.
DEFAULT_SETTINGS = {
    #: Share of the daily rate taken from the last 7 days; the rest is the last 30. 0.6 leans on
    #: the recent week without letting one odd week swing a 45-day stock level.
    "seven_day_weight": 0.6,
    #: The company's expected overall growth, as a fraction, applied to every projection.
    "global_growth_rate": 0.3,
    #: Applied to both lead total and buffer: (22 + 9.1) days x 1.5 is about the 45 days held.
    "stock_markup": 1.5,
    #: How far each leg can run late, in PERCENT of its own days.
    "slip_supplier_to_wh": 40.0,
    "slip_packing": 30.0,
    "slip_wh_to_ixd": 50.0,
    "slip_ixd_to_fba": 30.0,
}
SETTING_RANGES = {
    "seven_day_weight": (0.0, 1.0),
    "global_growth_rate": (-0.9, 5.0),
    # Floor 1: a markup below 1 holds LESS than the lead time needs, a guaranteed stock-out.
    "stock_markup": (1.0, 5.0),
    "slip_supplier_to_wh": (0.0, 300.0),
    "slip_packing": (0.0, 300.0),
    "slip_wh_to_ixd": (0.0, 300.0),
    "slip_ixd_to_fba": (0.0, 300.0),
}


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def setting_error(key: str, value) -> str | None:
    """Why a setting is refused, in words, or None when it is acceptable."""
    if key not in DEFAULT_SETTINGS:
        return f"Unknown setting {key!r}."
    number = _number(value)
    if number is None:
        return f"{key} must be a number, got {value!r}."
    low, high = SETTING_RANGES[key]
    if not low <= number <= high:
        return f"{key} must be between {low:g} and {high:g}, got {number:g}."
    return None


def settings_or_default(stored: Mapping | None) -> dict:
    """Stored settings over the defaults, each validated on READ: a bad stored value (or a key
    from the old weekly-blend settings) is ignored rather than allowed to distort every row."""
    merged = dict(DEFAULT_SETTINGS)
    for key, value in (stored or {}).items():
        if key in DEFAULT_SETTINGS and setting_error(key, value) is None:
            merged[key] = float(value)
    return merged


def edit_error(field: str, value) -> str | None:
    """Why a per-product edit is refused, or None."""
    if field not in EDIT_RANGES:
        return f"{field} cannot be edited."
    number = _number(value)
    if number is None:
        return f"{field} must be a number, got {value!r}."
    low, high = EDIT_RANGES[field]
    if not low <= number <= high:
        return f"{field} must be between {low:g} and {high:g}, got {number:g}."
    if field != "seasonal_impact" and number != int(number):
        return f"{field} is whole days, got {number:g}."
    return None


def normalize_name(name: str | None) -> str:
    """Case, space and hyphen insensitive, nothing more, so a genuinely different spelling
    ("Gobindobhog" against "Govind Bhog") is never silently merged."""
    return re.sub(r"[\s-]+", "", name or "").casefold()


def match_defaults(name: str, defaults: Mapping[str, dict]) -> dict | None:
    target = normalize_name(name)
    if not target:
        return None
    for candidate, config in defaults.items():
        if normalize_name(candidate) == target:
            return config
    return None


def daily_rate(kg_30d: float | None, kg_7d: float | None, weight: float) -> float | None:
    """kg/day to project from. None with no 30-day figure (an unknown pack weight): a dash."""
    if kg_30d is None:
        return None
    rate_30 = kg_30d / 30
    if kg_7d is None:
        return rate_30
    return weight * (kg_7d / 7) + (1 - weight) * rate_30


def compute(row: Mapping, settings: Mapping) -> dict:
    """Every derived figure for one product. `row` carries kg_30d, kg_7d, seasonal_impact and the
    four legs; `settings` is `settings_or_default(...)`."""
    legs = {leg: float(row.get(leg) or 0) for leg, _ in LEGS}
    lead_total = sum(legs.values())
    buffer_days = sum(legs[leg] * settings[slip] / 100 for leg, slip in LEGS)
    markup = settings["stock_markup"]
    seasonal = float(row.get("seasonal_impact") if row.get("seasonal_impact") is not None else 1.0)

    base = daily_rate(row.get("kg_30d"), row.get("kg_7d"), settings["seven_day_weight"])
    out = {"lead_total": round(lead_total, 1), "buffer_days": round(buffer_days, 2),
           "daily_rate": None, "projection_30d": None, "ideal_fba": None, "ideal_wh": None,
           "total_stock": None, "cover_days": round((lead_total + buffer_days) * markup, 1)}
    if base is None:
        return out
    projected_daily = base * seasonal * (1 + settings["global_growth_rate"])
    ideal_fba = projected_daily * lead_total * markup
    ideal_wh = projected_daily * buffer_days * markup
    out.update(daily_rate=round(base, 2), projection_30d=round(projected_daily * 30, 1),
               ideal_fba=round(ideal_fba, 1), ideal_wh=round(ideal_wh, 1),
               total_stock=round(ideal_fba + ideal_wh, 1))
    return out


def to_purchase(total_stock, fba_kg):
    """What to buy before warehouse stock: Total stock to hold less what is at FBA or on its way.
    Never negative (a surplus is not a purchase), and a dash when either side is unknown."""
    if total_stock is None or fba_kg is None:
        return None
    return round(max(0.0, total_stock - fba_kg), 1)


def sum_rows(rows: list[Mapping]) -> dict:
    """Totals of a set of rows: kilograms add; a row with no figure leaves it out (never 0 kg)."""
    def total(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals), 1) if vals else None
    return {"products": len(rows), "kg_30d": total("kg_30d"), "kg_7d": total("kg_7d"),
            "projection_30d": total("projection_30d"), "ideal_fba": total("ideal_fba"),
            "ideal_wh": total("ideal_wh"), "total_stock": total("total_stock"),
            # Each row's To purchase is already clamped at 0, so this is the sum of clamped rows: a
            # surplus of one product never cancels a shortfall of another (the Orders tab's to_buy).
            "fba_kg": total("fba_kg"), "to_purchase": total("to_purchase")}
