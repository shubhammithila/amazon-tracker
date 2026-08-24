"""Who may see what. One list of areas, one place that decides.

The app began with two passwords — ``APP_PASSWORD`` and ``OPS_PASSWORD`` — and which
one you typed decided everything. That worked for two people and cannot express the
case the owner actually has: someone who prints the packed sheet for the accounts team
but must not see projections or purchase costs.

So permission is now a **set of areas** granted per user, and a role is just a preset
that fills that set in. Two consequences worth stating:

* **Areas, not routes.** Forty-odd routes with individual grants would be forty
  chances for a wrong default to expose purchase costs. An area maps to a tab the
  owner can point at, which is the unit he actually thinks in.
* **Deny by default.** ``has()`` returns False for anything it does not recognise, so
  a new area added to this list is invisible to every existing user until it is
  granted. The opposite default would silently widen access on deploy.

``ADMIN`` is deliberately not an area in the tick list. It is a separate flag, because
"can change what other people can see" is a different kind of power from "can see the
invoice tab", and mixing them lets a user grant themselves the rest.
"""
from __future__ import annotations

# ─── The areas ───────────────────────────────────────────────────────────────
#
# Keys are stored in the database, so they are permanent: renaming one silently
# revokes it for every user who had it. Labels are what the owner reads and may be
# changed freely.

DASHBOARD = "dashboard"
INVOICE = "invoice"
PORTFOLIO = "portfolio"
PROJECTIONS = "projections"
SHIPMENT = "shipment"
PACKING = "packing"
#: Amazon Easy Ship orders and the daily picking sheet. An AREA rather than admin-only,
#: because the warehouse is who needs it — the sheet is what they pick against. It carries
#: order totals and destination cities, but no buyer names or street addresses: Amazon
#: withholds those without the PII role and this feature does not ask for them.
ORDERS = "orders"

#: Display order is deliberate: it matches the nav, so the tick list on the Users
#: screen reads down in the same order as the tabs it controls.
AREAS: list[tuple[str, str, str]] = [
    (DASHBOARD, "Dashboard", "Scraped prices, BSR, ratings and the product table."),
    (INVOICE, "Invoice", "Generate and save GST invoices. Sees purchase rates."),
    (PORTFOLIO, "Portfolio", "Churn analysis — which products are losing ground."),
    (PROJECTIONS, "Projections", "Sales forecasts and reorder alerts."),
    (SHIPMENT, "Shipment", "Plan what ships: quantities, verification, Amazon upload."),
    (PACKING, "Daily packing", "Record units and cartons packed. The warehouse screen."),
    (ORDERS, "Orders", "Amazon Easy Ship orders and today's picking sheet."),
]

AREA_KEYS = frozenset(key for key, _label, _help in AREAS)

#: The `packing` area is the only one an ops user gets, and it is also open to anyone
#: with `shipment` — the owner supervises packing, so gating his own view of it behind
#: a second tick would be a papercut with no security value.
IMPLIED: dict[str, frozenset[str]] = {
    SHIPMENT: frozenset({PACKING}),
}

# ─── Presets ─────────────────────────────────────────────────────────────────
#
# A preset is a starting point, not a type. The owner ticks from here and adjusts;
# nothing in the app stores which preset was used, because a user who was "Packer" and
# then had Invoice added is not a Packer any more and pretending otherwise is how the
# label and the reality drift apart.

ROLE_OWNER = "owner"
ROLE_PACKER = "packer"
ROLE_ACCOUNTS = "accounts"
ROLE_CUSTOM = "custom"

PRESETS: dict[str, frozenset[str]] = {
    ROLE_OWNER: frozenset(AREA_KEYS),
    ROLE_PACKER: frozenset({PACKING}),
    # The case that motivated per-area permissions: prints the packed sheet and
    # raises invoices, sees no projections and no purchase-driven planning figures.
    ROLE_ACCOUNTS: frozenset({INVOICE, PACKING}),
}

PRESET_LABELS: dict[str, str] = {
    ROLE_OWNER: "Owner — everything",
    ROLE_PACKER: "Packer — daily packing only",
    ROLE_ACCOUNTS: "Accounts — invoices and the packed sheet",
    ROLE_CUSTOM: "Custom — tick individually",
}


# ─── Reading and writing the grant ───────────────────────────────────────────
#
# Stored as a comma-separated string rather than JSON or a join table. A join table is
# the textbook answer and would be three extra queries per request for a table with
# six possible rows; JSON in a column cannot be indexed either and reads worse in a
# sqlite3 shell when something has gone wrong at 11pm.

def serialise(areas) -> str:
    """A grant as it is stored. Unknown keys are dropped, order is canonical.

    Dropping rather than raising: this is fed from an HTTP form, and a stale checkbox
    name from a cached page should not 500 the save. Canonical order means two
    equivalent grants compare equal as strings, which makes "did this change?" cheap.
    """
    wanted = {str(a).strip().lower() for a in (areas or [])}
    return ",".join(key for key, _l, _h in AREAS if key in wanted)


def parse(stored: str | None) -> frozenset[str]:
    """A stored grant back into a set, expanded through IMPLIED.

    Expansion happens on READ, not on write, so changing what an area implies applies
    immediately to existing users instead of needing a data migration. Same reasoning
    as joining the shipment sort priority rather than denormalising it.
    """
    granted = {
        part.strip().lower()
        for part in (stored or "").split(",")
        if part.strip()
    }
    granted &= AREA_KEYS  # a key removed from AREAS stops granting anything

    for area, implies in IMPLIED.items():
        if area in granted:
            granted |= implies
    return frozenset(granted)


def has(stored: str | None, area: str, *, is_admin: bool = False) -> bool:
    """May this grant reach this area?

    ``is_admin`` short-circuits to True. That is the definition of admin here — the
    owner should not be able to lock himself out of a tab by mis-ticking a box, and a
    self-inflicted lockout on a single-owner app has no recovery path short of a
    sqlite3 shell.
    """
    if is_admin:
        return True
    return area in parse(stored)


def preset_for(stored: str | None, *, is_admin: bool = False) -> str:
    """Which preset this grant matches, or ``custom``. For display only.

    Derived rather than stored, so the label can never disagree with the actual
    permissions — which is the failure mode of keeping a `role` column beside them.
    """
    if is_admin:
        return ROLE_OWNER
    granted = parse(stored)
    for name, areas in PRESETS.items():
        if granted == parse(serialise(areas)):
            return name
    return ROLE_CUSTOM


def describe(stored: str | None, *, is_admin: bool = False) -> list[str]:
    """Human labels for a grant, for the users table and the audit trail."""
    if is_admin:
        return ["Everything (admin)"]
    granted = parse(stored)
    return [label for key, label, _help in AREAS if key in granted] or ["No access"]
