# FBA stock from the API, and "To purchase" — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (this project is
> inline only, never subagents). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read FBA stock (on hand + reserved + every inbound state) daily from Amazon's FBA Inventory
API, show it in kg on Projections with "To purchase = Total stock to hold − FBA stock", feed the
same stock to the Shipment tab's plan, and add "To purchase" to the Profit tab.

**Architecture:** A new read-only `app/inventory/` package fetches `GET /fba/inventory/v1/summaries`,
reduces each FBA SKU to units held, and stores one run per fetch (`fba_inventory_refresh` +
`fba_inventory_row`). Projections maps child ASINs to its row ids (flavour key, else Amazon parent),
weighs them with the MRP sheet, and derives `fba_kg` / `to_purchase`. The Shipment tab's generate
fetches fresh, falling back to the latest good run, then to the CSV. The Profit tab reads the
Projections figure by row id, like `total_stock`.

**Tech Stack:** FastAPI, async SQLAlchemy, SQLite, Alembic, httpx (SP-API via `app.shipment.spapi._get`),
APScheduler, vanilla JS templates, pytest + Node JS harness.

Spec: `docs/superpowers/specs/2026-10-13-fba-stock-to-purchase-design.md`.

## Global Constraints

- Read-only towards Amazon. No import of inbound-plan writers, ads apply, or economics writers in `app/inventory/`.
- Units held = fulfillable + inbound working + inbound shipped + inbound receiving + reserved (total) + researching + future supply (reserved + buyable). Unfulfillable excluded.
- FBA SKUs only, by `app.portfolio.logic._channel_of(sku) == CHANNEL_FBA`.
- To purchase per row = `max(0, total_stock - fba_kg)`; the total sums the clamped rows.
- Unknown is a dash (None), never 0: unknown weight, no good run, or no total_stock.
- Only a `done` run is ever read as current. Refused when ≥ `ALL_ZERO_STOCK_THRESHOLD` (10) SKUs and every one is zero.
- Times stated in IST through `ist.utc_hhmm`. Nightly job 07:45 IST, id `fba_inventory_refresh`.
- Every new migration adds a detector branch to `deploy/update-ec2.sh`, newest first.
- Whole numbers on screen. Commit messages end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Never commit `scripts/annotate_fc_recommendation.py` or `reports/`.
- Tests: `venv/Scripts/python -m pytest -q`. Mutation harnesses must print `All N mutations caught`.

## File map

| File | Responsibility |
|---|---|
| `app/inventory/__init__.py` | package |
| `app/inventory/logic.py` | pure: `held_units`, `reduce_summaries`, `looks_empty` |
| `app/inventory/fetch.py` | page the summaries endpoint |
| `app/inventory/repository.py` | store/read runs, purge |
| `app/inventory/refresh.py` | fetch → guard → store, one at a time |
| `app/models.py` | `FbaInventoryRefresh`, `FbaInventoryRow` |
| `alembic/versions/a7d3c9e15b20_fba_inventory.py` | the two tables |
| `deploy/update-ec2.sh` | detector branch + required tables |
| `app/scheduler.py` | 07:45 IST job |
| `app/portfolio/repository.py` | `parent_of_children` |
| `app/projections/logic.py` | `to_purchase`, `sum_rows` gains two keys |
| `app/projections/service.py` | attach `fba_kg`/`to_purchase`, status, unmapped |
| `app/routers/projections.py` | `POST /projections/refresh-stock`, download columns |
| `templates/projections.html` | columns, tiles, cards, banner, button |
| `app/routers/shipment.py` | stock source order; CSV fallback rule N − L |
| `templates/shipment.html` | name the stock source |
| `app/routers/portfolio.py`, `app/portfolio/columns.py`, `app/portfolio/export.py`, `templates/portfolio.html` | Profit "To purchase" |
| `tests/test_inventory.py`, `tests/test_projections.py`, `tests/test_shipment_stock_csv.py`, profit tests | tests |
| `scripts/mutate_inventory.py` | mutation harness |

---

### Task 1: Pure inventory logic

**Files:** Create `app/inventory/__init__.py` (empty docstring), `app/inventory/logic.py`; Test `tests/test_inventory.py`

**Interfaces — Produces:**
- `held_units(details: dict) -> int`
- `reduce_summaries(summaries: list[dict]) -> list[dict]` — FBA rows only, each `{"seller_sku","asin","fnsku","fulfillable","inbound_working","inbound_shipped","inbound_receiving","reserved","researching","future_supply","unfulfillable","amazon_updated_at","held"}`
- `units_by_asin(rows: list[dict]) -> dict[str, int]`
- `looks_empty(rows: list[dict]) -> bool`
- `QUANTITY_FIELDS: tuple[str, ...]` (the 8 stored quantity names)

- [ ] **Step 1: failing tests** in `tests/test_inventory.py`:

```python
"""FBA stock from Amazon's FBA Inventory API (13 Oct 2026)."""
import pytest
from app.inventory import logic

pytestmark = pytest.mark.regression


def summary(sku, asin="B0TEST00001", *, f=0, w=0, s=0, r=0, res=0, rch=0, unf=0, fut=(0, 0)):
    return {"sellerSku": sku, "asin": asin, "fnSku": "X0", "lastUpdatedTime": "2026-10-13T02:00:00Z",
            "inventoryDetails": {
                "fulfillableQuantity": f, "inboundWorkingQuantity": w, "inboundShippedQuantity": s,
                "inboundReceivingQuantity": r,
                "reservedQuantity": {"totalReservedQuantity": res},
                "researchingQuantity": {"totalResearchingQuantity": rch},
                "unfulfillableQuantity": {"totalUnfulfillableQuantity": unf},
                "futureSupplyQuantity": {"reservedFutureSupplyQuantity": fut[0],
                                         "futureSupplyBuyableQuantity": fut[1]}}}


def test_every_held_state_counts_and_damaged_does_not():
    s = summary("1kg cs FBA", f=1, w=2, s=4, r=8, res=16, rch=32, unf=999, fut=(64, 128))
    assert logic.held_units(s["inventoryDetails"]) == 255


def test_the_13_oct_under_count_comes_out_at_amazons_figure():
    """HF_gbrice_1kg_FBA: the CSV's nine columns read 0, Amazon's total 340, all inbound shipped."""
    rows = logic.reduce_summaries([summary("HF_gbrice_1kg_FBA", s=340)])
    assert rows[0]["held"] == 340 and rows[0]["inbound_shipped"] == 340


def test_flex_rows_and_their_999_placeholder_are_not_fba_stock():
    rows = logic.reduce_summaries([summary("0.25 fc ch FBA", f=102), summary("0.25 fc ch flex", f=999),
                                   summary("0.2kg mk fba", f=3)])
    assert [r["seller_sku"] for r in rows] == ["0.25 fc ch FBA", "0.2kg mk fba"]


def test_two_fba_skus_on_one_asin_are_summed():
    rows = logic.reduce_summaries([summary("a FBA", "B0X0000001", f=5), summary("b_FBA", "B0X0000001", s=7)])
    assert logic.units_by_asin(rows) == {"B0X0000001": 12}


def test_missing_nested_blocks_read_as_zero_not_a_crash():
    assert logic.held_units({"fulfillableQuantity": 4}) == 4


def test_dozens_of_skus_all_zero_is_a_failed_read_but_a_few_is_real():
    many = logic.reduce_summaries([summary(f"s{i} FBA", f"B0Z{i:07d}") for i in range(12)])
    few = logic.reduce_summaries([summary(f"s{i} FBA", f"B0Z{i:07d}") for i in range(3)])
    assert logic.looks_empty(many) and not logic.looks_empty(few)
    many[0]["held"] = 1
    assert not logic.looks_empty(many)
```

- [ ] **Step 2:** `venv/Scripts/python -m pytest tests/test_inventory.py -q` → FAIL (no module).

- [ ] **Step 3: implement** `app/inventory/logic.py`:

```python
"""Units held at Amazon, from one FBA Inventory API summary. Pure.

The owner's rule, 13 Oct 2026: stock counts as at FBA once a shipment is created, inbound, being
received, or anything else Amazon holds, so every state is added and only damaged stock is left out.
Measured that day: the Manage FBA Inventory CSV's `afn-inbound-shipped-quantity` missed 2,925 units
on 17 SKUs that Amazon's own total and this API both carry.
"""
from __future__ import annotations

from app.portfolio.logic import CHANNEL_FBA, _channel_of
from app.routers.shipment import ALL_ZERO_STOCK_THRESHOLD

QUANTITY_FIELDS = ("fulfillable", "inbound_working", "inbound_shipped", "inbound_receiving",
                   "reserved", "researching", "future_supply", "unfulfillable")
HELD_FIELDS = QUANTITY_FIELDS[:-1]


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _parts(d: dict) -> dict:
    fut = d.get("futureSupplyQuantity") or {}
    return {
        "fulfillable": _int(d.get("fulfillableQuantity")),
        "inbound_working": _int(d.get("inboundWorkingQuantity")),
        "inbound_shipped": _int(d.get("inboundShippedQuantity")),
        "inbound_receiving": _int(d.get("inboundReceivingQuantity")),
        "reserved": _int((d.get("reservedQuantity") or {}).get("totalReservedQuantity")),
        "researching": _int((d.get("researchingQuantity") or {}).get("totalResearchingQuantity")),
        "future_supply": _int(fut.get("reservedFutureSupplyQuantity")) + _int(fut.get("futureSupplyBuyableQuantity")),
        "unfulfillable": _int((d.get("unfulfillableQuantity") or {}).get("totalUnfulfillableQuantity")),
    }


def held_units(details: dict) -> int:
    parts = _parts(details or {})
    return sum(parts[k] for k in HELD_FIELDS)


def reduce_summaries(summaries: list[dict]) -> list[dict]:
    """FBA SKUs only (a flex SKU's 999 is a placeholder, not stock at Amazon)."""
    out = []
    for s in summaries:
        sku = str(s.get("sellerSku") or "").strip()
        asin = str(s.get("asin") or "").strip().upper()
        if not sku or not asin or _channel_of(sku) != CHANNEL_FBA:
            continue
        parts = _parts(s.get("inventoryDetails") or {})
        out.append({"seller_sku": sku, "asin": asin, "fnsku": s.get("fnSku") or "",
                    "amazon_updated_at": s.get("lastUpdatedTime") or "", **parts,
                    "held": sum(parts[k] for k in HELD_FIELDS)})
    return out


def units_by_asin(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r["asin"]] = out.get(r["asin"], 0) + int(r["held"])
    return out


def looks_empty(rows: list[dict]) -> bool:
    """The CSV's guard, on the API answer: dozens of FBA SKUs all at zero is a failed read."""
    return len(rows) >= ALL_ZERO_STOCK_THRESHOLD and not any(r["held"] for r in rows)
```

- [ ] **Step 4:** run the test file → PASS.
- [ ] **Step 5: commit** `feat(inventory): units held per FBA SKU from the FBA Inventory API answer`.

---

### Task 2: Storage — models, migration, deploy detector

**Files:** Modify `app/models.py`; Create `alembic/versions/a7d3c9e15b20_fba_inventory.py`; Modify `deploy/update-ec2.sh`

**Interfaces — Produces:** `FbaInventoryRefresh(id, started_at, finished_at, status, source, sku_count, units, error)`, `FbaInventoryRow(id, refresh_id, seller_sku, asin, fnsku, fulfillable, inbound_working, inbound_shipped, inbound_receiving, reserved, researching, future_supply, unfulfillable, amazon_updated_at)`.

- [ ] **Step 1:** append to `app/models.py` (near `ProjectionRow`):

```python
class FbaInventoryRefresh(Base):
    """One read of Amazon's FBA Inventory API. Only a `done` run is ever read as current stock;
    a failed or refused run is kept so the screen can say what happened."""
    __tablename__ = "fba_inventory_refresh"
    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime)
    status = Column(String(12), nullable=False, default="running")   # running|done|failed|refused
    source = Column(String(12), nullable=False, default="nightly")   # nightly|button|shipment
    sku_count = Column(Integer, nullable=False, default=0)
    units = Column(Integer, nullable=False, default=0)
    error = Column(Text)


class FbaInventoryRow(Base):
    """One FBA SKU in one run, every quantity kept apart so a figure can be explained."""
    __tablename__ = "fba_inventory_row"
    __table_args__ = (Index("idx_fba_inventory_row_run_sku", "refresh_id", "seller_sku", unique=True),)
    id = Column(Integer, primary_key=True)
    refresh_id = Column(Integer, ForeignKey("fba_inventory_refresh.id", ondelete="CASCADE"), nullable=False)
    seller_sku = Column(String(80), nullable=False)
    asin = Column(String(10), nullable=False)
    fnsku = Column(String(20))
    fulfillable = Column(Integer, nullable=False, default=0)
    inbound_working = Column(Integer, nullable=False, default=0)
    inbound_shipped = Column(Integer, nullable=False, default=0)
    inbound_receiving = Column(Integer, nullable=False, default=0)
    reserved = Column(Integer, nullable=False, default=0)
    researching = Column(Integer, nullable=False, default=0)
    future_supply = Column(Integer, nullable=False, default=0)
    unfulfillable = Column(Integer, nullable=False, default=0)
    amazon_updated_at = Column(String(30))
```

(Check `Text`, `ForeignKey`, `Index`, `datetime` are already imported in models.py; add if not.)

- [ ] **Step 2:** create the migration, `revision = "a7d3c9e15b20"`, `down_revision = "f3b8d2a64c19"`, `op.create_table` for both tables with the columns above (`server_default="0"` on integer counts, `sa.ForeignKeyConstraint(["refresh_id"], ["fba_inventory_refresh.id"], ondelete="CASCADE")`), `op.create_index("idx_fba_inventory_row_run_sku", "fba_inventory_row", ["refresh_id", "seller_sku"], unique=True)`; downgrade drops index then both tables.

- [ ] **Step 3:** in `deploy/update-ec2.sh` add above the `product_key` branch:

```bash
elif "fba_inventory_refresh" in tables:
    print("a7d3c9e15b20")                           # head: FBA stock from the Inventory API
```

and change the `product_key` comment from `# head: ...` to `# projections keyed like Profit rows`. Add `"fba_inventory_refresh", "fba_inventory_row",` to the `need` set with a comment `# FBA stock from the Inventory API (a7d3c9e15b20).`

- [ ] **Step 4:** `venv/Scripts/python -m pytest tests/test_schema_migrations.py -q` → PASS (it runs the detector at each recent revision). Back up local DB, then `venv/Scripts/alembic upgrade head`.
- [ ] **Step 5: commit** `feat(inventory): fba_inventory_refresh and fba_inventory_row tables`.

---

### Task 3: Fetch, repository, refresh

**Files:** Create `app/inventory/fetch.py`, `app/inventory/repository.py`, `app/inventory/refresh.py`; Modify `tests/test_inventory.py`

**Interfaces — Produces:**
- `fetch.fetch_summaries(get=None) -> list[dict]` (`get` defaults to `spapi._get`; raises `fetch.InventoryFetchError` past `MAX_PAGES = 30`)
- `repository.latest_good(db) -> FbaInventoryRefresh | None`, `repository.latest(db) -> FbaInventoryRefresh | None`, `repository.rows_for(db, refresh_id) -> list[dict]`, `repository.purge(db, keep=30) -> int`
- `refresh.run(source: str, *, get=None) -> dict` → `{"id", "status", "sku_count", "units", "error"}`; `{"refused": True}` when already running

- [ ] **Step 1: failing tests** (append):

```python
from sqlalchemy import func, select
from app.inventory import fetch, refresh, repository
from app.models import FbaInventoryRefresh, FbaInventoryRow


def pager(pages):
    calls = []

    async def get(path, params):
        calls.append(dict(params))
        i = len(calls) - 1
        body = {"payload": {"inventorySummaries": pages[i]}}
        if i + 1 < len(pages):
            body["pagination"] = {"nextToken": f"t{i + 1}"}
        return body
    return get, calls


async def test_fetch_pages_to_the_end_and_asks_for_details():
    get, calls = pager([[summary("a FBA")], [summary("b FBA")]])
    out = await fetch.fetch_summaries(get=get)
    assert [s["sellerSku"] for s in out] == ["a FBA", "b FBA"]
    assert calls[0]["details"] == "true" and calls[1]["nextToken"] == "t1"


async def test_a_run_stores_fba_rows_and_becomes_current(db_schema, read_committed):
    get, _ = pager([[summary("1kg cs FBA", "B0DK1LJ3N3", f=10, s=5), summary("1kg cs flex", "B0DK1LJ3N3", f=999)]])
    out = await refresh.run("button", get=get)
    assert out["status"] == "done" and out["units"] == 15 and out["sku_count"] == 1
    async with read_committed() as s:
        run = await repository.latest_good(s)
        assert run.id == out["id"]
        assert [r["seller_sku"] for r in await repository.rows_for(s, run.id)] == ["1kg cs FBA"]


async def test_a_failed_or_refused_run_never_becomes_current(db_schema, read_committed):
    good, _ = pager([[summary("a FBA", f=3)]])
    first = await refresh.run("nightly", get=good)

    async def boom(path, params):
        raise RuntimeError("Amazon 503")
    failed = await refresh.run("nightly", get=boom)
    zeros, _ = pager([[summary(f"s{i} FBA", f"B0Z{i:07d}") for i in range(12)]])
    refused = await refresh.run("nightly", get=zeros)
    assert failed["status"] == "failed" and "503" in failed["error"]
    assert refused["status"] == "refused"
    async with read_committed() as s:
        assert (await repository.latest_good(s)).id == first["id"]
        assert (await repository.latest(s)).status == "refused"


async def test_purge_keeps_the_newest_30_runs(db_schema, read_committed):
    get, _ = pager([[summary("a FBA", f=1)]] * 1)
    for _ in range(32):
        g, _ = pager([[summary("a FBA", f=1)]])
        await refresh.run("nightly", get=g)
    async with read_committed() as s:
        assert (await s.execute(select(func.count()).select_from(FbaInventoryRefresh))).scalar() == 30
        assert (await s.execute(select(func.count()).select_from(FbaInventoryRow))).scalar() == 30
```

- [ ] **Step 2:** run → FAIL.

- [ ] **Step 3: implement**

`app/inventory/fetch.py`:

```python
"""Page Amazon's FBA Inventory API. Read-only: one GET per page, no report queue (13 Oct 2026:
8 pages, 354 SKUs, a few seconds)."""
from __future__ import annotations

from app.config import get_settings

PATH = "/fba/inventory/v1/summaries"
MAX_PAGES = 30


class InventoryFetchError(RuntimeError):
    pass


async def fetch_summaries(get=None) -> list[dict]:
    if get is None:
        from app.shipment.spapi import _get as get
    market = get_settings().sp_api_marketplace_id
    out, token = [], None
    for _ in range(MAX_PAGES):
        params = {"details": "true", "granularityType": "Marketplace", "granularityId": market,
                  "marketplaceIds": market}
        if token:
            params["nextToken"] = token
        body = await get(PATH, params)
        out += (body.get("payload") or {}).get("inventorySummaries") or []
        token = (body.get("pagination") or {}).get("nextToken")
        if not token:
            return out
    # A cap that is hit is a truncated answer, and a truncated answer reads as missing stock.
    raise InventoryFetchError(f"Amazon returned more than {MAX_PAGES} pages of inventory")
```

`app/inventory/repository.py`:

```python
"""Runs of the FBA Inventory read. Only a `done` run is current stock."""
from __future__ import annotations

from sqlalchemy import delete, select

from app.inventory.logic import QUANTITY_FIELDS
from app.models import FbaInventoryRefresh, FbaInventoryRow

KEEP_RUNS = 30


async def latest_good(db):
    return (await db.execute(select(FbaInventoryRefresh).where(FbaInventoryRefresh.status == "done")
                             .order_by(FbaInventoryRefresh.id.desc()).limit(1))).scalar()


async def latest(db):
    return (await db.execute(select(FbaInventoryRefresh).where(FbaInventoryRefresh.status != "running")
                             .order_by(FbaInventoryRefresh.id.desc()).limit(1))).scalar()


async def rows_for(db, refresh_id: int) -> list[dict]:
    rows = (await db.execute(select(FbaInventoryRow).where(FbaInventoryRow.refresh_id == refresh_id)
                             .order_by(FbaInventoryRow.seller_sku))).scalars().all()
    out = []
    for r in rows:
        d = {"seller_sku": r.seller_sku, "asin": r.asin, **{k: getattr(r, k) for k in QUANTITY_FIELDS}}
        d["held"] = sum(d[k] for k in QUANTITY_FIELDS[:-1])
        out.append(d)
    return out


async def purge(db, keep: int = KEEP_RUNS) -> int:
    ids = (await db.execute(select(FbaInventoryRefresh.id).order_by(FbaInventoryRefresh.id.desc())
                            .offset(keep))).scalars().all()
    if ids:
        await db.execute(delete(FbaInventoryRow).where(FbaInventoryRow.refresh_id.in_(ids)))
        await db.execute(delete(FbaInventoryRefresh).where(FbaInventoryRefresh.id.in_(ids)))
    return len(ids)
```

`app/inventory/refresh.py`:

```python
"""Fetch -> guard -> store. One at a time; `finally` clears the flag (a cancelled task must not
leave every later refresh refused, the Portfolio refresh lesson)."""
from __future__ import annotations

import logging
from datetime import datetime

from app.database import async_session
from app.inventory import fetch, logic, repository
from app.models import FbaInventoryRefresh, FbaInventoryRow

logger = logging.getLogger(__name__)
STATE = {"running": False}


async def run(source: str, *, get=None) -> dict:
    if STATE["running"]:
        return {"refused": True}
    STATE["running"] = True
    try:
        async with async_session() as db:
            run_row = FbaInventoryRefresh(source=source, status="running", started_at=datetime.utcnow())
            db.add(run_row)
            await db.commit()
            try:
                rows = logic.reduce_summaries(await fetch.fetch_summaries(get=get))
                if logic.looks_empty(rows):
                    run_row.status = "refused"
                    run_row.error = (f"Amazon listed {len(rows)} FBA SKUs and every one at zero, "
                                     "so the answer was not used; yesterday's stock stands.")
                else:
                    for r in rows:
                        db.add(FbaInventoryRow(refresh_id=run_row.id, seller_sku=r["seller_sku"], asin=r["asin"],
                                               fnsku=r["fnsku"], amazon_updated_at=r["amazon_updated_at"],
                                               **{k: r[k] for k in logic.QUANTITY_FIELDS}))
                    run_row.status = "done"
                run_row.sku_count = len(rows)
                run_row.units = sum(r["held"] for r in rows)
            except Exception as exc:  # noqa: BLE001 - recorded, shown on screen
                logger.warning("FBA inventory read failed: %s", exc)
                run_row.status = "failed"
                run_row.error = str(exc)[:500]
            run_row.finished_at = datetime.utcnow()
            await repository.purge(db)
            await db.commit()
            return {"id": run_row.id, "status": run_row.status, "sku_count": run_row.sku_count,
                    "units": run_row.units, "error": run_row.error}
    finally:
        STATE["running"] = False
```

- [ ] **Step 4:** run → PASS. Add the source test:

```python
import ast
from pathlib import Path

@pytest.mark.parametrize("module", ["logic", "fetch", "repository", "refresh"])
def test_inventory_never_writes_to_amazon(module):
    text = (Path(__file__).resolve().parent.parent / "app" / "inventory" / f"{module}.py").read_text(encoding="utf-8")
    names = {n.attr for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Attribute)} | \
            {a.name for n in ast.walk(ast.parse(text)) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not names & {"_post", "create_inbound_plan", "confirm_placement", "apply_changes",
                        "save_economics_daily", "set_packing_information"}
```

- [ ] **Step 5: commit** `feat(inventory): page the FBA Inventory API and keep the last 30 runs`.

---

### Task 4: Nightly job

**Files:** Modify `app/scheduler.py`; Test `tests/test_retention_and_scheduler.py` (or the file that asserts `portfolio_refresh` registration — find with `grep -n "portfolio_refresh" tests/*.py`).

- [ ] **Step 1: failing test** beside the portfolio job test:

```python
def test_fba_stock_is_read_at_0745_ist_after_the_sales_job(...same fixture as the portfolio test...):
    from app import scheduler
    assert scheduler.FBA_INVENTORY_IST == (7, 45)
    assert scheduler.FBA_INVENTORY_IST > scheduler.PORTFOLIO_REFRESH_IST
    # and the registered job ids include "fba_inventory_refresh" on the orders-only flag set
```

(Copy the exact arrangement of the existing job-id test for `portfolio_refresh` with `ORDER_REFRESH_ENABLED` only, and add `"fba_inventory_refresh"` to the expected set.)

- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** in `app/scheduler.py` beside `PORTFOLIO_REFRESH_IST` add `FBA_INVENTORY_IST = (7, 45)`; add

```python
async def scheduled_fba_inventory():
    """Read FBA stock (on hand + every inbound state) for Projections and the Shipment tab. Read-only."""
    if not get_settings().spapi_configured:
        logger.debug("FBA inventory skipped: SP-API is not configured")
        return
    from app.inventory import refresh as inventory_refresh
    result = await inventory_refresh.run("nightly")
    logger.info("FBA inventory: %s", result)
```

and after the portfolio job registration:

```python
    inventory_utc = ist.utc_hhmm(*FBA_INVENTORY_IST)
    scheduler.add_job(
        scheduled_fba_inventory,
        CronTrigger(hour=inventory_utc[0], minute=inventory_utc[1]),
        id="fba_inventory_refresh", replace_existing=True, max_instances=1, coalesce=True,
    )
    parts.append(f"FBA stock at {ist.label(*FBA_INVENTORY_IST)}")
```

- [ ] **Step 4:** run the scheduler tests → PASS.
- [ ] **Step 5: commit** `feat(inventory): read FBA stock every morning at 07:45 IST`.

---

### Task 5: Projections figures (service + logic)

**Files:** Modify `app/portfolio/repository.py`, `app/projections/logic.py`, `app/projections/service.py`; Test `tests/test_projections.py`

**Interfaces:**
- Consumes: `inventory.repository.latest_good`, `latest`, `rows_for`; `logic.units_by_asin`.
- Produces: `portfolio.repository.parent_of_children(db) -> dict[str, str]`; `projections.logic.to_purchase(total_stock, fba_kg) -> float | None`; row keys `fba_kg`, `to_purchase`; `sum_rows` keys `fba_kg`, `to_purchase`; payload keys `fba_stock` = `{"as_of": iso|None, "status": "done"|"failed"|"refused"|None, "error": str|None, "stale": bool, "unmapped": [{"product", "kg"}], "unknown_weight": [sku...]}`; `service.stock_by_key` returns `{key: {"total_stock", "to_purchase"}}`.

- [ ] **Step 1: failing tests** (append to `tests/test_projections.py`; extend the `profit` fixture to monkeypatch the stock inputs):

```python
def test_to_purchase_is_clamped_and_a_dash_when_unknown():
    assert logic.to_purchase(3205.0, 1426.0) == pytest.approx(1779.0)
    assert logic.to_purchase(500.0, 900.0) == 0.0
    assert logic.to_purchase(None, 900.0) is None and logic.to_purchase(500.0, None) is None


def test_the_total_to_purchase_is_the_sum_of_CLAMPED_rows():
    """One product in surplus must never cancel two short ones (the Orders tab's to_buy rule)."""
    rows = [{"to_purchase": 0.0, "fba_kg": 900.0}, {"to_purchase": 100.0, "fba_kg": 0.0},
            {"to_purchase": 50.0, "fba_kg": 10.0}]
    assert logic.sum_rows(rows)["to_purchase"] == 150.0


@pytest.fixture
def stock(monkeypatch):
    """FBA units per child ASIN, the MRP weights, and the child -> row mapping."""
    state = {"units": {"B0CHANA1": 1000, "B0CHANA2": 200, "B0RICE1": 50, "B0ORPHAN": 30},
             "weights": {"B0CHANA1": 0.5, "B0CHANA2": 2.0, "B0RICE1": 1.0, "B0ORPHAN": 1.0},
             "names": {"B0ORPHAN": "Old Thing"},
             "parents": {"B0CHANA1": "B0CHANA", "B0CHANA2": "B0CHANA", "B0RICE1": "B0RICE",
                         "B0ORPHAN": "B0GONE"},
             "run": {"as_of": "2026-10-13T02:15:00", "status": "done", "error": None}}

    async def fake_stock(db):
        return state["units"], state["run"]

    async def fake_catalogue():
        return ({a: {"weight": w, "name": state["names"].get(a, a)} for a, w in state["weights"].items()},
                None, "sheet")

    async def fake_parents(db):
        return state["parents"]

    monkeypatch.setattr(service, "_fba_units", fake_stock)
    monkeypatch.setattr("app.shipment.catalogue.load_catalogue", fake_catalogue)
    monkeypatch.setattr("app.portfolio.repository.parent_of_children", fake_parents)
    monkeypatch.setattr("app.portfolio.repository.flavour_keys", lambda db, cat: _async({}))
    return state


async def _async(v):
    return v


async def test_fba_kg_weighs_each_child_and_to_purchase_follows(profit, stock, db):
    rows = {r["product"]: r for r in (await service.build(db))["rows"]}
    chana = rows["Chana Sattu"]
    assert chana["fba_kg"] == pytest.approx(1000 * 0.5 + 200 * 2.0)            # 900 kg
    assert chana["to_purchase"] == pytest.approx(max(0.0, chana["total_stock"] - 900.0), abs=0.1)
    assert rows["Jau Sattu"]["fba_kg"] == 0.0          # held nothing: a real zero once a run exists


async def test_stock_for_a_row_not_on_screen_is_named_not_dropped(profit, stock, db):
    data = await service.build(db)
    assert data["fba_stock"]["unmapped"] == [{"product": "Old Thing", "kg": 30.0}]


async def test_no_good_run_is_a_dash_never_buy_everything(profit, stock, db):
    stock["units"], stock["run"] = None, {"as_of": None, "status": None, "error": None}
    service._CACHE.clear()
    rows = (await service.build(db))["rows"]
    assert all(r["fba_kg"] is None and r["to_purchase"] is None for r in rows)


async def test_an_unknown_pack_weight_leaves_that_rows_fba_kg_a_dash(profit, stock, db):
    stock["weights"]["B0CHANA2"] = None
    service._CACHE.clear()
    chana = next(r for r in (await service.build(db))["rows"] if r["product"] == "Chana Sattu")
    assert chana["fba_kg"] is None and chana["to_purchase"] is None
    assert "B0CHANA2" in (await service.build(db))["fba_stock"]["unknown_weight"]
```

(The existing `profit` fixture's parents carry `parent_asin` B0CHANA / B0JAU / B0RICE / B0NEW / B0NOWT; keep them.) Existing tests that do not use `stock` must still pass: give `_fba_units` a default that returns `(None, {"as_of": None, "status": None, "error": None})` when no inventory run exists, which an empty test DB yields naturally.

- [ ] **Step 2:** run → FAIL.

- [ ] **Step 3: implement**

`app/portfolio/repository.py` (near `flavour_keys`):

```python
async def parent_of_children(db: AsyncSession) -> dict[str, str]:
    """child ASIN -> Amazon's parent ASIN, from every child ever stored (ASIN grain)."""
    out: dict[str, str] = {}
    for parent, child in await db.execute(
            select(EconomicsDaily.parent_asin, EconomicsDaily.child_asin)
            .where(EconomicsDaily.seller_sku == ASIN_GRAIN, EconomicsDaily.parent_asin.is_not(None))
            .distinct()):
        out.setdefault(child, parent)
    return out
```

`app/projections/logic.py`: add

```python
def to_purchase(total_stock, fba_kg):
    """What to buy before warehouse stock: never negative, a dash when either side is unknown."""
    if total_stock is None or fba_kg is None:
        return None
    return round(max(0.0, total_stock - fba_kg), 1)
```

and in `sum_rows` add `"fba_kg": total("fba_kg"), "to_purchase": total("to_purchase")` (rows are already clamped, so the sum is of clamped figures).

`app/projections/service.py`:
- add `STALE_HOURS = 36`.
- add:

```python
async def _fba_units(db) -> tuple[dict[str, int] | None, dict]:
    """(units per child ASIN from the latest GOOD run, or None; what the newest run said)."""
    from app.inventory import logic as inv_logic, repository as inv_repo
    good = await inv_repo.latest_good(db)
    newest = await inv_repo.latest(db)
    info = {"as_of": good.finished_at.isoformat() if good and good.finished_at else None,
            "status": newest.status if newest else None,
            "error": newest.error if newest and newest.status != "done" else None}
    if good is None:
        return None, info
    return inv_logic.units_by_asin(await inv_repo.rows_for(db, good.id)), info


async def _fba_by_key(db, units: dict[str, int] | None):
    """({row key: kg or None}, unknown-weight child ASINs, catalogue, {child: row key})."""
    from app.portfolio import repository as pf_repo
    from app.shipment import catalogue
    if units is None:
        return {}, [], {}, {}
    products, _warning, _source = await catalogue.load_catalogue()
    flavours = await pf_repo.flavour_keys(db, products)
    parents = await pf_repo.parent_of_children(db)
    kg: dict[str, float | None] = {}
    unknown = []
    keys: dict[str, str] = {}
    for child, n in units.items():
        key = keys[child] = flavours.get(child) or parents.get(child) or child
        weight = (products.get(child) or {}).get("weight")
        if not n:
            kg.setdefault(key, 0.0)
            continue
        if not weight:
            unknown.append(child)
            kg[key] = None
            continue
        if key in kg and kg[key] is None:
            continue
        kg[key] = round((kg.get(key) or 0.0) + n * float(weight), 3)
    return kg, unknown, products, keys
```

- in `_version`, append the latest inventory run id: `(await db.execute(select(func.max(FbaInventoryRefresh.id)))).scalar()` (import `FbaInventoryRefresh`).
- in `build`, after `rows = _rows(...)` and before the inactive filter:

```python
    units, run_info = await _fba_units(db)
    fba_kg, unknown, products, keys = await _fba_by_key(db, units)
    shown_all = {r["key"] for r in rows}
    for r in rows:
        r["fba_kg"] = None if units is None else fba_kg.get(r["key"], 0.0)
        r["to_purchase"] = logic.to_purchase(r["total_stock"], r["fba_kg"])
    unmapped_kg: dict[str, float] = {}
    for child, key in keys.items():
        weight = (products.get(child) or {}).get("weight")
        if key in shown_all or not units[child] or not weight:
            continue
        name = (products.get(child) or {}).get("name") or child
        unmapped_kg[name] = round(unmapped_kg.get(name, 0.0) + units[child] * float(weight), 1)
    unmapped = [{"product": n, "kg": k} for n, k in sorted(unmapped_kg.items(), key=lambda x: -x[1])]
```

`_fba_by_key` therefore returns four values: `(kg_by_key, unknown, products, keys)` where `keys = {child: row key}` for every child in `units` (and `({}, [], {}, {})` when `units is None`). Store on the payload:

```python
        "fba_stock": {**run_info, "stale": _stale(run_info["as_of"]),
                      "unmapped": unmapped, "unknown_weight": sorted(unknown)},
```

with

```python
def _stale(as_of: str | None) -> bool:
    if not as_of:
        return True
    return (datetime.utcnow() - datetime.fromisoformat(as_of)).total_seconds() > STALE_HOURS * 3600
```

(`unmapped` is computed against ALL rows including inactive ones before the inactive filter, so a product hidden behind "Show inactive" is named rather than lost.)
- `stock_by_key` returns `{r["key"]: {"total_stock": r["total_stock"], "to_purchase": r["to_purchase"]} for r in data["rows"]}`; update `app/routers/portfolio._attach_stock` to set `parent["total_stock"] = (stock.get(id) or {}).get("total_stock")` (and `to_purchase` arrives in Task 9). Update `test_the_profit_tab_shows_the_SAME_total_stock` accordingly only if it reads `stock_by_key` directly (it goes through `_attach_stock`, so it should pass unchanged).

- [ ] **Step 4:** `venv/Scripts/python -m pytest tests/test_projections.py tests/test_inventory.py -q` → PASS.
- [ ] **Step 5: commit** `feat(projections): FBA stock in kg and To purchase per product`.

---

### Task 6: Projections screen, refresh button, downloads

**Files:** Modify `app/routers/projections.py`, `templates/projections.html`; Test `tests/test_projections.py`

- [ ] **Step 1: failing tests:**

```python
async def test_refresh_stock_reads_amazon_and_returns_the_new_table(profit, auth_client, monkeypatch):
    calls = []

    async def fake_run(source, **kw):
        calls.append(source)
        return {"id": 1, "status": "done", "sku_count": 1, "units": 5, "error": None}
    monkeypatch.setattr("app.inventory.refresh.run", fake_run)
    r = await auth_client.post("/projections/refresh-stock")
    assert r.status_code == 200 and calls == ["button"] and "rows" in r.json()


async def test_refresh_stock_while_one_runs_is_409(profit, auth_client, monkeypatch):
    async def busy(source, **kw):
        return {"refused": True}
    monkeypatch.setattr("app.inventory.refresh.run", busy)
    assert (await auth_client.post("/projections/refresh-stock")).status_code == 409


def test_in_fba_and_to_purchase_follow_total_stock():
    from tests.js_harness import run_template_js
    order = run_template_js(ROOT / "templates" / "projections.html", [], ["LEGS", "COLS"],
                            "emit(COLS.map(c => c[0]).slice(0, 6));")
    assert order == ["product", "category", "total_stock", "fba_kg", "to_purchase", "ideal_fba"]
```

and extend `test_the_downloads_carry_total_stock_to_hold` to assert `"In FBA / transit" in headers and "To purchase" in headers`. Update `test_category_then_total_stock_follow_the_product_name` (still first four `product, category, total_stock, ...` → change its expected 4th item to `"fba_kg"`).

- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: implement**

Router:

```python
@router.post("/refresh-stock")
async def refresh_stock(include_inactive: bool = False, db: AsyncSession = Depends(get_db),
                        grant=Depends(require_area(permissions.PROJECTIONS))):
    """Read FBA stock from Amazon now (seconds) and return the recalculated table."""
    from app.inventory import refresh as inventory_refresh
    result = await inventory_refresh.run("button")
    if result.get("refused"):
        return JSONResponse({"error": "A stock refresh is already running."}, status_code=409)
    service._CACHE.clear()
    return await service.build(db, include_inactive=include_inactive)
```

In `_table`, after the `ideal_fba` column line make the final columns
`[Column("total_stock", "Total stock to hold", "kg"), Column("fba_kg", "In FBA / transit", "kg"), Column("to_purchase", "To purchase", "kg"), Column("ideal_fba", "Ideal FBA", "kg")]` (drop the old trailing pair).

Template (`templates/projections.html`):
- COLS: insert `["fba_kg", "In FBA / transit", "num"], ["to_purchase", "To purchase", "num"],` after `total_stock`.
- `rowHtml`: after the total_stock cell add
  `` `<td class="num">${kg(r.fba_kg)}</td><td class="num stock" title="Before warehouse stock: subtract what your warehouse holds">${kg(r.to_purchase)}</td>` ``
- `totalsHtml`: after the total_stock cell add `` `<td class="num">${kg(sum(rows, "fba_kg"))}</td><td class="num">${kg(sum(rows, "to_purchase"))}</td>` ``.
- `renderTiles`: tiles become Total stock to hold · In FBA / transit (`t.fba_kg`) · To purchase (`t.to_purchase`, key text "To purchase · before warehouse stock") · Projection, next 30 days.
- `renderCategories`: meta line `to hold · buy ${kg(c.to_purchase)}`.
- controls: add `<button class="btn-outline" id="stock-btn" type="button">Refresh stock</button>` after the lead toggle, and a handler:

```js
$("stock-btn").addEventListener("click", async () => {
  $("stock-btn").disabled = true; $("note").textContent = "Reading FBA stock from Amazon…";
  try{
    const r = await fetch("/projections/refresh-stock" + (includeInactive ? "?include_inactive=true" : ""), {method: "POST"});
    const out = await r.json().catch(() => ({}));
    if(!r.ok){ $("note").textContent = out.error || "Could not read FBA stock."; return; }
    data = out; $("note").textContent = ""; render();
  } finally { $("stock-btn").disabled = false; }
});
```

- `renderMessages`, add before `$("messages").innerHTML = …`:

```js
  const fs = data.fba_stock || {};
  if(!fs.as_of){
    out.push(`<div class="banner warn">FBA stock has not been read from Amazon yet, so In FBA and To purchase are blank. Press Refresh stock.</div>`);
  }else if(fs.status && fs.status !== "done"){
    out.push(`<div class="banner error">The last FBA stock read did not work (${esc(fs.error || fs.status)}). Showing stock as of ${esc(fs.as_of.slice(0, 16).replace("T", " "))} UTC.</div>`);
  }else if(fs.stale){
    out.push(`<div class="banner warn">FBA stock is from ${esc(fs.as_of.slice(0, 10))}, more than a day old. Press Refresh stock.</div>`);
  }
  if((fs.unknown_weight || []).length){
    out.push(`<div class="banner warn">No pack weight in the MRP sheet for stock held under: ${fs.unknown_weight.map(esc).join(", ")}.</div>`);
  }
  if((fs.unmapped || []).length){
    out.push(`<div class="banner warn">Stock held for products not in this table: ${fs.unmapped.map(x => esc(x.product) + " " + kg(x.kg)).join(", ")}.</div>`);
  }
```

- subtitle: append ` · FBA stock as of ${...}` when `data.fba_stock.as_of` (local time via `new Date(as_of + "Z").toLocaleString("en-IN")` — an instant, not a date-only string, so `test_local_dates.py` allows it).

- [ ] **Step 4:** run `tests/test_projections.py tests/test_local_dates.py tests/test_theme.py tests/test_template_render_targets.py -q` → PASS. Preview locally (`preview_start tracker`, open `/portfolio-page/projections`, press Refresh stock — the local `.env` has SP-API credentials) and check the columns, tiles and banners.
- [ ] **Step 5: commit** `feat(projections): In FBA / transit and To purchase on screen, Refresh stock button, downloads`.

---

### Task 7: Shipment tab uses the API stock

**Files:** Modify `app/routers/shipment.py`, `templates/shipment.html`; Test `tests/test_shipment_stock_csv.py`

**Interfaces — Produces:** `async def fba_stock_for_plan(stock_content: bytes) -> tuple[dict[str, int], dict]` returning `(units by ASIN, {"source": "api"|"snapshot"|"csv", "as_of": iso|None, "warning": str|None})`.

- [ ] **Step 1: failing tests:**

```python
async def test_generate_uses_a_fresh_api_read_and_names_it(monkeypatch, db_schema):
    from app.routers import shipment as sh

    async def fake_run(source, **kw):
        assert source == "shipment"
        return {"id": 7, "status": "done", "sku_count": 1, "units": 340, "error": None}

    async def fake_units(db):
        return {"B0GCGGRTBK": 340}, {"as_of": "2026-10-13T09:12:00", "status": "done", "error": None}
    monkeypatch.setattr("app.inventory.refresh.run", fake_run)
    monkeypatch.setattr("app.projections.service._fba_units", fake_units)
    units, info = await sh.fba_stock_for_plan(REAL_HEADER_24)
    assert units == {"B0GCGGRTBK": 340} and info["source"] == "api"


async def test_amazon_down_falls_back_to_a_recent_snapshot_then_to_the_csv(monkeypatch, db_schema):
    from app.routers import shipment as sh
    from datetime import datetime, timedelta

    async def failed(source, **kw):
        return {"id": 8, "status": "failed", "sku_count": 0, "units": 0, "error": "503"}
    recent = (datetime.utcnow() - timedelta(hours=3)).isoformat()
    state = {"as_of": recent}

    async def snap(db):
        return {"B0X0000001": 5}, {"as_of": state["as_of"], "status": "failed", "error": "503"}
    monkeypatch.setattr("app.inventory.refresh.run", failed)
    monkeypatch.setattr("app.projections.service._fba_units", snap)
    units, info = await sh.fba_stock_for_plan(REAL_HEADER_24)
    assert info["source"] == "snapshot" and units == {"B0X0000001": 5} and info["warning"]
    state["as_of"] = (datetime.utcnow() - timedelta(hours=40)).isoformat()
    row = b"0.5kg cs 1 FBA,X0,B0CWGXYLT6,M,New,179,No,0,Yes,57,100,5,7,282,1,0,170,0,0,0,0,0,100,store\n"
    units, info = await sh.fba_stock_for_plan(REAL_HEADER_24 + row)
    assert info["source"] == "csv" and units == {"B0CWGXYLT6": 277}       # N 282 - unsellable 5
```

Rewrite the two old-rule tests (`test_the_owners_real_column_layout_parses`, `test_the_three_double_counting_columns_are_never_summed`) to the new CSV rule: when `afn-total-quantity` is present a FBA row counts `N − L`; the nine-column sum is used only when N is absent. State in the docstring that the assertion flipped on 13 Oct because column Q (inbound shipped) under-reported 2,925 units while N carried them. Keep a test that W (onhand-buyable) and J (warehouse) are never added.

- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: implement**
  - In `parse_stock_csv`, inside the row loop: `if has_total: total = max(0, int(_clean_number(row.get("afn-total-quantity"))) - int(_clean_number(row.get("afn-unsellable-quantity", 0))))` else the nine-column sum; `has_total = "afn-total-quantity" in df.columns`; count `afn-total-quantity` as a recognised quantity column for the no-column guard. Update the comment block above `stock_cols` to record the 13 Oct finding.
  - Add:

```python
SNAPSHOT_MAX_HOURS = 36


async def fba_stock_for_plan(stock_content: bytes) -> tuple[dict[str, int], dict]:
    """FBA stock for a new plan: a fresh API read, else a recent good run, else the uploaded CSV.

    The CSV still supplies sales and merchant SKUs; only the stock figure moves to the API,
    because the CSV's inbound-shipped column missed 2,925 units already on their way (13 Oct)."""
    from datetime import datetime
    from app.database import async_session
    from app.inventory import refresh as inventory_refresh
    from app.projections import service as projections

    result = await inventory_refresh.run("shipment")
    async with async_session() as db:
        units, info = await projections._fba_units(db)
    fresh = result.get("status") == "done"
    if units is not None and fresh:
        return units, {"source": "api", "as_of": info["as_of"], "warning": None}
    if units is not None and info["as_of"]:
        age = (datetime.utcnow() - datetime.fromisoformat(info["as_of"])).total_seconds() / 3600
        if age <= SNAPSHOT_MAX_HOURS:
            return units, {"source": "snapshot", "as_of": info["as_of"], "warning": (
                f"Amazon's stock could not be read just now ({result.get('error') or 'busy'}), so FBA "
                f"stock is this morning's reading from {info['as_of'][:16].replace('T', ' ')} UTC.")}
    return parse_stock_csv(stock_content), {"source": "csv", "as_of": None, "warning": (
        "Amazon's stock could not be read and there is no recent reading, so FBA stock comes from the "
        "uploaded file (Amazon's total minus damaged).")}
```

  - In `generate_plan`, replace `stock = parse_stock_csv(stock_content)` with: keep the `try: parse_stock_csv(...)` call as validation (so a bad file still 400s), then `stock, stock_info = await fba_stock_for_plan(stock_content)`; add `"stock_source": stock_info` to `payload["catalogue"]`; `if stock_info["warning"]: warnings.insert(0, stock_info["warning"])` (insert after the sheet warning).
  - `templates/shipment.html`, in the `if(catalogueInfo)` block after the sheet sentence:

```js
    const ss = c.stock_source || {};
    if(ss.source === "api") bits.push(`<br/>FBA stock read from Amazon just now, every inbound shipment included.`);
    else if(ss.source === "snapshot") bits.push(`<br/><strong>FBA stock is this morning's reading</strong>; Amazon could not be read just now.`);
    else if(ss.source === "csv") bits.push(`<br/><strong>FBA stock is from the uploaded file</strong>; Amazon could not be read.`);
```

  - Existing generate tests: they post a stock CSV with no SP-API in tests; `refresh.run` will fail (`SpApiNotConfigured`) and no snapshot exists → CSV path, so their numbers come from `parse_stock_csv` as before. Any fixture that includes `afn-total-quantity` changes rule: fix those fixtures' N to equal the old sum + L, or update expected numbers, recording why.

- [ ] **Step 4:** `venv/Scripts/python -m pytest tests/ -q -k "shipment"` → PASS.
- [ ] **Step 5: commit** `fix(shipment): FBA stock from Amazon's API at generate; CSV fallback counts Amazon's total minus damaged`.

---

### Task 8: Mutation harness, docs, full suite, deploy stage 1

**Files:** Create `scripts/mutate_inventory.py` (model on `scripts/mutate_projections.py`: a list of `(file, old, new, tests)` and the shared runner); Modify `CLAUDE.md`

- [ ] **Step 1:** mutations (each must be caught):
  1. `logic.py`: drop `"inbound_shipped",` from `QUANTITY_FIELDS` order use → replace `HELD_FIELDS = QUANTITY_FIELDS[:-1]` with `HELD_FIELDS = tuple(f for f in QUANTITY_FIELDS[:-1] if f != "inbound_shipped")`
  2. `HELD_FIELDS = QUANTITY_FIELDS[:-1]` → `HELD_FIELDS = QUANTITY_FIELDS` (counts damaged)
  3. `_channel_of(sku) != CHANNEL_FBA` → `False` (includes flex)
  4. `repository.latest_good`: `== "done"` → `!= "running"`
  5. `projections/logic.to_purchase`: `max(0.0, total_stock - fba_kg)` → `total_stock - fba_kg`
  6. `service._fba_by_key`: `kg[key] = None` (unknown weight) → `continue`
  7. `service.build`: `None if units is None else fba_kg.get(r["key"], 0.0)` → `fba_kg.get(r["key"], 0.0)`
  8. `shipment.fba_stock_for_plan`: `if units is not None and fresh:` → `if units is not None:` (stale snapshot passed as fresh)
  9. `shipment.parse_stock_csv`: the `has_total` branch removed (back to nine columns)
  10. `refresh.run`: `if logic.looks_empty(rows):` → `if False:`
  11. `fetch.fetch_summaries`: the `raise InventoryFetchError` → `return out`
  12. `scheduler`: `FBA_INVENTORY_IST = (7, 45)` → `(7, 15)`
- [ ] **Step 2:** `venv/Scripts/python scripts/mutate_inventory.py` → `All 12 mutations caught`. Also re-run `scripts/mutate_projections.py` (must still be 17/17) and fix any pinned string it needs re-pointed.
- [ ] **Step 3:** CLAUDE.md: add `app/inventory/` to Key Files; a section "FBA stock — from the Inventory API, every morning" under Projections recording the 13 Oct measurement (7,711 vs 10,636; the 17 SKUs; API inbound shipped), the rule, the run statuses, the 07:45 job, the Shipment source order, and that the CSV rule flipped to N − L; update the scheduler line and the test count.
- [ ] **Step 4:** `venv/Scripts/python -m pytest -q` → all green.
- [ ] **Step 5:** commit, push, deploy: on the box `git fetch origin claude/stoic-allen-bb3a55 && git checkout origin/claude/stoic-allen-bb3a55 -- deploy/update-ec2.sh`, then `echo y | bash deploy/update-ec2.sh`; check `venv/bin/alembic current` = `a7d3c9e15b20`.
- [ ] **Step 6: verify on production:** press Refresh stock (or `venv/bin/python -c "import asyncio; from app.inventory import refresh; print(asyncio.run(refresh.run('button')))"`); confirm `status done`, ~350 SKUs read / ~240 FBA; Projections shows In FBA / transit ≈ live kg (≈7,700 kg on 13 Oct) and To purchase = max(0, Total − FBA) per row; `journalctl -u tracker | grep Scheduler` lists `FBA stock at 07:45 IST`.

---

### Task 9: Profit tab "To purchase" (stage 2)

**Files:** Modify `app/routers/portfolio.py` (`_attach_stock`), `app/portfolio/columns.py`, `app/portfolio/export.py`, `templates/portfolio.html`; Tests `tests/test_projections.py`, `tests/test_portfolio_columns.py`, `tests/test_portfolio_columns_render.py`

- [ ] **Step 1: failing tests:**
  - in `test_projections.py`, extend `test_the_profit_tab_shows_the_SAME_total_stock` to also assert `data["parents"][0]["to_purchase"] == to_purchase_by_key["B0CHANA"]` (from `/projections/last?include_inactive=true` rows) and `data["parents"][1]["to_purchase"] is None`; extend the failure test to assert `to_purchase is None`.
  - `tests/test_portfolio_columns.py`: the expected id list gains `"to_purchase"` after `"total_stock"` (19 columns).
  - `tests/test_portfolio_columns_render.py`: VOCAB gains `{id:"to_purchase",label:"To purchase",locked:false}` after total_stock; bump the count it asserts by one.
  - a Node test (in `test_projections.py`):

```python
def test_the_profit_to_purchase_column_is_product_rows_only_and_totals_its_rows():
    from tests.js_harness import run_portfolio_js
    out = run_portfolio_js('''
const d = COLUMN_DEFS.to_purchase;
const t = computeTotals([{to_purchase: 100, sizes: []}, {to_purchase: null, sizes: []}, {to_purchase: 50.4, sizes: []}]);
emit([d.row({to_purchase: 1486.5}), d.detail({}), t.purchase, t.purchased]);''')
    assert out == ["1,487 kg", '<span class="dim">—</span>', 150.4, 2]
```

(If `computeTotals` needs more fields on each row, copy the minimal row shape used by the existing `total_stock` Node test.)
  - export: `tests/test_portfolio_export*.py` — assert `COLUMN_KINDS["to_purchase"] == "kg"` and `export.totals(rows)["to_purchase"]` sums the rows' figures.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: implement** (mirror `total_stock` exactly):
  - `_attach_stock`: `entry = stock.get(parent.get("parent_asin")) or {}`; `parent["total_stock"] = entry.get("total_stock")`; `parent["to_purchase"] = entry.get("to_purchase")`.
  - `columns.py`: `{"id": "to_purchase", "label": "To purchase", "locked": False}` after `total_stock`.
  - `export.py`: `COLUMN_KINDS["to_purchase"] = "kg"`; in `totals()` `purchased = [r["to_purchase"] for r in rows if r.get("to_purchase") is not None]` and `"to_purchase": sum(purchased) if purchased else None`.
  - `templates/portfolio.html`: `FIELDS.to_purchase = {label: "To purchase (kg)", get: r => r.to_purchase, kind: "num"}`; `COLUMN_DEFS.to_purchase = {num: true, sortKey: "to_purchase", width: 110, title: "Total stock to hold minus stock at FBA or on its way, before warehouse stock", row: r => kg(r.to_purchase), detail: r => '<span class="dim">—</span>', total: t => t.purchased ? kg(t.purchase) : '<span class="dim">—</span>'}` (copy the `label`/`title` keys the `total_stock` def uses); `FALLBACK_ORDER` gains `"to_purchase"` after `"total_stock"`; `computeTotals` gains `purchasedRows`, `purchase`, `purchased` like `stockedRows` and returns them.
- [ ] **Step 4:** `venv/Scripts/python -m pytest -q` → green; run `scripts/mutate_portfolio_columns.py`, `mutate_portfolio_ui.py`, `mutate_portfolio_active_weight.py` (all must still print `All N mutations caught`; re-point any entry that a new column shifted). Add two mutations to `scripts/mutate_inventory.py`: `_attach_stock` drops `parent["to_purchase"] = ...`; the Profit `detail` renderer returns `kg(r.to_purchase)`.
- [ ] **Step 5:** CLAUDE.md: the Profit tab paragraph gains "To purchase"; commit `feat(profit): To purchase column beside Total stock to hold`; push; deploy (no migration); verify on production that the Profit "To purchase" equals Projections for every product row (script: fetch both payloads with a minted session cookie and diff by `parent_asin` — expect 0 mismatches).
