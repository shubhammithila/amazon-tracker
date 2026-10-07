# Portfolio → Repeat customers sub-tab — Implementation Plan

> **For agentic workers:** execute INLINE with superpowers:executing-plans (this user forbids
> subagents). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A "Repeat customers" sub-tab under Portfolio showing, per parent product, 30/60/90-day
same-product repeat %, "came from another product" %, and on expand the cross-product flows and
basket pairs — with a brand total row (Mithila by default) — built from Amazon's FBA shipments
report keyed on a hashed buyer email, and validated against Amazon's own Brand Analytics.

**Architecture:** A new `app/repeat/` package. A nightly job pulls the last 7 days of
`GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL`, hashes the masked buyer email with a stored salt
(raw email never stored), resolves SKU → child ASIN → parent ASIN, and upserts one row per shipped
order line into `customer_order_lines`. All metrics are computed on READ by one pure function
(`app/repeat/logic.py`), cross-checked by a deliberately naive reference implementation on
randomised histories. A sibling page `/portfolio-page/repeat` renders it; a two-button switch
links it with the existing Profit page.

**Tech stack:** FastAPI, SQLAlchemy async, SQLite, Alembic, SP-API Reports API 2021-06-30,
vanilla JS, Node render tests via `tests/js_harness.py`.

## Measured facts this plan rests on (06–07 Oct 2026, live account)

| Fact | Measurement |
|---|---|
| Customer key exists | FBA shipments report `buyer-email` filled on 461/461 rows; all `…@marketplace.amazon.in` relay addresses |
| Key is consistent within an order | **0** of 32,351 orders carry two different keys |
| Key is stable across months | 785 July buyers reappear in Aug, 624 in Sep |
| SKU → parent coverage | **100%** via `economics_daily` MSKU rows; only skips were 387 rows with no purchase date and 167 units at price 0 (replacements) |
| History depth | Oct 2025 (134 rows / 2 days) and Apr 2025 (56 rows / 2 days) both DONE → **≥ 18 months** |
| Report window cap | a 90-day request is **FATAL**; 30-day chunks work |
| Report latency | **30–40 min per 30-day chunk**, queue is SERIAL per report type and shared with an external nightly requester (00:20 UTC, not this app) |
| Easy Ship | the All Orders report has **no buyer field** for Merchant orders (~37% of orders) → FBA-only by construction |
| Brand Analytics check (Sep, same ASIN) | unique customers ours vs Amazon: **914/925, 737/747, 223/223, 489/494**; our repeat % runs **0.3–0.6 pt lower** (Easy Ship repeats invisible). Easy-Ship-heavy ASINs diverge (Kulthi Dal 186 vs 509) |
| Brands among parents | Mithila Foods 62, Prepto 15, Howrah Foods 14 |

## Decisions taken (the owner's)

- **Follow-up-window repeat.** For window N ∈ {30, 60, 90}: the cohort is customers who bought
  product X in the 30-day period **ending N days before `as_of`**; each is anchored at their first
  purchase of X in that period; **same repeat %** = share who bought X again on a **later day**
  within N days of the anchor. Every customer gets the full N days, so 30/60/90 are comparable.
- **Cross flow = share of its buyers.** On X's row, **came from other %** = share of X's cohort who
  bought a DIFFERENT product of ours on an earlier day within the N days before the anchor. The
  expand panel names the source products ("came from"), the destinations ("went on to", forward
  N days) and the same-order pairs ("bought together").
- Data: nightly fetch + one-off backfill; SKU → parent exactly as Portfolio; one pure calculation
  tested against a naive reference and against Brand Analytics; screen = one row per parent with a
  brand total row and an expand panel. (Approved as proposed.)

## Global constraints

- **Never store, log or render the raw buyer email, name, phone or address.** Only
  `buyer_key = HMAC-SHA256(salt, lower(strip(email)))[:32]`. Salt lives in `portfolio_settings`
  row `customer_key_salt`, created once.
- **FBA only, stated on screen.** Every product row carries `fba_share` (FBA units ÷ all units
  ordered, last 90 days); below 0.6 the row is marked "partial — many Easy Ship orders".
- **A repeat is a later DAY.** Two orders on the same IST day are not a repeat; two products in one
  order are a basket, not a cross flow.
- **Percentages come from counts, never averaged.** The brand total counts UNIQUE customers, so it
  is NOT the sum of the rows (one customer buying two products is one brand customer).
- **`None`, never 0%,** when a cohort has fewer than `MIN_COHORT = 20` buyers or the window lacks
  history; the screen prints `—` with the reason in a tooltip.
- **IST days** via `app/ist.py` only. Report timestamps are converted once in the parser.
- Zero-price lines (free replacements) are excluded and counted.
- Report requests are ≤ 30 days (`MAX_REPORT_DAYS = 30`).
- Every migration adds a branch to `deploy/update-ec2.sh`'s detector (newest first).
- New colours: none. `static/theme.css` only; `tests/test_theme.py` must pass.
- Tests: `venv/Scripts/python -m pytest -q`; mutation harness must print `All N mutations caught`.

## File structure

```
app/repeat/__init__.py         package marker + one-line docstring
app/repeat/keys.py             salt load/create, customer_key()
app/repeat/fetch.py            SP-API report: create → poll → download → rows; split_days
app/repeat/parse.py            report rows → line dicts + skip counters (pure)
app/repeat/repository.py       save_lines (upsert), load_lines, purge, sku_map, resolve_missing,
                               fba share inputs, run records
app/repeat/logic.py            build_orders, metrics, covered_from, name_parents, pct (pure)
app/repeat/service.py          assemble the JSON payload for one brand
app/repeat/refresh.py          run(window), run_incremental(), STATE
app/routers/repeat.py          GET /portfolio/repeat, POST /portfolio/repeat/refresh, GET …/refresh-status
templates/portfolio_repeat.html  the sub-tab page
templates/_portfolio_tabs.html   Profit | Repeat customers switch (fragment)
alembic/versions/a4c7e2f19b30_customer_order_lines.py
scripts/backfill_repeat.py     18 months in 30-day chunks, resumable
scripts/validate_repeat.py     compare a month against Brand Analytics, non-zero exit on failure
scripts/mutate_repeat.py       mutation harness
tests/test_repeat_storage.py, test_repeat_keys.py, test_repeat_parse.py, test_repeat_fetch.py,
tests/test_repeat_logic.py, test_repeat_reference.py, test_repeat_api.py, test_repeat_page.py
```

Modified: `app/models.py`, `app/main.py`, `app/scheduler.py`, `deploy/update-ec2.sh`,
`templates/portfolio.html` (include the switch), `tests/js_harness.py` (template parameter),
`tests/test_theme.py` (fragment exemption), `tests/test_retention_and_scheduler.py`, `CLAUDE.md`.

---

### Task 1: Storage — two tables, migration, deploy detector, repository

**Files:**
- Modify: `app/models.py` (append after `EconomicsRefresh`)
- Create: `alembic/versions/a4c7e2f19b30_customer_order_lines.py`
- Modify: `deploy/update-ec2.sh` (detector branch + required tables)
- Create: `app/repeat/__init__.py`, `app/repeat/repository.py`
- Test: `tests/test_repeat_storage.py`

**Interfaces — Produces:**
- `CustomerOrderLine`, `RepeatRefresh` models.
- `repository.save_lines(db, lines: list[dict]) -> int`
- `repository.load_lines(db, since: str) -> list[dict]` — dicts `{amazon_order_id, buyer_key, day: date, child_asin, parent_asin, units}`, only rows with `parent_asin`.
- `repository.purge(db, keep_from: str) -> int`
- `repository.sku_map(db, catalogue: dict) -> dict[str, tuple[str, str]]` — sku → (child, parent)
- `repository.resolve_missing(db, mapping) -> int`
- `repository.units_by_parent(db, start: str, end: str) -> tuple[dict[str,int], dict[str,int]]` — (FBA units from lines, all-channel units ordered from economics)
- `repository.record_run(db, **fields) -> None`, `repository.done_runs(db) -> list[tuple[str, str]]`, `repository.last_run(db) -> dict | None`

- [ ] **Step 1: Write the failing tests** — `tests/test_repeat_storage.py`

```python
"""customer_order_lines: one row per shipped order line, upserted, never doubled."""
from datetime import date

import pytest

from app.repeat import repository

pytestmark = pytest.mark.regression


def _line(order="171-1", item="I1", buyer="k1", day="2026-09-01", sku="1kg cs FBA",
          child="B0CHILD001", parent="B0PARENT01", units=1):
    return {"amazon_order_id": order, "shipment_item_id": item, "buyer_key": buyer,
            "purchase_day": day, "seller_sku": sku, "child_asin": child,
            "parent_asin": parent, "units": units}


async def test_saving_the_same_line_twice_keeps_one_row_with_the_newer_values(db):
    await repository.save_lines(db, [_line(units=1)])
    await repository.save_lines(db, [_line(units=3)])
    rows = await repository.load_lines(db, since="2026-01-01")
    assert len(rows) == 1 and rows[0]["units"] == 3


async def test_two_items_of_one_order_are_two_rows(db):
    await repository.save_lines(db, [_line(item="I1"), _line(item="I2", child="B0CHILD002")])
    assert len(await repository.load_lines(db, since="2026-01-01")) == 2


async def test_load_returns_a_DATE_and_skips_unresolved_lines(db):
    await repository.save_lines(db, [_line(), _line(order="171-2", parent=None, child=None)])
    rows = await repository.load_lines(db, since="2026-01-01")
    assert [r["day"] for r in rows] == [date(2026, 9, 1)]


async def test_purge_is_scoped_to_days_before_the_cutoff(db):
    await repository.save_lines(db, [_line(order="a", day="2025-01-01"), _line(order="b", day="2026-09-01")])
    assert await repository.purge(db, keep_from="2026-01-01") == 1
    assert [r["amazon_order_id"] for r in await repository.load_lines(db, since="2000-01-01")] == ["b"]


async def test_resolve_missing_fills_only_null_rows(db):
    await repository.save_lines(db, [_line(order="a", child=None, parent=None),
                                     _line(order="b", sku="other FBA", child="B0KEEP0001", parent="B0KEEPPAR1")])
    n = await repository.resolve_missing(db, {"1kg cs FBA": ("B0CHILD001", "B0PARENT01"),
                                             "other FBA": ("B0WRONG001", "B0WRONG001")})
    assert n == 1
    rows = {r["amazon_order_id"]: r for r in await repository.load_lines(db, since="2000-01-01")}
    assert rows["a"]["parent_asin"] == "B0PARENT01" and rows["b"]["parent_asin"] == "B0KEEPPAR1"


async def test_the_raw_email_column_does_not_exist():
    from app.models import CustomerOrderLine
    cols = {c.name for c in CustomerOrderLine.__table__.columns}
    assert not {c for c in cols if "email" in c or "name" in c or "phone" in c or "address" in c}
```

- [ ] **Step 2: Run** `venv/Scripts/python -m pytest tests/test_repeat_storage.py -q` → FAIL (`No module named app.repeat`).

- [ ] **Step 3: Models** — append to `app/models.py` after `EconomicsRefresh`:

```python
class CustomerOrderLine(Base):
    """One SHIPPED FBA order line, keyed to a hashed customer. The Repeat sub-tab's only source.

    From `GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL`, whose masked `buyer-email` is stable per
    buyer (0 of 32,351 orders carried two keys; 785 July buyers reappeared in August). **Only a
    salted HMAC of it is stored** — no email, name, phone or address column exists, and a test
    asserts that. Easy Ship orders carry no buyer field anywhere, so this table is FBA-only.

    UNIQUE on (order, shipment item): the nightly fetch overlaps 7 days, so a re-read must update.
    `child_asin`/`parent_asin` are resolved at ingest (and re-tried while NULL) because the SKU map
    comes from `economics_daily`, which keeps only 90 days.
    """
    __tablename__ = "customer_order_lines"
    __table_args__ = (
        Index("idx_customer_order_lines_item", "amazon_order_id", "shipment_item_id", unique=True),
        Index("idx_customer_order_lines_day", "purchase_day"),
    )

    id = Column(Integer, primary_key=True)
    amazon_order_id = Column(String(30), nullable=False)
    shipment_item_id = Column(String(30), nullable=False)
    buyer_key = Column(String(32), nullable=False)
    #: IST calendar day of the PURCHASE, `YYYY-MM-DD`.
    purchase_day = Column(String(10), nullable=False)
    seller_sku = Column(String(80), nullable=False, default="", server_default="")
    child_asin = Column(String(10))
    parent_asin = Column(String(10))
    units = Column(Integer, nullable=False, default=0, server_default="0")
    fetched_at = Column(DateTime, default=datetime.utcnow)


class RepeatRefresh(Base):
    """One fetch of the shipments report. Done windows define what history is COVERED."""
    __tablename__ = "repeat_refresh"

    id = Column(Integer, primary_key=True)
    window_start = Column(String(10))
    window_end = Column(String(10))
    #: "done" | "failed"
    status = Column(String(12), nullable=False, default="done")
    rows_seen = Column(Integer, default=0)
    lines_stored = Column(Integer, default=0)
    skipped_no_date = Column(Integer, default=0)
    skipped_zero_price = Column(Integer, default=0)
    skipped_no_key = Column(Integer, default=0)
    unresolved_sku = Column(Integer, default=0)
    error = Column(Text)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)
```

- [ ] **Step 4: Migration** — `alembic/versions/a4c7e2f19b30_customer_order_lines.py`:

```python
"""customer_order_lines + repeat_refresh (Portfolio → Repeat customers)

Revision ID: a4c7e2f19b30
Revises: c3d8e1f5a702
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4c7e2f19b30"
down_revision: Union[str, None] = "c3d8e1f5a702"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "customer_order_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("amazon_order_id", sa.String(30), nullable=False),
        sa.Column("shipment_item_id", sa.String(30), nullable=False),
        sa.Column("buyer_key", sa.String(32), nullable=False),
        sa.Column("purchase_day", sa.String(10), nullable=False),
        sa.Column("seller_sku", sa.String(80), nullable=False, server_default=""),
        sa.Column("child_asin", sa.String(10)),
        sa.Column("parent_asin", sa.String(10)),
        sa.Column("units", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fetched_at", sa.DateTime()),
    )
    op.create_index("idx_customer_order_lines_item", "customer_order_lines",
                    ["amazon_order_id", "shipment_item_id"], unique=True)
    op.create_index("idx_customer_order_lines_day", "customer_order_lines", ["purchase_day"])
    op.create_table(
        "repeat_refresh",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("window_start", sa.String(10)),
        sa.Column("window_end", sa.String(10)),
        sa.Column("status", sa.String(12), nullable=False, server_default="done"),
        sa.Column("rows_seen", sa.Integer()),
        sa.Column("lines_stored", sa.Integer()),
        sa.Column("skipped_no_date", sa.Integer()),
        sa.Column("skipped_zero_price", sa.Integer()),
        sa.Column("skipped_no_key", sa.Integer()),
        sa.Column("unresolved_sku", sa.Integer()),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("repeat_refresh")
    op.drop_index("idx_customer_order_lines_day", table_name="customer_order_lines")
    op.drop_index("idx_customer_order_lines_item", table_name="customer_order_lines")
    op.drop_table("customer_order_lines")
```

- [ ] **Step 5: Deploy detector** — in `deploy/update-ec2.sh`, insert as the FIRST `elif` (above
  `"preferences_json" in cols("users")`):

```bash
elif "customer_order_lines" in tables:
    print("a4c7e2f19b30")                           # head: Repeat customers (FBA order lines)
```
  and change the following line's comment from `# head: per-login display preferences` to
  `# per-login display preferences`. Add `"customer_order_lines", "repeat_refresh"` to the
  required-tables set the post-migration check asserts (search the script for the set containing
  `"economics_daily"`).

- [ ] **Step 6: Repository** — `app/repeat/__init__.py`:

```python
"""Repeat customers: FBA order lines keyed to a hashed buyer, and the metrics read from them."""
```

`app/repeat/repository.py`:

```python
"""The only reads and writes of `customer_order_lines` and `repeat_refresh`."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CustomerOrderLine, EconomicsDaily, RepeatRefresh

_CHUNK = 400


async def save_lines(db: AsyncSession, lines: list[dict]) -> int:
    """Upsert by (order, shipment item): delete the matching rows, then insert. A re-read of an
    overlapping window corrects rather than doubles."""
    by_key = {(l["amazon_order_id"], l["shipment_item_id"]): l for l in lines}
    keys = list(by_key)
    for i in range(0, len(keys), _CHUNK):
        chunk = keys[i:i + _CHUNK]
        wanted = set(chunk)
        found = await db.execute(
            select(CustomerOrderLine.id, CustomerOrderLine.amazon_order_id,
                   CustomerOrderLine.shipment_item_id)
            .where(CustomerOrderLine.amazon_order_id.in_({k[0] for k in chunk})))
        ids = [r.id for r in found if (r.amazon_order_id, r.shipment_item_id) in wanted]
        if ids:
            await db.execute(delete(CustomerOrderLine).where(CustomerOrderLine.id.in_(ids)))
    db.add_all(CustomerOrderLine(**line) for line in by_key.values())
    await db.commit()
    return len(by_key)


async def load_lines(db: AsyncSession, since: str) -> list[dict]:
    rows = await db.execute(
        select(CustomerOrderLine.amazon_order_id, CustomerOrderLine.buyer_key,
               CustomerOrderLine.purchase_day, CustomerOrderLine.child_asin,
               CustomerOrderLine.parent_asin, CustomerOrderLine.units)
        .where(CustomerOrderLine.purchase_day >= since,
               CustomerOrderLine.parent_asin.is_not(None)))
    return [{"amazon_order_id": r[0], "buyer_key": r[1], "day": date.fromisoformat(r[2]),
             "child_asin": r[3], "parent_asin": r[4], "units": int(r[5] or 0)} for r in rows]


async def purge(db: AsyncSession, keep_from: str) -> int:
    result = await db.execute(
        delete(CustomerOrderLine).where(CustomerOrderLine.purchase_day < keep_from))
    await db.commit()
    return result.rowcount or 0


async def sku_map(db: AsyncSession, catalogue: dict) -> dict[str, tuple[str, str]]:
    """seller SKU → (child ASIN, parent ASIN). Economics MSKU rows first (Amazon's own pairing,
    the same route the Portfolio uses); the MRP sheet's FBA SKU column fills gaps."""
    parent_of: dict[str, str] = {}
    for child, parent in await db.execute(
            select(EconomicsDaily.child_asin, func.max(EconomicsDaily.parent_asin))
            .group_by(EconomicsDaily.child_asin)):
        parent_of[child] = parent or child
    mapping: dict[str, tuple[str, str]] = {}
    for sku, child in await db.execute(
            select(EconomicsDaily.seller_sku, func.max(EconomicsDaily.child_asin))
            .where(EconomicsDaily.seller_sku != "").group_by(EconomicsDaily.seller_sku)):
        mapping[sku.strip()] = (child, parent_of.get(child, child))
    for asin, rec in (catalogue or {}).items():
        sku = (rec.get("fba_sku") or "").strip()
        if sku and sku not in mapping:
            mapping[sku] = (asin, parent_of.get(asin, asin))
    return mapping


async def resolve_missing(db: AsyncSession, mapping: dict[str, tuple[str, str]]) -> int:
    rows = (await db.execute(
        select(CustomerOrderLine.id, CustomerOrderLine.seller_sku)
        .where(CustomerOrderLine.parent_asin.is_(None)))).all()
    fixed = 0
    for row_id, sku in rows:
        hit = mapping.get((sku or "").strip())
        if hit:
            await db.execute(update(CustomerOrderLine).where(CustomerOrderLine.id == row_id)
                             .values(child_asin=hit[0], parent_asin=hit[1]))
            fixed += 1
    await db.commit()
    return fixed


async def units_by_parent(db: AsyncSession, start: str, end: str):
    """(FBA units from our lines, ALL-channel units ordered from economics) per parent."""
    fba = dict((await db.execute(
        select(CustomerOrderLine.parent_asin, func.sum(CustomerOrderLine.units))
        .where(CustomerOrderLine.purchase_day.between(start, end),
               CustomerOrderLine.parent_asin.is_not(None))
        .group_by(CustomerOrderLine.parent_asin))).all())
    allc = dict((await db.execute(
        select(func.coalesce(EconomicsDaily.parent_asin, EconomicsDaily.child_asin),
               func.sum(EconomicsDaily.units_ordered))
        .where(EconomicsDaily.day.between(start, end), EconomicsDaily.seller_sku == "")
        .group_by(func.coalesce(EconomicsDaily.parent_asin, EconomicsDaily.child_asin)))).all())
    return ({k: int(v or 0) for k, v in fba.items()}, {k: int(v or 0) for k, v in allc.items()})


async def record_run(db: AsyncSession, **fields) -> None:
    db.add(RepeatRefresh(finished_at=datetime.utcnow(), **fields))
    await db.commit()


async def done_runs(db: AsyncSession) -> list[tuple[str, str]]:
    rows = await db.execute(select(RepeatRefresh.window_start, RepeatRefresh.window_end)
                            .where(RepeatRefresh.status == "done"))
    return [(a, b) for a, b in rows if a and b]


async def last_run(db: AsyncSession) -> dict | None:
    row = (await db.execute(select(RepeatRefresh).order_by(RepeatRefresh.id.desc()).limit(1))
           ).scalar_one_or_none()
    if not row:
        return None
    return {"window": [row.window_start, row.window_end], "status": row.status,
            "error": row.error, "lines_stored": row.lines_stored,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None}
```

  Check `EconomicsDaily.units_ordered` exists (`grep -n units_ordered app/models.py`); it does per
  CLAUDE.md. If the column has another name, use that name.

- [ ] **Step 7: Run** `venv/Scripts/python -m pytest tests/test_repeat_storage.py tests/test_schema_migrations.py -q` → PASS (the detector tests parametrise over the newest three revisions and now include `a4c7e2f19b30`).

- [ ] **Step 8: Commit**
```bash
git add app/models.py app/repeat alembic/versions/a4c7e2f19b30_customer_order_lines.py deploy/update-ec2.sh tests/test_repeat_storage.py
git commit -m "feat(repeat): customer_order_lines storage, migration and deploy detector branch"
```

---

### Task 2: The customer key — salted HMAC, salt created once

**Files:** Create `app/repeat/keys.py`; Test `tests/test_repeat_keys.py`

**Interfaces — Produces:** `keys.customer_key(email: str | None, salt: str) -> str | None`;
`keys.load_or_create_salt(db) -> str`; `keys.SALT_NAME = "customer_key_salt"`.

- [ ] **Step 1: Failing tests** — `tests/test_repeat_keys.py`

```python
import pytest

from app.repeat import keys

pytestmark = pytest.mark.regression


def test_the_key_ignores_case_and_surrounding_space():
    assert keys.customer_key(" AbC@marketplace.amazon.in ", "s") == keys.customer_key("abc@marketplace.amazon.in", "s")


def test_a_different_salt_gives_a_different_key():
    assert keys.customer_key("a@x", "s1") != keys.customer_key("a@x", "s2")


def test_blank_email_has_no_key():
    assert keys.customer_key("", "s") is None and keys.customer_key(None, "s") is None


def test_the_key_never_contains_the_email():
    k = keys.customer_key("abc123@marketplace.amazon.in", "s")
    assert len(k) == 32 and "abc123" not in k and "@" not in k


async def test_the_salt_is_created_ONCE_and_then_reused(db):
    first = await keys.load_or_create_salt(db)
    second = await keys.load_or_create_salt(db)
    assert first == second and len(first) >= 32
```

- [ ] **Step 2: Run** → FAIL (module missing).

- [ ] **Step 3: Implement** `app/repeat/keys.py`

```python
"""The customer key: a salted HMAC of Amazon's masked buyer email. The email itself is never kept.

Salted so a leaked database cannot be joined against any other list of Amazon relay addresses.
The salt is generated once and stored in `portfolio_settings`; a restored backup restores it too,
so keys stay stable. **Rotating it severs every customer's history** — do not.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PortfolioSettings

SALT_NAME = "customer_key_salt"


def customer_key(email: str | None, salt: str) -> str | None:
    normal = (email or "").strip().lower()
    if not normal:
        return None
    return hmac.new(salt.encode(), normal.encode(), hashlib.sha256).hexdigest()[:32]


async def load_or_create_salt(db: AsyncSession) -> str:
    row = (await db.execute(select(PortfolioSettings).where(PortfolioSettings.name == SALT_NAME))
           ).scalar_one_or_none()
    if row and row.value_json:
        return json.loads(row.value_json)["salt"]
    salt = secrets.token_hex(32)
    db.add(PortfolioSettings(name=SALT_NAME, value_json=json.dumps({"salt": salt}),
                             updated_by="repeat"))
    await db.commit()
    return salt
```

- [ ] **Step 4: Run** → PASS. Also run `tests/test_portfolio_*` settings tests to confirm the
  thresholds loader ignores the new row name (it filters on its own `SETTINGS_NAME`).

- [ ] **Step 5: Commit** `git commit -m "feat(repeat): salted customer key, salt created once"`

---

### Task 3: Report client and parser

**Files:** Create `app/repeat/fetch.py`, `app/repeat/parse.py`; Tests `tests/test_repeat_parse.py`, `tests/test_repeat_fetch.py`

**Interfaces — Produces:**
- `parse.parse_rows(rows: list[dict], salt: str, mapping: dict[str, tuple[str,str]]) -> tuple[list[dict], dict]` — lines ready for `save_lines`, and counters `{rows_seen, skipped_no_date, skipped_zero_price, skipped_no_key, unresolved_sku}`.
- `fetch.split_days(start: date, end: date, size: int = 30) -> list[tuple[date, date]]` (inclusive ends)
- `fetch.fetch_rows(start: date, end: date, *, client, sleep=asyncio.sleep) -> list[dict]`
- `fetch.ReportFailed(Exception)`

- [ ] **Step 1: Failing parser tests** — `tests/test_repeat_parse.py`

```python
"""Report rows → lines. Column names are the REAL report's (probed 06 Oct 2026)."""
import pytest

from app.repeat import keys
from app.repeat.parse import parse_rows

pytestmark = pytest.mark.regression
MAP = {"1kg cs FBA": ("B0CHILD001", "B0PARENT01")}


def _row(**over):
    row = {"amazon-order-id": "171-1", "shipment-item-id": "D1", "purchase-date": "2026-09-30T20:00:00+00:00",
           "buyer-email": "abc@marketplace.amazon.in", "sku": "1kg cs FBA", "quantity-shipped": "2",
           "item-price": "180.00", "buyer-name": "", "ship-postal-code": "800001"}
    row.update(over)
    return row


def test_a_purchase_after_1830_utc_lands_on_the_NEXT_ist_day():
    lines, _ = parse_rows([_row()], "s", MAP)
    assert lines[0]["purchase_day"] == "2026-10-01"


def test_the_line_carries_the_hashed_key_and_resolved_asins_and_no_pii():
    lines, _ = parse_rows([_row()], "s", MAP)
    line = lines[0]
    assert line["buyer_key"] == keys.customer_key("abc@marketplace.amazon.in", "s")
    assert (line["child_asin"], line["parent_asin"], line["units"]) == ("B0CHILD001", "B0PARENT01", 2)
    assert "abc" not in repr(line) and "800001" not in repr(line)


@pytest.mark.parametrize("over, counter", [
    ({"purchase-date": ""}, "skipped_no_date"),
    ({"item-price": "0.00"}, "skipped_zero_price"),
    ({"buyer-email": ""}, "skipped_no_key"),
])
def test_unusable_rows_are_skipped_AND_counted(over, counter):
    lines, counts = parse_rows([_row(**over)], "s", MAP)
    assert lines == [] and counts[counter] == 1 and counts["rows_seen"] == 1


def test_an_unknown_sku_is_KEPT_unresolved_and_counted():
    lines, counts = parse_rows([_row(sku="new FBA")], "s", MAP)
    assert lines[0]["parent_asin"] is None and counts["unresolved_sku"] == 1


def test_a_blank_price_is_not_treated_as_a_replacement():
    lines, _ = parse_rows([_row(**{"item-price": ""})], "s", MAP)
    assert len(lines) == 1


def test_a_missing_shipment_item_id_falls_back_to_the_order_item_id():
    row = _row(); row.pop("shipment-item-id"); row["amazon-order-item-id"] = "OI9"
    assert parse_rows([row], "s", MAP)[0][0]["shipment_item_id"] == "OI9"
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** `app/repeat/parse.py`

```python
"""FBA shipments report rows → `customer_order_lines` rows. Pure: no I/O."""
from __future__ import annotations

from datetime import datetime, timezone

from app import ist
from app.repeat.keys import customer_key


def _ist_day(stamp: str) -> str:
    when = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    if when.tzinfo:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return ist.day_of(when)


def _is_free(price: str | None) -> bool:
    """A free replacement ships at 0. Blank means unknown, which is NOT free."""
    try:
        return (price or "").strip() != "" and float(price) == 0.0
    except ValueError:
        return False


def parse_rows(rows, salt, mapping):
    counts = {"rows_seen": 0, "skipped_no_date": 0, "skipped_zero_price": 0,
              "skipped_no_key": 0, "unresolved_sku": 0}
    lines = []
    for row in rows:
        counts["rows_seen"] += 1
        if not (row.get("purchase-date") or "").strip():
            counts["skipped_no_date"] += 1
            continue
        if _is_free(row.get("item-price")):
            counts["skipped_zero_price"] += 1
            continue
        key = customer_key(row.get("buyer-email"), salt)
        if not key:
            counts["skipped_no_key"] += 1
            continue
        sku = (row.get("sku") or "").strip()
        child, parent = mapping.get(sku, (None, None))
        if parent is None:
            counts["unresolved_sku"] += 1
        lines.append({
            "amazon_order_id": row["amazon-order-id"].strip(),
            "shipment_item_id": (row.get("shipment-item-id") or row.get("amazon-order-item-id") or "").strip(),
            "buyer_key": key,
            "purchase_day": _ist_day(row["purchase-date"]),
            "seller_sku": sku,
            "child_asin": child,
            "parent_asin": parent,
            "units": int(float(row.get("quantity-shipped") or 0)),
        })
    return lines, counts
```

- [ ] **Step 4: Failing fetch tests** — `tests/test_repeat_fetch.py`

```python
import gzip
from datetime import date

import pytest

from app.repeat import fetch

pytestmark = pytest.mark.regression


def test_split_days_never_exceeds_30_and_covers_every_day_once():
    parts = fetch.split_days(date(2026, 1, 1), date(2026, 3, 15))
    assert all((b - a).days + 1 <= 30 for a, b in parts)
    days = [a.toordinal() + i for a, b in parts for i in range((b - a).days + 1)]
    assert days == list(range(date(2026, 1, 1).toordinal(), date(2026, 3, 15).toordinal() + 1))


async def test_fetch_creates_polls_and_parses_a_gzipped_tsv(monkeypatch):
    calls = []
    tsv = "amazon-order-id\tsku\n171-1\t1kg cs FBA\n".encode()

    async def fake_post(path, body=None, client=None, method="POST"):
        calls.append(("POST", path, body)); return {"reportId": "R1"}

    states = iter([{"processingStatus": "IN_QUEUE"}, {"processingStatus": "DONE", "reportDocumentId": "D1"}])

    async def fake_get(path, params=None, client=None):
        calls.append(("GET", path, params))
        if path.endswith("/reports/R1"):
            return next(states)
        return {"url": "https://s3/doc", "compressionAlgorithm": "GZIP"}

    class Resp:  # the pre-signed S3 download
        content = gzip.compress(tsv)

    class Client:
        async def get(self, url):
            assert "x-amz-access-token" not in repr(url)
            return Resp()

    async def no_sleep(_): pass
    monkeypatch.setattr(fetch.spapi, "_post", fake_post)
    monkeypatch.setattr(fetch.spapi, "_get", fake_get)
    rows = await fetch.fetch_rows(date(2026, 9, 1), date(2026, 9, 7), client=Client(), sleep=no_sleep)
    assert rows == [{"amazon-order-id": "171-1", "sku": "1kg cs FBA"}]
    body = calls[0][2]
    assert body["reportType"] == "GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL"
    assert body["dataStartTime"] == "2026-08-31T18:30Z"      # midnight IST on 1 Sep
    assert body["dataEndTime"] == "2026-09-07T18:30Z"        # midnight IST ending 7 Sep


async def test_a_window_over_30_days_is_refused_before_any_call():
    with pytest.raises(ValueError):
        await fetch.fetch_rows(date(2026, 1, 1), date(2026, 2, 15), client=None)


async def test_a_FATAL_report_raises(monkeypatch):
    async def fake_post(*a, **k): return {"reportId": "R1"}
    async def fake_get(*a, **k): return {"processingStatus": "FATAL"}
    async def no_sleep(_): pass
    monkeypatch.setattr(fetch.spapi, "_post", fake_post)
    monkeypatch.setattr(fetch.spapi, "_get", fake_get)
    with pytest.raises(fetch.ReportFailed):
        await fetch.fetch_rows(date(2026, 9, 1), date(2026, 9, 2), client=object(), sleep=no_sleep)
```

- [ ] **Step 5: Implement** `app/repeat/fetch.py`

```python
"""SP-API Reports: `GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL`, create → poll → download.

Measured: a 90-day request is FATAL, 30 days works; a 30-day chunk takes 30–40 minutes because the
queue is SERIAL per report type and shared with another requester on this account. So the nightly
job asks for 7 days and the backfill walks 30-day chunks, storing each as it lands.
The pre-signed S3 download carries NO SP-API headers, on purpose.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import io
from datetime import date, timedelta

from app import ist
from app.config import get_settings
from app.shipment import spapi

REPORT_TYPE = "GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL"
REPORTS = "/reports/2021-06-30"
MAX_REPORT_DAYS = 30
POLL_INTERVAL = 30.0
POLL_MAX = 240          # 2 hours: the queue has been measured at 40+ minutes behind other reports


class ReportFailed(Exception):
    pass


def split_days(start: date, end: date, size: int = MAX_REPORT_DAYS) -> list[tuple[date, date]]:
    out, cur = [], start
    while cur <= end:
        stop = min(end, cur + timedelta(days=size - 1))
        out.append((cur, stop))
        cur = stop + timedelta(days=1)
    return out


async def fetch_rows(start: date, end: date, *, client, sleep=asyncio.sleep) -> list[dict]:
    if (end - start).days + 1 > MAX_REPORT_DAYS:
        raise ValueError(f"{start}..{end} is over {MAX_REPORT_DAYS} days; Amazon refuses it")
    created = await spapi._post(f"{REPORTS}/reports", {
        "reportType": REPORT_TYPE,
        "marketplaceIds": [get_settings().sp_api_marketplace_id],
        "dataStartTime": ist.utc_instant(start),
        "dataEndTime": ist.utc_instant(end + timedelta(days=1)),
    }, client=client)
    report_id = created["reportId"]
    for _ in range(POLL_MAX):
        status = await spapi._get(f"{REPORTS}/reports/{report_id}", client=client)
        state = status.get("processingStatus")
        if state == "DONE":
            return await _download(status["reportDocumentId"], client)
        if state in ("FATAL", "CANCELLED"):
            raise ReportFailed(f"Amazon reported {state} for {start}..{end}")
        await sleep(POLL_INTERVAL)
    raise ReportFailed(f"report for {start}..{end} still not ready after {POLL_MAX} polls")


async def _download(document_id: str, client) -> list[dict]:
    doc = await spapi._get(f"{REPORTS}/documents/{document_id}", client=client)
    raw = (await client.get(doc["url"])).content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", "replace")
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))
```

  Note: `ist.utc_instant(date(2026,9,1))` must return `"2026-08-31T18:30Z"`; if its format
  differs (e.g. seconds), adjust the test's expected strings to the helper's real output rather
  than the helper.

- [ ] **Step 6: Run** both test files → PASS. **Step 7: Commit**
  `git commit -m "feat(repeat): FBA shipments report client and parser"`

---

### Task 4: The calculation — one pure function, hand-built histories

**Files:** Create `app/repeat/logic.py`; Test `tests/test_repeat_logic.py`

**Interfaces — Produces:**
- `logic.WINDOWS = (30, 60, 90)`, `PERIOD_DAYS = 30`, `MIN_COHORT = 20`, `FLOW_TOP = 8`, `BASKET_DAYS = 90`
- `logic.Order(order_id, buyer, day, parents: frozenset)`; `logic.build_orders(lines) -> list[Order]`
- `logic.period(n, as_of) -> (date, date)`; `logic.window_status(n, as_of, history_from) -> (bool, str|None)`
- `logic.metrics(orders, brand_of: dict[parent, brand], as_of: date, history_from: date|None) -> dict` with keys
  `windows[n] = {period, available, reason}`, `parents[p][n] = {buyers, same, came_from, went_on}`,
  `brands[b][n] = {buyers, repeat}`, `flows[p][n] = {came_from: [(q, customers)], went_on: [...]}`,
  `basket[p] = {orders, multi, with: [(q, orders)]}`
- `logic.pct(part, whole) -> float | None`; `logic.covered_from(runs, until) -> date | None`;
  `logic.name_parents(children: dict[parent, list[name]]) -> dict[parent, name]`

**Definitions (copy into the module docstring):** for window N the cohort period is the 30 days
ending `as_of − N`. A customer joins X's cohort on their first purchase of X in that period
(the anchor). **same** = an order containing X on a day in (anchor, anchor+N]. **went_on** = an order
containing another product in (anchor, anchor+N]. **came_from** = an order containing another product
on a day in [anchor−N, anchor). Brand: anchor = first purchase of ANY product of brand B in the period;
**repeat** = any later-day order of a B product within N days. A window is unavailable when history
does not reach `period_start − N` (came_from looks back N days).

- [ ] **Step 1: Failing tests** — `tests/test_repeat_logic.py`

```python
"""Hand-built customer histories with known answers. Values chosen UNEQUAL so a wrong field fails."""
from datetime import date, timedelta

import pytest

from app.repeat import logic

pytestmark = pytest.mark.regression
AS_OF = date(2026, 10, 1)
P30 = logic.period(30, AS_OF)[0]          # 2026-08-03: first day of the 30-day cohort period
HIST = date(2025, 1, 1)
BRAND = {"CS": "Mithila Foods", "JS": "Mithila Foods", "HF": "Howrah Foods"}


def orders(*specs):
    """spec = (buyer, day offset from P30, products...) → one order each."""
    lines = []
    for i, (buyer, off, *prods) in enumerate(specs):
        for p in prods:
            lines.append({"amazon_order_id": f"o{i}", "buyer_key": buyer,
                          "day": P30 + timedelta(days=off), "parent_asin": p})
    return logic.build_orders(lines)


def run(*specs, history=HIST):
    return logic.metrics(orders(*specs), BRAND, AS_OF, history)


def test_the_cohort_period_is_the_30_days_ending_N_days_before_as_of():
    assert logic.period(30, AS_OF) == (date(2026, 8, 3), date(2026, 9, 1))
    assert logic.period(90, AS_OF) == (date(2026, 6, 4), date(2026, 7, 3))


def test_a_reorder_on_day_20_is_a_30_day_repeat():
    m = run(("a", 0, "CS"), ("a", 20, "CS"))
    assert m["parents"]["CS"][30] == {"buyers": 1, "same": 1, "came_from": 0, "went_on": 0}


def test_a_reorder_on_day_31_is_NOT_a_30_day_repeat():
    m = run(("a", 0, "CS"), ("a", 31, "CS"))
    assert m["parents"]["CS"][30]["same"] == 0


def test_a_second_order_the_SAME_day_is_not_a_repeat():
    m = run(("a", 0, "CS"), ("a", 0, "CS"))
    assert m["parents"]["CS"][30]["same"] == 0


def test_the_anchor_is_the_FIRST_purchase_in_the_period_so_one_customer_counts_once():
    m = run(("a", 0, "CS"), ("a", 5, "CS"), ("a", 10, "CS"))
    assert m["parents"]["CS"][30]["buyers"] == 1 and m["parents"]["CS"][30]["same"] == 1


def test_chana_then_jau_is_a_CAME_FROM_on_jau_and_a_WENT_ON_on_chana():
    m = run(("a", 0, "CS"), ("a", 12, "JS"))
    assert m["parents"]["JS"][30]["came_from"] == 1 and m["parents"]["JS"][30]["same"] == 0
    assert m["parents"]["CS"][30]["went_on"] == 1
    assert m["flows"]["JS"][30]["came_from"] == [("CS", 1)]
    assert m["flows"]["CS"][30]["went_on"] == [("JS", 1)]


def test_two_products_in_ONE_order_are_a_basket_not_a_cross_flow():
    m = run(("a", 0, "CS", "JS"))
    assert m["parents"]["JS"][30]["came_from"] == 0 and m["parents"]["CS"][30]["went_on"] == 0
    assert m["basket"]["CS"]["with"] == [("JS", 1)] and m["basket"]["CS"]["multi"] == 1


def test_the_brand_total_counts_UNIQUE_customers_not_the_sum_of_rows():
    m = run(("a", 0, "CS"), ("a", 1, "JS"), ("b", 2, "CS"))
    rows = m["parents"]["CS"][30]["buyers"] + m["parents"]["JS"][30]["buyers"]
    assert rows == 3 and m["brands"]["Mithila Foods"][30]["buyers"] == 2
    assert m["brands"]["Mithila Foods"][30]["repeat"] == 1        # a came back (day 1) within 30


def test_a_different_brand_does_not_count_as_a_brand_repeat():
    m = run(("a", 0, "CS"), ("a", 5, "HF"))
    assert m["brands"]["Mithila Foods"][30]["repeat"] == 0


def test_a_purchase_outside_the_period_does_not_join_the_cohort():
    m = run(("a", -1, "CS"), ("a", 30, "CS"))   # day before and day after the 30-day period
    assert "CS" not in m["parents"] or 30 not in m["parents"]["CS"]


def test_a_window_without_enough_history_is_UNAVAILABLE_with_the_reason():
    m = run(("a", 0, "CS"), history=date(2026, 7, 1))
    assert m["windows"][30]["available"] is True
    assert m["windows"][90]["available"] is False
    assert "2026-03-06" in m["windows"][90]["reason"]           # 90-day period start minus 90


def test_pct_is_None_below_the_minimum_cohort_and_a_ratio_above():
    assert logic.pct(5, logic.MIN_COHORT - 1) is None
    assert logic.pct(5, 50) == 0.1


def test_flows_are_ordered_by_count_then_id_so_two_renders_agree():
    specs = [("a", 0, "CS"), ("a", 3, "JS"), ("b", 0, "CS"), ("b", 3, "HF"), ("c", 0, "CS"), ("c", 3, "HF")]
    assert run(*specs)["flows"]["CS"][30]["went_on"] == [("HF", 2), ("JS", 1)]


def test_covered_from_merges_adjacent_windows_and_stops_at_a_gap():
    runs = [("2026-01-01", "2026-01-30"), ("2026-01-31", "2026-03-01"), ("2026-03-10", "2026-10-01")]
    assert logic.covered_from(runs, date(2026, 9, 1)) == date(2026, 3, 10)
    assert logic.covered_from(runs[:2], date(2026, 2, 15)) == date(2026, 1, 1)
    assert logic.covered_from(runs, date(2026, 3, 5)) is None


def test_name_parents_uses_the_shared_name_and_disambiguates_a_DERIVED_collision():
    names = logic.name_parents({
        "P1": ["Peri Peri Roasted Chana", "Nimbu Pudina Roasted Chana"],
        "P2": ["Roasted Chana"],
        "P3": ["Chana Sattu"],
    })
    assert names == {"P1": "Roasted Chana (2 flavours)", "P2": "Roasted Chana", "P3": "Chana Sattu"}
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** `app/repeat/logic.py`

```python
"""Repeat-customer metrics. Pure: order lines in, counts out. Every figure on the tab comes from here.

<paste the Definitions paragraph from the plan here verbatim>
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

from app.portfolio.logic import family_label

WINDOWS = (30, 60, 90)
PERIOD_DAYS = 30
#: Below this many buyers a percentage is noise; the screen shows a dash and the count.
MIN_COHORT = 20
FLOW_TOP = 8
BASKET_DAYS = 90


@dataclass(frozen=True)
class Order:
    order_id: str
    buyer: str
    day: date
    parents: frozenset


def build_orders(lines) -> list[Order]:
    acc: dict[str, list] = {}
    for line in lines:
        if not line.get("parent_asin"):
            continue
        o = acc.setdefault(line["amazon_order_id"], [line["buyer_key"], line["day"], set()])
        o[2].add(line["parent_asin"])
        o[1] = min(o[1], line["day"])
    return [Order(k, b, d, frozenset(p)) for k, (b, d, p) in acc.items()]


def period(n: int, as_of: date) -> tuple[date, date]:
    end = as_of - timedelta(days=n)
    return end - timedelta(days=PERIOD_DAYS - 1), end


def window_status(n: int, as_of: date, history_from: date | None) -> tuple[bool, str | None]:
    need = period(n, as_of)[0] - timedelta(days=n)
    if history_from is None or history_from > need:
        return False, f"needs order history from {need.isoformat()}"
    return True, None


def pct(part: int, whole: int) -> float | None:
    if whole < MIN_COHORT:
        return None
    return part / whole


def _top(counter: Counter) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:FLOW_TOP]


def metrics(orders: Sequence[Order], brand_of: Mapping[str, str], as_of: date,
            history_from: date | None) -> dict:
    by_buyer: dict[str, list[Order]] = defaultdict(list)
    for o in orders:
        by_buyer[o.buyer].append(o)
    for history in by_buyer.values():
        history.sort(key=lambda o: (o.day, o.order_id))
    out = {"windows": {}, "parents": defaultdict(dict), "brands": defaultdict(dict),
           "flows": defaultdict(dict)}
    for n in WINDOWS:
        start, end = period(n, as_of)
        ok, reason = window_status(n, as_of, history_from)
        out["windows"][n] = {"period": (start, end), "available": ok, "reason": reason}
        if not ok:
            continue
        span = timedelta(days=n)
        counts = defaultdict(lambda: {"buyers": 0, "same": 0, "came_from": 0, "went_on": 0})
        came, went = defaultdict(Counter), defaultdict(Counter)
        brand_counts = defaultdict(lambda: {"buyers": 0, "repeat": 0})
        for history in by_buyer.values():
            in_period = [o for o in history if start <= o.day <= end]
            if not in_period:
                continue
            anchors: dict[str, date] = {}
            for o in in_period:
                for p in o.parents:
                    anchors.setdefault(p, o.day)
            for p, anchor in anchors.items():
                c = counts[p]
                c["buyers"] += 1
                after = [o for o in history if anchor < o.day <= anchor + span]
                before = [o for o in history if anchor - span <= o.day < anchor]
                if any(p in o.parents for o in after):
                    c["same"] += 1
                dest = {q for o in after for q in o.parents if q != p}
                src = {q for o in before for q in o.parents if q != p}
                if dest:
                    c["went_on"] += 1
                    went[p].update(dest)
                if src:
                    c["came_from"] += 1
                    came[p].update(src)
            brand_anchor: dict[str, date] = {}
            for o in in_period:
                for p in o.parents:
                    if brand_of.get(p):
                        brand_anchor.setdefault(brand_of[p], o.day)
            for b, anchor in brand_anchor.items():
                bc = brand_counts[b]
                bc["buyers"] += 1
                if any(anchor < o.day <= anchor + span and any(brand_of.get(q) == b for q in o.parents)
                       for o in history):
                    bc["repeat"] += 1
        for p, c in counts.items():
            out["parents"][p][n] = dict(c)
            out["flows"][p][n] = {"came_from": _top(came[p]), "went_on": _top(went[p])}
        for b, bc in brand_counts.items():
            out["brands"][b][n] = dict(bc)
    first = as_of - timedelta(days=BASKET_DAYS - 1)
    basket = defaultdict(lambda: {"orders": 0, "multi": 0, "with": Counter()})
    for o in orders:
        if not first <= o.day <= as_of:
            continue
        for p in o.parents:
            k = basket[p]
            k["orders"] += 1
            others = o.parents - {p}
            if others:
                k["multi"] += 1
                k["with"].update(others)
    out["basket"] = {p: {"orders": k["orders"], "multi": k["multi"], "with": _top(k["with"])}
                     for p, k in basket.items()}
    return out


def covered_from(runs: Iterable[tuple[str, str]], until: date) -> date | None:
    """Start of the CONTIGUOUS stored history that reaches `until`, from the done fetch windows."""
    merged: list[list[date]] = []
    for a, b in sorted((date.fromisoformat(a), date.fromisoformat(b)) for a, b in runs):
        if merged and a <= merged[-1][1] + timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    for a, b in merged:
        if a <= until <= b:
            return a
    return None


def name_parents(children: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """The Profit view's naming rule: children BIGGEST SELLER FIRST; one flavour keeps its name,
    several get `family_label`, and only a DERIVED name that collides gets "(N flavours)"."""
    names, derived = {}, {}
    for parent, child_names in children.items():
        distinct = list(dict.fromkeys(n for n in child_names if n))
        flavours = list(dict.fromkeys(n.casefold() for n in distinct))
        if len(flavours) > 1:
            names[parent], derived[parent] = family_label(distinct), len(flavours)
        else:
            names[parent] = distinct[0] if distinct else parent
    taken = Counter(names.values())
    for parent, count in derived.items():
        if taken[names[parent]] > 1:
            names[parent] = f"{names[parent]} ({count} flavours)"
    return names
```

  `flavour_groups` in Portfolio groups sizes by product name; if the parity test in Task 6 shows
  it groups differently from `casefold()` distinctness, change `flavours` to match it rather than
  the test.

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `git commit -m "feat(repeat): repeat, cross-flow and basket metrics (pure)"`

---

### Task 5: An independent reference implementation, compared on random histories

The owner asked for data that is "tested well". Hand-built cases prove the definitions; this proves
the optimised code agrees with a deliberately naive one on thousands of shapes nobody thought of.

**Files:** Test `tests/test_repeat_reference.py`

- [ ] **Step 1: Write the test** (it should PASS immediately; then Step 2 proves it can fail)

```python
"""A naive day-by-day re-statement of the definitions, compared with logic.metrics on random data."""
import random
from datetime import date, timedelta

import pytest

from app.repeat import logic

pytestmark = pytest.mark.regression
AS_OF = date(2026, 10, 1)
PRODUCTS = ["CS", "JS", "RC", "HF"]
BRAND = {"CS": "M", "JS": "M", "RC": "M", "HF": "H"}


def naive(lines, n):
    """Per (buyer, product): walk every day explicitly. No shared code with logic.metrics."""
    start, end = AS_OF - timedelta(days=n + 29), AS_OF - timedelta(days=n)
    bought = {}                                   # (buyer, day) -> set of products, per ORDER kept apart
    for l in lines:
        bought.setdefault((l["buyer_key"], l["day"]), {}).setdefault(l["amazon_order_id"], set()).add(l["parent_asin"])
    buyers = {l["buyer_key"] for l in lines}
    res = {}
    for p in PRODUCTS:
        b = s = c = w = 0
        for buyer in buyers:
            d, anchor = start, None
            while d <= end and anchor is None:
                if any(p in ps for ps in bought.get((buyer, d), {}).values()):
                    anchor = d
                d += timedelta(days=1)
            if anchor is None:
                continue
            b += 1
            later = [ps for k in range(1, n + 1) for ps in bought.get((buyer, anchor + timedelta(days=k)), {}).values()]
            earlier = [ps for k in range(1, n + 1) for ps in bought.get((buyer, anchor - timedelta(days=k)), {}).values()]
            s += any(p in ps for ps in later)
            w += any(ps - {p} for ps in later)
            c += any(ps - {p} for ps in earlier)
        if b:
            res[p] = {"buyers": b, "same": s, "came_from": c, "went_on": w}
    return res


@pytest.mark.parametrize("seed", range(40))
def test_metrics_agree_with_the_naive_restatement(seed):
    rnd = random.Random(seed)
    lines = []
    for i in range(rnd.randint(50, 400)):
        buyer = f"b{rnd.randint(0, 60)}"
        day = AS_OF - timedelta(days=rnd.randint(0, 260))
        for p in rnd.sample(PRODUCTS, rnd.choice([1, 1, 1, 2])):
            lines.append({"amazon_order_id": f"o{i}", "buyer_key": buyer, "day": day, "parent_asin": p})
    m = logic.metrics(logic.build_orders(lines), BRAND, AS_OF, date(2025, 1, 1))
    for n in logic.WINDOWS:
        got = {p: w[n] for p, w in m["parents"].items() if n in w}
        assert got == naive(lines, n), f"seed {seed}, window {n}"
```

- [ ] **Step 2: Prove it can fail.** Temporarily change `anchor < o.day <= anchor + span` to
  `anchor <= o.day <= anchor + span` in `logic.metrics`, run
  `venv/Scripts/python -m pytest tests/test_repeat_reference.py -q` → FAIL; revert; → PASS.

- [ ] **Step 3: Commit** `git commit -m "test(repeat): naive reference implementation on 40 random histories"`

---

### Task 6: Service, names parity, API routes

**Files:** Create `app/repeat/service.py`, `app/routers/repeat.py`; Modify `app/main.py`;
Tests `tests/test_repeat_api.py`

**Interfaces:**
- Consumes: Task 1 repository, Task 4 logic.
- Produces: `service.build_payload(db, catalogue, brand: str | None, today: date) -> dict`:

```
{"as_of": "YYYY-MM-DD"|None, "history_from": ..., "brand": "Mithila Foods", "brands": [..],
 "windows": {"30": {"period": [s, e], "available": bool, "reason": str|None}, ...},
 "total": {"30": {"buyers": int, "repeat_pct": float|None}, ...},
 "rows": [{"parent_asin", "product", "brand", "fba_share": float|None,
           "w": {"30": {"buyers", "same_pct", "came_from_pct", "went_on_pct"}, ...},
           "flows": {"30": {"came_from": [{"product", "customers"}], "went_on": [...]}, ...},
           "basket": {"orders", "multi_pct", "with": [{"product", "orders"}]}}],
 "last_refresh": {...}|None, "min_cohort": 20, "fba_partial_below": 0.6}
```
- Routes: `GET /portfolio/repeat?brand=` → payload; `POST /portfolio/repeat/refresh` → starts
  `refresh.run_incremental` in the background (Task 7), 409 if running; `GET /portfolio/repeat/refresh-status` → `refresh.STATE`.
- `as_of` = min(latest done window end, `ist.yesterday()`) − `SHIP_LAG_DAYS (3)`;
  `history_from` = `logic.covered_from(done_runs, as_of)`.

- [ ] **Step 1: Failing tests** — `tests/test_repeat_api.py`

```python
from datetime import date, timedelta

import pytest

from app import ist
from app.repeat import repository

pytestmark = pytest.mark.regression


async def _seed(db, buyers=25):
    as_of = ist.yesterday() - timedelta(days=3)
    start30 = as_of - timedelta(days=59)
    lines = []
    for i in range(buyers):
        lines.append({"amazon_order_id": f"a{i}", "shipment_item_id": "1", "buyer_key": f"k{i}",
                      "purchase_day": start30.isoformat(), "seller_sku": "s", "child_asin": "B0CHILD001",
                      "parent_asin": "B0PARENT01", "units": 1})
        if i < 5:   # 5 of 25 come back on day 10 -> 20%
            lines.append({**lines[-1], "amazon_order_id": f"b{i}",
                          "purchase_day": (start30 + timedelta(days=10)).isoformat()})
    await repository.save_lines(db, lines)
    await repository.record_run(db, window_start=(as_of - timedelta(days=400)).isoformat(),
                                window_end=(as_of + timedelta(days=3)).isoformat(), status="done")


@pytest.fixture
def catalogue(monkeypatch):
    cat = {"B0CHILD001": {"name": "Chana Sattu", "brand": "Mithila Foods", "fba_sku": "s"}}

    async def fake():
        return cat, None, "sheet"
    monkeypatch.setattr("app.repeat.service.load_catalogue", fake)
    return cat


async def test_the_route_returns_the_30_day_same_repeat_from_counts(auth_client, db, catalogue):
    await _seed(db)
    data = (await auth_client.get("/portfolio/repeat")).json()
    row = next(r for r in data["rows"] if r["parent_asin"] == "B0PARENT01")
    assert row["product"] == "Chana Sattu" and row["w"]["30"]["buyers"] == 25
    assert row["w"]["30"]["same_pct"] == pytest.approx(0.2)
    assert data["total"]["30"]["repeat_pct"] == pytest.approx(0.2)
    assert data["brand"] == "Mithila Foods"


async def test_a_small_cohort_returns_None_not_zero(auth_client, db, catalogue):
    await _seed(db, buyers=10)
    row = (await auth_client.get("/portfolio/repeat")).json()["rows"][0]
    assert row["w"]["30"]["buyers"] == 10 and row["w"]["30"]["same_pct"] is None


async def test_nothing_stored_yet_says_so_instead_of_zeros(auth_client, catalogue):
    data = (await auth_client.get("/portfolio/repeat")).json()
    assert data["rows"] == [] and data["as_of"] is None


async def test_the_payload_never_carries_a_buyer_key(auth_client, db, catalogue):
    await _seed(db)
    assert "k1" not in (await auth_client.get("/portfolio/repeat")).text


async def test_the_route_needs_the_portfolio_area(client):
    r = await client.get("/portfolio/repeat", follow_redirects=False)
    assert r.status_code in (302, 303, 401)


async def test_names_match_the_profit_view_for_the_same_children():
    from app.portfolio import logic as pf
    from app.repeat import logic
    from tests.test_portfolio_api import ECON_ROWS, CATALOGUE   # use whatever the shared fixture exports
    data = pf.portfolio(ECON_ROWS, CATALOGUE, {})
    children = {p["parent_asin"]: [s["product"] for s in p["sizes"]] for p in data["parents"]}
    names = logic.name_parents(children)
    assert names == {p["parent_asin"]: p["product"] for p in data["parents"]}
```

  For the parity test, open `tests/test_portfolio_api.py`, find how `_seed_snapshot` builds its
  economics rows and catalogue, and import or rebuild those exact objects (if they are not
  module-level, copy the builder into this test). Include at least one multi-flavour parent.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** `app/repeat/service.py`

```python
"""Assemble the Repeat tab's payload. Reads stored rows only; never calls Amazon."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from app.repeat import logic, repository
from app.shipment.catalogue import load_catalogue

SHIP_LAG_DAYS = 3          # an order bought on day D can ship D+1..D+3; newer days are incomplete
FBA_PARTIAL_BELOW = 0.6
DEFAULT_BRAND = "Mithila Foods"


async def build_payload(db, brand: str | None, today: date) -> dict:
    catalogue, _, _ = await load_catalogue()
    runs = await repository.done_runs(db)
    last = await repository.last_run(db)
    if not runs:
        return {"as_of": None, "history_from": None, "brand": brand or DEFAULT_BRAND, "brands": [],
                "windows": {}, "total": {}, "rows": [], "last_refresh": last,
                "min_cohort": logic.MIN_COHORT, "fba_partial_below": FBA_PARTIAL_BELOW}
    latest = min(max(date.fromisoformat(b) for _, b in runs), today - timedelta(days=1))
    as_of = latest - timedelta(days=SHIP_LAG_DAYS)
    history_from = logic.covered_from(runs, as_of)
    since = (as_of - timedelta(days=max(logic.WINDOWS) * 2 + logic.PERIOD_DAYS)).isoformat()
    lines = await repository.load_lines(db, since)

    children, brand_of = defaultdict(dict), {}
    for line in lines:
        rec = catalogue.get(line["child_asin"]) or {}
        children[line["parent_asin"]][line["child_asin"]] = children[line["parent_asin"]].get(line["child_asin"], 0) + line["units"]
        if rec.get("brand"):
            brand_of.setdefault(line["parent_asin"], rec["brand"])
    names = logic.name_parents({
        p: [(catalogue.get(c) or {}).get("name") or c for c, _ in sorted(kids.items(), key=lambda kv: -kv[1])]
        for p, kids in children.items()})
    brand = brand or DEFAULT_BRAND
    m = logic.metrics(logic.build_orders(lines), brand_of, as_of, history_from)
    fba, allc = await repository.units_by_parent(
        db, (as_of - timedelta(days=89)).isoformat(), as_of.isoformat())

    def label(p):
        return names.get(p, p)

    rows = []
    for p, per in m["parents"].items():
        if brand_of.get(p) != brand:
            continue
        w = {str(n): {"buyers": c["buyers"], "same_pct": logic.pct(c["same"], c["buyers"]),
                      "came_from_pct": logic.pct(c["came_from"], c["buyers"]),
                      "went_on_pct": logic.pct(c["went_on"], c["buyers"])} for n, c in per.items()}
        flows = {str(n): {k: [{"product": label(q), "customers": v} for q, v in f[k]]
                          for k in ("came_from", "went_on")} for n, f in m["flows"][p].items()}
        b = m["basket"].get(p, {"orders": 0, "multi": 0, "with": []})
        rows.append({
            "parent_asin": p, "product": label(p), "brand": brand_of.get(p, ""),
            "fba_share": (min(1.0, fba.get(p, 0) / allc[p]) if allc.get(p) else None),
            "w": w, "flows": flows,
            "basket": {"orders": b["orders"], "multi_pct": logic.pct(b["multi"], b["orders"]),
                       "with": [{"product": label(q), "orders": v} for q, v in b["with"]]},
        })
    biggest = lambda r: -max((v["buyers"] for v in r["w"].values()), default=0)
    rows.sort(key=lambda r: (biggest(r), r["product"].casefold()))
    total = {str(n): {"buyers": c["buyers"], "repeat_pct": logic.pct(c["repeat"], c["buyers"])}
             for n, c in m["brands"].get(brand, {}).items()}
    windows = {str(n): {"period": [s.isoformat(), e.isoformat()], "available": w["available"],
                        "reason": w["reason"]} for n, w in m["windows"].items() for s, e in [w["period"]]}
    return {"as_of": as_of.isoformat(), "history_from": history_from.isoformat() if history_from else None,
            "brand": brand, "brands": sorted(set(brand_of.values())), "windows": windows,
            "total": total, "rows": rows, "last_refresh": last,
            "min_cohort": logic.MIN_COHORT, "fba_partial_below": FBA_PARTIAL_BELOW}
```

`app/routers/repeat.py`

```python
"""Portfolio → Repeat customers. Read-only towards Amazon except the explicit Refresh."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app import ist, permissions
from app.database import get_db
from app.repeat import refresh, service
from app.routers.auth import require_area

router = APIRouter(prefix="/portfolio/repeat", tags=["repeat"])


@router.get("")
async def repeat(brand: str | None = None, db=Depends(get_db),
                 grant=Depends(require_area(permissions.PORTFOLIO))):
    return await service.build_payload(db, brand, ist.today())


@router.post("/refresh")
async def start_refresh(grant=Depends(require_area(permissions.PORTFOLIO))):
    if refresh.STATE["running"]:
        return JSONResponse({"error": "A repeat-customer refresh is already running."}, status_code=409)
    asyncio.create_task(refresh.run_incremental())
    return {"started": True}


@router.get("/refresh-status")
async def status(grant=Depends(require_area(permissions.PORTFOLIO))):
    return refresh.STATE
```

  Use whatever DB dependency `app/routers/portfolio.py` uses (grep `Depends(get_` there) —
  copy it exactly rather than `get_db` if the name differs. Register in `app/main.py`:
  add `repeat` to the routers import and `app.include_router(repeat.router)` next to
  `app.include_router(portfolio.router)`. Task 7 creates `app/repeat/refresh.py`; for this task
  create a stub with `STATE = {"running": False, "phase": "", "error": None}` and
  `async def run_incremental(): ...` so the router imports, and replace it fully in Task 7.

- [ ] **Step 4: Run** `venv/Scripts/python -m pytest tests/test_repeat_api.py tests/test_unauthenticated_access.py -q` → PASS (the unauthenticated test enumerates routes, so the new ones are covered automatically).

- [ ] **Step 5: Commit** `git commit -m "feat(repeat): payload service and /portfolio/repeat routes"`

---

### Task 7: Refresh — nightly 7 days, resumable 18-month backfill, scheduler

**Files:** Replace stub `app/repeat/refresh.py`; Create `scripts/backfill_repeat.py`;
Modify `app/scheduler.py`; Tests `tests/test_repeat_refresh.py`, `tests/test_retention_and_scheduler.py`

**Interfaces — Produces:** `refresh.STATE`, `refresh.NIGHTLY_DAYS = 7`, `refresh.RETENTION_DAYS = 400`,
`refresh.run(start: date, end: date, *, db_factory=async_session, fetch_rows=fetch.fetch_rows) -> dict`,
`refresh.run_incremental(**kw) -> dict`; scheduler job id `repeat_refresh` at `REPEAT_REFRESH_IST = (10, 0)`.

- [ ] **Step 1: Failing tests** — `tests/test_repeat_refresh.py`

```python
from datetime import date

import pytest

from app.models import CustomerOrderLine, RepeatRefresh
from app.repeat import refresh

pytestmark = pytest.mark.regression
ROW = {"amazon-order-id": "171-1", "shipment-item-id": "D1", "purchase-date": "2026-09-01T05:00:00+00:00",
       "buyer-email": "a@marketplace.amazon.in", "sku": "1kg cs FBA", "quantity-shipped": "1", "item-price": "90"}


@pytest.fixture(autouse=True)
def no_catalogue(monkeypatch):
    async def fake():
        return {}, None, "none"
    monkeypatch.setattr("app.repeat.refresh.load_catalogue", fake)


async def test_each_chunk_is_stored_and_recorded_as_it_lands(db_schema, count_rows):
    seen = []

    async def fake_fetch(start, end, *, client, sleep=None):
        seen.append((start, end))
        return [dict(ROW, **{"amazon-order-id": f"o{start.isoformat()}"})]
    result = await refresh.run(date(2026, 7, 1), date(2026, 9, 15), fetch_rows=fake_fetch)
    assert len(seen) == 3 and result["lines_stored"] == 3
    assert await count_rows(CustomerOrderLine) == 3 and await count_rows(RepeatRefresh) == 3


async def test_a_failed_chunk_keeps_the_chunks_before_it_and_is_recorded(db_schema, count_rows, read_committed):
    calls = 0

    async def flaky(start, end, *, client, sleep=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Amazon reported FATAL")
        return [dict(ROW, **{"amazon-order-id": f"o{calls}"})]
    result = await refresh.run(date(2026, 7, 1), date(2026, 9, 15), fetch_rows=flaky)
    assert result["error"] and await count_rows(CustomerOrderLine) == 1
    statuses = [r.status for r in await read_committed(RepeatRefresh)]
    assert sorted(statuses) == ["done", "failed"]


async def test_the_running_flag_clears_even_when_cancelled(db_schema):
    import asyncio

    async def hang(*a, **k):
        await asyncio.sleep(3600)
    task = asyncio.create_task(refresh.run(date(2026, 9, 1), date(2026, 9, 2), fetch_rows=hang))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert refresh.STATE["running"] is False


async def test_incremental_asks_for_the_last_7_ist_days(monkeypatch, db_schema):
    from app import ist
    asked = []

    async def fake_run(start, end, **kw):
        asked.append((start, end)); return {}
    monkeypatch.setattr(refresh, "run", fake_run)
    await refresh.run_incremental()
    end = ist.yesterday()
    assert asked == [(end - __import__("datetime").timedelta(days=6), end)]
```

  Adapt `count_rows` / `read_committed` call shapes to `tests/conftest.py` (both exist; read their
  docstrings for the exact signature).

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** `app/repeat/refresh.py`

```python
"""Fetch FBA shipments into customer_order_lines. Nightly: last 7 days. Backfill: 30-day chunks.

Each chunk is stored and recorded the moment it lands, so a 9-hour backfill interrupted at chunk 12
keeps chunks 1–11, and `repository.done_runs` tells the screen exactly what history is covered.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

from app import ist
from app.config import get_settings
from app.database import async_session
from app.repeat import fetch, keys, repository
from app.repeat.parse import parse_rows
from app.shipment.catalogue import load_catalogue

log = logging.getLogger(__name__)
NIGHTLY_DAYS = 7
#: 90-day window + 30-day period + 90-day look-back = 210 days needed; a year kept for headroom.
RETENTION_DAYS = 400
STATE = {"running": False, "phase": "", "error": None, "window": None}


async def run(start: date, end: date, *, db_factory=async_session,
              fetch_rows=fetch.fetch_rows) -> dict:
    if STATE["running"]:
        return {"error": "already running"}
    STATE.update(running=True, error=None, phase="starting", window=[start.isoformat(), end.isoformat()])
    stored, error = 0, None
    try:
        catalogue, _, _ = await load_catalogue()
        async with db_factory() as db:
            salt = await keys.load_or_create_salt(db)
            mapping = await repository.sku_map(db, catalogue)
        async with httpx.AsyncClient(timeout=get_settings().sp_api_timeout) as client:
            for a, b in fetch.split_days(start, end):
                STATE["phase"] = f"{a} → {b}"
                try:
                    rows = await fetch_rows(a, b, client=client)
                except Exception as exc:          # recorded, and earlier chunks are kept
                    error = str(exc)
                    async with db_factory() as db:
                        await repository.record_run(db, window_start=a.isoformat(),
                                                    window_end=b.isoformat(), status="failed", error=error)
                    break
                lines, counts = parse_rows(rows, salt, mapping)
                async with db_factory() as db:
                    stored += await repository.save_lines(db, lines)
                    await repository.record_run(db, window_start=a.isoformat(), window_end=b.isoformat(),
                                                status="done", lines_stored=len(lines), **counts)
        async with db_factory() as db:
            await repository.resolve_missing(db, mapping)
    finally:
        try:
            async with db_factory() as db:
                await repository.purge(db, (ist.today() - timedelta(days=RETENTION_DAYS)).isoformat())
        except Exception:
            log.exception("repeat purge failed")
        STATE.update(running=False, phase="", error=error)
    return {"lines_stored": stored, "error": error}


async def run_incremental(**kw) -> dict:
    end = ist.yesterday()
    return await run(end - timedelta(days=NIGHTLY_DAYS - 1), end, **kw)
```

  `sp_api_timeout` exists in settings (used by `spapi._get`); if not, use `httpx.AsyncClient(timeout=120)`.

- [ ] **Step 4: Backfill script** — `scripts/backfill_repeat.py`

```python
"""One-off: fill customer_order_lines with ~18 months, 30 days at a time. Resumable.

Run on the server in `screen`:  cd /opt/amazon-tracker && venv/bin/python scripts/backfill_repeat.py
Each 30-day chunk takes 30–40 min (Amazon's queue is serial), so 18 months is ~9–12 hours.
Chunks already recorded as done are skipped, so re-running after an interruption continues.
"""
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ist  # noqa: E402
from app.database import async_session  # noqa: E402
from app.repeat import fetch, refresh, repository  # noqa: E402

MONTHS = 18


async def main():
    end = ist.yesterday()
    start = end - timedelta(days=MONTHS * 30)
    async with async_session() as db:
        done = set(await repository.done_runs(db))
    for a, b in reversed(fetch.split_days(start, end)):        # newest first: useful soonest
        if (a.isoformat(), b.isoformat()) in done:
            print("skip", a, b); continue
        print("fetch", a, b, flush=True)
        result = await refresh.run(a, b)
        print("  ", result, flush=True)
        if result.get("error"):
            print("stopping; re-run to continue"); return 1
    async with async_session() as db:
        runs = await repository.done_runs(db)
    from app.repeat.logic import covered_from
    print("covered from", covered_from(runs, end - timedelta(days=3)))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 5: Scheduler** — in `app/scheduler.py` add beside `scheduled_projections_refresh`:

```python
#: 10:00 IST, after the 07:30 portfolio, 08:00 ads and Sunday 09:30 projections jobs. Never writes
#: to Amazon. 7 days, because a shipment can land days after its purchase and the window overlaps.
REPEAT_REFRESH_IST = (10, 0)


async def scheduled_repeat_refresh():
    from app.repeat import refresh as repeat_refresh
    try:
        result = await repeat_refresh.run_incremental()
        logger.info("Scheduled repeat refresh: %s", result)
    except Exception:
        logger.exception("Scheduled repeat refresh failed")
```

  and register it right after the `portfolio_refresh` job, under the same condition:

```python
    repeat_utc = ist.utc_hhmm(*REPEAT_REFRESH_IST)
    scheduler.add_job(
        scheduled_repeat_refresh,
        CronTrigger(hour=repeat_utc[0], minute=repeat_utc[1]),
        id="repeat_refresh", replace_existing=True, max_instances=1, coalesce=True,
    )
    parts.append(f"repeat customers at {ist.label(*REPEAT_REFRESH_IST)}")
```
  (use the module's existing logger name — grep `logger =` / `log =` at the top.)

  Add to `tests/test_retention_and_scheduler.py` (follow how it asserts `portfolio_refresh`):
  assert `repeat_refresh` is registered when only `ORDER_REFRESH_ENABLED` is on; that
  `REPEAT_REFRESH_IST > PORTFOLIO_REFRESH_IST`; and add `scheduled_repeat_refresh` to the
  source test that forbids scheduled jobs from reaching `apply_changes`/`plan_run`/`open_run`.

- [ ] **Step 6: Run** the two test files → PASS. **Step 7: Commit**
  `git commit -m "feat(repeat): nightly refresh, resumable backfill, 10:00 IST job"`

---

### Task 8: The page — Profit | Repeat switch, table, totals row, expand panel

**Files:** Create `templates/portfolio_repeat.html`, `templates/_portfolio_tabs.html`;
Modify `templates/portfolio.html` (include the switch under `<h2>`), `app/main.py` (page route),
`tests/js_harness.py` (template parameter), `tests/test_theme.py` (fragment exemption);
Test `tests/test_repeat_page.py`

- [ ] **Step 1: Generalise the harness** — in `tests/js_harness.py` change `_script()` to
  `_script(template: Path = TEMPLATE)` and add:

```python
def run_template_js(template: Path, functions: list[str], consts: list[str], body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the render tests need it")
    script = _script(template)
    parts = ["let data = {}; const $ = () => null;", "function emit(v){ console.log(JSON.stringify(v)); }"]
    parts += [_const(script, n) for n in consts] + [_function(script, n) for n in functions]
    parts.append(body)
    path = os.path.join(tempfile.gettempdir(), "tpl_render_test.js")
    Path(path).write_text("\n".join(parts), encoding="utf-8")
    result = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])
```
  Leave `run_portfolio_js` unchanged.

- [ ] **Step 2: Failing render tests** — `tests/test_repeat_page.py`

```python
from pathlib import Path

import pytest

from tests.js_harness import run_template_js

pytestmark = pytest.mark.regression
T = Path(__file__).parent.parent / "templates" / "portfolio_repeat.html"
FUNCS = ["esc", "pct", "num", "cellPct", "rowHtml", "totalRowHtml", "flowsHtml"]
DATA = """data = {min_cohort: 20, fba_partial_below: 0.6,
 windows: {"30": {available: true}, "60": {available: true}, "90": {available: false, reason: "needs order history from 2026-03-06"}},
 total: {"30": {buyers: 1200, repeat_pct: 0.086}, "60": {buyers: 1100, repeat_pct: 0.127}}};
const ROW = {parent_asin: "P1", product: "Jau Sattu", fba_share: 0.92,
 w: {"30": {buyers: 1404, same_pct: 0.077, came_from_pct: 0.071, went_on_pct: 0.05},
     "60": {buyers: 1300, same_pct: 0.143, came_from_pct: 0.091, went_on_pct: 0.06}},
 flows: {"60": {came_from: [{product: "Chana Sattu", customers: 73}], went_on: [{product: "Chana Sattu", customers: 58}]}},
 basket: {orders: 900, multi_pct: 0.21, with: [{product: "Chana Sattu", orders: 193}]}};"""


def cells(html_expr):
    return run_template_js(T, FUNCS, [], DATA + f"""
const h = {html_expr};
emit([...h.matchAll(/data-col="([^"]+)"[^>]*>([\\s\\S]*?)<\\/td>/g)].map(m => [m[1], m[2].replace(/<[^>]+>/g,"").trim()]));""")


def test_a_row_shows_each_window_s_figures_in_its_own_column():
    got = dict(cells("rowHtml(ROW)"))
    assert got["buyers-30"] == "1,404" and got["same-30"] == "7.7%" and got["from-30"] == "7.1%"
    assert got["same-60"] == "14.3%" and got["from-60"] == "9.1%"


def test_an_unavailable_window_renders_a_dash_not_zero():
    got = dict(cells("rowHtml(ROW)"))
    assert got["same-90"] == "—" and got["from-90"] == "—"


def test_the_total_row_is_the_brand_figure_and_has_no_cross_flow():
    got = dict(cells("totalRowHtml()"))
    assert got["same-30"] == "8.6%" and got["from-30"] == "—"


def test_a_low_fba_share_is_flagged_partial():
    out = run_template_js(T, FUNCS, [], DATA + 'emit(rowHtml(Object.assign({}, ROW, {fba_share: 0.4})).includes("partial"));')
    assert out is True


def test_the_expand_panel_names_sources_destinations_and_basket():
    out = run_template_js(T, FUNCS, [], DATA + 'emit(flowsHtml(ROW, "60"));')
    assert "Came from" in out and "Chana Sattu" in out and "73" in out
    assert "Went on to" in out and "58" in out and "Bought together" in out and "193" in out


async def test_both_portfolio_pages_carry_the_switch(auth_client):
    for path in ("/portfolio-page", "/portfolio-page/repeat"):
        html = (await auth_client.get(path)).text
        assert 'href="/portfolio-page/repeat"' in html and 'href="/portfolio-page"' in html


def test_the_page_never_renders_a_buyer_key_field():
    assert "buyer_key" not in T.read_text(encoding="utf-8")
```

- [ ] **Step 3: Run** → FAIL.

- [ ] **Step 4: The switch fragment** — `templates/_portfolio_tabs.html`

```html
{# Profit | Repeat customers. A fragment: no <head>, exempted in tests/test_theme.py. #}
<div class="seg pf-tabs" style="margin:4px 0 10px">
  <a class="seg-btn{% if pf_tab == 'profit' %} on{% endif %}" href="/portfolio-page">Profit</a>
  <a class="seg-btn{% if pf_tab == 'repeat' %} on{% endif %}" href="/portfolio-page/repeat">Repeat customers</a>
</div>
```
  In `templates/portfolio.html` insert `{% with pf_tab = 'profit' %}{% include "_portfolio_tabs.html" %}{% endwith %}`
  directly after `<h2>Portfolio review</h2>`. Add `.seg-btn` anchors need `text-decoration:none`:
  add `a.seg-btn{text-decoration:none;display:inline-block}` to portfolio.html's CSS.
  In `tests/test_theme.py` add `"_portfolio_tabs.html"` to `FRAGMENTS`.

- [ ] **Step 5: Page route** — `app/main.py`, after `portfolio_page`:

```python
@app.get("/portfolio-page/repeat", response_class=HTMLResponse)
async def portfolio_repeat_page(request: Request, grant=Depends(require_area(permissions.PORTFOLIO))):
    return templates.TemplateResponse(
        request, "portfolio_repeat.html", {"active": "portfolio", "grant": grant, "pf_tab": "repeat"})
```
  (match `portfolio_page`'s exact `TemplateResponse` call shape.)

- [ ] **Step 6: The page** — `templates/portfolio_repeat.html`. Copy the `<head>` (meta, title,
  `theme.css` link) and the `.card`, `.seg`, `.seg-btn`, `table`, `th`, `td`, `.num`, `.dim`,
  `.table-wrap`, `.banner` rules from `templates/portfolio.html` (only `var(--…)` colours). Body:

```html
{% import "_icons.html" as icons %}
{% include "_icon_sprite.html" %}
<header><span>📊</span><h1>Amazon Tracker v2</h1>{% include "nav.html" %}
<div class="header-right"><a href="/logout">Logout</a></div></header>
<main>
<h2>Portfolio review</h2>
{% include "_portfolio_tabs.html" %}
<div class="subtitle" id="subtitle">Loading…</div>
<div id="messages"></div>
<div class="card"><div class="controls">
  <select id="brand"></select>
  <button class="btn" id="refresh-btn">{{ icons.icon("refresh") }} Refresh from Amazon</button>
  <span id="refresh-note" class="dim" style="font-size:12px">—</span>
</div></div>
<div class="card"><div class="table-wrap" id="table"></div></div>
</main>
<script>
const $ = id => document.getElementById(id);
let data = {};
let open = new Set();
let flowWindow = "60";
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = v => v == null ? "—" : Number(v).toLocaleString("en-IN");
function pct(v){ return v == null ? "—" : (v * 100).toFixed(1) + "%"; }
const WINS = ["30", "60", "90"];
function cellPct(id, v, why){ return `<td class="num" data-col="${id}"${why ? ` title="${esc(why)}"` : ""}>${pct(v)}</td>`; }
function winCells(w, n, cross){
  const avail = (data.windows[n] || {}).available, why = avail ? "" : (data.windows[n] || {}).reason;
  const c = avail ? (w[n] || {}) : {};
  const small = avail && c.buyers != null && c.buyers < data.min_cohort ? `only ${c.buyers} buyers` : why;
  return `<td class="num" data-col="buyers-${n}">${avail ? num(c.buyers) : "—"}</td>`
    + cellPct(`same-${n}`, c.same_pct ?? c.repeat_pct, small)
    + cellPct(`from-${n}`, cross ? c.came_from_pct : null, cross ? small : "a brand total has no cross flow");
}
function rowHtml(r){
  const partial = r.fba_share != null && r.fba_share < data.fba_partial_below;
  return `<tr class="row" data-p="${esc(r.parent_asin)}"><td data-col="product">${esc(r.product)}</td>`
    + `<td class="num" data-col="fba">${pct(r.fba_share)}${partial ? ' <span class="dim" title="Many orders ship by Easy Ship, which carries no customer key, so repeat reads low">partial</span>' : ""}</td>`
    + WINS.map(n => winCells(r.w, n, true)).join("") + `</tr>`;
}
function totalRowHtml(){
  return `<tr class="totals"><td data-col="product"><b>${esc(data.brand || "Brand")} — all products</b></td><td class="num" data-col="fba"></td>`
    + WINS.map(n => winCells(data.total || {}, n, false)).join("") + `</tr>`;
}
function flowsHtml(r, n){
  const list = (rows, k) => rows && rows.length ? rows.map(x => `${esc(x.product)} <b>${num(x[k])}</b>`).join(" · ") : "—";
  const f = (r.flows || {})[n] || {};
  return `<div class="dim">Window: ${WINS.map(w => `<a href="#" data-flow="${w}"${w === n ? ' class="on"' : ""}>${w}d</a>`).join(" ")}</div>`
    + `<div><b>Came from</b> (bought another product first, within ${n} days): ${list(f.came_from, "customers")}</div>`
    + `<div><b>Went on to</b> (bought next, within ${n} days): ${list(f.went_on, "customers")}</div>`
    + `<div><b>Bought together</b> (same order, last 90 days, ${pct(r.basket.multi_pct)} of its orders): ${list(r.basket.with, "orders")}</div>`;
}
function render(){
  const head = `<tr><th rowspan="2">Product</th><th rowspan="2" title="Share of units shipped by FBA. Easy Ship orders carry no customer key.">FBA share</th>`
    + WINS.map(n => `<th colspan="3">${n}-day</th>`).join("") + `</tr><tr>`
    + WINS.map(() => `<th title="Customers who bought it in the cohort period">Buyers</th><th title="Bought the SAME product again on a later day">Same repeat</th><th title="Had bought a DIFFERENT product of ours in the days before">From other</th>`).join("") + `</tr>`;
  const body = data.rows.map(r => rowHtml(r) + (open.has(r.parent_asin)
    ? `<tr class="detail"><td colspan="${2 + WINS.length * 3}">${flowsHtml(r, flowWindow)}</td></tr>` : "")).join("");
  $("table").innerHTML = `<table><thead>${head}${totalRowHtml()}</thead><tbody>${body}</tbody></table>`;
}
async function load(){
  const brand = $("brand").value;
  const res = await fetch("/portfolio/repeat" + (brand ? "?brand=" + encodeURIComponent(brand) : ""));
  data = await res.json();
  $("brand").innerHTML = data.brands.map(b => `<option${b === data.brand ? " selected" : ""}>${esc(b)}</option>`).join("");
  $("subtitle").textContent = data.as_of
    ? `FBA orders only · customers who bought in a 30-day period, followed for 30/60/90 days · data to ${data.as_of}`
    : "No customer history yet — press Refresh from Amazon, or run the backfill.";
  $("messages").innerHTML = data.last_refresh && data.last_refresh.status === "failed"
    ? `<div class="banner">Last refresh failed: ${esc(data.last_refresh.error)}</div>` : "";
  render();
}
$("brand").addEventListener("change", load);
$("table").addEventListener("click", e => {
  const flow = e.target.closest("[data-flow]");
  if(flow){ e.preventDefault(); flowWindow = flow.dataset.flow; render(); return; }
  const row = e.target.closest("tr.row");
  if(!row) return;
  const p = row.dataset.p;
  open.has(p) ? open.delete(p) : open.add(p);
  render();
});
$("refresh-btn").addEventListener("click", async () => {
  const r = await fetch("/portfolio/repeat/refresh", {method: "POST"});
  $("refresh-note").textContent = r.ok ? "Fetching the last 7 days (Amazon takes 5–15 minutes)…" : (await r.json()).error;
});
load();
</script>
```

  Note `winCells` reads `same_pct ?? repeat_pct` so the brand total (which has `repeat_pct`) and a
  product row share one renderer. The `_TOP_LEVEL` markers in the harness require top-level
  functions to be followed by `\nconst `, `\nlet `, `\n$(`, etc. — keep the order above.

- [ ] **Step 7: Run** `tests/test_repeat_page.py tests/test_theme.py tests/test_nav_consistency.py tests/test_template_render_targets.py tests/test_local_dates.py` → PASS.

- [ ] **Step 8: Browser check** (`preview_start` name `tracker`, seed a few lines locally or point
  at a copy of production data): `/portfolio-page/repeat` renders, the Profit/Repeat switch works
  both ways, a row expands and its window links switch, a 375px viewport has 0px sideways document
  scroll, console clean. Screenshot.

- [ ] **Step 9: Commit** `git commit -m "feat(repeat): Repeat customers sub-tab page and Profit|Repeat switch"`

---

### Task 9: Validation against Brand Analytics, and the mutation harness

**Files:** Create `scripts/validate_repeat.py`, `scripts/mutate_repeat.py`

- [ ] **Step 1: Validation script** — `scripts/validate_repeat.py`

```python
"""Compare one calendar month of stored lines with Amazon's own Brand Analytics repeat report.

Run on the server after the backfill:  venv/bin/python scripts/validate_repeat.py 2026-09
Brand Analytics counts ALL channels; we see FBA only. So on ASINs that ship almost entirely by FBA:
  * unique customers must agree within 3%   (proves the customer key)
  * our repeat % must not EXCEED Amazon's by more than 0.5 pt (Easy Ship repeats can only be missing)
Measured 06 Oct 2026: 914/925, 737/747, 223/223 customers; ours 0.3-0.6 pt lower. Exit 1 on failure.
"""
import asyncio
import calendar
import collections
import gzip
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import async_session  # noqa: E402
from app.repeat import repository  # noqa: E402
from app.shipment import spapi  # noqa: E402

R = "/reports/2021-06-30"


async def brand_analytics(month_start: date, month_end: date) -> dict:
    async with httpx.AsyncClient(timeout=120) as cl:
        created = await spapi._post(f"{R}/reports", {
            "reportType": "GET_BRAND_ANALYTICS_REPEAT_PURCHASE_REPORT",
            "marketplaceIds": [get_settings().sp_api_marketplace_id],
            "reportOptions": {"reportPeriod": "MONTH"},
            "dataStartTime": f"{month_start}T00:00:00Z", "dataEndTime": f"{month_end}T23:59:59Z"}, client=cl)
        for _ in range(120):
            await asyncio.sleep(15)
            st = await spapi._get(f"{R}/reports/{created['reportId']}", client=cl)
            if st["processingStatus"] in ("DONE", "FATAL", "CANCELLED"):
                break
        if st["processingStatus"] != "DONE":
            raise SystemExit(f"Brand Analytics report {st['processingStatus']}")
        doc = await spapi._get(f"{R}/documents/{st['reportDocumentId']}", client=cl)
        raw = (await cl.get(doc["url"])).content
        if doc.get("compressionAlgorithm") == "GZIP":
            raw = gzip.decompress(raw)
        return {x["asin"]: x for x in json.loads(raw)["dataByAsin"]}


async def main(month: str) -> int:
    y, m = map(int, month.split("-"))
    start, end = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    async with async_session() as db:
        lines = [l for l in await repository.load_lines(db, start.isoformat()) if l["day"] <= end]
    orders = collections.defaultdict(lambda: collections.defaultdict(set))   # asin -> buyer -> orders
    for l in lines:
        orders[l["child_asin"]][l["buyer_key"]].add(l["amazon_order_id"])
    ba = await brand_analytics(start, end)
    failures, checked = 0, 0
    print(f"{'asin':12} {'ours':>6} {'BA':>6} {'ours rep':>9} {'BA rep':>8}")
    for asin, buyers in sorted(orders.items(), key=lambda kv: -len(kv[1])):
        x = ba.get(asin)
        if not x or not x.get("uniqueCustomers") or x["uniqueCustomers"] < 100:
            continue
        ours_n = len(buyers)
        if ours_n < 0.95 * x["uniqueCustomers"]:
            continue                       # meaningful Easy Ship share: not comparable, skipped
        checked += 1
        ours_rep = sum(1 for o in buyers.values() if len(o) > 1) / ours_n
        ba_rep = float(x.get("repeatCustomersPctTotal") or 0)
        bad = abs(ours_n - x["uniqueCustomers"]) / x["uniqueCustomers"] > 0.03 or ours_rep > ba_rep + 0.005
        failures += bad
        print(f"{asin:12} {ours_n:6d} {x['uniqueCustomers']:6d} {ours_rep:9.1%} {ba_rep:8.1%} {'FAIL' if bad else 'ok'}")
    print(f"checked {checked} FBA-dominant ASINs, {failures} failed")
    return 1 if failures or not checked else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1])))
```

- [ ] **Step 2: Mutation harness** — `scripts/mutate_repeat.py`. Copy the runner body of
  `scripts/mutate_portfolio_columns.py` (temp copy of the repo files, apply one mutation, run the
  listed tests, restore, print `All N mutations caught` or the survivors). Tests to run:
  `tests/test_repeat_*.py`. Mutations (`(file, old, new, why)`):

| File | old → new | Breaks |
|---|---|---|
| logic.py | `anchor < o.day <= anchor + span` (in `after`) → `anchor <= o.day <= anchor + span` | same-day order counted as repeat |
| logic.py | `anchor - span <= o.day < anchor` → `anchor - span <= o.day <= anchor` | basket counted as cross flow |
| logic.py | `anchors.setdefault(p, o.day)` → `anchors[p] = o.day` | anchor moves to the LAST purchase |
| logic.py | `if whole < MIN_COHORT:` → `if whole < 1:` | noise percentages shown |
| logic.py | `bc["buyers"] += 1` → `bc["buyers"] += len(o.parents)` | brand total double-counts |
| logic.py | `history_from > need` → `history_from > need + timedelta(days=60)` | immature window reported |
| logic.py | `return end - timedelta(days=PERIOD_DAYS - 1), end` → `return as_of - timedelta(days=PERIOD_DAYS - 1), as_of` | no follow-up time at all |
| parse.py | `float(price) == 0.0` → `False` | free replacements become repeats |
| parse.py | `return ist.day_of(when)` → `return when.date().isoformat()` | UTC day, off by one for 5½ h |
| keys.py | `.strip().lower()` → `.strip()` | one buyer split into two keys |
| repository.py | delete the line `await db.execute(delete(CustomerOrderLine).where(CustomerOrderLine.id.in_(ids)))` (replace with `pass`) | re-read doubles rows |
| refresh.py | `break` (after recording failed) → `continue` | a failed chunk is skipped silently and later chunks claim coverage |
| service.py | `today - timedelta(days=1)` → `today` | as_of includes today's incomplete day |
| service.py | delete the two lines `if brand_of.get(p) != brand:` / `continue` | other brands leak into the Mithila table |
| template | `cross ? c.came_from_pct : null` → `c.came_from_pct` | brand total shows a fake cross flow |
| template | `${partial ? …: ""}` span → `""` | Easy-Ship-heavy rows not flagged |

- [ ] **Step 3: Run** `venv/Scripts/python scripts/mutate_repeat.py` → must print
  `All 16 mutations caught`. A survivor means a test asserts too little: strengthen the TEST, not
  the mutation. No summary line means the harness crashed — that is not a pass.

- [ ] **Step 4: Commit** `git commit -m "test(repeat): Brand Analytics validation script and mutation harness"`

---

### Task 10: Full suite, CLAUDE.md, deploy, backfill, verify on production

- [ ] **Step 1:** `venv/Scripts/python -m pytest -q` → all green (≈2,564 + new). Re-run the
  Portfolio harnesses that touch `portfolio.html` (`mutate_portfolio_columns.py`,
  `mutate_portfolio_ui.py`, `mutate_portfolio_groups.py`, `mutate_portfolio_active_weight.py`,
  `mutate_portfolio_sb.py`) since the switch was inserted into that template; each must print
  `All N mutations caught`.

- [ ] **Step 2: CLAUDE.md** — add a section "Portfolio → Repeat customers" under the Portfolio
  tab, recording: the measured facts table above; the two definitions; FBA-only and why; the
  salted key and that rotating the salt severs history; the 30-day report cap, serial queue and
  30–40 min chunks; the Brand Analytics acceptance criteria; the migration `a4c7e2f19b30` and its
  detector branch; the 10:00 IST job. Add `CustomerOrderLine/RepeatRefresh` to the models line and
  `app/repeat/` to Key Files. Update the test count.

- [ ] **Step 3: Commit and push**
```bash
git add -A ':!scripts/annotate_fc_recommendation.py' ':!reports/'
git commit -m "docs: Repeat customers sub-tab"
git push origin claude/stoic-allen-bb3a55
```

- [ ] **Step 4: Deploy** (one new migration, so check the script out first):
```bash
ssh ubuntu@13.233.144.148
cd /opt/amazon-tracker && git fetch origin claude/stoic-allen-bb3a55 \
  && git checkout origin/claude/stoic-allen-bb3a55 -- deploy/update-ec2.sh && bash deploy/update-ec2.sh
```
  Confirm `journalctl -u tracker | grep Scheduler` lists `repeat customers at 10:00 IST`.

- [ ] **Step 5: Backfill** in `screen` (~9–12 h): `venv/bin/python scripts/backfill_repeat.py`.
  It stores newest chunks first, so the 30- and 60-day columns fill within the first few hours;
  the 90-day column needs history from ~210 days back.

- [ ] **Step 6: Validate** — `venv/bin/python scripts/validate_repeat.py 2026-09` → exit 0, with
  the per-ASIN table pasted into the hand-off message. Then open `/portfolio-page/repeat` with a
  signed cookie on the box and check, by hand against the database:
  * the Mithila total row's 30-day buyers equals a direct `COUNT(DISTINCT buyer_key)` over Mithila
    lines in that period;
  * Chana Sattu's 30/60-day same repeat is close to the prototype measured on 06 Oct
    (cohort 1,514; 8.8% / 14.3% — the prototype's cohort was "first order before 05 Aug", so expect
    the same order of magnitude, not identical numbers);
  * the top flow pairs (Jau Sattu ↔ Chana Sattu) and basket pairs (Chana Sattu + Jau Sattu, 193)
    appear in the expand panels.

- [ ] **Step 7:** Disk check `df -h /` and `SELECT COUNT(*) FROM customer_order_lines` — expect
  ~12k lines/month, ≈150–200k rows for 18 months, ~30 MB.

## Self-review

- Spec coverage: 30/60/90 parent repeat (Task 4, 6, 8) · brand total as a totals row (Tasks 4, 6, 8)
  · cross-product repeat on the destination row (came_from, Tasks 4, 8) · flows + basket (Tasks 4, 8)
  · "tested well" (Tasks 4, 5, 9 + production validation in Task 10).
- Known limit, stated on screen and here: FBA only; Easy-Ship-heavy products read low and are
  flagged by `fba_share`.
- Out of scope: Easy Ship estimation, cohort trend charts, contacting customers (Amazon policy
  forbids marketing use of buyer data), any write to Amazon.
