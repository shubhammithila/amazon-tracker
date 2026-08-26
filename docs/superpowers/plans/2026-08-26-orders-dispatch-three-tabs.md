# Orders Dispatch: Three Tabs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the Orders dispatch screen into three tabs — weight & purchasing with editable raw stock, flat per-SKU packing with undo, and an auto-updating order list — plus five downloads and a flag that runs the orders refresh without waking the other scheduled jobs.

**Architecture:** Additive throughout. One new table (`product_raw_stock`, standing, keyed on parent product name), two new pure functions in `app/orders/logic.py`, four new routes, one new multi-sheet document builder, and one rewritten template. `is_todays_dispatch`, `bucket_for` and `dispatch_sheet`'s existing keys are not changed, so the 264-order rule and every existing test stay intact.

**Tech Stack:** FastAPI (async) · SQLAlchemy 2.0 async · SQLite (Alembic `batch_alter_table`) · openpyxl · reportlab · APScheduler · vanilla JS + Jinja2 · pytest (`venv/Scripts/python -m pytest`)

**Spec:** `docs/superpowers/specs/2026-08-25-orders-dispatch-three-tabs-design.md`

## Global Constraints

- **Run tests with** `venv/Scripts/python -m pytest -q` from the repo root. Suite is 1278 passing / 3 skipped before this work. Random order by default; add `-p no:randomly` when a single test is being iterated.
- **`Numeric` columns return `Decimal`, which `JSONResponse` cannot serialise.** Convert to `float` at the repository boundary. This app already shipped that exact bug with datetimes ("Object of type datetime is not JSON serializable", found on production).
- **`is_todays_dispatch` and `bucket_for` must not be modified.** They are pinned by the tests that caught the 247-order and 264-order bugs.
- **A pack size of 0 or None is excluded from every kilogram total and named on screen** — never counted as 0. Rule already in `picking_sheet` and `shipment_weight`.
- **`to_buy` clamps at 0, and the to-buy TOTAL sums the clamped per-product rows** — never `total_ordered − total_raw`.
- **Every new template colour must come from `static/theme.css`.** `tests/test_theme.py` fails any template that hardcodes a hex or `rgba()` value, re-declares `:root`, or omits the stylesheet link.
- **Every `getElementById(...)` that is written to must exist in the markup.** `tests/test_template_render_targets.py` enforces it — a function with an `if(!el) return;` guard fails silently otherwise.
- **New migration ⇒ new newest-first branch in `deploy/update-ec2.sh`'s baseline detector.** A stale detector stamped production backwards once and cost two failed deploys.
- **Commit after every task.** Never `git add -A`; name the files.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `app/models.py` | `ProductRawStock` ORM model | modify (append) |
| `alembic/versions/e5a1b83c26df_product_raw_stock.py` | create the table + unique index | create |
| `deploy/update-ec2.sh` | detector branch, post-migration table check | modify (~line 306, ~line 361) |
| `app/orders/repository.py` | `load_raw_stock` / `save_raw_stock` | modify (append) |
| `app/orders/logic.py` | `to_buy_kg`, `raw_stock_summary` | modify (append) |
| `app/config.py` | `order_refresh_enabled` setting | modify (line ~52) |
| `app/scheduler.py` | move the early return; gate orders job on either flag | modify (`setup_scheduler`) |
| `app/routers/orders.py` | `_dispatch` gains raw stock; 4 routes | modify |
| `app/shipment/documents.py` | `build_dispatch_xlsx`, `build_tobuy_xlsx` | modify (append) |
| `templates/orders.html` | three tabs, search per tab, undo, 60 s poll | rewrite |
| `tests/test_orders_dispatch.py` | logic + repository tests | modify (append) |
| `tests/test_orders_api.py` | route tests | modify (append) |
| `tests/test_retention_and_scheduler.py` | flag tests | modify (append) |
| `CLAUDE.md` | document the three tabs and the flag | modify |

Task order is dependency order: table → repository → logic → routes → documents → template → scheduler → docs.

---

### Task 1: The `product_raw_stock` table

**Files:**
- Modify: `app/models.py` (append after `OrderPackedEntry`)
- Create: `alembic/versions/e5a1b83c26df_product_raw_stock.py`
- Modify: `deploy/update-ec2.sh` (detector ~line 306, table check ~line 361)
- Test: `tests/test_orders_dispatch.py` (append)

**Interfaces:**
- Consumes: nothing
- Produces: `ProductRawStock` with columns `id: int`, `product: str`, `raw_kg: Numeric(10,2)`, `updated_at: datetime`, `updated_by: str`; unique index `idx_product_raw_stock_product` on `(product)`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_orders_dispatch.py`:

```python
# ─── Raw stock: standing, per parent product ─────────────────────────────────


def test_the_raw_stock_table_is_keyed_on_the_product_and_has_no_date():
    """Raw material on a shelf does not vanish at midnight.

    `order_packed_entries` is keyed on (pack_date, asin) because a packed count belongs to a
    day. Raw stock is the opposite: it is a standing quantity, and a `pack_date` would make it
    blank every morning — so tab 1 would read "buy everything" at 9am daily until 33 numbers
    were retyped.

    Keyed on the parent product NAME, not an ASIN, because raw material is bulk: there is no
    such thing as 500 g-flavoured raw sattu.
    """
    from app.models import ProductRawStock

    columns = ProductRawStock.__table__.c
    assert "product" in columns
    assert "raw_kg" in columns
    assert "pack_date" not in columns, (
        "raw stock must NOT be per-day; a dated field is blank every morning and the "
        "purchasing tab would demand re-entry before it meant anything"
    )
    # The unique index is the real guarantee that a repeated save updates one row.
    indexes = {index.name: index for index in ProductRawStock.__table__.indexes}
    assert "idx_product_raw_stock_product" in indexes
    assert indexes["idx_product_raw_stock_product"].unique is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `venv/Scripts/python -m pytest tests/test_orders_dispatch.py::test_the_raw_stock_table_is_keyed_on_the_product_and_has_no_date -q -p no:randomly`

Expected: FAIL with `ImportError: cannot import name 'ProductRawStock' from 'app.models'`

- [ ] **Step 3: Add the model**

Append to `app/models.py` (after the `OrderPackedEntry` class):

```python
class ProductRawStock(Base):
    """Raw material on hand for one parent product, in kilograms. **Standing, not per-day.**

    Feeds the Orders tab's purchasing view: `to_buy = max(0, ordered_kg - raw_kg)`.

    **No `pack_date`, unlike `OrderPackedEntry`, and the asymmetry is the point.** A packed
    count belongs to a day — it answers "what did we box on the 25th". Raw material on a shelf
    does not vanish at midnight, so a dated row would be blank every morning and the
    purchasing tab would demand 33 numbers be retyped before it meant anything.

    **Keyed on the parent product NAME, not an ASIN.** Raw material is bulk: there is no such
    thing as 500 g-flavoured raw sattu. The name is the catalogue's own `name`, which is the
    key `orders.logic.dispatch_sheet` already groups parents by.

    `Numeric(10, 2)` because this is a weight someone types, and 0.1 kg matters when the
    total reaches a courier. **Callers must convert to `float` before returning it in JSON** —
    SQLAlchemy hands back `Decimal`, which `JSONResponse` cannot serialise.

    Written by hand today. Built to be REPLACED: when the inventory tab exists it writes this
    table instead of a person, and nothing downstream changes.
    """
    __tablename__ = "product_raw_stock"
    __table_args__ = (
        Index("idx_product_raw_stock_product", "product", unique=True),
    )

    id = Column(Integer, primary_key=True)
    #: Parent product name as the MRP catalogue spells it, e.g. "ABC Sattu".
    product = Column(String(120), nullable=False)
    raw_kg = Column(Numeric(10, 2), default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    #: Who typed it, so a surprising number can be asked about.
    updated_by = Column(String(50))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `venv/Scripts/python -m pytest tests/test_orders_dispatch.py::test_the_raw_stock_table_is_keyed_on_the_product_and_has_no_date -q -p no:randomly`

Expected: PASS

- [ ] **Step 5: Create the migration**

Create `alembic/versions/e5a1b83c26df_product_raw_stock.py`:

```python
"""product_raw_stock

Raw material on hand per parent product, in kilograms, feeding the Orders tab's purchasing
view (to_buy = ordered_kg - raw_kg, clamped at 0).

Standing rather than per-day, unlike order_packed_entries: raw material on a shelf does not
vanish at midnight, and a dated row would be blank every morning — the purchasing tab would
demand 33 numbers be retyped before it meant anything.

Keyed on the parent product name rather than an ASIN because raw material is bulk.

No data migration: the table starts empty and the screen fills it. Later the inventory tab
will write it instead of a person.

Revision ID: e5a1b83c26df
Revises: d4f9a2c68b31
Create Date: 2026-08-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e5a1b83c26df"
down_revision: Union[str, None] = "d4f9a2c68b31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_raw_stock",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product", sa.String(length=120), nullable=False),
        sa.Column("raw_kg", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.String(length=50), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE: a repeated save must UPDATE one row, never insert a second standing quantity
    # for the same product — the same property (pack_date, asin) gives packed counts.
    op.create_index(
        "idx_product_raw_stock_product", "product_raw_stock", ["product"], unique=True
    )


def downgrade() -> None:
    op.drop_index("idx_product_raw_stock_product", table_name="product_raw_stock")
    op.drop_table("product_raw_stock")
```

- [ ] **Step 6: Add the deploy detector branch**

In `deploy/update-ec2.sh`, find this block (~line 306) and add the new **newest-first** branch:

```python
if not tables:
    print("")                                       # empty: migrate from scratch
elif "product_raw_stock" in tables:
    print("e5a1b83c26df")                           # head: raw stock for purchasing
elif "order_packed_entries" in tables:
    print("d4f9a2c68b31")                           # warehouse packed counts
elif "amazon_orders" in tables:
    print("c3d8e5f21a47")                           # amazon order cache
```

Then extend the post-migration table check (~line 361) so the new table is proven present:

```python
need = {"shipment_plans", "shipment_plan_items", "shipment_packing_days",
        "shipment_packing_entries", "product_categories", "users",
        "amazon_orders", "order_packed_entries", "product_raw_stock"}
```

- [ ] **Step 7: Verify the migration applies and the detector answers the new head**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py -q -p no:randomly`

Expected: PASS (16 tests). This test *runs* the detector heredoc against a freshly-migrated database and asserts it reports the true head, which is why a forgotten branch fails here rather than on the box.

- [ ] **Step 8: Commit**

```bash
git add app/models.py alembic/versions/e5a1b83c26df_product_raw_stock.py deploy/update-ec2.sh tests/test_orders_dispatch.py
git commit -m "feat: add product_raw_stock, standing raw material per parent product

Keyed on the parent product name with no pack_date, unlike order_packed_entries.
Raw material on a shelf does not vanish at midnight, and a dated row would be
blank every morning — the purchasing tab would demand 33 numbers be retyped
before it meant anything.

Built to be replaced: when the inventory tab exists it writes this table instead
of a person."
```

---

### Task 2: Reading and writing raw stock

**Files:**
- Modify: `app/orders/repository.py` (append; import `ProductRawStock`)
- Test: `tests/test_orders_dispatch.py` (append)

**Interfaces:**
- Consumes: `ProductRawStock` from Task 1
- Produces:
  - `async load_raw_stock(db) -> dict[str, float]` — `{product_name: raw_kg}`, **floats not Decimals**
  - `async save_raw_stock(db, entries: list[dict], updated_by: str = "") -> dict[str, float]` — entries are `{"product": str, "raw_kg": number}`; returns the full map afterwards

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orders_dispatch.py`:

```python
async def test_raw_stock_round_trips_as_a_float_not_a_decimal(db, db_schema):
    """`Numeric` hands back `Decimal`, which `JSONResponse` cannot serialise.

    This app already shipped that bug once with datetimes — "Object of type datetime is not
    JSON serializable", found in a browser on production, on the one path the owner hits when
    he is trying to find out what is happening. Converting at the repository boundary means
    every route inherits the fix rather than remembering it.
    """
    import json

    packed = await repository.save_raw_stock(db, [{"product": "ABC Sattu", "raw_kg": 32.5}])
    assert packed == {"ABC Sattu": 32.5}
    assert isinstance(packed["ABC Sattu"], float), (
        f"got {type(packed['ABC Sattu']).__name__}; a Decimal reaching JSONResponse is a 500"
    )
    json.dumps(packed)          # would raise on a Decimal


async def test_a_repeated_raw_stock_save_updates_one_row(db, db_schema):
    """The UNIQUE index makes it possible; the upsert makes it happen.

    Two standing quantities for one product is a contradiction, not a history.
    """
    await repository.save_raw_stock(db, [{"product": "ABC Sattu", "raw_kg": 10}])
    after = await repository.save_raw_stock(db, [{"product": "ABC Sattu", "raw_kg": 25}])
    assert after == {"ABC Sattu": 25.0}, "a repeated save did not update in place"


async def test_raw_stock_survives_a_change_of_day(db, db_schema):
    """The whole reason the table has no `pack_date`.

    Nothing here passes a date at all, so this test is really asserting the SHAPE: there is no
    per-day key to fall out of. A dated implementation would make the number unreachable
    tomorrow, and tab 1 would read "buy everything" every morning.
    """
    await repository.save_raw_stock(db, [{"product": "Usna Chawal", "raw_kg": 40}])
    assert await repository.load_raw_stock(db) == {"Usna Chawal": 40.0}


async def test_zero_raw_stock_is_stored_not_deleted(db, db_schema):
    """0 kg is a MEASUREMENT here, unlike a packed count of 0.

    `save_packed` deletes a zeroed row, because "0 packed" and "not counted" are the same
    thing on a worksheet. Raw stock is the opposite: "we have none" is exactly the fact that
    makes `to_buy` the full ordered weight, and deleting it would make the row look untouched.
    """
    await repository.save_raw_stock(db, [{"product": "Ragi Atta", "raw_kg": 5}])
    after = await repository.save_raw_stock(db, [{"product": "Ragi Atta", "raw_kg": 0}])
    assert after == {"Ragi Atta": 0.0}, "a deliberate zero was discarded"


async def test_a_negative_raw_stock_is_clamped(db, db_schema):
    """A minus sign in a weight box is a typo, not negative stock on a shelf."""
    after = await repository.save_raw_stock(db, [{"product": "Jau Atta", "raw_kg": -5}])
    assert after == {"Jau Atta": 0.0}


async def test_raw_stock_and_packed_counts_cannot_clobber_each_other(db, db_schema):
    """Two tables, two facts, entered by different people at different moments.

    A shared row would mean the owner's stock entry and the packer's count race, which is the
    failure the shipment feature's write separation exists to prevent.
    """
    await repository.save_raw_stock(db, [{"product": "ABC Sattu", "raw_kg": 12}])
    await repository.save_packed(db, "2026-08-26", [{"asin": "B0ABC500", "units": 7}])

    assert await repository.load_raw_stock(db) == {"ABC Sattu": 12.0}
    assert await repository.load_packed(db, "2026-08-26") == {"B0ABC500": 7}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_orders_dispatch.py -q -p no:randomly -k raw_stock`

Expected: FAIL with `AttributeError: module 'app.orders.repository' has no attribute 'save_raw_stock'`

- [ ] **Step 3: Implement the two functions**

In `app/orders/repository.py`, change the model import line to:

```python
from app.models import AmazonOrder, AmazonOrderItem, OrderPackedEntry, ProductRawStock
```

Then append at the end of the file:

```python
# ─── Raw stock: standing, per parent product, typed by the owner ──────────────
#
# Sits beside the packed counts above and shares their boundary note — these are OUR rows,
# not Amazon's cache. Two separate tables rather than one, because stock and packed are
# different facts entered at different moments by different people, and a shared row means
# one save can clobber the other.


async def load_raw_stock(db: AsyncSession) -> dict[str, float]:
    """`{product_name: raw_kg}` for every product with a standing quantity.

    **Floats, not `Decimal`.** `Numeric` hands back `Decimal` and `JSONResponse` cannot
    serialise it — the same defect this feature already shipped once with datetimes, found in
    a browser on production. Converting here means every route inherits the fix.
    """
    rows = await db.execute(select(ProductRawStock.product, ProductRawStock.raw_kg))
    return {product: float(raw_kg or 0) for product, raw_kg in rows.all()}


async def save_raw_stock(
    db: AsyncSession, entries: list[dict], updated_by: str = ""
) -> dict[str, float]:
    """Upsert standing raw stock. Returns the full `{product: raw_kg}` afterwards.

    SELECT-then-UPDATE-or-INSERT, the same dialect-neutral idiom as `save_packed` and
    `shipment.repository.save_packing_entries`. The UNIQUE index on `product` is the real
    guarantee that a repeated save updates one row rather than storing a second standing
    quantity for the same product.

    **A zero is STORED, not deleted** — the opposite of `save_packed`, deliberately. There, 0
    packed and "not counted" are the same thing on a worksheet, so the row goes. Here "we have
    none" is exactly the fact that makes `to_buy` the full ordered weight, and deleting it
    would leave the row looking untouched.

    Negatives clamp to 0: a minus sign in a weight box is a typo, not stock owed.

    The full map is returned so the screen re-renders from the committed truth rather than
    from what it believes it sent.
    """
    by_product: dict[str, float] = {}
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        product = str(raw.get("product") or "").strip()
        if not product:
            continue
        try:
            value = float(raw.get("raw_kg") or 0)
        except (TypeError, ValueError):
            # A non-numeric weight is dropped rather than stored as 0: 0 is a measurement
            # here, and inventing one from a typo would understate what must be bought.
            logger.warning("orders: ignored non-numeric raw stock for %r", product)
            continue
        # A repeated product in one payload: last wins, as on the packing screen.
        by_product[product] = max(0.0, round(value, 2))

    if not by_product:
        return await load_raw_stock(db)

    existing = {
        row.product: row
        for row in (
            await db.execute(
                select(ProductRawStock).where(
                    ProductRawStock.product.in_(sorted(by_product))
                )
            )
        ).scalars()
    }

    now = datetime.utcnow()
    for product, raw_kg in by_product.items():
        row = existing.get(product)
        if row is None:
            db.add(ProductRawStock(
                product=product, raw_kg=raw_kg, updated_at=now,
                updated_by=updated_by or None,
            ))
        else:
            row.raw_kg = raw_kg
            row.updated_at = now
            row.updated_by = updated_by or None

    await db.commit()
    return await load_raw_stock(db)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_orders_dispatch.py -q -p no:randomly -k raw_stock`

Expected: PASS (6 tests)

- [ ] **Step 5: Run the full suite**

Run: `venv/Scripts/python -m pytest -q`

Expected: `1285 passed, 3 skipped` (1278 + 7 new)

- [ ] **Step 6: Commit**

```bash
git add app/orders/repository.py tests/test_orders_dispatch.py
git commit -m "feat: read and write standing raw stock per product

Floats at the boundary, not Decimal: Numeric hands back Decimal and JSONResponse
cannot serialise it, which is the defect this feature already shipped once with
datetimes and had found in a browser on production.

A zero is stored rather than deleted, the opposite of save_packed. There, 0 packed
and 'not counted' are the same thing; here 'we have none' is the fact that makes
to_buy the full ordered weight."
```

---

### Task 3: `to_buy` arithmetic

**Files:**
- Modify: `app/orders/logic.py` (append)
- Test: `tests/test_orders_dispatch.py` (append)

**Interfaces:**
- Consumes: `dispatch_sheet(orders, catalogue, today, packed=None) -> dict` (existing, unchanged) whose `parents` entries carry `product: str`, `brand: str`, `kg: float`, `units: int`, `orders: int`, `packed: int`, `sizes: list[dict]`
- Produces:
  - `to_buy_kg(ordered_kg, raw_kg) -> float` — clamped at 0, rounded to 2dp
  - `raw_stock_summary(sheet: dict, raw_stock: Mapping) -> dict` returning
    `{"rows": [{"product","brand","ordered_kg","raw_kg","to_buy_kg","covered"}],
      "totals": {"ordered_kg","raw_kg","to_buy_kg","short_products"}}`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orders_dispatch.py`:

```python
# ─── Purchasing: ordered weight against raw stock on hand ────────────────────


def test_to_buy_is_the_shortfall_and_never_negative():
    """Surplus raw stock is not negative purchasing.

    Clamped because this number reaches a purchasing list, where "-7 kg" is not a quantity to
    buy. The same reason `remaining_for` clamps for the packer's sheet.
    """
    assert logic.to_buy_kg(35.0, 10.0) == pytest.approx(25.0)
    assert logic.to_buy_kg(22.5, 32.0) == 0.0, "a surplus produced a negative order"
    assert logic.to_buy_kg(18.0, 0.0) == pytest.approx(18.0)


def test_the_to_buy_total_sums_the_rows_and_does_not_subtract_the_totals():
    """**A surplus of one product must never offset a shortfall of another.**

    You cannot make rice out of sattu. Summing the clamped rows gives 43.00; subtracting the
    totals (75.50 - 42.00) gives 33.50, which is not a quantity anyone can buy.

    Deliberately built with one product in SURPLUS and two short, because with everything
    short the two formulas agree and the test would prove nothing. This error was caught
    reviewing the design, not the code — it looks entirely plausible in a totals row.
    """
    sheet = {"parents": [
        {"product": "Usna Chawal", "brand": "MF", "kg": 35.0,
         "units": 7, "orders": 7, "packed": 0, "sizes": []},
        {"product": "ABC Sattu", "brand": "MF", "kg": 22.5,
         "units": 37, "orders": 37, "packed": 0, "sizes": []},
        {"product": "Bengali Gobindobhog Rice", "brand": "HF", "kg": 18.0,
         "units": 17, "orders": 17, "packed": 0, "sizes": []},
    ]}
    summary = logic.raw_stock_summary(
        sheet, {"Usna Chawal": 10.0, "ABC Sattu": 32.0, "Bengali Gobindobhog Rice": 0.0}
    )

    assert summary["totals"]["to_buy_kg"] == pytest.approx(43.0), (
        "the to-buy total must sum the clamped rows; subtracting the totals lets a surplus "
        "of one product cancel a shortfall of another"
    )
    assert summary["totals"]["ordered_kg"] == pytest.approx(75.5)
    assert summary["totals"]["raw_kg"] == pytest.approx(42.0)
    assert summary["totals"]["short_products"] == 2


def test_a_covered_product_is_flagged_covered_and_reports_zero_to_buy():
    """The screen prints an em dash for these, so it needs the flag rather than guessing."""
    sheet = {"parents": [{"product": "ABC Sattu", "brand": "MF", "kg": 22.5,
                          "units": 37, "orders": 37, "packed": 0, "sizes": []}]}
    row = logic.raw_stock_summary(sheet, {"ABC Sattu": 32.0})["rows"][0]
    assert row["covered"] is True
    assert row["to_buy_kg"] == 0.0


def test_a_product_with_no_raw_stock_entry_needs_all_of_it():
    """Absent is 0, not unknown.

    A product nobody has typed a stock figure for must appear on the purchasing list at its
    full ordered weight — treating it as "unknown, skip" would silently drop it from the buy
    list, which is how a stockout reaches a Buy Box.
    """
    sheet = {"parents": [{"product": "Katarni Chuda", "brand": "MF", "kg": 13.0,
                          "units": 11, "orders": 8, "packed": 0, "sizes": []}]}
    row = logic.raw_stock_summary(sheet, {})["rows"][0]
    assert row["raw_kg"] == 0.0
    assert row["to_buy_kg"] == pytest.approx(13.0)
    assert row["covered"] is False


def test_purchasing_rows_stay_in_the_sheets_heaviest_first_order():
    """Tabs 1, 2 and 3 must read together, so none of them re-sorts.

    `dispatch_sheet` already ordered parents heaviest first; re-sorting here would make the
    purchasing tab disagree with the SKU tab about which product leads.
    """
    sheet = {"parents": [
        {"product": "Heavy", "brand": "MF", "kg": 35.0,
         "units": 7, "orders": 7, "packed": 0, "sizes": []},
        {"product": "Light", "brand": "MF", "kg": 2.0,
         "units": 4, "orders": 4, "packed": 0, "sizes": []},
    ]}
    rows = logic.raw_stock_summary(sheet, {})["rows"]
    assert [row["product"] for row in rows] == ["Heavy", "Light"]


def test_a_product_with_no_pack_size_contributes_no_kilograms_to_purchasing():
    """An unweighed line must not become 0 kg of demand.

    `dispatch_sheet` already excludes it from `kg`; this asserts the purchasing view inherits
    that rather than inventing a number. Treating unknown as 0 makes a 47 kg sheet report 40,
    and that figure reaches a courier — and here, a supplier.
    """
    orders = [_order("403-1", [_item("B0NOWEIGHT", 5)])]
    sheet = logic.dispatch_sheet(orders, CATALOGUE, TODAY)
    summary = logic.raw_stock_summary(sheet, {})
    row = next(r for r in summary["rows"] if r["product"] == "Mystery Mix")
    assert row["ordered_kg"] == 0.0
    assert row["to_buy_kg"] == 0.0, "an unweighable product produced a purchase quantity"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_orders_dispatch.py -q -p no:randomly -k "to_buy or purchasing or covered or raw_stock_entry or pack_size_contributes"`

Expected: FAIL with `AttributeError: module 'app.orders.logic' has no attribute 'to_buy_kg'`

- [ ] **Step 3: Implement both functions**

Append to `app/orders/logic.py`:

```python
# ─── Purchasing: today's ordered weight against raw material on hand ─────────


def to_buy_kg(ordered_kg, raw_kg) -> float:
    """The shortfall for one product, in kilograms. Never negative.

    Clamped at 0 for the same reason `remaining_for` clamps: this number reaches a purchasing
    list, and "-7 kg" is not a quantity anyone can order. A surplus is simply nothing to buy.
    """
    shortfall = float(ordered_kg or 0) - float(raw_kg or 0)
    return round(shortfall, 2) if shortfall > 0 else 0.0


def raw_stock_summary(sheet: Mapping, raw_stock: Mapping) -> dict:
    """Today's ordered weight per parent against raw stock on hand, and what to buy.

    Takes the sheet `dispatch_sheet` already produced rather than re-reading orders, so the
    purchasing tab cannot disagree with the SKU tab about how much of a product is due. Row
    order is inherited untouched — parents are already heaviest first, and re-sorting here
    would make the two tabs lead with different products.

    Returns::

        {"rows": [{"product", "brand", "ordered_kg", "raw_kg", "to_buy_kg", "covered"}],
         "totals": {"ordered_kg", "raw_kg", "to_buy_kg", "short_products"}}

    **The to-buy TOTAL sums the clamped rows; it is NOT `total_ordered - total_raw`.** Those
    two differ the moment any product is in surplus, and the subtraction is wrong because it
    lets a surplus of ABC Sattu cancel a shortfall of Usna Chawal — you cannot make rice out
    of sattu. On real numbers the difference was 43.00 kg against 33.50 kg, and only the first
    is a purchasing quantity. Caught reviewing the design rather than the code: the wrong
    version looks entirely plausible in a totals row.

    A product with no entry in `raw_stock` counts as 0 on hand, not "unknown". Absent has to
    mean "buy all of it", because skipping it would drop the product off the purchasing list
    and a stockout is what costs the Buy Box.
    """
    lookup = {
        str(product or "").strip(): float(value or 0)
        for product, value in (raw_stock or {}).items()
    }

    rows = []
    for parent in sheet.get("parents") or []:
        product = parent["product"]
        ordered = round(float(parent.get("kg") or 0), 2)
        raw = round(lookup.get(product, 0.0), 2)
        shortfall = to_buy_kg(ordered, raw)
        rows.append({
            "product": product,
            "brand": parent.get("brand") or "",
            "ordered_kg": ordered,
            "raw_kg": raw,
            "to_buy_kg": shortfall,
            "covered": shortfall == 0.0,
        })

    return {
        "rows": rows,
        "totals": {
            "ordered_kg": round(sum(row["ordered_kg"] for row in rows), 2),
            "raw_kg": round(sum(row["raw_kg"] for row in rows), 2),
            # Sum of the CLAMPED rows. See the docstring.
            "to_buy_kg": round(sum(row["to_buy_kg"] for row in rows), 2),
            "short_products": sum(1 for row in rows if not row["covered"]),
        },
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_orders_dispatch.py -q -p no:randomly`

Expected: PASS (all tests in the file)

- [ ] **Step 5: Mutation check — the to-buy total**

Temporarily change the total in `app/orders/logic.py` to the wrong-but-plausible version:

```python
            "to_buy_kg": round(
                sum(row["ordered_kg"] for row in rows)
                - sum(row["raw_kg"] for row in rows), 2),
```

Run: `venv/Scripts/python -m pytest tests/test_orders_dispatch.py -q -p no:randomly -k to_buy_total`

Expected: **FAIL** on `test_the_to_buy_total_sums_the_rows_and_does_not_subtract_the_totals`. If it passes, the test is worthless — fix the test before continuing. Then restore the correct version and re-run to confirm PASS.

- [ ] **Step 6: Mutation check — unclamp `to_buy_kg`**

Temporarily remove the clamp in `app/orders/logic.py`:

```python
def to_buy_kg(ordered_kg, raw_kg) -> float:
    return round(float(ordered_kg or 0) - float(raw_kg or 0), 2)
```

Run: `venv/Scripts/python -m pytest tests/test_orders_dispatch.py -q -p no:randomly -k "to_buy or covered"`

Expected: **FAIL** on `test_to_buy_is_the_shortfall_and_never_negative` (a surplus returns
`-9.5`) **and** on `test_a_covered_product_is_flagged_covered_and_reports_zero_to_buy` (a
negative is not `0.0`, so `covered` reads False). Restore the clamp and re-run to confirm PASS.

Two tests failing here rather than one is the point: the clamp protects both the number and the
flag the screen uses to print an em dash.

- [ ] **Step 7: Mutation check — a zero raw-stock entry gets deleted**

`save_raw_stock` stores a deliberate 0 where `save_packed` deletes one. Temporarily make it
behave like the packed version, in `app/orders/repository.py`:

```python
    for product, raw_kg in by_product.items():
        row = existing.get(product)
        if raw_kg == 0:
            if row is not None:
                await db.delete(row)
            continue
        if row is None:
            db.add(ProductRawStock(
                product=product, raw_kg=raw_kg, updated_at=now,
                updated_by=updated_by or None,
            ))
        else:
            row.raw_kg = raw_kg
            row.updated_at = now
            row.updated_by = updated_by or None
```

Run: `venv/Scripts/python -m pytest tests/test_orders_dispatch.py -q -p no:randomly -k zero_raw_stock`

Expected: **FAIL** on `test_zero_raw_stock_is_stored_not_deleted`. Restore and confirm PASS.

- [ ] **Step 8: Commit**

```bash
git add app/orders/logic.py tests/test_orders_dispatch.py
git commit -m "feat: compute what to buy from ordered weight and raw stock

The to-buy total sums the clamped per-product rows rather than subtracting the
totals. The two differ whenever anything is in surplus, and the subtraction lets a
surplus of one product cancel a shortfall of another — you cannot make rice out of
sattu. Tested with one product in surplus and two short, because with everything
short both formulas agree and the test would prove nothing."
```

---

### Task 4: `ORDER_REFRESH_ENABLED`

**Files:**
- Modify: `app/config.py` (after `scheduler_enabled`, line ~52)
- Modify: `app/scheduler.py` (`setup_scheduler`)
- Test: `tests/test_retention_and_scheduler.py` (append)

**Interfaces:**
- Consumes: `settings.scheduler_enabled: bool` (existing, defaults `True`)
- Produces: `settings.order_refresh_enabled: bool` (defaults `False`); `setup_scheduler()` registers `order_refresh` when **either** flag is true, and the other three jobs only when `scheduler_enabled`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_retention_and_scheduler.py`:

```python
# ─── The orders job has its own flag ─────────────────────────────────────────
#
# Production runs SCHEDULER_ENABLED=false, so nothing scheduled runs there. Turning it on to
# get the orders refresh would also wake the 06:00 product scrape (10 async workers), the
# 07:30 keyword track and the 09:15 purge — on a 951 MB box with no swap that has already
# OOM-killed a pip install. Waking three dormant jobs as a side effect of a UI change is not a
# decision the Orders feature gets to make.


def _jobs_with_flags(monkeypatch, *, scheduler_enabled: bool, order_refresh_enabled: bool):
    """Register jobs with both flags set explicitly. Returns {job_id: trigger}.

    A separate helper from `_registered_jobs`, which hardcodes `scheduler_enabled=True` and
    therefore cannot express the case that matters here.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    import app.scheduler as sch

    throwaway = AsyncIOScheduler()
    monkeypatch.setattr(sch, "scheduler", throwaway)
    monkeypatch.setattr(sch.settings, "scheduler_enabled", scheduler_enabled)
    monkeypatch.setattr(sch.settings, "order_refresh_enabled", order_refresh_enabled)
    monkeypatch.setattr(sch.settings, "daily_scrape_hour", 6)
    monkeypatch.setattr(sch.settings, "daily_scrape_minute", 0)
    monkeypatch.setattr(throwaway, "start", lambda *a, **k: None)

    sch.setup_scheduler()
    return {job.id: str(job.trigger) for job in throwaway.get_jobs()}


def test_the_orders_job_runs_on_its_own_flag_without_waking_the_others(monkeypatch):
    """Production's exact configuration: master off, orders on.

    Asserted on the registered job ids rather than on the flag, because the bug this guards is
    a refactor moving `if not settings.scheduler_enabled: return` back to the top of
    setup_scheduler — which reads as tidy and silently stops the orders refresh.
    """
    jobs = _jobs_with_flags(
        monkeypatch, scheduler_enabled=False, order_refresh_enabled=True
    )
    assert "order_refresh" in jobs, (
        "the orders refresh does not run with only its own flag set, so production would "
        "have to enable every dormant job to get it"
    )
    for dormant in ("daily_product_scrape", "daily_keyword_track", "daily_history_purge"):
        assert dormant not in jobs, (
            f"{dormant} woke up as a side effect of enabling the orders refresh"
        )


def test_the_master_flag_alone_still_registers_the_orders_job(monkeypatch):
    """The flags are OR'd, so no existing installation loses its refresh.

    `scheduler_enabled` defaults to True, so a fresh install must keep working without anyone
    learning about a second flag.
    """
    jobs = _jobs_with_flags(
        monkeypatch, scheduler_enabled=True, order_refresh_enabled=False
    )
    assert "order_refresh" in jobs
    assert "daily_product_scrape" in jobs, "the master flag stopped registering its own jobs"


def test_both_flags_off_registers_nothing(monkeypatch):
    """A deployment that wants no background work must get none."""
    jobs = _jobs_with_flags(
        monkeypatch, scheduler_enabled=False, order_refresh_enabled=False
    )
    assert jobs == {}, f"jobs registered with every flag off: {sorted(jobs)}"


def test_the_order_refresh_flag_defaults_to_off(monkeypatch):
    """Opt-in, so deploying this code changes no running system's behaviour by itself.

    Note the deliberate asymmetry: `scheduler_enabled` defaults to True, so a fresh install
    already gets the orders job through the OR. This flag is only load-bearing where the
    master flag has been explicitly turned off — which is exactly production.
    """
    from app.config import Settings

    assert Settings().order_refresh_enabled is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_retention_and_scheduler.py -q -p no:randomly -k "flag or orders_job"`

Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'order_refresh_enabled'`

- [ ] **Step 3: Add the setting**

In `app/config.py`, replace the line `scheduler_enabled: bool = True` with:

```python
    scheduler_enabled: bool = True
    #: Runs the half-hourly Amazon orders refresh WITHOUT the other scheduled jobs.
    #:
    #: Production sets `SCHEDULER_ENABLED=false`, so nothing scheduled runs there. Turning
    #: that back on to get the orders refresh would also wake the 06:00 product scrape (10
    #: async workers), the 07:30 keyword track and the 09:15 purge — on a 951 MB box with no
    #: swap that has already OOM-killed a pip install.
    #:
    #: OR'd with `scheduler_enabled` rather than replacing it, so an installation that already
    #: enables everything keeps its refresh without learning a second flag. Defaults False:
    #: `scheduler_enabled` already defaults True, so this only matters where that was
    #: explicitly turned off.
    order_refresh_enabled: bool = False
```

- [ ] **Step 4: Move the early return in `setup_scheduler`**

In `app/scheduler.py`, replace the opening of `setup_scheduler` and the orders-job registration. The function currently begins:

```python
def setup_scheduler():
    if not settings.scheduler_enabled:
        return
```

Replace that guard, and wrap the three daily jobs, so the body reads:

```python
def setup_scheduler():
    """Register the background jobs. Nothing runs unless a flag asks for it.

    **The guard is per-job rather than at the top, and that is deliberate.** This function
    used to return early on `not settings.scheduler_enabled`, which meant the only way to get
    the half-hourly orders refresh was to also wake the product scrape, the keyword track and
    the retention purge. Production runs with the master flag off precisely to keep those
    asleep on a 951 MB box, so the orders refresh needed its own switch.
    """
    if not (settings.scheduler_enabled or settings.order_refresh_enabled):
        return

    if settings.scheduler_enabled:
        scheduler.add_job(
            scheduled_product_scrape,
            CronTrigger(hour=settings.daily_scrape_hour, minute=settings.daily_scrape_minute),
            id="daily_product_scrape",
            replace_existing=True,
        )

        # Wrap with % 24 — a daily_scrape_hour of 23 would otherwise build an
        # invalid CronTrigger(hour=24) and crash scheduler setup at startup.
        keyword_hour = (settings.daily_scrape_hour + 1) % 24
        scheduler.add_job(
            scheduled_keyword_track,
            CronTrigger(hour=keyword_hour, minute=30),
            id="daily_keyword_track",
            replace_existing=True,
        )

        # Purge after both scrapes so a run is never competing with deletes.
        purge_hour = (settings.daily_scrape_hour + 3) % 24
        scheduler.add_job(
            scheduled_purge_old_history,
            CronTrigger(hour=purge_hour, minute=15),
            id="daily_history_purge",
            replace_existing=True,
        )

    # Every 30 minutes, and jittered by starting 4 minutes in rather than on the hour, so
    # an order refresh never begins in the same second as the 06:00 product scrape on a
    # 951 MB box.
    #
    # Registered on EITHER flag: `order_refresh_enabled` lets production run this alone,
    # while `scheduler_enabled` keeps it working for any installation that never learns
    # about the second flag.
    scheduler.add_job(
        scheduled_order_refresh,
        IntervalTrigger(minutes=ORDER_REFRESH_MINUTES, start_date=None),
        id="order_refresh",
        replace_existing=True,
        # A slow run must not stack up behind itself. refresh.run refuses a concurrent
        # start anyway, but coalescing keeps APScheduler from queueing missed runs.
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info(
        f"Scheduler started: "
        f"{'products at %02d:%02d, ' % (settings.daily_scrape_hour, settings.daily_scrape_minute) if settings.scheduler_enabled else ''}"
        f"orders every {ORDER_REFRESH_MINUTES}m"
    )
```

Note: `keyword_hour` and `purge_hour` are now local to the `if` block, so the old log line that referenced them must be replaced exactly as shown — leaving the original f-string would raise `NameError` when only the orders flag is set.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_retention_and_scheduler.py -q -p no:randomly`

Expected: PASS (all, including the four new tests and the pre-existing job-registration tests)

- [ ] **Step 6: Mutation check — move the guard back to the top**

Temporarily restore the original early return as the first statement of `setup_scheduler`:

```python
    if not settings.scheduler_enabled:
        return
```

Run: `venv/Scripts/python -m pytest tests/test_retention_and_scheduler.py -q -p no:randomly -k orders_job`

Expected: **FAIL** on `test_the_orders_job_runs_on_its_own_flag_without_waking_the_others`. Remove the mutation and re-run to confirm PASS.

- [ ] **Step 7: Run the full suite**

Run: `venv/Scripts/python -m pytest -q`

Expected: `1296 passed, 3 skipped`

- [ ] **Step 8: Commit**

```bash
git add app/config.py app/scheduler.py tests/test_retention_and_scheduler.py
git commit -m "feat: run the orders refresh without waking the other scheduled jobs

setup_scheduler returned early on `not scheduler_enabled`, so the only way to get
the half-hourly orders refresh was to also start the 06:00 product scrape (10 async
workers), the 07:30 keyword track and the 09:15 purge. Production keeps the master
flag off precisely to keep those asleep on a 951 MB box with no swap.

ORDER_REFRESH_ENABLED gates the orders job alone, OR'd with the master flag so no
existing installation loses its refresh. The guard moves down to the three jobs it
protects; a mutation restoring it to the top fails a named test."
```

---

### Task 5: `_dispatch` carries raw stock, and the raw-stock route

**Files:**
- Modify: `app/routers/orders.py` (`_dispatch` ~line 234; `dispatch` route; new route)
- Test: `tests/test_orders_api.py` (append)

**Interfaces:**
- Consumes: `repository.load_raw_stock(db)`, `repository.save_raw_stock(db, entries, updated_by)`, `logic.raw_stock_summary(sheet, raw_stock)` from Tasks 2–3
- Produces:
  - `_dispatch(db)` returns a **THREE-tuple** `(sheet, purchasing, meta)` — every existing caller must be updated
  - `GET /orders/dispatch` payload gains `purchasing`
  - `POST /orders/raw-stock` taking `{"entries":[{"product","raw_kg"}]}`, returning `{"status":"saved","raw_stock":{...}}`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orders_api.py`:

```python
# ─── Purchasing: raw stock and what to buy ───────────────────────────────────


async def test_the_dispatch_payload_carries_the_purchasing_view(auth_client, db):
    """Tab 1 reads the same payload as tabs 2 and 3, so nothing can disagree."""
    await repository.upsert_orders(db, [_dispatched("403-1")])
    await repository.replace_items(db, "403-1", [
        {"asin": KNOWN_ASIN, "seller_sku": "cs-500", "quantity_ordered": 10}
    ])

    body = (await auth_client.get("/orders/dispatch")).json()

    assert "purchasing" in body, "the purchasing view is missing from the payload"
    row = next(r for r in body["purchasing"]["rows"] if r["product"] == "Chana Sattu")
    assert row["ordered_kg"] == pytest.approx(5.0)        # 10 x 0.5 kg
    assert row["raw_kg"] == 0.0
    assert row["to_buy_kg"] == pytest.approx(5.0)
    assert row["covered"] is False


async def test_raw_stock_saves_and_reaches_the_purchasing_view(auth_client, db):
    """Type a stock figure, reload, and `to_buy` reflects it."""
    await repository.upsert_orders(db, [_dispatched("403-1")])
    await repository.replace_items(db, "403-1", [
        {"asin": KNOWN_ASIN, "quantity_ordered": 10}
    ])

    r = await auth_client.post(
        "/orders/raw-stock", json={"entries": [{"product": "Chana Sattu", "raw_kg": 3.5}]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["raw_stock"]["Chana Sattu"] == pytest.approx(3.5)

    body = (await auth_client.get("/orders/dispatch")).json()
    row = next(r for r in body["purchasing"]["rows"] if r["product"] == "Chana Sattu")
    assert row["raw_kg"] == pytest.approx(3.5)
    assert row["to_buy_kg"] == pytest.approx(1.5)         # 5.0 ordered - 3.5 on hand


async def test_the_raw_stock_response_is_json_safe(auth_client, db):
    """`Numeric` returns `Decimal`, which JSONResponse cannot serialise.

    Asserted over HTTP rather than on the repository, because that is where the failure
    surfaced last time: a 500 and a "Could not reach the server" banner, found in a browser.
    """
    import json

    await _seed(db)
    r = await auth_client.post(
        "/orders/raw-stock", json={"entries": [{"product": "Chana Sattu", "raw_kg": 12.25}]}
    )
    assert r.status_code == 200, r.text
    json.dumps(r.json())


async def test_a_malformed_raw_stock_body_is_refused(auth_client, db):
    """`entries` must be a list, or the save silently stores nothing."""
    await _seed(db)
    r = await auth_client.post("/orders/raw-stock", json={"entries": {"product": "x"}})
    assert r.status_code == 400, r.text


async def test_the_raw_stock_route_takes_no_date(auth_client, db):
    """Unlike `/packed/{pack_date}`, and deliberately.

    A packed count belongs to a day, so that route refuses any date but today — a laptop in
    another timezone would otherwise file this morning's count against yesterday. Raw stock is
    standing, so a date here would make the number unreachable tomorrow.
    """
    await _seed(db)
    r = await auth_client.post(
        "/orders/raw-stock", json={"entries": [{"product": "Chana Sattu", "raw_kg": 8}]}
    )
    assert r.status_code == 200, r.text
    body = (await auth_client.get("/orders/dispatch")).json()
    row = next(r for r in body["purchasing"]["rows"] if r["product"] == "Chana Sattu")
    assert row["raw_kg"] == pytest.approx(8.0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_orders_api.py -q -p no:randomly -k "purchasing or raw_stock"`

Expected: FAIL — `KeyError: 'purchasing'` on the first, 404 on the rest.

- [ ] **Step 3: Update `_dispatch`**

In `app/routers/orders.py`, replace the whole `_dispatch` function with:

```python
async def _dispatch(db: AsyncSession):
    """Today's dispatch, the purchasing view, and the meta. Returns (sheet, purchasing, meta).

    ONE function behind all three tabs and all five downloads, so a printed sheet cannot
    disagree with the monitor about a quantity — the same reasoning `_sheet_and_orders`
    carries, and the reason the shipment feature funnels its five downloads through
    `_document_rows`.

    Today is taken in IST at call time and never stored, so a screen left open overnight is
    correct in the morning without a refresh having run.
    """
    today = datetime.now(logic.IST).date()
    pack_date = today.isoformat()
    orders = await repository.load_orders(db, days=WINDOW_DAYS)
    sheet_catalogue, warning, source = await catalogue.load_catalogue()
    packed = await repository.load_packed(db, pack_date)
    raw_stock = await repository.load_raw_stock(db)
    sheet = logic.dispatch_sheet(orders, sheet_catalogue, today, packed=packed)
    purchasing = logic.raw_stock_summary(sheet, raw_stock)
    return sheet, purchasing, {
        "source": source, "warning": warning, "today": pack_date, "pack_date": pack_date,
    }
```

- [ ] **Step 4: Update the `dispatch` route to send `purchasing`**

Replace the body of the `dispatch` route (keep the decorator and signature):

```python
    sheet, purchasing, meta = await _dispatch(db)
    last = await repository.last_refreshed_at(db)
    return JSONResponse({
        "sheet": sheet,
        "purchasing": purchasing,
        "pack_date": meta["pack_date"],
        "today_ist": meta["today"],
        "catalogue_source": meta["source"],
        "catalogue_warning": meta["warning"],
        "last_refreshed_at": last.isoformat() if last else None,
        "refresh": refresh.status(),
        "is_admin": bool(getattr(grant, "is_admin", False)),
    })
```

- [ ] **Step 5: Add the raw-stock route**

Insert immediately after the `save_packed` route:

```python
@router.post("/raw-stock")
async def save_raw_stock(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ORDERS)),
):
    """Record raw material on hand per product. `{"entries": [{"product", "raw_kg"}]}`.

    **No date in the path, unlike `/packed/{pack_date}`, and that asymmetry is the design.**
    A packed count belongs to a day; raw material on a shelf is a standing quantity, so a date
    here would make the number unreachable tomorrow and the purchasing tab would demand
    re-entry every morning.

    Kilograms rather than units: raw material is bulk, and there is no such thing as
    500 g-flavoured raw sattu.
    """
    try:
        body = await request.json()
    except Exception:                       # noqa: BLE001 - a malformed body is a 400
        return JSONResponse({"error": "Expected a JSON body."}, status_code=400)

    entries = (body or {}).get("entries")
    if not isinstance(entries, list):
        return JSONResponse(
            {"error": "entries must be a list of {product, raw_kg} objects."},
            status_code=400,
        )

    raw_stock = await repository.save_raw_stock(
        db, entries, updated_by=getattr(grant, "username", "") or ""
    )
    logger.info("orders: raw stock saved for %d product(s)", len(raw_stock))
    return JSONResponse({"status": "saved", "raw_stock": raw_stock})
```

- [ ] **Step 6: Fix the other `_dispatch` caller**

`download_dispatch` unpacks two values and will now raise `ValueError: too many values to
unpack`. Change its first statement to:

```python
    sheet, purchasing, meta = await _dispatch(db)
```

Verify no caller was missed: `grep -n "await _dispatch(" app/routers/orders.py` — every hit
must unpack three names.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_orders_api.py -q -p no:randomly`

Expected: PASS

- [ ] **Step 8: Run the full suite and commit**

Run: `venv/Scripts/python -m pytest -q`
Expected: `1301 passed, 3 skipped`

```bash
git add app/routers/orders.py tests/test_orders_api.py
git commit -m "feat: serve the purchasing view and accept raw stock

_dispatch returns (sheet, purchasing, meta) so all three tabs and all five
downloads come from one aggregation — the reason the shipment feature funnels its
downloads through _document_rows.

POST /orders/raw-stock takes no date, unlike /packed/{pack_date}: a packed count
belongs to a day, raw material on a shelf is standing, and a date would make the
number unreachable tomorrow."
```

---

### Task 6: Excel downloads — combined workbook and to-buy list

**Files:**
- Modify: `app/shipment/documents.py` (append)
- Test: `tests/test_shipment_documents.py` (append)

**Interfaces:**
- Consumes: `_write_sheet(worksheet, headers, rows, widths)` (existing, `app/shipment/documents.py:69`)
- Produces:
  - `build_dispatch_xlsx(sheet, purchasing, subtitle, tab="all") -> io.BytesIO`
  - `build_tobuy_xlsx(purchasing, subtitle) -> io.BytesIO`
  - `_purchase_rows(purchasing) -> tuple[list[str], list[list]]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_shipment_documents.py`:

```python
# ─── The dispatch workbook and the to-buy list ───────────────────────────────


def _dispatch_fixture():
    """A dispatch sheet plus its purchasing view, with one product in SURPLUS.

    The surplus is what distinguishes a correct to-buy total from one that subtracts the
    totals, and it is what must be ABSENT from the to-buy list. Built by hand rather than from
    a live sheet so the expected numbers are visible in the test.
    """
    from app.orders import logic

    sheet = {
        "parents": [
            {"product": "Usna Chawal", "brand": "MF", "kg": 35.0, "units": 7,
             "orders": 7, "packed": 0, "remaining": 7,
             "sizes": [{"asin": "B0RICE5KG", "weight": 5.0, "weight_label": "5 kg",
                        "seller_sku": "5kg uc", "units": 7, "orders": 7, "kg": 35.0,
                        "packed": 0, "remaining": 7, "over_packed": 0, "known": True}]},
            {"product": "ABC Sattu", "brand": "MF", "kg": 22.5, "units": 37,
             "orders": 37, "packed": 29, "remaining": 8,
             "sizes": [{"asin": "B0ABC500", "weight": 0.5, "weight_label": "500g",
                        "seller_sku": "abc500", "units": 29, "orders": 29, "kg": 14.5,
                        "packed": 29, "remaining": 0, "over_packed": 0, "known": True},
                       {"asin": "B0ABC1KG", "weight": 1.0, "weight_label": "1 kg",
                        "seller_sku": "abc1kg", "units": 8, "orders": 8, "kg": 8.0,
                        "packed": 0, "remaining": 8, "over_packed": 0, "known": True}]},
        ],
        "orders": [
            {"amazon_order_id": "403-1", "parent": "Usna Chawal", "weight": 5.0,
             "weight_label": "5 kg", "seller_sku": "5kg uc", "asin": "B0RICE5KG",
             "quantity": 1, "known": True, "city": "PUNE", "state": "MAHARASHTRA",
             "easyship_status": "PickedUp"},
        ],
        "totals": {"orders": 8, "units": 44, "kg": 57.5, "packed": 29, "remaining": 15,
                   "over_packed": 0, "sizes_without_weight": 0, "parents": 2},
        "unknown_asins": [],
    }
    purchasing = logic.raw_stock_summary(sheet, {"Usna Chawal": 10.0, "ABC Sattu": 32.0})
    return sheet, purchasing


def test_the_dispatch_workbook_has_one_worksheet_per_tab():
    """Three tabs on screen, three worksheets in the file, named the same.

    One file rather than three downloads: they are read together, and three files get
    separated on a bench — the same reason the PDF footer prints "page 1 of 3".
    """
    from openpyxl import load_workbook

    from app.shipment import documents

    sheet, purchasing = _dispatch_fixture()
    book = load_workbook(documents.build_dispatch_xlsx(sheet, purchasing, "26 Aug (IST)"))
    assert book.sheetnames == ["Weight & purchase", "By SKU", "Orders"]


def test_the_workbooks_purchasing_total_sums_the_clamped_rows():
    """25.00 + 0, not 57.50 - 42.00 = 15.50.

    A supplier reads this file, and a surplus of sattu cannot cover a shortfall of rice.
    """
    from openpyxl import load_workbook

    from app.shipment import documents

    sheet, purchasing = _dispatch_fixture()
    book = load_workbook(documents.build_dispatch_xlsx(sheet, purchasing, "26 Aug (IST)"))
    values = [[cell.value for cell in row] for row in book["Weight & purchase"].iter_rows()]
    total_row = next(row for row in values if row and row[0] == "TOTAL")
    assert total_row[-1] == pytest.approx(25.0), (
        f"to-buy total is {total_row[-1]}; it must sum the clamped rows"
    )


def test_a_single_tab_workbook_holds_only_that_worksheet():
    """The per-tab download is the combined one with the others removed.

    One builder rather than two that could disagree about a quantity.
    """
    from openpyxl import load_workbook

    from app.shipment import documents

    sheet, purchasing = _dispatch_fixture()
    book = load_workbook(
        documents.build_dispatch_xlsx(sheet, purchasing, "26 Aug (IST)", tab="sku")
    )
    assert book.sheetnames == ["By SKU"]


def test_the_to_buy_list_omits_covered_products():
    """A purchasing list is a list of things to BUY.

    ABC Sattu has 32 kg against 22.5 kg ordered, so it must not appear — a zero row invites
    someone to order zero of it.
    """
    from openpyxl import load_workbook

    from app.shipment import documents

    _sheet, purchasing = _dispatch_fixture()
    book = load_workbook(documents.build_tobuy_xlsx(purchasing, "26 Aug (IST)"))
    text = "\n".join(
        " ".join(str(cell.value) for cell in row if cell.value is not None)
        for row in book.active.iter_rows()
    )
    assert "Usna Chawal" in text, "a short product is missing from the to-buy list"
    assert "ABC Sattu" not in text, "a covered product appeared on the purchasing list"


def test_an_empty_to_buy_list_says_so_rather_than_printing_an_empty_table():
    """Nothing to buy is good news, not a broken download."""
    from openpyxl import load_workbook

    from app.orders import logic
    from app.shipment import documents

    sheet = {"parents": [{"product": "ABC Sattu", "brand": "MF", "kg": 10.0, "units": 20,
                          "orders": 20, "packed": 0, "sizes": []}]}
    purchasing = logic.raw_stock_summary(sheet, {"ABC Sattu": 50.0})
    book = load_workbook(documents.build_tobuy_xlsx(purchasing, "26 Aug (IST)"))
    text = "\n".join(
        " ".join(str(cell.value) for cell in row if cell.value is not None)
        for row in book.active.iter_rows()
    )
    assert "Nothing to buy" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_shipment_documents.py -q -p no:randomly -k "workbook or to_buy"`

Expected: FAIL with `AttributeError: module 'app.shipment.documents' has no attribute 'build_dispatch_xlsx'`

- [ ] **Step 3: Implement both builders**

Append to `app/shipment/documents.py`:

```python
#: Column widths for the three dispatch worksheets, in Excel character units.
_XLSX_PURCHASE_WIDTHS = [34, 8, 14, 14, 14]
_XLSX_SKU_WIDTHS = [30, 10, 22, 12, 14, 10]
_XLSX_ORDER_WIDTHS = [24, 22, 8, 30, 12, 28, 16]

#: Which worksheet each `tab=` value keeps. `all` keeps every one.
_XLSX_TAB_SHEETS = {
    "weight": "Weight & purchase",
    "sku": "By SKU",
    "orders": "Orders",
}


def _purchase_rows(purchasing: dict) -> tuple[list[str], list[list]]:
    """The purchasing table as headers + rows, shared by the workbook and the to-buy list.

    One function so the two files cannot disagree about a weight — the same reason all five
    downloads come from one aggregation.
    """
    headers = ["Product", "Brand", "Ordered kg", "Raw stock kg", "To buy kg"]
    rows = [
        [
            row["product"],
            row["brand"],
            float(row["ordered_kg"]),
            float(row["raw_kg"]),
            # A covered product shows blank rather than 0.00: a zero reads as a
            # measurement, and this column is a purchasing instruction.
            "" if row["covered"] else float(row["to_buy_kg"]),
        ]
        for row in purchasing.get("rows") or []
    ]
    return headers, rows


def build_dispatch_xlsx(
    sheet: dict, purchasing: dict, subtitle: str, tab: str = "all"
) -> io.BytesIO:
    """Today's dispatch as one workbook with a worksheet per screen tab.

    **One file, not three.** The tabs are read together — purchasing says what to bring to the
    bench, the SKU sheet is what gets counted, the order sheet is what gets checked off. Three
    separate files get separated on a warehouse bench, the same reason `_page_footer` prints
    "page 1 of 3".

    `tab` drops the worksheets that were not asked for, so a single-tab download is this same
    builder rather than a second code path that could disagree about a quantity.

    Worksheet names match the tab labels exactly, so nobody has to work out which is which.
    """
    from openpyxl import Workbook

    book = Workbook()

    # ── Sheet 1: weight and purchasing ──
    purchase = book.active
    purchase.title = "Weight & purchase"
    headers, rows = _purchase_rows(purchasing)
    ptotals = purchasing.get("totals") or {}
    rows = rows + [[
        "TOTAL", "",
        float(ptotals.get("ordered_kg") or 0),
        float(ptotals.get("raw_kg") or 0),
        # Sum of the CLAMPED rows, never total_ordered - total_raw: a surplus of one
        # product cannot cover a shortfall of another.
        float(ptotals.get("to_buy_kg") or 0),
    ]]
    _write_sheet(purchase, headers, rows, _XLSX_PURCHASE_WIDTHS)

    # ── Sheet 2: per SKU, flat, in the sheet's existing order ──
    by_sku = book.create_sheet("By SKU")
    sku_rows = [
        [
            parent["product"],
            size.get("weight_label") or "",
            size.get("seller_sku") or size.get("asin") or "",
            int(size["units"]),
            int(size.get("packed") or 0),
            int(size.get("remaining") or 0),
        ]
        for parent in sheet.get("parents") or []
        for size in parent.get("sizes") or []
    ]
    stotals = sheet.get("totals") or {}
    sku_rows.append([
        "TOTAL", "", "",
        int(stotals.get("units") or 0),
        int(stotals.get("packed") or 0),
        int(stotals.get("remaining") or 0),
    ])
    _write_sheet(
        by_sku, ["Product", "Size", "SKU", "Ordered", "Packed today", "Left"],
        sku_rows, _XLSX_SKU_WIDTHS,
    )

    # ── Sheet 3: every order line ──
    orders_sheet = book.create_sheet("Orders")
    order_rows = [
        [
            row["amazon_order_id"],
            row.get("seller_sku") or row.get("asin") or "",
            int(row["quantity"]),
            row["parent"],
            row.get("weight_label") or "",
            ", ".join(part for part in (row.get("city"), row.get("state")) if part),
            row.get("easyship_status") or "",
        ]
        for row in sheet.get("orders") or []
    ]
    _write_sheet(
        orders_sheet,
        ["Order", "SKU", "Qty", "Item", "Weight", "Destination", "Amazon status"],
        order_rows or [["No orders", "", 0, "", "", "", ""]],
        _XLSX_ORDER_WIDTHS,
    )

    wanted = _XLSX_TAB_SHEETS.get(tab)
    if wanted is not None:
        for name in list(book.sheetnames):
            if name != wanted:
                del book[name]

    # Provenance goes in the workbook's metadata rather than a spare row, so it cannot be
    # sorted away from the data it describes.
    book.properties.title = "Dispatch sheet"
    book.properties.description = subtitle

    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    return buffer


def build_tobuy_xlsx(purchasing: dict, subtitle: str) -> io.BytesIO:
    """The purchasing shortfall only — the products where `to_buy > 0`.

    **Filtered, not sorted.** A covered product is ABSENT rather than shown with a zero: this
    file gets pasted into a supplier email, and a row reading "ABC Sattu … 0" invites someone
    to order zero of it.

    With nothing short it says so in words rather than rendering an empty table, because an
    empty grid reads as a failed download on the one day the news is good.
    """
    from openpyxl import Workbook

    headers = ["Product", "Brand", "Ordered kg", "Raw stock kg", "To buy kg"]
    short = [row for row in (purchasing.get("rows") or []) if not row["covered"]]

    book = Workbook()
    worksheet = book.active
    worksheet.title = "To buy"

    if not short:
        rows = [["Nothing to buy — every product is covered by raw stock.", "", "", "", ""]]
    else:
        rows = [
            [row["product"], row["brand"], float(row["ordered_kg"]),
             float(row["raw_kg"]), float(row["to_buy_kg"])]
            for row in short
        ]
        rows.append([
            "TOTAL", "",
            round(sum(row["ordered_kg"] for row in short), 2),
            round(sum(row["raw_kg"] for row in short), 2),
            round(sum(row["to_buy_kg"] for row in short), 2),
        ])

    _write_sheet(worksheet, headers, rows, _XLSX_PURCHASE_WIDTHS)
    book.properties.title = "To buy"
    book.properties.description = subtitle

    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    return buffer
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_shipment_documents.py -q -p no:randomly`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/shipment/documents.py tests/test_shipment_documents.py
git commit -m "feat: dispatch workbook with a sheet per tab, and a to-buy list

One workbook rather than three downloads: the tabs are read together and three
files get separated on a bench. A single-tab file is this same builder with the
other worksheets removed, so there is one code path.

The to-buy list is filtered, not sorted — a covered product is absent rather than
present with a zero, because this file gets pasted into a supplier email and a row
reading 'ABC Sattu ... 0' invites ordering zero of it."
```

---

### Task 7: The PDF learns `tab`, and the five download routes

**Files:**
- Modify: `app/shipment/documents.py` (`build_dispatch_pdf`)
- Modify: `app/routers/orders.py` (`download_dispatch`; new xlsx route)
- Test: `tests/test_orders_api.py` (append)

**Interfaces:**
- Consumes: `build_dispatch_xlsx`, `build_tobuy_xlsx` from Task 6
- Produces:
  - `build_dispatch_pdf(sheet, subtitle, tab="all", purchasing=None) -> io.BytesIO` (extended signature; existing two-arg calls still work)
  - `_dispatch_subtitle(sheet, meta) -> str` in `app/routers/orders.py`
  - `GET /orders/download/dispatch.pdf?tab=all|weight|sku|orders`
  - `GET /orders/download/dispatch.xlsx?tab=all|weight|sku|orders|tobuy`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orders_api.py` (add `import io` at the top of the file if absent):

```python
async def test_every_download_variant_returns_a_file(auth_client, db):
    """Five files, two formats, one aggregation behind all of them."""
    await repository.upsert_orders(db, [_dispatched("403-1")])
    await repository.replace_items(db, "403-1", [
        {"asin": KNOWN_ASIN, "seller_sku": "cs-500", "quantity_ordered": 4}
    ])

    for tab in ("all", "weight", "sku", "orders"):
        r = await auth_client.get(f"/orders/download/dispatch.pdf?tab={tab}")
        assert r.status_code == 200, f"pdf tab={tab}: {r.text[:200]}"
        assert r.content.startswith(b"%PDF-"), f"pdf tab={tab} is not a PDF"

    for tab in ("all", "weight", "sku", "orders", "tobuy"):
        r = await auth_client.get(f"/orders/download/dispatch.xlsx?tab={tab}")
        assert r.status_code == 200, f"xlsx tab={tab}: {r.text[:200]}"
        # Every .xlsx is a zip archive, so it starts with PK.
        assert r.content[:2] == b"PK", f"xlsx tab={tab} is not a workbook"


async def test_an_unknown_download_tab_is_refused(auth_client, db):
    """A typo must not silently export the wrong section."""
    await _seed(db)
    r = await auth_client.get("/orders/download/dispatch.xlsx?tab=nonsense")
    assert r.status_code == 400, r.text
    assert "nonsense" in r.json()["error"]


async def test_tobuy_is_excel_only(auth_client, db):
    """It is pasted into a supplier email, not read at a bench.

    Refused rather than silently served as the combined PDF, which is what a caller asking for
    `tab=tobuy` would NOT expect to receive.
    """
    await _seed(db)
    r = await auth_client.get("/orders/download/dispatch.pdf?tab=tobuy")
    assert r.status_code == 400, r.text


async def test_the_downloads_agree_with_the_screen_about_the_totals(auth_client, db):
    """The property the single aggregation exists to guarantee.

    A download that aggregated separately is how a printed sheet and a monitor start
    disagreeing about a quantity — the failure `_document_rows` prevents on the shipment side.
    """
    from openpyxl import load_workbook

    await repository.upsert_orders(db, [_dispatched("403-1"), _dispatched("403-2")])
    for order_id in ("403-1", "403-2"):
        await repository.replace_items(db, order_id, [
            {"asin": KNOWN_ASIN, "seller_sku": "cs-500", "quantity_ordered": 3}
        ])

    screen = (await auth_client.get("/orders/dispatch")).json()
    expected_units = screen["sheet"]["totals"]["units"]

    r = await auth_client.get("/orders/download/dispatch.xlsx?tab=sku")
    book = load_workbook(io.BytesIO(r.content))
    values = [[cell.value for cell in row] for row in book.active.iter_rows()]
    total_row = next(row for row in values if row and row[0] == "TOTAL")
    assert total_row[3] == expected_units, (
        f"the workbook says {total_row[3]} units, the screen says {expected_units}"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_orders_api.py -q -p no:randomly -k "download_variant or unknown_download or tobuy or agree_with_the_screen"`

Expected: FAIL — 404 on the `.xlsx` route.

- [ ] **Step 3: Extend `build_dispatch_pdf`**

In `app/shipment/documents.py`, change the signature to:

```python
def build_dispatch_pdf(
    sheet: dict, subtitle: str, tab: str = "all", purchasing: dict | None = None
) -> io.BytesIO:
```

Add to its docstring, after the existing "Two sections in one document" paragraph:

```
    `tab` narrows it to one section, and `purchasing` adds the weight-and-purchase table that
    tab 1 shows. Defaulted so the existing two-argument calls keep working unchanged.
```

Immediately after the line `styles = _paragraph_styles()`, insert the purchasing section:

```python
    # ── Section 0: weight and purchasing, when asked for ──
    if tab in ("all", "weight") and purchasing is not None:
        purchase_rows = []
        for row in purchasing.get("rows") or []:
            purchase_rows.append([
                Paragraph(_escape(row["product"]), styles["loud"]),
                Paragraph(_escape(row["brand"]), styles["quiet"]),
                Paragraph(f"{row['ordered_kg']:.2f}", styles["plain"]),
                Paragraph(f"{row['raw_kg']:.2f}", styles["plain"]),
                # An em dash, not 0.00: a zero in a purchasing column reads as a
                # measurement rather than as "nothing to do".
                Paragraph(
                    "—" if row["covered"] else f"{row['to_buy_kg']:.2f}",
                    styles["quantity"],
                ),
            ])
        ptotals = purchasing.get("totals") or {}
        purchase_rows.append([
            Paragraph("TOTAL", styles["totals"]),
            Paragraph("", styles["totals"]),
            Paragraph(f"{float(ptotals.get('ordered_kg') or 0):.2f}", styles["totals_qty"]),
            Paragraph(f"{float(ptotals.get('raw_kg') or 0):.2f}", styles["totals_qty"]),
            Paragraph(f"{float(ptotals.get('to_buy_kg') or 0):.2f}", styles["totals_qty"]),
        ])
        if not purchase_rows:
            purchase_rows = [[Paragraph("Nothing due today.", styles["plain"])]
                             + [Paragraph("", styles["plain"])] * 4]

        purchase_table = Table(
            [[_head_cell(head) for head in
              ["Product", "Brand", "Ordered kg", "Raw kg", "To buy kg"]]] + purchase_rows,
            colWidths=_dispatch_widths([70, 22, 30, 30, 32]),
            repeatRows=1,
        )
        purchase_table.setStyle(_dispatch_table_style(totals_row=len(purchase_rows)))
        elements.append(Paragraph("Weight &amp; purchase", styles["loud"]))
        elements.append(Spacer(1, 2 * mm))
        elements.append(purchase_table)
        if tab == "all":
            elements.append(Spacer(1, 7 * mm))
```

Then wrap the two existing blocks so each is emitted only when wanted, leaving their bodies
unchanged:

- Put `if tab in ("all", "sku"):` around the existing "Section 1: parent products" block,
  from `summary_headers = [...]` through `elements.append(summary)`.
- Put `if tab in ("all", "orders"):` around the existing "Section 2: every order" block, from
  `elements.append(Spacer(1, 7 * mm))` through `elements.append(order_table)`.

Guard against an empty document, immediately before `doc.build(...)`:

```python
    # reportlab raises on a document with no flowables, and an unknown `tab` reaching here
    # would produce exactly that — a 500 on a download rather than a readable refusal.
    if len(elements) <= 3:                  # title + subtitle + spacer only
        elements.append(Paragraph("Nothing to show for this section.", styles["plain"]))
```

- [ ] **Step 4: Add the route constants and the subtitle helper**

In `app/routers/orders.py`, add after `SECTION_LABELS`:

```python
#: Download variants that both formats offer. Validated against this set so a typo cannot
#: silently export the wrong section — the same guard `download_picking_sheet` applies.
DOWNLOAD_TABS = ("all", "weight", "sku", "orders")

#: `tobuy` is Excel-only: it is pasted into a supplier email rather than read at a bench.
XLSX_ONLY_TABS = ("tobuy",)
```

Add beside `_dispatch`:

```python
def _dispatch_subtitle(sheet: dict, meta: dict) -> str:
    """The provenance line every dispatch document carries.

    Stated on every file rather than left to whoever prints it: a sheet with no date gets
    worked from tomorrow, and these numbers change every few minutes as counts are entered.
    """
    totals = sheet["totals"]
    parts = [
        f"{meta['today']} (IST)",
        f"{totals['orders']} orders",
        f"{totals['units']} units",
        f"{totals['kg']} kg net",
        f"{totals['parents']} product(s)",
    ]
    if totals["packed"]:
        parts.append(f"{totals['packed']} packed")
    if totals["sizes_without_weight"]:
        parts.append(f"{totals['sizes_without_weight']} line(s) with no pack size")
    return " · ".join(parts)
```

- [ ] **Step 5: Replace `download_dispatch` and add the xlsx route**

```python
@router.get("/download/dispatch.pdf")
async def download_dispatch(
    request: Request,
    tab: str = "all",
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ORDERS)),
):
    """The dispatch sheet as a PDF — all three sections, or one of them.

    Built through the same `_dispatch` the screen uses, so paper and monitor cannot disagree. A
    PDF because this one is read on the floor and ticked with a pen; the Excel variants are for
    accounts and for suppliers.
    """
    if tab not in DOWNLOAD_TABS:
        return JSONResponse({"error": f"Unknown section {tab!r}."}, status_code=400)

    sheet, purchasing, meta = await _dispatch(db)
    stream = documents.build_dispatch_pdf(
        sheet, _dispatch_subtitle(sheet, meta), tab=tab, purchasing=purchasing
    )
    filename = f"dispatch-{tab}-{meta['today']}.pdf"
    return StreamingResponse(
        stream,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/download/dispatch.xlsx")
async def download_dispatch_xlsx(
    request: Request,
    tab: str = "all",
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ORDERS)),
):
    """The dispatch sheet as Excel: the combined workbook, one tab, or the to-buy list.

    `tab=tobuy` holds only the products that are short, for pasting into a supplier email.
    """
    if tab not in DOWNLOAD_TABS + XLSX_ONLY_TABS:
        return JSONResponse({"error": f"Unknown section {tab!r}."}, status_code=400)

    sheet, purchasing, meta = await _dispatch(db)
    subtitle = _dispatch_subtitle(sheet, meta)

    if tab == "tobuy":
        stream = documents.build_tobuy_xlsx(purchasing, subtitle)
        filename = f"to-buy-{meta['today']}.xlsx"
    else:
        stream = documents.build_dispatch_xlsx(sheet, purchasing, subtitle, tab=tab)
        filename = f"dispatch-{tab}-{meta['today']}.xlsx"

    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_orders_api.py tests/test_shipment_documents.py -q -p no:randomly`

Expected: PASS

- [ ] **Step 7: Run the full suite and commit**

Run: `venv/Scripts/python -m pytest -q`
Expected: `1311 passed, 3 skipped`

```bash
git add app/routers/orders.py app/shipment/documents.py tests/test_orders_api.py
git commit -m "feat: five dispatch downloads, all from one aggregation

?tab= selects the combined file or a single section; tobuy is Excel-only because it
is pasted into a supplier email rather than read at a bench, and asking for it as a
PDF is refused rather than silently served as the combined document.

The PDF guards against an empty flowable list, which reportlab raises on — that
would have been a 500 on a download instead of a readable refusal."
```

---

### Task 8: The three-tab template

**Files:**
- Modify: `templates/orders.html` (rewrite the body and script; keep the `<head>`/theme link)
- Test: `tests/test_orders_api.py` (append template assertions)

**Interfaces:**
- Consumes: `GET /orders/dispatch` (with `purchasing`), `POST /orders/packed/{pack_date}`, `POST /orders/raw-stock`, `GET /orders/refresh-status`, the download routes
- Produces: three tab panels with ids `tab-weight`, `tab-sku`, `tab-orders`; search inputs `search-weight`, `search-sku`, `search-orders`; a save bar; a 60-second poll

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orders_api.py`:

```python
def test_the_template_has_three_tabs_each_with_its_own_search(auth_client):
    """Three tabs, three search boxes. Asserted on the markup because the ids are contracts.

    `tests/test_template_render_targets.py` already fails any getElementById that is written to
    without a matching element — this asserts the other direction, that the tabs the design
    calls for actually exist.
    """
    source = _orders_source()
    for tab in ("tab-weight", "tab-sku", "tab-orders"):
        assert f'id="{tab}"' in source, f"{tab} panel is missing"
    for box in ("search-weight", "search-sku", "search-orders"):
        assert f'id="{box}"' in source, f"{box} is missing, so that tab cannot be filtered"


def test_the_poll_never_re_renders_a_tab_with_inputs():
    """A 60-second poll that redrew the SKU tab would eat the packer's keystrokes mid-number.

    The poll exists so tab 3's Amazon statuses update themselves. Tabs 1 and 2 hold number
    boxes, so the poll must touch neither — asserted on the source because the failure is
    invisible until someone is typing.
    """
    source = _orders_source()
    assert "function pollOrders(" in source, "the orders poll is missing"
    start = source.index("function pollOrders(")
    body = source[start:start + 900]
    for forbidden in ("renderWeight(", "renderSku("):
        assert forbidden not in body, (
            f"pollOrders calls {forbidden} — it would redraw a tab containing inputs and "
            "discard whatever the packer was typing"
        )


def test_the_packed_and_raw_stock_saves_post_to_their_own_routes():
    """Two facts, two routes, and only one of them carries a date."""
    source = _orders_source()
    assert "/orders/packed/${" in source, "the packed save does not send a date"
    assert '"/orders/raw-stock"' in source, "the raw stock save is missing"
    assert "/orders/raw-stock/${" not in source, (
        "the raw stock route must not take a date: raw stock is standing, and a date would "
        "make the number unreachable tomorrow"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_orders_api.py -q -p no:randomly -k "three_tabs or poll_never or their_own_routes"`

Expected: FAIL — `tab-weight panel is missing`

- [ ] **Step 3: Rewrite the template**

Replace `templates/orders.html` entirely. Keep the existing `<head>` (the `theme.css` link and
the `<style>` block) and extend it; the new content is the tab strip, three panels and the
script.

Required structure — every id below is referenced by the script, so all must exist
(`tests/test_template_render_targets.py` enforces it):

```
header (unchanged, includes nav.html)
main
  h2 + subtitle
  #messages
  .card  → #refresh-btn, #refresh-note, #progress-wrap, #progress-fill, #progress-label,
           #download-menu (the five links)
  #kpis
  tab strip → #tabbtn-weight, #tabbtn-sku, #tabbtn-orders
  #tab-weight   → #search-weight, #weight-area, #tobuy-btn
  #tab-sku      → #search-sku, #sku-area
  #tab-orders   → #search-orders, #orders-area, #orders-updated
  #admin-area   → #toggle-other, #other-view
.savebar → #save-count, #save-btn, #discard-btn
```

Key script contracts, written exactly:

```javascript
/* ── State ──────────────────────────────────────────────────────────────────
   The server computed the sheet, the purchasing view, the IST dates and the rollups. This
   page renders and filters but never aggregates — a second aggregation here is how a printed
   sheet and a screen start disagreeing about a quantity, which is the failure `_dispatch`
   exists to prevent by being the one code path behind both. */
let data = null;             // GET /orders/dispatch
let other = null;            // GET /orders, the admin picking sheet, loaded on demand
let tab = "weight";
let polling = null;          // the refresh poller, while a refresh runs
let ordersPoll = null;       // the 60 s local re-read for tab 3
let saving = false;
const packed = {};           // asin -> units, as edited
const rawStock = {};         // product -> kg, as edited
const savedPacked = {};      // asin -> last COMMITTED units, for undo
const savedRaw = {};         // product -> last COMMITTED kg, for undo
const dirtyPacked = new Set();
const dirtyRaw = new Set();
```

```javascript
/* ── Tab 3 keeps itself current ─────────────────────────────────────────────
   Amazon statuses change through the day: the scheduled job refreshes from Amazon every 30
   minutes, and this re-reads the LOCAL rows every 60 seconds so an open page shows the change
   without a reload. It costs no Amazon calls — `getOrders` allows one every 22.5 s, so a page
   that polled Amazon would 429 the moment two people opened the tab.

   **It re-renders tab 3 ONLY.** Tabs 1 and 2 hold number boxes, and redrawing one of those
   under the cursor would discard whatever was being typed. */
function startOrdersPoll(){
  if(ordersPoll) clearInterval(ordersPoll);
  ordersPoll = setInterval(async () => {
    if(document.hidden) return;                  // a background tab need not poll
    try{
      const r = await fetch("/orders/dispatch");
      if(!r.ok) return;
      const fresh = await r.json();
      data.sheet.orders = fresh.sheet.orders;
      data.last_refreshed_at = fresh.last_refreshed_at;
      if(tab === "orders") renderOrders();
      renderOrdersUpdated();
    }catch(err){ /* a failed poll is not worth a banner; the next one will do */ }
  }, 60000);
}
```

```javascript
/* Undo reverts a row to its last SAVED value. A committed number is corrected by typing over
   it, which is why there is no undo-the-save: keeping an audit trail to roll one back would be
   a bigger machine than retyping the old figure. */
function undoPacked(asin){
  packed[asin] = Number(savedPacked[asin] || 0);
  dirtyPacked.delete(asin);
  renderSku();
  renderSaveBar();
}

function undoRaw(product){
  rawStock[product] = Number(savedRaw[product] || 0);
  dirtyRaw.delete(product);
  renderWeight();
  renderSaveBar();
}

function discardAll(){
  Object.keys(savedPacked).forEach(a => { packed[a] = Number(savedPacked[a] || 0); });
  Object.keys(savedRaw).forEach(p => { rawStock[p] = Number(savedRaw[p] || 0); });
  dirtyPacked.clear();
  dirtyRaw.clear();
  render();
  renderSaveBar();
}
```

```javascript
/* One save button for both tabs: the packer may have typed in either, and asking him to
   remember which tab a number lives on is how a count gets lost. Two requests, because they
   are two different facts on two different routes — only the packed one carries a date. */
async function save(){
  if(saving || !(dirtyPacked.size || dirtyRaw.size)) return;
  saving = true; renderSaveBar();
  try{
    if(dirtyPacked.size){
      const entries = [...dirtyPacked].map(asin => ({asin, units: n(packed[asin])}));
      const r = await fetch(`/orders/packed/${data.pack_date}`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({entries}),
      });
      const body = await r.json();
      if(!r.ok){
        /* 409 means the IST day rolled over while this page was open. Said plainly, because
           the honest fix is a reload — silently refiling the count onto a new date would put
           this morning's work on the wrong day. */
        message(body.error || "Could not save the packed counts.", "error");
        return;
      }
      dirtyPacked.clear();
      Object.keys(body.packed || {}).forEach(a => {
        packed[a] = n(body.packed[a]); savedPacked[a] = n(body.packed[a]);
      });
    }
    if(dirtyRaw.size){
      const entries = [...dirtyRaw].map(product => ({product, raw_kg: n(rawStock[product])}));
      const r = await fetch("/orders/raw-stock", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({entries}),
      });
      const body = await r.json();
      if(!r.ok){ message(body.error || "Could not save the raw stock.", "error"); return; }
      dirtyRaw.clear();
      Object.keys(body.raw_stock || {}).forEach(p => {
        rawStock[p] = n(body.raw_stock[p]); savedRaw[p] = n(body.raw_stock[p]);
      });
    }
    await load();                    // re-derive to_buy from the committed figures
    message("Saved.", "good");
  }catch(err){
    message("Could not reach the server. Your numbers are still on screen — try Save again.",
            "error");
  }finally{
    saving = false; renderSaveBar();
  }
}
```

Tab 1's rows render `—` when `row.covered`, and the raw-stock input as
`<input class="qty" type="number" min="0" step="0.1" data-product="...">`. Tab 2 renders one
flat row per size with `ORDERED`, a `PACKED TODAY` input keyed `data-asin`, and `LEFT`; a row
where `size.units !== size.orders` shows `${units} / ${orders} ord` in the ordered cell.

Keep from the current template, unchanged: `dateIST()` (and its no-`new Date(` property),
`esc()`, `message()`, `ago()`, the refresh progress bar wiring, the over-pack warning, the
admin-only `#admin-area` gated on `data.is_admin`, and the `beforeunload` guard.

Colours must come from `theme.css` variables only — `tests/test_theme.py` fails any hardcoded
hex or `rgba()`. The save bar uses `box-shadow: var(--shadow-lg)`.

- [ ] **Step 4: Run the template tests**

Run: `venv/Scripts/python -m pytest tests/test_orders_api.py tests/test_theme.py tests/test_template_render_targets.py tests/test_nav_consistency.py -q -p no:randomly`

Expected: PASS. A `test_no_template_hardcodes_a_colour[orders.html]` failure names the literal
to replace with a variable; a render-target failure names the id that is written to but absent
from the markup.

- [ ] **Step 5: Mutation check — let the poll redraw a tab with inputs**

Temporarily add `renderSku();` inside `pollOrders`'s interval body, next to `renderOrders()`:

```javascript
      if(tab === "orders") renderOrders();
      renderSku();                            // MUTATION: redraws a tab with inputs
      renderOrdersUpdated();
```

Run: `venv/Scripts/python -m pytest tests/test_orders_api.py -q -p no:randomly -k poll_never`

Expected: **FAIL** on `test_the_poll_never_re_renders_a_tab_with_inputs`. Remove the line and
confirm PASS.

This one is worth doing carefully, because the real symptom is invisible to every other test:
the suite stays green while a packer loses a half-typed number every sixty seconds.

- [ ] **Step 6: Mutation check — the em dash becomes 0.00**

The covered-product dash is a rendering rule, so it needs its own guard. First add the test to
`tests/test_orders_api.py`:

```python
def test_a_covered_product_renders_a_dash_not_a_zero():
    """A zero in a purchasing column reads as a measurement, not as "nothing to do".

    Asserted on the template because it is a rendering decision: the number is already 0.0 in
    the payload, and printing it literally is what makes the tab ambiguous.
    """
    source = _orders_source()
    assert 'row.covered ? "—"' in source, (
        "a covered product must render an em dash; 0.00 reads as a weight someone measured"
    )
```

Then temporarily replace that expression in the template's tab-1 renderer with
`n(row.to_buy_kg).toFixed(2)`.

Run: `venv/Scripts/python -m pytest tests/test_orders_api.py -q -p no:randomly -k covered_product_renders`

Expected: **FAIL**. Restore the dash and confirm PASS.

- [ ] **Step 7: Verify in a browser against production data**

This step is not optional: three bugs in this feature were found only this way (the ship-by
date rendering 05:30 the next morning, the `renderInvoiceBar` with no `<div>`, and the
`intakeFromShipment` fields the screen discarded).

```bash
scp -i "C:\Users\LENOVO\Desktop\old downloads\amazon-tracker-key.pem" \
  ubuntu@13.233.144.148:/opt/amazon-tracker/tracker.db ./tracker_prod_copy.db
cp tracker.db tracker_local_backup.db && cp tracker_prod_copy.db tracker.db
venv/Scripts/python -m alembic upgrade head
```

Start the server with `preview_start` (name `tracker`), sign in, open `/orders-page` and check:

1. Tab 1 lists 33 products heaviest first; typing a raw-stock figure changes `TO BUY` on that
   row and the TOTAL, and the total equals the sum of the visible rows rather than
   `ordered − raw`.
2. Tab 2 lists 68 flat rows; typing a packed count updates `LEFT`, the KPI strip and the
   footer without the input losing focus mid-number.
3. Undo on a typed row restores the saved value; **Discard unsaved** clears both tabs.
4. Save, reload — both numbers persist.
5. Tab 3 shows 281 lines and `updated N min ago`.
6. Each search box filters only its own tab.
7. All five downloads open.

Then restore: `cp tracker_local_backup.db tracker.db` and delete the copies.

- [ ] **Step 8: Run the full suite and commit**

Run: `venv/Scripts/python -m pytest -q`
Expected: `1314 passed, 3 skipped`

```bash
git add templates/orders.html tests/test_orders_api.py
git commit -m "feat: the dispatch screen becomes three tabs

Weight and purchasing, per-SKU packing, and the order list — split by question
rather than compressing one table. Measured on production, the old single table was
101 rows and 7 columns where UNITS and ORDERS were identical on 57 of 68 rows and 7
products had a parent row that was an identical twin of its only size row.

Tab 3 re-reads local rows every 60 seconds so Amazon statuses update themselves, and
it re-renders that tab ONLY: redrawing a tab with number boxes would discard
whatever the packer was typing."
```

---

### Task 9: Document it

**Files:**
- Modify: `CLAUDE.md` (the "Orders tab" section)

- [ ] **Step 1: Replace the Orders tab heading and opening**

Replace the current opening paragraph of `## Orders tab — today's dispatch` with:

```markdown
## Orders tab — today's dispatch, in three tabs

`/orders-page`. Three tabs over the same 264-order day, split by the question being asked:

| Tab | Rows | Editable | Answers |
|---|---|---|---|
| Weight & purchase | 33 parents | raw stock (kg) | how much goes out, and what must I buy |
| By SKU | 68 flat rows | packed today | what has been boxed against each SKU |
| Orders | 281 lines | nothing | which orders exactly, and where |

Asked for as *"show them only the data which is waiting for pickup"*, *"Each parent item total
weight orders … sort it total weight wise"*, and *"they should be able to put how many units
has he packed against each sku"*.

**It was one table and it read as haphazard.** Measured before redesigning rather than guessed:
101 rows and 7 columns, where UNITS and ORDERS were identical on **57 of 68** size rows, **7 of
33** products had a parent row that was an identical twin of its only size row, and **41 of 68**
rows held three units or fewer. The longest product name is 29 characters, so column width was
never the constraint — the screen was answering three questions at once.
```

- [ ] **Step 2: Add the raw-stock and purchasing subsection**

Insert after the packed-counts subsection:

```markdown
### Raw stock is STANDING; packed counts are per-day
`product_raw_stock` is UNIQUE on `(product)` with **no `pack_date`**, and that asymmetry with
`order_packed_entries` is deliberate. A packed count belongs to a day. Raw material on a shelf
does not vanish at midnight, so a dated row would be blank every morning and the purchasing tab
would read "buy everything" at 9am daily until 33 numbers were retyped.

Keyed on the parent product NAME, not an ASIN: raw material is bulk, and there is no such thing
as 500 g-flavoured raw sattu. Kilograms, because that is how it is bought.

`raw_kg` is `Numeric`, so SQLAlchemy hands back **`Decimal`, which `JSONResponse` cannot
serialise** — the repository converts to `float` so every route inherits the fix. That exact
defect already shipped once with datetimes and was found in a browser on production.

**A zero is STORED here, not deleted** — the opposite of `save_packed`. There, 0 packed and
"not counted" are the same thing on a worksheet. Here "we have none" is the fact that makes
`to_buy` the full ordered weight, and deleting the row would leave it looking untouched.

**`to_buy` clamps at 0, and the TOTAL sums the clamped rows.** Never
`total_ordered − total_raw`: those differ the moment any product is in surplus, and the
subtraction lets a surplus of ABC Sattu cancel a shortfall of Usna Chawal — you cannot make
rice out of sattu. On real numbers that was 43.00 kg against 33.50 kg. Caught reviewing the
design, not the code, because the wrong version looks entirely plausible in a totals row.

Built to be replaced: when an inventory tab exists it writes this table instead of a person.

### Tracking ID is not obtainable, and this was measured
The Orders tab has no tracking column because Amazon does not give us one. Probed four routes
on the live account: `getOrders` (32 fields, none), `getOrderItems` (none),
`/easyShip/2022-03-23/package` (**403**), and `GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL`,
which returned 1,283 rows that were **every one `AFN`** — 0 of our 100 Easy Ship ids. The
Reports API itself works (a 3,042-row orders report generated fine), so this is Amazon
withholding the field behind the restricted role, not a permissions accident. A column that can
never populate reads as broken data, so it is omitted rather than shipped empty.

### The orders refresh has its own flag
`ORDER_REFRESH_ENABLED` runs the half-hourly refresh **without** the 06:00 product scrape, the
07:30 keyword track or the 09:15 purge. Production keeps `SCHEDULER_ENABLED=false` precisely to
keep those asleep on a 951 MB box with no swap that has already OOM-killed a `pip install`.

The two flags are OR'd, so an installation that enables everything keeps its refresh.
`setup_scheduler`'s opening `if not settings.scheduler_enabled: return` **moved down** to the
three jobs it guards — restoring it to the top reads as tidy and silently stops the orders
refresh, which is why a test asserts the registered job ids rather than the flag.

Tab 3 also re-reads LOCAL rows every 60 seconds, so an open page shows a status change without
a reload. It re-renders tab 3 only: redrawing a tab with number boxes would discard whatever
the packer was typing.
```

- [ ] **Step 3: Verify no stale claim survives**

```bash
grep -n "split into four sections\|no local .packed. tick\|七" CLAUDE.md
```

Expected: no hit describing the old single-table layout. Fix any that appear.

- [ ] **Step 4: Run the full suite and commit**

Run: `venv/Scripts/python -m pytest -q`
Expected: `1314 passed, 3 skipped`

```bash
git add CLAUDE.md
git commit -m "docs: record the three-tab dispatch screen and its measured constraints

Documents why raw stock is standing while packed counts are per-day, why the to-buy
total sums clamped rows rather than subtracting totals, that tracking ID was proven
unobtainable across four API routes, and why the orders refresh needed a flag of its
own rather than waking three dormant jobs on a 951 MB box."
```

---

## Deploy

After Task 9, deploy and enable the flag — in this order, so the first scheduled run has the
new table:

```bash
git push origin claude/stoic-allen-bb3a55
ssh -i "C:\Users\LENOVO\Desktop\old downloads\amazon-tracker-key.pem" ubuntu@13.233.144.148 \
  "cd /opt/amazon-tracker && echo y | bash deploy/update-ec2.sh"
ssh -i "C:\Users\LENOVO\Desktop\old downloads\amazon-tracker-key.pem" ubuntu@13.233.144.148 \
  "echo 'ORDER_REFRESH_ENABLED=true' >> /opt/amazon-tracker/.env && sudo systemctl restart tracker"
```

`update-ec2.sh` is invoked as `bash deploy/update-ec2.sh` and fed `y`: the script prompts before
stashing `app/invoice/hsn_master.json` (91 hand-verified GST classifications on the box against
15 in git), and `git checkout` of that file drops the execute bit.

Then verify on the box:

```bash
ssh -i "...amazon-tracker-key.pem" ubuntu@13.233.144.148 \
  "cd /opt/amazon-tracker && git log --oneline -1 && venv/bin/python -c \"
import sqlite3
c = sqlite3.connect('tracker.db')
print('raw stock table:', bool(c.execute(
    \\\"select name from sqlite_master where name='product_raw_stock'\\\").fetchone()))
\" && grep -c ORDER_REFRESH_ENABLED .env"
```

Expected: the new commit, `raw stock table: True`, and `1`.

Confirm the job registered: `sudo journalctl -u tracker --since '2 min ago' | grep Scheduler`
should print `Scheduler started: orders every 30m` with no mention of products.
