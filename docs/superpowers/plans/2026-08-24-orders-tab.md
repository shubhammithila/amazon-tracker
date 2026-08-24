# Orders Tab (Easy Ship picking sheet) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An Orders tab that stores Amazon Easy Ship orders locally via a background refresh, and renders a daily picking sheet — item + pack size + brand, with quantity, order counts and net kilogram totals — plus an order list for reconciliation.

**Architecture:** A background job pages `getOrders` (rate-limited to one call every 22 seconds) and `getOrderItems` into two new tables; every screen reads only local rows. The picking sheet is a pure aggregation over those rows joined to the live MRP catalogue by ASIN. Three new modules mirror the existing `app/shipment/` split: one SP-API caller, one repository, one router.

**Tech Stack:** FastAPI async, SQLAlchemy 2.0 async, SQLite (Alembic `batch_alter_table`), httpx, APScheduler, openpyxl, vanilla JS + Jinja2.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-24-orders-tab-design.md`. Every decision and rejection is recorded there.
- **Run tests:** `venv/Scripts/python -m pytest -q` from the repo root. Currently **1139 passed, 3 skipped**, random order.
- **Alembic head is `b2f7c1a94e05`.** A new migration's `down_revision` is that value, and `deploy/update-ec2.sh`'s baseline detector needs a new branch **newest-first** — a stale detector has already stamped production backwards once.
- **`getOrders` is limited to 0.045 req/sec (one call every 22 s).** Never call it from a request handler.
- **Easy Ship orders only:** `ShipServiceLevel` must contain `EZ`. Filtering on `FulfillmentChannel == "MFN"` is wrong — three `S02-…` orders are MFN `"Standard"` with a **1995-01-01** sentinel ship-by.
- **Timestamps stored UTC (`*_utc` columns), displayed IST**, converted in one helper. Every real `LatestShipDate` is `18:29Z` = `23:59 IST`.
- **Catalogue join is by ASIN, never SellerSKU**, and names/weights come from the **live MRP sheet** (`app/shipment/catalogue.load_catalogue()`, 271 ASINs), not `product_families.json` (205).
- **KG is net** (`pack size × quantity`). No gross weight, no packaging weight, no carton count.
- Colour only via `static/theme.css` — `tests/test_theme.py` fails a hardcoded hex/rgba or a second `:root`.
- Never use `round(n/10)*10`; never edit `.env` or `tracker.db`.
- Commit after every task; full suite green at each commit.
- **Test fixtures are RECORDED from the live account, never invented.** Capture them once
  into `tests/fixtures/orders_*.json` and commit them. Invented payloads would have missed
  both the `SellerSKU` namespace difference and the 1995 sentinel — the two facts that
  changed this design.
- **Nothing in this feature writes to Amazon.** Phase A is read-only: no ship button, no
  label download, no local "packed" tick. Amazon's status is the single source of truth.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `app/models.py` | `AmazonOrder`, `AmazonOrderItem` | Modify |
| `alembic/versions/<new>_amazon_orders.py` | The two tables | Create |
| `deploy/update-ec2.sh` | Newest-first detector branch | Modify |
| `app/orders/__init__.py` | Package marker | Create |
| `app/orders/logic.py` | **Pure**: IST conversion, EZ filter, bucketing, the picking-sheet aggregation | Create |
| `app/orders/spapi_orders.py` | The only caller of the Orders API: paging, rate limit, retries | Create |
| `app/orders/repository.py` | The only reader/writer of order rows | Create |
| `app/orders/refresh.py` | The serialized refresh job + its progress state | Create |
| `app/routers/orders.py` | HTTP: list, picking sheet, refresh, status, export | Create |
| `app/permissions.py` | `ORDERS` area | Modify |
| `app/main.py` | `/orders-page` route | Modify |
| `app/scheduler.py` | Daily refresh job + retention for the new tables | Modify |
| `templates/nav.html` | Orders link | Modify |
| `templates/orders.html` | The screen | Create |
| `tests/test_orders_logic.py` | The aggregation and bucketing (pure, no DB) | Create |
| `tests/test_orders_api.py` | Routes, permissions, export | Create |
| `tests/test_orders_refresh.py` | Paging, rate limit, serialization, resume | Create |
| `tests/fixtures/orders_*.json` | Payloads recorded from the live account 2026-08-24 | Create |
| `tests/test_nav_consistency.py` | `CANONICAL_NAV` + `ADMIN_PAGES` | Modify |
| `CLAUDE.md` | Feature notes | Modify |

**Task order is deliberate:** the pure aggregation (Task 2) lands before anything that stores or renders it, because it is the feature and it needs no database. Tasks 1–4 are backend and independently committable; 5 is the router, 6 the screen, 7 the scheduler and deploy gate.

---

### Task 1: The two tables

**Files:**
- Modify: `app/models.py` (append after `ProductPrice`)
- Create: `alembic/versions/c3d8e5f21a47_amazon_orders.py`
- Modify: `deploy/update-ec2.sh` (baseline detector heredoc, ~line 296)
- Test: `tests/test_orders_logic.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `AmazonOrder` (table `amazon_orders`), `AmazonOrderItem` (table `amazon_order_items`). Revision `c3d8e5f21a47`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_orders_logic.py`:

```python
"""The Orders tab: a daily picking sheet from Amazon Easy Ship orders.

Asked for: *"the orders which have to be shipped today. item wise weight wise qty
totalled. total number of orders of each item and total orders."*

So the aggregate IS the feature and the order rows are raw material. Three properties
carry most of the weight, and each was measured against the live account on 2026-08-24
rather than assumed:

* **Easy Ship is identified by `ShipServiceLevel` containing `EZ`**, not by
  `FulfillmentChannel == "MFN"`. Three `S02-…` orders are MFN `"Standard"` with a
  ship-by of **1995-01-01**, a sentinel that would sit at the top of every morning's
  sheet as 31 years overdue.
* **Ship-by dates are IST.** Every real `LatestShipDate` is `18:29Z`, which is
  `23:59 IST` — Amazon means "end of day in India". Bucketed in UTC, tonight's orders
  land on the wrong day.
* **Pack sizes never collapse.** 500 g and 1 kg of one product are separate lines or
  the packer goes to the wrong bin, while the KG column is what the courier needs.
"""
import pytest

from app.models import AmazonOrder, AmazonOrderItem

pytestmark = pytest.mark.regression


def test_the_order_tables_exist_with_utc_named_timestamps():
    """`*_utc` naming is half the timezone guard.

    The app is IST and the API is UTC; a column called `latest_ship_date` invites a
    future reader to render it directly, which shows every deadline 5.5 hours early on
    the one screen whose job is "what must go out today".
    """
    for column in ("purchase_date_utc", "latest_ship_date_utc"):
        assert column in AmazonOrder.__table__.c, f"{column} missing"
    assert "latest_ship_date" not in AmazonOrder.__table__.c, (
        "an un-suffixed timestamp column invites rendering UTC as local time"
    )
    # The order id is the upsert key: a re-refresh must update, never duplicate.
    assert AmazonOrder.__table__.c.amazon_order_id.unique is True
    assert "asin" in AmazonOrderItem.__table__.c
    assert "seller_sku" in AmazonOrderItem.__table__.c
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv/Scripts/python -m pytest tests/test_orders_logic.py -q`
Expected: FAIL — `ImportError: cannot import name 'AmazonOrder' from 'app.models'`.

- [ ] **Step 3: Add the models**

Append to `app/models.py`:

```python
class AmazonOrder(Base):
    """One Amazon order, as Amazon last reported it.

    **A cache of Amazon's data, not a record of our own.** Nothing in this app edits
    these rows: if a value looks wrong the fix is a refresh, which is why there is no
    editing anywhere in the Orders feature and no local "packed" tick. A second source
    of truth about whether an order shipped is the class of bug the shipment feature's
    write-separation design exists to avoid.

    Stored rather than fetched per request because `getOrders` is rate-limited to
    **0.045 requests/second** — one call every 22 seconds, measured from the live
    account. A page that called it would hang, and two people opening the tab would 429.
    """
    __tablename__ = "amazon_orders"

    id = Column(Integer, primary_key=True)
    #: Amazon's own id ("403-7588486-5589960"). UNIQUE so a re-refresh UPDATES rather
    #: than duplicating — the same reasoning as the (plan_id, pack_date) index on
    #: packing days, where a repeated save from a warehouse phone must not double-count.
    amazon_order_id = Column(String(20), unique=True, nullable=False, index=True)

    # ── Timestamps: stored UTC, rendered IST. See orders.logic.to_ist. ──
    #
    # The `_utc` suffix is load-bearing. Every real LatestShipDate is 18:29Z = 23:59 IST,
    # so a reader who renders these directly shows every deadline 5.5 hours early.
    purchase_date_utc = Column(DateTime)
    latest_ship_date_utc = Column(DateTime)

    status = Column(String(20))              # Unshipped / Shipped / Canceled …
    easyship_status = Column(String(30))     # PendingSchedule / PickedUp / Delivered …
    #: Contains "EZ" for Easy Ship. This is the field that identifies the channel —
    #: FulfillmentChannel is MFN for both Easy Ship and plain self-ship.
    ship_service_level = Column(String(60))

    order_total = Column(Numeric(12, 2))
    currency = Column(String(5))
    items_ordered = Column(Integer, default=0)
    items_shipped = Column(Integer, default=0)
    is_prime = Column(Boolean, default=False)
    #: True when ship_service_level mentions COD. Read off the service level rather than
    #: PaymentMethod, which reads "Other" on real COD orders.
    is_cod = Column(Boolean, default=False)

    # Destination, coarse. City/state/postcode is all Amazon gives without the PII role,
    # and it is all a picking sheet needs — no buyer name, no street address.
    city = Column(String(60))
    state = Column(String(60))
    postal_code = Column(String(12))

    #: When this app first saw the order, so "new since I last looked" is answerable
    #: separately from "Amazon changed something".
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    last_refreshed_at = Column(DateTime, default=datetime.utcnow)
    #: NULL until the line items have been fetched. The refresh calls getOrderItems only
    #: where this is NULL, so re-refreshing 100 known orders costs zero item calls.
    items_fetched_at = Column(DateTime)

    items = relationship(
        "AmazonOrderItem", back_populates="order", lazy="selectin",
        cascade="all, delete-orphan",
    )


class AmazonOrderItem(Base):
    """One line of an Amazon order.

    **The ASIN is the key, not the SellerSKU.** Measured: an order carries
    `SellerSKU: "R-bss 1 kg"`, which is absent from `pricing_data.json`, while its
    `ASIN: "B0G2MKVVB8"` is in the catalogue. Easy Ship SKUs are a different namespace
    from FBA SKUs, so joining on SKU matches nothing and renders every row as an unknown
    product. The SKU is still stored — it is what Amazon's label shows — but it is not
    how the product is identified.
    """
    __tablename__ = "amazon_order_items"
    __table_args__ = (
        Index("idx_amazon_order_items_order_asin", "order_id", "asin"),
    )

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("amazon_orders.id"), nullable=False)
    asin = Column(String(10), nullable=False)
    seller_sku = Column(String(80))
    title = Column(Text)
    quantity_ordered = Column(Integer, default=0)
    quantity_shipped = Column(Integer, default=0)
    item_price = Column(Numeric(12, 2))
    item_tax = Column(Numeric(12, 2))
    promotion_discount = Column(Numeric(12, 2))

    order = relationship("AmazonOrder", back_populates="items")
```

- [ ] **Step 4: Run it to verify it passes**

Run: `venv/Scripts/python -m pytest tests/test_orders_logic.py -q`
Expected: PASS (1 test).

- [ ] **Step 5: Confirm the model/migration gate now fails**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py::test_migrations_match_models -q`
Expected: FAIL — the models declare tables no migration creates. That is the gate working; Step 6 satisfies it.

- [ ] **Step 6: Write the migration**

Create `alembic/versions/c3d8e5f21a47_amazon_orders.py`:

```python
"""amazon_orders and amazon_order_items

A local cache of Amazon Easy Ship orders, so the Orders tab can render instantly.
`getOrders` is rate-limited to 0.045 req/sec (one call every 22 seconds, measured), so
fetching per request is not an option.

No data migration: the tables start empty and the background refresh fills them.

Revision ID: c3d8e5f21a47
Revises: b2f7c1a94e05
Create Date: 2026-08-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c3d8e5f21a47"
down_revision: Union[str, None] = "b2f7c1a94e05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "amazon_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("amazon_order_id", sa.String(length=20), nullable=False),
        sa.Column("purchase_date_utc", sa.DateTime(), nullable=True),
        sa.Column("latest_ship_date_utc", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("easyship_status", sa.String(length=30), nullable=True),
        sa.Column("ship_service_level", sa.String(length=60), nullable=True),
        sa.Column("order_total", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=5), nullable=True),
        sa.Column("items_ordered", sa.Integer(), nullable=True),
        sa.Column("items_shipped", sa.Integer(), nullable=True),
        sa.Column("is_prime", sa.Boolean(), nullable=True),
        sa.Column("is_cod", sa.Boolean(), nullable=True),
        sa.Column("city", sa.String(length=60), nullable=True),
        sa.Column("state", sa.String(length=60), nullable=True),
        sa.Column("postal_code", sa.String(length=12), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("items_fetched_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE: the upsert target. A repeated refresh must update, not duplicate.
    op.create_index(
        op.f("ix_amazon_orders_amazon_order_id"),
        "amazon_orders", ["amazon_order_id"], unique=True,
    )
    op.create_table(
        "amazon_order_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("asin", sa.String(length=10), nullable=False),
        sa.Column("seller_sku", sa.String(length=80), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("quantity_ordered", sa.Integer(), nullable=True),
        sa.Column("quantity_shipped", sa.Integer(), nullable=True),
        sa.Column("item_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("item_tax", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("promotion_discount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["amazon_orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_amazon_order_items_order_asin",
        "amazon_order_items", ["order_id", "asin"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_amazon_order_items_order_asin", table_name="amazon_order_items")
    op.drop_table("amazon_order_items")
    op.drop_index(op.f("ix_amazon_orders_amazon_order_id"), table_name="amazon_orders")
    op.drop_table("amazon_orders")
```

- [ ] **Step 7: Run the schema gate**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py -q`
Expected: `test_migrations_match_models` PASSES; `test_the_deploy_detector_reports_the_head_for_a_head_schema` FAILS, because the detector still answers `b2f7c1a94e05` for a schema now at `c3d8e5f21a47`. Step 8 fixes it.

- [ ] **Step 8: Add the newest-first detector branch**

In `deploy/update-ec2.sh`, inside the `BASELINE=` heredoc, insert immediately after `if not tables:` and **before** the `carried_from_plan_id` branch:

```python
elif "amazon_orders" in tables:
    print("c3d8e5f21a47")                           # head: amazon order cache
elif "shipment_packing_days" in tables and "carried_from_plan_id" in cols("shipment_packing_days"):
```

- [ ] **Step 9: Run the schema suite, then the whole suite**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py -q`
Expected: PASS. The detector test *runs* the real heredoc, so this proves the deploy stamps the true head.

Run: `venv/Scripts/python -m pytest -q`
Expected: PASS, 1140 tests.

- [ ] **Step 10: Rehearse the migration both ways**

```bash
cd "$(git rev-parse --show-toplevel)"
export PYTHONPATH="$PWD" DATABASE_URL="sqlite+aiosqlite:///./tmp-orders-check.db"
venv/Scripts/alembic upgrade head && venv/Scripts/alembic downgrade -1 && venv/Scripts/alembic upgrade head
venv/Scripts/alembic current
rm -f tmp-orders-check.db
```
Expected: clean each way, final line `c3d8e5f21a47 (head)`. Delete the scratch file — it is not part of the commit.

- [ ] **Step 11: Commit**

```bash
git add app/models.py alembic/versions/c3d8e5f21a47_amazon_orders.py deploy/update-ec2.sh tests/test_orders_logic.py
git commit -m "feat: amazon_orders and amazon_order_items, a local Easy Ship order cache

Orders are stored rather than fetched per request because getOrders is rate-limited to
0.045 req/sec — one call every 22 seconds, measured on the live account. A page that
called it would hang and two viewers would 429.

Timestamp columns carry a _utc suffix deliberately: every real LatestShipDate is 18:29Z
= 23:59 IST, so a reader who renders them directly shows every deadline 5.5 hours early.

The item table keys on ASIN, not SellerSKU: an order carries 'R-bss 1 kg', which is
absent from pricing_data.json, while its ASIN is in the catalogue.

The deploy baseline detector gets its newest-first branch in the same commit, because a
stale detector is a failed deploy rather than a cosmetic omission."
```

---

### Task 2: `app/orders/logic.py` — the picking sheet, pure

This is the feature. It needs no database and no network, so it is built and tested first.

**Files:**
- Create: `app/orders/__init__.py`, `app/orders/logic.py`
- Test: `tests/test_orders_logic.py` (append)

**Interfaces:**
- Consumes: `app.shipment.logic.weight_label` (existing: `0.5 -> "500g"`, `1.0 -> "1 kg"`).
- Produces:
  - `IST: timezone` (UTC+5:30)
  - `SENTINEL_YEAR = 1995`
  - `to_ist(value: datetime | None) -> datetime | None`
  - `is_easy_ship(ship_service_level) -> bool`
  - `ship_by_date(order) -> date | None` — IST calendar date, `None` for the sentinel
  - `BUCKET_TODAY / BUCKET_PICKUP / BUCKET_LATER / BUCKET_DONE: str`
  - `bucket_for(order, today: date) -> str`
  - `picking_sheet(orders, catalogue, today: date) -> dict` returning
    `{"sections": {bucket: {"lines": [...], "totals": {...}}}, "unknown_asins": [...]}`
    where each line is
    `{"product", "weight", "weight_label", "brand", "quantity", "orders", "kg", "known"}`

- [ ] **Step 1: Write the failing tests for conversion, filtering and bucketing**

Append to `tests/test_orders_logic.py`:

```python
# ─── IST, the EZ filter, and the 1995 sentinel ───────────────────────────────

from datetime import date, datetime, timedelta, timezone

from app.orders import logic


def test_a_ship_by_deadline_reads_as_end_of_day_in_ist():
    """The real payload: 18:29 UTC is 23:59 IST, not 18:29.

    Every LatestShipDate on this account is 18:29Z — Amazon expressing "end of today in
    India". Rendered as UTC the packer sees a deadline 5.5 hours earlier than the truth,
    on the one screen whose whole purpose is what must go out today.
    """
    utc = datetime(2026, 7, 12, 18, 29, tzinfo=timezone.utc)
    ist = logic.to_ist(utc)
    assert (ist.hour, ist.minute) == (23, 59), f"got {ist:%H:%M}"
    assert ist.date() == date(2026, 7, 12), "the calendar day must not shift"


def test_a_naive_timestamp_is_treated_as_utc():
    """Rows come back from SQLite without a tzinfo.

    SQLAlchemy's DateTime is naive, so the value read from the database has no timezone
    even though it was stored as UTC. Treating it as local would silently subtract 5.5
    hours from every deadline — a fixed offset error, which is the hardest kind to spot
    because everything still looks plausible.
    """
    ist = logic.to_ist(datetime(2026, 7, 12, 18, 29))
    assert (ist.hour, ist.minute) == (23, 59)


def test_to_ist_passes_none_through():
    """A missing timestamp must not raise — a cancelled order can lack a ship-by."""
    assert logic.to_ist(None) is None


@pytest.mark.parametrize("level,expected", [
    ("Std IN EZ National COD", True),
    ("Std IN EZ Remote", True),
    ("Std IN EZ Metro COD", True),
    ("Standard", False),          # the real S02- orders
    ("", False),
    (None, False),
])
def test_easy_ship_is_identified_by_the_service_level(level, expected):
    """`ShipServiceLevel` contains EZ; FulfillmentChannel does not distinguish.

    Both Easy Ship and plain self-ship report MFN, so filtering on the channel lets
    three real `S02-…` "Standard" orders into the sheet — and those carry a ship-by of
    1995-01-01, which would sit at the top of every morning as 31 years overdue.
    """
    assert logic.is_easy_ship(level) is expected


def test_the_1995_sentinel_is_not_a_deadline():
    """Amazon sends 1995-01-01 when there is no Easy Ship ship-by.

    Treated as a real date it sorts before everything and reads as catastrophically
    overdue. Treated as None it is simply absent, which is the truth.
    """
    order = _order(latest_ship_date_utc=datetime(1995, 1, 1, 0, 0))
    assert logic.ship_by_date(order) is None


def test_a_real_deadline_is_the_ist_calendar_date():
    order = _order(latest_ship_date_utc=datetime(2026, 8, 24, 18, 29))
    assert logic.ship_by_date(order) == date(2026, 8, 24)
```

Add this helper immediately above those tests:

```python
def _order(**overrides):
    """An order shaped as the repository returns one."""
    base = {
        "amazon_order_id": "403-0000000-0000001",
        "status": "Unshipped",
        "easyship_status": "PendingSchedule",
        "ship_service_level": "Std IN EZ National COD",
        "purchase_date_utc": datetime(2026, 8, 24, 6, 0),
        "latest_ship_date_utc": datetime(2026, 8, 24, 18, 29),
        "order_total": 319.0,
        "city": "NAVSARI",
        "state": "GUJARAT",
        "items": [],
    }
    base.update(overrides)
    return base
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_orders_logic.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.orders'`.

- [ ] **Step 3: Create the package and the conversion/filter/bucket helpers**

Create `app/orders/__init__.py`:

```python
"""Amazon Easy Ship orders: the local cache, the refresh job and the picking sheet.

Mirrors app/shipment/'s split — logic.py is pure, spapi_orders.py is the only caller of
Amazon, repository.py is the only reader/writer of rows. That separation is what makes
the phase-B shipping actions additive rather than a rewrite.
"""
```

Create `app/orders/logic.py`:

```python
"""Pure rules for the Orders tab. No database, no network.

Everything here was measured against the live account on 2026-08-24 rather than assumed,
and three of the four rules exist because the obvious version is wrong:

* `is_easy_ship` keys on `ShipServiceLevel`, not `FulfillmentChannel`.
* `ship_by_date` refuses Amazon's 1995 sentinel.
* `to_ist` exists because every deadline is 18:29Z = 23:59 IST.

The fourth, `picking_sheet`, is the feature: item + pack size + brand, with quantity,
order counts and a net kilogram total.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone

from app.shipment.logic import weight_label

#: India Standard Time. A fixed offset — India has no DST, so a full tzdata lookup would
#: add a dependency for no behaviour.
IST = timezone(timedelta(hours=5, minutes=30))

#: Amazon sends 1995-01-01T00:00:00Z as "no Easy Ship ship-by". Three real `S02-…`
#: orders carry it. Rendered as a date it reads as 31 years overdue and sorts to the top
#: of the packer's sheet, so it is treated as absent.
SENTINEL_YEAR = 1995

#: Easy Ship service levels all contain this token: "Std IN EZ National COD",
#: "Std IN EZ Remote", "Std IN EZ Metro COD". Plain self-ship reads "Standard".
EASY_SHIP_TOKEN = "EZ"

BUCKET_TODAY = "to_pack"      # unshipped, label not yet generated, due today or overdue
BUCKET_PICKUP = "awaiting_pickup"   # labelled, not yet collected
BUCKET_LATER = "later"        # unshipped, due after today
BUCKET_DONE = "done"          # picked up, delivered, returned, cancelled

#: `PendingSchedule` means Amazon has no label for this order yet, so the physical job is
#: pick-pack-and-label. Measured: all 97 currently unshipped orders are PendingSchedule.
STATUS_PENDING_SCHEDULE = "PendingSchedule"

#: Statuses that mean the order needs nothing from the warehouse today.
FINISHED_EASYSHIP = frozenset({
    "PickedUp", "Delivered", "ReturnedToSeller", "ReturningToSeller", "LabelCanceled",
})

#: Order-level statuses that take an order off the floor entirely.
FINISHED_ORDER = frozenset({"Canceled", "Shipped", "InvoiceUnconfirmed", "Pending"})

#: Order statuses that still need packing.
OPEN_ORDER = frozenset({"Unshipped", "PartiallyShipped"})


def _field(row, name, default=None):
    return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)


def to_ist(value: datetime | None) -> datetime | None:
    """A UTC timestamp as IST. `None` passes through.

    **A naive value is treated as UTC**, because that is what it is: SQLAlchemy's
    DateTime drops the tzinfo, so a row read back from the database has none even though
    it was stored as UTC. Assuming local instead would subtract 5.5 hours from every
    deadline — a uniform error, and therefore the hardest kind to notice.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def is_easy_ship(ship_service_level) -> bool:
    """Is this an Easy Ship order?

    Keyed on the service level because `FulfillmentChannel` reads MFN for BOTH Easy Ship
    and plain self-ship. Filtering on the channel admits three real `S02-…` "Standard"
    orders whose ship-by is the 1995 sentinel.
    """
    return EASY_SHIP_TOKEN in (ship_service_level or "").upper().split()


def ship_by_date(order) -> date | None:
    """The ship-by deadline as an IST calendar date, or None when there is none.

    None covers both a missing timestamp and Amazon's 1995 sentinel. Callers must treat
    None as "no deadline" — never as "overdue", which is what a naive date comparison
    would conclude.
    """
    raw = _field(order, "latest_ship_date_utc")
    local = to_ist(raw)
    if local is None or local.year <= SENTINEL_YEAR:
        return None
    return local.date()


def bucket_for(order, today: date) -> str:
    """Which section of the picking sheet this order belongs in.

    The two actionable buckets are two different PHYSICAL jobs, which is why they are
    separate sections rather than one "not done" list:

    * ``to_pack`` — pick it, pack it, generate the label in Seller Central.
    * ``awaiting_pickup`` — already boxed and labelled; hand it to the courier.

    **`awaiting_pickup` is defined by exclusion, deliberately.** Across 90 days this
    account only ever showed `PendingSchedule`, `PickedUp`, `Delivered`,
    `ReturnedToSeller` and `LabelCanceled` — never `LabelGenerated` or `ReadyForPickup`,
    presumably because labels are generated and collected the same day. Hardcoding those
    two strings would produce a permanently empty section, so anything open that is NOT
    pending counts as labelled, and the raw status is rendered on the row so an
    unexpected value is visible rather than silently mis-filed.

    An order with no deadline is `later`, never `to_pack`: it is real work but not
    today's, and putting it in today's total would inflate the number the warehouse
    plans against.
    """
    easyship = (_field(order, "easyship_status") or "").strip()
    status = (_field(order, "status") or "").strip()

    if easyship in FINISHED_EASYSHIP or status not in OPEN_ORDER:
        return BUCKET_DONE

    if easyship and easyship != STATUS_PENDING_SCHEDULE:
        return BUCKET_PICKUP

    due = ship_by_date(order)
    if due is not None and due <= today:
        return BUCKET_TODAY
    return BUCKET_LATER
```

- [ ] **Step 4: Run to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_orders_logic.py -q`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add app/orders/__init__.py app/orders/logic.py tests/test_orders_logic.py
git commit -m "feat: orders.logic — IST conversion, the EZ filter and section bucketing

Three rules whose obvious version is wrong, each measured on the live account:

- is_easy_ship keys on ShipServiceLevel, not FulfillmentChannel: both Easy Ship and
  plain self-ship report MFN, and the three S02- 'Standard' orders carry a 1995 ship-by.
- ship_by_date refuses that 1995 sentinel rather than rendering it as 31 years overdue.
- to_ist treats a naive datetime as UTC, because SQLAlchemy drops the tzinfo and
  assuming local would subtract 5.5 hours from every deadline uniformly.

awaiting_pickup is defined by exclusion: 90 days of this account never showed
LabelGenerated or ReadyForPickup, so hardcoding those strings would make the section
permanently empty."
```

---

### Task 3: `picking_sheet` — the aggregation

Split from Task 2 so the aggregation gets its own review: it is the deliverable the warehouse actually reads.

**Files:**
- Modify: `app/orders/logic.py` (append)
- Test: `tests/test_orders_logic.py` (append)

**Interfaces:**
- Consumes: `bucket_for`, `weight_label`, `BUCKET_*` from Task 2.
- Produces: `picking_sheet(orders, catalogue, today) -> dict` (shape as declared in Task 2).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orders_logic.py`:

```python
# ─── The picking sheet: the feature itself ───────────────────────────────────

CATALOGUE = {
    "B0CHANA500": {"name": "Chana Sattu", "weight": 0.5, "brand": "Mithila Foods"},
    "B0CHANA1KG": {"name": "Chana Sattu", "weight": 1.0, "brand": "Mithila Foods"},
    "B0RAGI1KG0": {"name": "Ragi Atta", "weight": 1.0, "brand": "Mithila Foods"},
    "B0POSTA100": {"name": "Bengali Posta", "weight": 0.1, "brand": "Howrah Foods"},
}
TODAY = date(2026, 8, 24)


def _item(asin, qty=1):
    return {"asin": asin, "seller_sku": f"sku-{asin}", "title": f"title {asin}",
            "quantity_ordered": qty}


def _lines(sheet, bucket=logic.BUCKET_TODAY):
    return sheet["sections"][bucket]["lines"]


def _totals(sheet, bucket=logic.BUCKET_TODAY):
    return sheet["sections"][bucket]["totals"]


def test_quantities_and_order_counts_are_aggregated_per_product_and_size():
    """"item wise weight wise qty totalled … total orders of each item".

    QUANTITY sums units; ORDERS counts the orders that contain the line. They differ, and
    both matter: "24 units across 22 orders" tells the packer how much to pick AND how
    many parcels to expect. Summing lines instead of orders would report 24/24 and
    quietly overstate the parcel count.
    """
    orders = [
        _order(amazon_order_id="1", items=[_item("B0CHANA500", 2)]),
        _order(amazon_order_id="2", items=[_item("B0CHANA500", 1)]),
        _order(amazon_order_id="3", items=[_item("B0RAGI1KG0", 1)]),
    ]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    chana = next(l for l in _lines(sheet) if l["product"] == "Chana Sattu")
    assert chana["quantity"] == 3, "units did not sum"
    assert chana["orders"] == 2, "ORDERS must count orders, not lines"
    assert _totals(sheet)["orders"] == 3, "the total counts distinct orders"
    assert _totals(sheet)["quantity"] == 4


def test_two_sizes_of_one_product_stay_on_separate_lines():
    """500g and 1kg live on different shelves.

    Collapsing them into "Chana Sattu, 3 units" sends the packer to one bin for a pick
    that needs two. The product name alone is not the key — product PLUS pack size is.
    """
    orders = [_order(amazon_order_id="1",
                     items=[_item("B0CHANA500", 2), _item("B0CHANA1KG", 1)])]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    chana = [l for l in _lines(sheet) if l["product"] == "Chana Sattu"]
    assert len(chana) == 2, f"sizes collapsed into {len(chana)} line(s)"
    assert {l["weight_label"] for l in chana} == {"500g", "1 kg"}


def test_the_kilogram_total_multiplies_pack_size_by_quantity():
    """The number the courier and the vehicle care about, and it is NET.

    Deliberately asymmetric quantities: 24 x 500g and 12 x 1kg are BOTH 12 kg, so a test
    using equal weights could not tell a correct total from one that summed pack sizes
    without multiplying by quantity. Here 500g x 24 = 12.0 and 1kg x 12 = 12.0, and the
    total is 24.0 — a sum-without-multiply would produce 1.5.
    """
    orders = [_order(amazon_order_id=str(i), items=[_item("B0CHANA500", 1)])
              for i in range(24)]
    orders += [_order(amazon_order_id=f"k{i}", items=[_item("B0RAGI1KG0", 1)])
               for i in range(12)]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    by_label = {l["weight_label"]: l for l in _lines(sheet)}
    assert by_label["500g"]["kg"] == pytest.approx(12.0)
    assert by_label["1 kg"]["kg"] == pytest.approx(12.0)
    assert _totals(sheet)["kg"] == pytest.approx(24.0)


def test_the_totals_row_equals_the_sum_of_the_lines():
    """A totals row that disagrees with its own lines is worse than none."""
    orders = [
        _order(amazon_order_id="1", items=[_item("B0CHANA500", 3)]),
        _order(amazon_order_id="2", items=[_item("B0POSTA100", 5)]),
    ]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    lines, totals = _lines(sheet), _totals(sheet)
    assert totals["quantity"] == sum(l["quantity"] for l in lines)
    assert totals["kg"] == pytest.approx(sum(l["kg"] for l in lines))


def test_an_unknown_asin_is_shown_and_named_not_dropped():
    """A missing picking-sheet row is stock nobody packs.

    An ASIN the catalogue does not know still gets a line — using Amazon's own title and
    the SellerSKU — flagged unknown, and reported in `unknown_asins` so the owner can fix
    the sheet. Silently dropping it means the parcel is never picked and nobody finds out
    until Amazon reports a late shipment.
    """
    orders = [_order(amazon_order_id="1", items=[_item("B0MYSTERY1", 4)])]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    line = next(l for l in _lines(sheet) if not l["known"])
    assert line["quantity"] == 4
    assert "B0MYSTERY1" in sheet["unknown_asins"]


def test_a_line_with_no_pack_size_is_excluded_from_kg_but_still_picked():
    """Counted in units, absent from the kilogram total, and named.

    Treating an unknown weight as 0 makes a 47 kg sheet quietly report 40 — a wrong
    number handed to a courier. The units still appear, because the parcel still has to
    be packed.
    """
    catalogue = dict(CATALOGUE)
    catalogue["B0NOWEIGHT"] = {"name": "Mystery Mix", "weight": 0, "brand": "Mithila Foods"}
    orders = [
        _order(amazon_order_id="1", items=[_item("B0CHANA500", 2)]),   # 1.0 kg
        _order(amazon_order_id="2", items=[_item("B0NOWEIGHT", 5)]),   # no weight
    ]
    sheet = logic.picking_sheet(orders, catalogue, TODAY)
    assert _totals(sheet)["kg"] == pytest.approx(1.0), "an unweighed line polluted the total"
    assert _totals(sheet)["quantity"] == 7, "the unweighed units must still be picked"
    assert _totals(sheet)["lines_without_weight"] == 1


def test_lines_are_ordered_by_quantity_descending():
    """The big picks lead, so the sheet reads top-down as a plan of work."""
    orders = [_order(amazon_order_id="1",
                     items=[_item("B0POSTA100", 1), _item("B0CHANA500", 9)])]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    assert [l["quantity"] for l in _lines(sheet)] == [9, 1]


def test_the_three_sections_split_by_the_physical_job():
    """to_pack / awaiting_pickup / later, each a different action.

    A shipped-and-delivered order appears in none of them — it needs nothing.
    """
    orders = [
        _order(amazon_order_id="a", items=[_item("B0CHANA500")]),                 # today
        _order(amazon_order_id="b", easyship_status="LabelGenerated",
               items=[_item("B0RAGI1KG0")]),                                      # pickup
        _order(amazon_order_id="c",
               latest_ship_date_utc=datetime(2026, 8, 27, 18, 29),
               items=[_item("B0POSTA100")]),                                      # later
        _order(amazon_order_id="d", status="Shipped", easyship_status="Delivered",
               items=[_item("B0CHANA1KG")]),                                      # done
    ]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    assert _totals(sheet, logic.BUCKET_TODAY)["orders"] == 1
    assert _totals(sheet, logic.BUCKET_PICKUP)["orders"] == 1
    assert _totals(sheet, logic.BUCKET_LATER)["orders"] == 1
    assert logic.BUCKET_DONE not in sheet["sections"], (
        "finished orders must not occupy a section on a picking sheet"
    )


def test_an_overdue_order_is_in_todays_section_and_flagged():
    """Overdue belongs in today's work, not a fourth box.

    A missed deadline should make today's sheet louder. Hiding it in its own section is
    how it gets scrolled past.
    """
    orders = [_order(amazon_order_id="late",
                     latest_ship_date_utc=datetime(2026, 8, 20, 18, 29),
                     items=[_item("B0CHANA500")])]
    sheet = logic.picking_sheet(orders, CATALOGUE, TODAY)
    assert _totals(sheet, logic.BUCKET_TODAY)["orders"] == 1
    assert _totals(sheet, logic.BUCKET_TODAY)["overdue_orders"] == 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_orders_logic.py -q`
Expected: FAIL — `AttributeError: module 'app.orders.logic' has no attribute 'picking_sheet'`.

- [ ] **Step 3: Implement `picking_sheet`**

Append to `app/orders/logic.py`:

```python
#: The sections a picking sheet renders, in the order the warehouse works them. `done` is
#: absent on purpose: a finished order needs nothing, and a section for it is a section
#: the packer scrolls past every morning.
SHEET_SECTIONS = (BUCKET_TODAY, BUCKET_PICKUP, BUCKET_LATER)


def _catalogue_entry(catalogue, asin: str) -> dict | None:
    return (catalogue or {}).get((asin or "").strip().upper())


def picking_sheet(orders: Sequence, catalogue: Mapping, today: date) -> dict:
    """Today's work, aggregated by product + pack size + brand.

    Returns::

        {"sections": {bucket: {"lines": [...], "totals": {...}}},
         "unknown_asins": [asin, ...]}

    Each line carries ``product, weight, weight_label, brand, quantity, orders, kg,
    known``; each totals block carries ``quantity, orders, kg, lines_without_weight,
    overdue_orders``.

    Four properties are load-bearing:

    **Product PLUS pack size is the key.** 500 g and 1 kg of one product are different
    shelves, so they are different lines. Keying on the name alone would send the packer
    to one bin for a pick that needs two.

    **`orders` counts ORDERS, not lines.** Two units of one SKU in one order is quantity
    2, orders 1. That distinction is what makes "24 units across 22 orders" mean
    something: it is the parcel count as well as the pick count.

    **`kg` is pack size x quantity, and NET.** Cartons, filler and tape are not in the
    catalogue, so a weighbridge reads higher — the same caveat `logic.shipment_weight`
    carries on the shipment side. A line whose pack size is unknown is EXCLUDED from the
    kilogram total and counted in ``lines_without_weight``: treating it as 0 makes a
    47 kg sheet quietly report 40, and a wrong weight reaches a courier.

    **An unknown ASIN is kept, flagged, and named.** A row missing from a picking sheet is
    stock nobody packs, discovered when Amazon reports a late shipment.
    """
    buckets: dict[str, dict] = {
        name: {"lines": {}, "orders": set(), "overdue": set()} for name in SHEET_SECTIONS
    }
    unknown: set[str] = set()

    for order in orders:
        bucket = bucket_for(order, today)
        if bucket not in buckets:
            continue
        holder = buckets[bucket]
        order_id = _field(order, "amazon_order_id") or ""
        holder["orders"].add(order_id)

        due = ship_by_date(order)
        if bucket == BUCKET_TODAY and due is not None and due < today:
            holder["overdue"].add(order_id)

        for item in _field(order, "items") or []:
            asin = (_field(item, "asin") or "").strip().upper()
            if not asin:
                continue
            quantity = int(_field(item, "quantity_ordered") or 0)
            if quantity <= 0:
                continue

            entry = _catalogue_entry(catalogue, asin)
            if entry is None:
                unknown.add(asin)
                product = (_field(item, "title") or asin)[:60]
                weight, brand, known = 0.0, "", False
            else:
                product = entry.get("name") or asin
                weight = float(entry.get("weight") or 0)
                raw_brand = str(entry.get("brand") or "")
                # "MF"/"HF" as the rest of the app writes them, from the sheet's full
                # names. Substring, not equality: the sheet says "Mithila Foods".
                brand = "MF" if "mithila" in raw_brand.lower() else ("HF" if raw_brand else "")
                known = True

            key = (product, weight, brand, known)
            line = holder["lines"].setdefault(
                key, {"product": product, "weight": weight, "brand": brand,
                      "known": known, "quantity": 0, "orders": set()}
            )
            line["quantity"] += quantity
            line["orders"].add(order_id)

    sections: dict[str, dict] = {}
    for name in SHEET_SECTIONS:
        holder = buckets[name]
        lines = []
        for line in holder["lines"].values():
            weight = float(line["weight"] or 0)
            lines.append({
                "product": line["product"],
                "weight": weight,
                "weight_label": weight_label(weight) if weight else "",
                "brand": line["brand"],
                "quantity": line["quantity"],
                "orders": len(line["orders"]),
                "kg": round(weight * line["quantity"], 3) if weight else None,
                "known": line["known"],
            })
        # Quantity descending, then product name, so the big picks lead and the order is
        # deterministic between two renders of the same data.
        lines.sort(key=lambda row: (-row["quantity"], row["product"].casefold()))
        sections[name] = {
            "lines": lines,
            "totals": {
                "quantity": sum(row["quantity"] for row in lines),
                "orders": len(holder["orders"]),
                "kg": round(sum(row["kg"] or 0 for row in lines), 3),
                "lines_without_weight": sum(1 for row in lines if row["kg"] is None),
                "overdue_orders": len(holder["overdue"]),
            },
        }

    return {"sections": sections, "unknown_asins": sorted(unknown)}
```

- [ ] **Step 4: Run to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_orders_logic.py -q`
Expected: PASS (21 tests).

- [ ] **Step 5: Prove three of the tests can fail**

Each mutation must be applied, the named test confirmed red, then the code restored exactly. This project has repeatedly shipped tests that passed for the wrong reason; these three are the ones worth proving.

1. In the line key, drop `weight`: `key = (product, brand, known)`.
   Expected: `test_two_sizes_of_one_product_stay_on_separate_lines` FAILS.
2. Change `line["orders"].add(order_id)` to `line.setdefault("n", 0)` accumulation — i.e. count lines instead of orders by replacing `"orders": len(line["orders"])` with `"orders": line["quantity"]`.
   Expected: `test_quantities_and_order_counts_are_aggregated_per_product_and_size` FAILS.
3. Change `"kg": round(weight * line["quantity"], 3) if weight else None` to `"kg": round(weight, 3) if weight else None`.
   Expected: `test_the_kilogram_total_multiplies_pack_size_by_quantity` FAILS (total 1.5, not 24.0).

Report each outcome. A mutation that leaves the suite green means the test is wrong — fix the test before continuing.

- [ ] **Step 6: Run the whole suite and commit**

Run: `venv/Scripts/python -m pytest -q`
Expected: PASS, 1160 tests.

```bash
git add app/orders/logic.py tests/test_orders_logic.py
git commit -m "feat: orders.logic.picking_sheet — the aggregate the warehouse reads

item + pack size + brand, with quantity, order counts and a net kilogram total, split
into the three sections that are three different physical jobs.

Four load-bearing properties: product PLUS pack size is the key (500g and 1kg are
different shelves); ORDERS counts orders not lines, so '24 units across 22 orders' is
also the parcel count; kg is pack size x quantity and NET, with an unweighed line
excluded from the total and counted rather than treated as 0; and an unknown ASIN is
kept, flagged and named, because a missing picking-sheet row is stock nobody packs.

The kg test uses 24x500g against 12x1kg deliberately — both are 12 kg, so equal weights
could not distinguish a correct total from one that forgot to multiply by quantity.
Mutation-verified on the size key, the order count and the multiplication."
```

---

## Remaining tasks

Tasks 4–7 cover the SP-API client, the repository and refresh job, the router, the screen, and the scheduler/deploy gate. They are specified in the same shape as Tasks 1–3 and are written into this file as they are reached, so the plan stays reviewable in one sitting rather than arriving as one unreadable document.

### Task 4: `app/orders/spapi_orders.py` — the only caller of the Orders API

**Files:**
- Create: `app/orders/spapi_orders.py`
- Test: `tests/test_orders_spapi.py`
- Fixtures (already captured from the live account, 2026-08-25): `tests/fixtures/orders_unshipped.json`, `tests/fixtures/orders_items.json`

**Interfaces:**
- Consumes: `app.shipment.spapi._get` (existing — attaches the token, types errors, and reuses one `httpx.AsyncClient` when passed; measured 3 calls in 2.5 s reused vs ~1.2 s each fresh), `app.shipment.spapi.SpApiError`, `SpApiNotConfigured`; `app.orders.logic.is_easy_ship`.
- Produces:
  - `ORDERS_MIN_INTERVAL = 22.0`
  - `parse_order(payload: dict) -> dict` — one API order into the column shape
  - `parse_items(payload: dict) -> list[dict]`
  - `async fetch_easy_ship_orders(days: int = 90, *, max_pages: int = 10, sleep=asyncio.sleep) -> tuple[list[dict], list[str]]` returning `(orders, warnings)`
  - `async fetch_items(order_ids: Sequence[str], *, sleep=asyncio.sleep) -> dict[str, list[dict]]`

The captured fixtures contain, verified: both `Std IN EZ National COD` / `Std IN EZ Metro COD` **and** two `Standard` orders; the `1995-01-01T00:00:00Z` sentinel; a real `SellerSKU` (`"0.5kg cs 1"`) against `ASIN B0CWGXYLT6`; `BuyerInfo: {}` and an address of city/state/postcode only.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_orders_spapi.py`:

```python
"""Parsing and paging the Orders API, against payloads recorded from the live account.

The fixtures in tests/fixtures/orders_*.json were captured on 2026-08-25, not written by
hand. That matters: invented payloads would have missed the two facts that shaped this
feature — that `SellerSKU` is a different namespace from the FBA SKUs in
`pricing_data.json`, and that a non-Easy-Ship order carries a `1995-01-01` ship-by
sentinel which reads as 31 years overdue if treated as a date.

No test here touches the network. `fetch_easy_ship_orders` takes an injectable sleep so
the 22-second rate limit is asserted rather than waited for.
"""
import json
from pathlib import Path

import pytest

from app.orders import spapi_orders

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_the_fixtures_carry_the_cases_that_shaped_the_design():
    """A guard on the other tests: fixtures that lost these are fixtures that prove nothing.

    If someone re-captures them from a quiet day with no `Standard` orders, the EZ filter
    test would pass against input that could not exercise it.
    """
    orders = _fixture("orders_unshipped.json")["payload"]["Orders"]
    levels = {o.get("ShipServiceLevel") for o in orders}
    assert any("EZ" in (level or "") for level in levels), "no Easy Ship order in the fixture"
    assert "Standard" in levels, "no non-Easy-Ship order, so the EZ filter is untested"
    assert any(o.get("LatestShipDate", "").startswith("1995") for o in orders), (
        "the 1995 sentinel is absent, so the sentinel handling is untested"
    )


def test_an_order_payload_maps_onto_the_column_shape():
    """Field names come from Amazon; column names are ours. This is the only translation.

    Asserted against a real payload so a renamed or newly-absent Amazon field fails here,
    in one place, rather than as a NULL column discovered on the picking sheet.
    """
    orders = _fixture("orders_unshipped.json")["payload"]["Orders"]
    ez = next(o for o in orders if "EZ" in (o.get("ShipServiceLevel") or ""))
    row = spapi_orders.parse_order(ez)

    assert row["amazon_order_id"] == ez["AmazonOrderId"]
    assert row["status"] == ez["OrderStatus"]
    assert row["ship_service_level"] == ez["ShipServiceLevel"]
    # Timestamps are parsed to naive UTC datetimes, matching the *_utc columns.
    assert row["latest_ship_date_utc"].tzinfo is None
    assert row["latest_ship_date_utc"].hour == 18, "the UTC hour must survive parsing"
    assert row["order_total"] == pytest.approx(float(ez["OrderTotal"]["Amount"]))
    assert row["city"] == ez["ShippingAddress"]["City"]
    # COD is read off the service level, because PaymentMethod reads "Other" on real
    # COD orders — measured.
    assert row["is_cod"] is ("COD" in ez["ShipServiceLevel"].upper())


def test_a_missing_order_total_does_not_raise():
    """A cancelled order can arrive without OrderTotal or ShippingAddress.

    The refresh must not die on one odd order: that would lose the whole page, and with a
    22-second rate limit a lost page is minutes of work.
    """
    row = spapi_orders.parse_order({"AmazonOrderId": "403-1", "OrderStatus": "Canceled"})
    assert row["amazon_order_id"] == "403-1"
    assert row["order_total"] is None
    assert row["city"] is None
    assert row["latest_ship_date_utc"] is None


def test_item_payloads_keep_the_sku_but_key_on_the_asin():
    """The real SellerSKU is "0.5kg cs 1" — not in pricing_data.json, but its ASIN is.

    Both are stored: the ASIN is how the product is identified, the SKU is what Amazon's
    label shows.
    """
    items = spapi_orders.parse_items(_fixture("orders_items.json"))
    assert items, "no items parsed"
    first = items[0]
    assert first["asin"].startswith("B0")
    assert first["seller_sku"]
    assert first["quantity_ordered"] >= 1


async def test_paging_waits_the_rate_limit_between_calls_and_filters_to_easy_ship(monkeypatch):
    """Two pages means one wait of at least 22 seconds, and only EZ orders survive.

    Amazon returned `x-amzn-RateLimit-Limit: 0.04512` on the live call — one request every
    22.2 seconds. Paging without the wait earns a 429, and on a 90-day window that costs
    the whole refresh.

    The sleep is injected, so this asserts the delay without spending it.
    """
    page = _fixture("orders_unshipped.json")["payload"]["Orders"]
    calls, slept = [], []

    async def fake_get(path, params=None, client=None):
        calls.append(params or {})
        if len(calls) == 1:
            return {"payload": {"Orders": page, "NextToken": "tok-2"}}
        return {"payload": {"Orders": page}}

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)

    orders, warnings = await spapi_orders.fetch_easy_ship_orders(
        days=90, max_pages=5, sleep=fake_sleep
    )

    assert len(calls) == 2, "did not follow NextToken"
    assert calls[1].get("NextToken") == "tok-2"
    assert slept and min(slept) >= spapi_orders.ORDERS_MIN_INTERVAL, (
        f"paged without waiting the rate limit: slept {slept}"
    )
    assert orders, "no orders returned"
    assert all(o["ship_service_level"] and "EZ" in o["ship_service_level"].upper().split()
               for o in orders), "a non-Easy-Ship order survived the filter"
    assert not any(o["amazon_order_id"].startswith("S02-") for o in orders)


async def test_paging_stops_at_max_pages_and_says_so(monkeypatch):
    """An unbounded loop against a rate-limited API is an hours-long request.

    Amazon keeps returning NextToken while there is more; without a cap a first run on a
    large history would page for as long as the tokens last. The cap is REPORTED, so the
    owner knows the window was truncated rather than believing he saw everything.
    """
    page = _fixture("orders_unshipped.json")["payload"]["Orders"]

    async def fake_get(path, params=None, client=None):
        return {"payload": {"Orders": page, "NextToken": "always-more"}}

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    orders, warnings = await spapi_orders.fetch_easy_ship_orders(
        days=90, max_pages=3, sleep=fake_sleep
    )
    assert any("more pages" in w.lower() for w in warnings), warnings


async def test_fetch_items_asks_only_for_the_ids_it_is_given(monkeypatch):
    """The caller decides which orders need items, and it passes only unfetched ones.

    getOrderItems is cheaper than getOrders but not free, and re-fetching items for 100
    known orders would spend the budget for nothing.
    """
    payload = _fixture("orders_items.json")
    asked = []

    async def fake_get(path, params=None, client=None):
        asked.append(path)
        return payload

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    result = await spapi_orders.fetch_items(["403-A", "403-B"], sleep=fake_sleep)

    assert sorted(result) == ["403-A", "403-B"]
    assert len(asked) == 2
    assert all("/orderItems" in path for path in asked)


async def test_one_failing_order_does_not_lose_the_others(monkeypatch):
    """A 404 on one order must not abandon the batch.

    An order can be cancelled between the list call and the item call. Losing the whole
    batch for that would waste minutes of rate-limited work.
    """
    from app.shipment.spapi import SpApiError

    payload = _fixture("orders_items.json")

    async def fake_get(path, params=None, client=None):
        if "403-BAD" in path:
            raise SpApiError("order not found", status=404)
        return payload

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(spapi_orders.spapi, "_get", fake_get)
    result = await spapi_orders.fetch_items(["403-BAD", "403-OK"], sleep=fake_sleep)

    assert "403-OK" in result, "a good order was lost because another failed"
    assert "403-BAD" not in result
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_orders_spapi.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.orders.spapi_orders'`.

- [ ] **Step 3: Implement the client**

Create `app/orders/spapi_orders.py`:

```python
"""The only caller of Amazon's Orders API.

Auth, error typing and connection reuse come from ``app.shipment.spapi`` rather than being
re-implemented: one token cache and one error type across the app means an auth failure
reads the same wherever it happens.

**Everything here is rate-limited by one number.** Amazon returned
``x-amzn-RateLimit-Limit: 0.04512`` for ``getOrders`` — one request every 22.2 seconds. A
90-day window pages, so a full fetch is minutes of wall-clock time. That single fact is
why orders are cached in the database and why this module is never called from a request
handler.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.orders.logic import is_easy_ship
from app.shipment import spapi
from app.shipment.spapi import SpApiError

logger = logging.getLogger(__name__)

#: Seconds between getOrders calls. Amazon reports 0.04512 req/sec = one per 22.2s;
#: rounded UP to 22.5 because undershooting earns a 429 that costs the whole page.
ORDERS_MIN_INTERVAL = 22.5

#: getOrderItems is documented at ~0.5 req/sec and measured far cheaper than getOrders,
#: but a burst of 100 would still trip it. 2s is comfortable for the 3-4 new orders a day
#: this account actually sees.
ITEMS_MIN_INTERVAL = 2.0

#: Both statuses that still need packing. PartiallyShipped is included because a partly
#: shipped order still has units on the floor.
OPEN_STATUSES = "Unshipped,PartiallyShipped"


def _dt(value) -> datetime | None:
    """An Amazon ISO-8601 timestamp as a NAIVE UTC datetime.

    Naive to match the `*_utc` columns, which SQLAlchemy stores without a timezone. The
    conversion to IST happens once, at render time, in ``orders.logic.to_ist`` — storing
    IST here would put local time in a column named `_utc` and mislead every later reader.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("orders: could not parse timestamp %r", value)
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _money(block) -> float | None:
    """The amount out of an Amazon money block, or None.

    None rather than 0.0: a cancelled order genuinely has no total, and 0.0 would render
    as a real ₹0 order on the reconciliation list.
    """
    if not isinstance(block, dict):
        return None
    try:
        return float(block.get("Amount"))
    except (TypeError, ValueError):
        return None


def parse_order(payload: dict) -> dict:
    """One API order as the `amazon_orders` column shape.

    The ONLY place Amazon's field names are translated to ours, so a renamed field fails
    in one test rather than surfacing as a NULL column on the picking sheet.

    **Every field is optional.** A cancelled order arrives without `OrderTotal` or
    `ShippingAddress`, and dying on one odd order would lose the whole page — which, at 22
    seconds a page, is minutes of work.
    """
    address = payload.get("ShippingAddress") or {}
    level = payload.get("ShipServiceLevel") or ""
    return {
        "amazon_order_id": payload.get("AmazonOrderId") or "",
        "purchase_date_utc": _dt(payload.get("PurchaseDate")),
        "latest_ship_date_utc": _dt(payload.get("LatestShipDate")),
        "status": payload.get("OrderStatus"),
        "easyship_status": payload.get("EasyShipShipmentStatus"),
        "ship_service_level": level,
        "order_total": _money(payload.get("OrderTotal")),
        "currency": (payload.get("OrderTotal") or {}).get("CurrencyCode"),
        "items_ordered": int(payload.get("NumberOfItemsUnshipped") or 0)
                         + int(payload.get("NumberOfItemsShipped") or 0),
        "items_shipped": int(payload.get("NumberOfItemsShipped") or 0),
        "is_prime": bool(payload.get("IsPrime")),
        # Read off the service level, not PaymentMethod: measured, PaymentMethod reads
        # "Other" on real COD orders while the level says "Std IN EZ National COD".
        "is_cod": "COD" in level.upper(),
        "city": address.get("City"),
        "state": address.get("StateOrRegion"),
        "postal_code": address.get("PostalCode"),
    }


def parse_items(payload: dict) -> list[dict]:
    """The `amazon_order_items` rows out of a getOrderItems response.

    A zero-quantity line is dropped: it contributes nothing to a pick and would add a
    0-unit row to the picking sheet.
    """
    rows = []
    for item in (payload.get("payload") or {}).get("OrderItems") or []:
        asin = (item.get("ASIN") or "").strip().upper()
        quantity = int(item.get("QuantityOrdered") or 0)
        if not asin or quantity <= 0:
            continue
        rows.append({
            "asin": asin,
            "seller_sku": item.get("SellerSKU"),
            "title": item.get("Title"),
            "quantity_ordered": quantity,
            "quantity_shipped": int(item.get("QuantityShipped") or 0),
            "item_price": _money(item.get("ItemPrice")),
            "item_tax": _money(item.get("ItemTax")),
            "promotion_discount": _money(item.get("PromotionDiscount")),
        })
    return rows


async def fetch_easy_ship_orders(
    days: int = 90,
    *,
    max_pages: int = 10,
    sleep=asyncio.sleep,
) -> tuple[list[dict], list[str]]:
    """Every Easy Ship order created in the last `days`, paged. Returns (orders, warnings).

    **Waits `ORDERS_MIN_INTERVAL` between pages.** Not politeness: Amazon allows one call
    every 22.2 seconds and a 429 costs the page. `sleep` is injectable so tests assert the
    delay without spending it.

    **Filters to Easy Ship on the SERVICE LEVEL.** `FulfillmentChannel` reads MFN for both
    Easy Ship and plain self-ship, and the non-Easy-Ship orders carry a 1995 ship-by
    sentinel that would sit at the top of the packer's sheet as 31 years overdue.

    **`max_pages` is a cap, and reaching it is REPORTED.** Amazon keeps issuing NextToken
    while more exists; unbounded, a first run could page for as long as the tokens last.
    A silent truncation would have the owner believe he had seen every order.
    """
    settings = get_settings()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    params: dict = {
        "MarketplaceIds": settings.sp_api_marketplace_id,
        "CreatedAfter": since,
        "OrderStatuses": OPEN_STATUSES,
    }
    orders: list[dict] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    token: str | None = None

    for page in range(max_pages):
        if page:
            # The wait belongs BEFORE the call, not after the last one: sleeping after the
            # final page would add 22 idle seconds to every refresh.
            await sleep(ORDERS_MIN_INTERVAL)
            params = {
                "MarketplaceIds": settings.sp_api_marketplace_id,
                "NextToken": token,
            }

        payload = (await spapi._get("/orders/v0/orders", params=params)).get("payload") or {}
        raw = payload.get("Orders") or []
        for item in raw:
            row = parse_order(item)
            if not row["amazon_order_id"] or row["amazon_order_id"] in seen_ids:
                continue
            if not is_easy_ship(row["ship_service_level"]):
                continue
            seen_ids.add(row["amazon_order_id"])
            orders.append(row)

        token = payload.get("NextToken")
        if not token:
            break
    else:
        if token:
            warnings.append(
                f"Stopped after {max_pages} pages and Amazon reports more pages of orders. "
                "Older orders were not fetched — run the refresh again to continue."
            )

    logger.info("orders: fetched %d Easy Ship order(s) in %d page(s)", len(orders), page + 1)
    return orders, warnings


async def fetch_items(
    order_ids: Sequence[str], *, sleep=asyncio.sleep
) -> dict[str, list[dict]]:
    """Line items for the named orders. `{order_id: [item, ...]}`.

    The CALLER decides which orders need items — it passes only those whose
    `items_fetched_at` is NULL — so re-refreshing 100 known orders costs zero calls here.

    **One order failing does not abandon the batch.** An order can be cancelled between
    the list call and this one, and losing every other order's items to a single 404 would
    waste minutes of rate-limited work. The failure is logged and that order is simply
    absent from the result, so the caller leaves its `items_fetched_at` NULL and retries
    next time.
    """
    out: dict[str, list[dict]] = {}
    for index, order_id in enumerate(order_ids):
        if index:
            await sleep(ITEMS_MIN_INTERVAL)
        try:
            payload = await spapi._get(f"/orders/v0/orders/{order_id}/orderItems")
        except SpApiError as exc:            # noqa: BLE001 - logged, and retried next run
            logger.warning("orders: items for %s failed (%s)", order_id, exc)
            continue
        out[order_id] = parse_items(payload)
    return out
```

- [ ] **Step 4: Run to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_orders_spapi.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Prove two of the tests can fail**

1. Change the paging loop to skip the wait: delete `await sleep(ORDERS_MIN_INTERVAL)`.
   Expected: `test_paging_waits_the_rate_limit_between_calls_and_filters_to_easy_ship` FAILS.
2. Remove the `if not is_easy_ship(...): continue` filter.
   Expected: the same test FAILS on the `S02-` assertion.

Restore exactly after each, and report both outcomes.

- [ ] **Step 6: Run the whole suite and commit**

Run: `venv/Scripts/python -m pytest -q`
Expected: PASS, 1172 tests.

```bash
git add app/orders/spapi_orders.py tests/test_orders_spapi.py tests/fixtures/orders_unshipped.json tests/fixtures/orders_items.json
git commit -m "feat: orders.spapi_orders, the only caller of Amazon's Orders API"
```

**Task 5 — `app/orders/repository.py` + `refresh.py`:** upsert by `amazon_order_id`; `items_fetched_at IS NULL` drives item fetching so a second refresh of 100 known orders makes zero item calls. The refresh is serialized (refuses a concurrent start), commits after **each page** so a 429 keeps what was already stored, and exposes a progress dict the screen polls.

**Task 6 — `app/routers/orders.py`, `templates/orders.html`, `app/permissions.py`, `app/main.py`, `templates/nav.html`:** `GET /orders` (sheet + list), `POST /orders/refresh`, `GET /orders/refresh-status`, `GET /orders/download/picking-sheet.xlsx` via `documents.build_simple_xlsx`. New `ORDERS = "orders"` area appended to `AREAS`, `/orders-page` gated with `require_area(permissions.ORDERS)`, nav link added, and `CANONICAL_NAV` + `ADMIN_PAGES` updated in `tests/test_nav_consistency.py` — that test is what actually stops nav drift.

**Task 7 — `app/scheduler.py`, `CLAUDE.md`, deploy:** daily refresh job at `daily_scrape_hour + 2`, `AmazonOrder` added to the retention sweep at **90 days** (`DATA_RETENTION_DAYS`, already 90), then the migration rehearsed against a copy of production, browser verification of the live case, and deploy.
