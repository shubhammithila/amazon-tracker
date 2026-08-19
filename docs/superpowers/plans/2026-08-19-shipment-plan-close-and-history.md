# Shipment Plan Close, Carry-Forward and History — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner close a shipment plan, carry packed-but-unshipped days onto the next plan without re-packing them, and read any closed plan's day-wise packing, invoice numbers and Amazon shipment ids for reconciliation.

**Architecture:** A packed-but-unshipped day is moved between plans by updating one column (`ShipmentPackingDay.plan_id`) and stamping `carried_from_plan_id`. No new arithmetic is added anywhere, because every aggregation in `app/shipment/logic.py` reaches days through `repository.load_days(plan_id)`, which filters on `plan_id` alone. History is exposed by widening the existing single-source readers (`_document_rows`, `_plan_payload`) to take an optional plan id rather than always asking for the active plan.

**Tech Stack:** FastAPI (async), SQLAlchemy async, SQLite locally / PostgreSQL in production, Alembic (`batch_alter_table`), pytest with `anyio`, vanilla JS templates.

## Global Constraints

- **Design source:** `docs/superpowers/specs/2026-08-19-shipment-plan-close-and-history-design.md`. Every decision and rejection is recorded there; do not re-litigate them in code.
- **Run tests with:** `venv/Scripts/python -m pytest -q` from the repo root. The suite is 1043 tests and runs in random order — never rely on test ordering.
- **`get_active_plan()` must keep matching `status == "active"` and nothing else.** That omission is what keeps 11 packing endpoints draft-blind. Widen it and the warehouse packs unfinished plans.
- **`logic.remaining_for` must keep its two-argument signature** `(planned, packed)`. A test asserts the signature itself. Never add an `available` parameter.
- **`_recompute_day_units` must never touch `total_cartons`.**
- **Never use `round(n/10)*10`** — use `logic.round_to_10`.
- **Colour lives only in `static/theme.css`.** `tests/test_theme.py` fails any template that hardcodes a hex/rgba colour or re-declares `:root`.
- **Every new migration MUST add a branch to the baseline detector in `deploy/update-ec2.sh`, newest first.** A stale detector is a failed deploy — it has already stamped production backwards once.
- **Alembic head is currently `9e4b1c7a2f56`.** The new migration's `down_revision` is that value.
- **Commit after every task**, and keep the full suite green at each commit.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `app/models.py` | `ShipmentPackingDay.carried_from_plan_id`, `ShipmentPlan.closed_at` | Modify |
| `alembic/versions/<new>_close_plans_and_carry_days.py` | The two nullable columns | Create |
| `deploy/update-ec2.sh` | New newest-first detector branch | Modify |
| `app/shipment/logic.py` | `carriable_days()` — pure eligibility rule | Modify |
| `app/shipment/repository.py` | `carry_days_to_plan()`, `close_plan()`, `list_plans()`, `days_blocking_close()` | Modify |
| `app/routers/shipment.py` | `POST /plan/{id}/close`, `GET /plans`, `GET /plan/{id}/detail`, `?plan_id=` on downloads, `attach_invoice` fix | Modify |
| `templates/shipment.html` | Close button, history panel, carried-in badge | Modify |
| `tests/test_shipment_close_and_history.py` | Every property in the spec's Verification section | Create |

Tasks 1–4 are backend and independently committable. Task 5 is the router surface. Task 6 is the UI. Task 7 is the deploy gate and manual rehearsal.

---

### Task 1: Schema — the two nullable columns

**Files:**
- Modify: `app/models.py` (`ShipmentPlan` ~line 201, `ShipmentPackingDay`)
- Create: `alembic/versions/b2f7c1a94e05_close_plans_and_carry_days.py`
- Modify: `deploy/update-ec2.sh` (baseline detector, ~line 307)
- Test: `tests/test_shipment_close_and_history.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ShipmentPackingDay.carried_from_plan_id` (`Integer`, nullable), `ShipmentPlan.closed_at` (`DateTime`, nullable). Revision id `b2f7c1a94e05`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_shipment_close_and_history.py`:

```python
"""Closing a shipment plan, carrying its unshipped days, and reading it back.

Asked for: *"lets give a button to close the shipment plan and if there is some
uninvoiced or unverified units we can carry forward it to the next shipment plan and
we can give a button to view recent or old shipment plan history."*

The live case: 10-18 Aug shipped and invoiced, then 19 Aug packed 400 units in 9
cartons and verified — below the 25-carton threshold, so it cannot ship alone. Sales
data has moved and a new plan is wanted. Those 400 units are in boxes and must not be
packed again.

Two properties carry most of the weight:

* **The DAY moves, not its units.** ``logic.remaining_for`` deliberately ignores
  ``available``, so adding carried units there would tell the packer to box them a
  second time. Moving ``plan_id`` needs no new arithmetic because every aggregation
  reaches days through ``load_days(plan_id)``.
* **A closed plan stays readable.** Closing used to make ``attach_invoice`` 404 and
  ``packed.xlsx`` unavailable, which is the reconciliation hole this closes.
"""
import pytest

from app.models import ShipmentPackingDay, ShipmentPlan

pytestmark = pytest.mark.regression


def test_the_carry_and_close_columns_exist_on_the_models():
    """Declared on the models, so `test_migrations_match_models` compares them.

    `carried_from_plan_id` is the lineage: without it a day that moved is
    indistinguishable from one packed against the new plan, and reconciliation cannot
    explain why a plan shows units for a day it never opened.
    """
    assert hasattr(ShipmentPackingDay, "carried_from_plan_id")
    assert hasattr(ShipmentPlan, "closed_at")
    assert ShipmentPackingDay.__table__.c.carried_from_plan_id.nullable is True
    assert ShipmentPlan.__table__.c.closed_at.nullable is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: FAIL — `AssertionError` on `hasattr(ShipmentPackingDay, "carried_from_plan_id")`.

- [ ] **Step 3: Add the columns to the models**

In `app/models.py`, inside `ShipmentPlan`, after the `created_at` column:

```python
    # When the owner retired this plan. Distinct from a plan merely being superseded:
    # `status` says WHAT it is, this says WHEN it stopped, which is what the history
    # list sorts and labels by. Nullable, so no backfill is needed for plans closed
    # before this column existed.
    closed_at = Column(DateTime)
```

In `app/models.py`, inside `ShipmentPackingDay`, after `destination_state`:

```python
    # The plan this day was packed against BEFORE it was carried forward, or NULL if
    # it has always belonged to its current plan.
    #
    # A plain Integer, deliberately NOT a ForeignKey: the source plan can be deleted
    # (DELETE /shipment/plan/{id} cascades), and a FK would either block that delete
    # or null the lineage out. This column's only job is to explain, on screen and in
    # a reconciliation, why a plan holds units for a date it never opened — so an id
    # that no longer resolves is still better than no id at all.
    carried_from_plan_id = Column(Integer)
```

- [ ] **Step 4: Run it to verify it passes**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: PASS (1 test).

- [ ] **Step 5: Confirm the model/migration gate now fails**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py::test_migrations_match_models -q`
Expected: FAIL — the models declare columns no migration creates. This is the gate working; Step 6 satisfies it.

- [ ] **Step 6: Write the migration**

Create `alembic/versions/b2f7c1a94e05_close_plans_and_carry_days.py`:

```python
"""closed_at on a plan, carried_from_plan_id on a packing day

Two nullable columns, no backfill, so every pre-existing row reads "never closed" and
"never carried" — which is true of all of them.

`carried_from_plan_id` is intentionally NOT a foreign key. The source plan can be
deleted (the plan delete cascades to its items, days and entries), and a FK would
either refuse that delete or null this column out. The value's only purpose is to
explain why a plan holds units for a date it never opened, and a stale id still does
that; a NULL does not.

Revision ID: b2f7c1a94e05
Revises: 9e4b1c7a2f56
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b2f7c1a94e05"
down_revision: Union[str, None] = "9e4b1c7a2f56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table because SQLite rewrites the table for an ALTER; PostgreSQL
    # ignores the batching. Consistent with the migrations around it.
    with op.batch_alter_table("shipment_plans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("closed_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("shipment_packing_days", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("carried_from_plan_id", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("shipment_packing_days", schema=None) as batch_op:
        batch_op.drop_column("carried_from_plan_id")
    with op.batch_alter_table("shipment_plans", schema=None) as batch_op:
        batch_op.drop_column("closed_at")
```

- [ ] **Step 7: Run the schema gate**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py -q`
Expected: `test_migrations_match_models` PASSES; `test_the_deploy_detector_reports_the_head_for_a_head_schema` FAILS, because the detector still answers `9e4b1c7a2f56` for a schema now at `b2f7c1a94e05`. Step 8 fixes it.

- [ ] **Step 8: Add the newest-first detector branch**

In `deploy/update-ec2.sh`, inside the `BASELINE=` heredoc, insert immediately after the `if not tables:` branch and **before** the `product_prices` branch:

```python
elif "shipment_packing_days" in tables and "carried_from_plan_id" in cols("shipment_packing_days"):
    print("b2f7c1a94e05")                           # head: close plans, carry days
elif "product_prices" in tables:
```

- [ ] **Step 9: Run the full schema suite**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py -q`
Expected: PASS. The detector test *runs* the real heredoc, so this proves the deploy will stamp the true head.

- [ ] **Step 10: Run the whole suite**

Run: `venv/Scripts/python -m pytest -q`
Expected: PASS, 1044 tests.

- [ ] **Step 11: Commit**

```bash
git add app/models.py alembic/versions/b2f7c1a94e05_close_plans_and_carry_days.py deploy/update-ec2.sh tests/test_shipment_close_and_history.py
git commit -m "feat: closed_at on a plan, carried_from_plan_id on a packing day

Two nullable columns, no backfill. carried_from_plan_id is deliberately not a
foreign key: the source plan can be deleted, and a stale id still explains why a
plan holds units for a date it never opened.

The deploy baseline detector gets its newest-first branch in the same commit,
because a stale detector is a failed deploy rather than a cosmetic omission."
```

---

### Task 2: `logic.carriable_days` — the pure eligibility rule

**Files:**
- Modify: `app/shipment/logic.py` (after `carry_over`, ~line 685)
- Test: `tests/test_shipment_close_and_history.py`

**Interfaces:**
- Consumes: `logic.STATUS_HELD`, `STATUS_SUBMITTED`, `STATUS_VERIFIED`, `STATUS_SHIPPED`, `logic.packed_units` — all existing.
- Produces:
  - `logic.CARRIABLE_STATUSES: frozenset[str]`
  - `logic.carriable_days(days: Sequence) -> dict` returning `{"carry": [day, ...], "blocked": [{"pack_date": str, "reason": str}, ...], "shipped_uninvoiced": [day, ...]}`

Pure, and separate from the repository, for the same reason the other aggregations are: the eligibility rule is what decides whether real boxes move between plans, and it must be testable without a database.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_shipment_close_and_history.py`:

```python
# ─── The eligibility rule, pure ──────────────────────────────────────────────

from app.shipment import logic


def _day(pack_date, status, units=100, cartons=5, **extra):
    """A day shaped as `load_days_with_entries` returns one."""
    day = {
        "pack_date": pack_date,
        "status": status,
        "total_units": units,
        "total_cartons": cartons,
        "invoice_id": None,
        "inbound_plan_id": None,
        "shipment_confirmation_id": None,
        "entries": [{"asin": "B0AAA00001", "units": units, "note": None}],
    }
    day.update(extra)
    return day


def test_a_held_or_verified_day_with_no_shipment_carries():
    """The live case. 19 Aug is verified, unshipped, uninvoiced — it must move.

    Verified is carriable, not just held: the threshold is not the only reason a day
    sits unshipped, and the owner's approval does not make boxes ship.
    """
    days = [
        _day("2026-08-19", logic.STATUS_VERIFIED, units=400, cartons=9),
        _day("2026-08-20", logic.STATUS_HELD, units=50, cartons=2),
        _day("2026-08-21", logic.STATUS_SUBMITTED, units=80, cartons=3),
    ]
    result = logic.carriable_days(days)
    assert [d["pack_date"] for d in result["carry"]] == [
        "2026-08-19", "2026-08-20", "2026-08-21",
    ]
    assert result["blocked"] == []


def test_a_shipped_day_does_not_carry():
    """Its units are already in the next plan's fba_stock.

    `parse_stock_csv` sums the three afn-inbound columns, so a shipped day is already
    inside `deficit = projection - fba_stock`. Carrying it would count the same units
    twice and inflate every plan after.
    """
    days = [_day("2026-08-18", logic.STATUS_SHIPPED, units=296, cartons=16)]
    result = logic.carriable_days(days)
    assert result["carry"] == []


def test_an_invoiced_day_does_not_carry():
    """A GST number has been spent against those exact quantities."""
    days = [_day("2026-08-17", logic.STATUS_VERIFIED, invoice_id=41)]
    result = logic.carriable_days(days)
    assert result["carry"] == []


def test_a_day_with_a_half_created_amazon_shipment_blocks_the_close():
    """`inbound_plan_id` with no confirmation means a plan exists at Amazon.

    `clear_inbound_plan` scopes its cleanup by (plan_id, inbound_plan_id), so moving
    the day would detach it from the only query that can cancel it — leaving a plan
    Amazon holds and this app can no longer reach.
    """
    days = [_day("2026-08-20", logic.STATUS_VERIFIED, inbound_plan_id="wf123")]
    result = logic.carriable_days(days)
    assert result["carry"] == []
    assert len(result["blocked"]) == 1
    assert result["blocked"][0]["pack_date"] == "2026-08-20"
    assert "amazon" in result["blocked"][0]["reason"].lower()


def test_an_open_day_blocks_the_close():
    """The packer is mid-shift. Moving the day under him loses the count."""
    days = [_day("2026-08-20", logic.STATUS_OPEN)]
    result = logic.carriable_days(days)
    assert result["carry"] == []
    assert len(result["blocked"]) == 1
    assert "open" in result["blocked"][0]["reason"].lower()


def test_an_empty_day_neither_carries_nor_blocks():
    """A day row with no entries is bookkeeping, not boxes. Moving it would put an
    empty date on the new plan and make the history list read as if work happened."""
    days = [_day("2026-08-20", logic.STATUS_HELD, units=0, cartons=0, entries=[])]
    result = logic.carriable_days(days)
    assert result["carry"] == []
    assert result["blocked"] == []


def test_shipped_but_uninvoiced_days_are_reported_separately():
    """They stay on the old plan, but the owner must be told before it closes.

    Nothing carries them — the units are at Amazon. What they need is to stay
    invoice-able, which is the history half of this feature. Silence here is the
    held-days bug again: the boxes leave every screen with no invoice raised.
    """
    days = [
        _day("2026-08-18", logic.STATUS_SHIPPED, invoice_id=None),
        _day("2026-08-17", logic.STATUS_SHIPPED, invoice_id=41),
    ]
    result = logic.carriable_days(days)
    assert [d["pack_date"] for d in result["shipped_uninvoiced"]] == ["2026-08-18"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: FAIL — `AttributeError: module 'app.shipment.logic' has no attribute 'carriable_days'`.

- [ ] **Step 3: Implement `carriable_days`**

In `app/shipment/logic.py`, after `carry_over` ends (before the `# ─── The Amazon inbound-plan body ───` banner):

```python
#: Statuses a day may be carried onto the next plan from. `shipped` is absent and that
#: absence is the arithmetic: `parse_stock_csv` sums the three afn-inbound columns into
#: fba_stock, so a shipped day is ALREADY inside `deficit = projection - fba_stock`.
#: Carrying it would count the same units twice, in every plan after.
CARRIABLE_STATUSES = frozenset({STATUS_HELD, STATUS_SUBMITTED, STATUS_VERIFIED})


def carriable_days(days: Sequence) -> dict:
    """Split a closing plan's days into carry / blocked / shipped-uninvoiced.

    Pure, and separate from the repository on purpose: this decides whether real boxes
    move between plans, so it must be provable without a database.

    Returns ``{"carry": [...], "blocked": [{"pack_date", "reason"}], "shipped_uninvoiced": [...]}``.

    **carry** — packed but not shipped, and not invoiced. These are boxes on the floor
    that Amazon has no record of, so the next plan cannot see them any other way. They
    move as DAYS: every aggregation here reaches days through ``load_days(plan_id)``,
    so updating that one column makes ``packed_units_by_asin`` count them (the packer is
    told to box the remainder, not the whole plan), ``shippable_units_by_asin`` add
    their cartons toward the threshold, and ``carry_over`` combine them with the new
    plan's held days.

    **blocked** — the close must refuse rather than move these:

    * ``inbound_plan_id`` with no ``shipment_confirmation_id``: a plan is half-created
      at Amazon, and ``clear_inbound_plan`` scopes its cleanup by (plan_id,
      inbound_plan_id) — moving the day detaches it from the only query that can
      cancel it.
    * ``open``: the packer is still entering counts.

    **shipped_uninvoiced** — nothing to carry (the units are at Amazon) but the owner
    must be told, because closing used to make ``attach_invoice`` unreachable for them.

    An empty day is neither carried nor blocked: a day row with no entries is
    bookkeeping, not boxes, and moving it would put an idle date on the new plan.
    """
    carry, blocked, shipped_uninvoiced = [], [], []

    for day in days:
        def field(name, default=None):
            return (
                day.get(name, default)
                if isinstance(day, Mapping)
                else getattr(day, name, default)
            )

        status = field("status")
        entries = field("entries") or []
        units = packed_units(entries)

        if status == STATUS_SHIPPED:
            if not field("invoice_id"):
                shipped_uninvoiced.append(day)
            continue

        if status == STATUS_OPEN:
            if units:
                blocked.append({
                    "pack_date": field("pack_date") or "",
                    "reason": "the day is still open — submit or clear it first",
                })
            continue

        if status not in CARRIABLE_STATUSES:
            continue
        if field("invoice_id"):
            continue
        if not units:
            continue

        if field("inbound_plan_id") and not field("shipment_confirmation_id"):
            blocked.append({
                "pack_date": field("pack_date") or "",
                "reason": (
                    "an Amazon shipment is part-created for this day — confirm or "
                    "cancel it before closing the plan"
                ),
            })
            continue

        carry.append(day)

    return {
        "carry": carry,
        "blocked": blocked,
        "shipped_uninvoiced": shipped_uninvoiced,
    }
```

- [ ] **Step 4: Run them to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Verify the tests are not passing vacuously**

Temporarily change `CARRIABLE_STATUSES` to `frozenset({STATUS_HELD})` and re-run.
Expected: `test_a_held_or_verified_day_with_no_shipment_carries` FAILS. Restore the line.

Then temporarily delete the `if field("inbound_plan_id") and not field("shipment_confirmation_id"):` block and re-run.
Expected: `test_a_day_with_a_half_created_amazon_shipment_blocks_the_close` FAILS. Restore it.

If either mutation leaves the suite green, the test is not testing what it claims — fix the test before continuing.

- [ ] **Step 6: Run the whole suite**

Run: `venv/Scripts/python -m pytest -q`
Expected: PASS, 1051 tests.

- [ ] **Step 7: Commit**

```bash
git add app/shipment/logic.py tests/test_shipment_close_and_history.py
git commit -m "feat: logic.carriable_days, the pure carry-forward eligibility rule

Splits a closing plan's days into carry / blocked / shipped-uninvoiced. Shipped is
absent from CARRIABLE_STATUSES on purpose: parse_stock_csv already sums the three
afn-inbound columns into fba_stock, so carrying a shipped day double-counts.

A day with a part-created Amazon inbound plan blocks the close, because
clear_inbound_plan scopes its cleanup by (plan_id, inbound_plan_id) and moving the
day would detach it from the only query that can cancel it."
```

---

### Task 3: `repository.carry_days_to_plan` and `close_plan`

**Files:**
- Modify: `app/shipment/repository.py` (after `finalise_plan`, ~line 201)
- Test: `tests/test_shipment_close_and_history.py`

**Interfaces:**
- Consumes: `logic.carriable_days`, `load_days_with_entries`, `load_plan_items`, `get_plan`, `create_plan`, `STATUS_CLOSED`, `STATUS_DRAFT`.
- Produces:
  - `repository.carry_days_to_plan(db, from_plan_id: int, to_plan_id: int, pack_dates: list[str]) -> list[str]` — returns the dates actually moved.
  - `repository.ensure_rows_for_asins(db, plan_id: int, asins: list[str], source_plan_id: int) -> list[str]` — inserts To-Ship-0 rows, returns ASINs added.
  - `repository.close_plan(db, plan_id: int, to_plan_id: int | None) -> dict` — returns `{"closed": bool, "carried": [dates], "orphan_asins": [asin], "target_plan_id": int}`.

- [ ] **Step 1: Write the failing tests — carrying moves the day and its entries**

Append to `tests/test_shipment_close_and_history.py`:

```python
# ─── Carrying a day between plans ────────────────────────────────────────────

from app.shipment import repository


async def test_carrying_a_day_moves_it_and_stamps_its_origin(db, plan_factory):
    """One column update, and the lineage that explains it.

    The day must appear on the new plan with its entries intact, and the old plan must
    say where it went — otherwise a reconciliation of the old plan shows a date that
    simply vanished.
    """
    old = await plan_factory()
    new = await plan_factory(status=repository.STATUS_DRAFT)
    await repository.save_packing_entries(
        db, old.id, "2026-08-19",
        [{"asin": "B0AAA00001", "units": 400}], cartons=9,
    )

    moved = await repository.carry_days_to_plan(db, old.id, new.id, ["2026-08-19"])
    assert moved == ["2026-08-19"]

    old_days = await repository.load_days_with_entries(db, old.id)
    new_days = await repository.load_days_with_entries(db, new.id)
    assert [d["pack_date"] for d in old_days] == []
    assert [d["pack_date"] for d in new_days] == ["2026-08-19"]
    assert new_days[0]["total_units"] == 400
    assert new_days[0]["total_cartons"] == 9, "the carton count did not travel"
    assert new_days[0]["entries"] == [
        {"asin": "B0AAA00001", "units": 400, "note": None}
    ]
    assert new_days[0]["carried_from_plan_id"] == old.id


async def test_a_carried_day_keeps_its_verified_status(db, plan_factory):
    """19 Aug is verified and must stay so, or the invoice it still needs is blocked.

    Re-opening it would also throw away the owner's approval of numbers he did see.
    """
    old = await plan_factory()
    new = await plan_factory(status=repository.STATUS_DRAFT)
    await repository.save_packing_entries(
        db, old.id, "2026-08-19", [{"asin": "B0AAA00001", "units": 400}], cartons=9,
    )
    day = await repository.get_day(db, old.id, "2026-08-19")
    day.status = logic.STATUS_VERIFIED
    await db.commit()

    await repository.carry_days_to_plan(db, old.id, new.id, ["2026-08-19"])

    carried = await repository.get_day(db, new.id, "2026-08-19")
    assert carried is not None
    assert carried.status == logic.STATUS_VERIFIED


async def test_carrying_is_idempotent(db, plan_factory):
    """Closing twice must not move anything twice, or duplicate a date.

    The UNIQUE index on (plan_id, pack_date) would reject the second insert, but the
    second call should simply find nothing to do rather than raise.
    """
    old = await plan_factory()
    new = await plan_factory(status=repository.STATUS_DRAFT)
    await repository.save_packing_entries(
        db, old.id, "2026-08-19", [{"asin": "B0AAA00001", "units": 400}], cartons=9,
    )

    first = await repository.carry_days_to_plan(db, old.id, new.id, ["2026-08-19"])
    second = await repository.carry_days_to_plan(db, old.id, new.id, ["2026-08-19"])
    assert first == ["2026-08-19"]
    assert second == []
    new_days = await repository.load_days_with_entries(db, new.id)
    assert len(new_days) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: FAIL — `AttributeError: module 'app.shipment.repository' has no attribute 'carry_days_to_plan'`.

Note: `test_carrying_a_day_moves_it_and_stamps_its_origin` also asserts `carried_from_plan_id` is present in the payload; that is added in Step 4.

- [ ] **Step 3: Implement `carry_days_to_plan`**

In `app/shipment/repository.py`, after `finalise_plan`:

```python
async def carry_days_to_plan(
    db: AsyncSession, from_plan_id: int, to_plan_id: int, pack_dates: list[str]
) -> list[str]:
    """Move packed days onto another plan. Returns the dates actually moved.

    **The day moves; its units are not copied anywhere.** That is the whole design.
    ``logic.remaining_for`` deliberately ignores ``available``, so adding the units to
    the new plan's stock column would tell the packer to box them a second time — 400
    already-packed units plus a 500-unit plan is 900 units of instruction. Because every
    aggregation reaches days through ``load_days(plan_id)``, updating that one column
    makes the new plan count them correctly with no new arithmetic at all.

    Status is preserved, including `verified`: the owner's approval refers to numbers he
    saw, and re-opening the day would discard it and block the invoice the day still
    needs.

    Idempotent. A date already absent from the source plan is skipped rather than
    raising, so closing twice moves nothing twice — and a date already present on the
    TARGET is skipped too, because the UNIQUE index on (plan_id, pack_date) would
    otherwise reject the whole transaction.
    """
    if not pack_dates:
        return []

    existing_on_target = {
        d.pack_date
        for d in (
            await db.execute(
                select(ShipmentPackingDay).where(
                    ShipmentPackingDay.plan_id == to_plan_id,
                    ShipmentPackingDay.pack_date.in_(list(pack_dates)),
                )
            )
        ).scalars()
    }

    result = await db.execute(
        select(ShipmentPackingDay).where(
            ShipmentPackingDay.plan_id == from_plan_id,
            ShipmentPackingDay.pack_date.in_(list(pack_dates)),
        )
    )

    moved: list[str] = []
    for day in result.scalars():
        if day.pack_date in existing_on_target:
            logger.warning(
                "carry: %s already exists on plan %s, left on plan %s",
                day.pack_date, to_plan_id, from_plan_id,
            )
            continue
        day.plan_id = to_plan_id
        # Stamped only on the first move, so a day carried twice still points at where
        # it was originally packed rather than at the intermediate plan.
        if day.carried_from_plan_id is None:
            day.carried_from_plan_id = from_plan_id
        moved.append(day.pack_date)

    if moved:
        await db.commit()
    return sorted(moved)
```

- [ ] **Step 4: Expose the lineage in the day payload**

In `app/shipment/repository.py`, inside `load_days_with_entries`, in the dict built per day, after `"destination_state": day.destination_state,`:

```python
                # NULL unless this day was packed against a different plan and carried
                # forward. The screen badges it, and a reconciliation needs it to explain
                # why a plan holds units for a date it never opened.
                "carried_from_plan_id": day.carried_from_plan_id,
```

- [ ] **Step 5: Run them to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: PASS (11 tests).

- [ ] **Step 6: Commit**

```bash
git add app/shipment/repository.py tests/test_shipment_close_and_history.py
git commit -m "feat: repository.carry_days_to_plan moves a packed day between plans

One column update plus a lineage stamp. The units are not copied anywhere: every
aggregation reaches days through load_days(plan_id), so moving the day makes the new
plan count them with no new arithmetic — and putting them in `available` instead would
tell the packer to box them a second time.

Idempotent in both directions: a date missing from the source or already present on
the target is skipped rather than raising."
```

---

### Task 3b: Orphan rows and `close_plan`

**Files:**
- Modify: `app/shipment/repository.py`
- Test: `tests/test_shipment_close_and_history.py`

**Interfaces:**
- Consumes: `carry_days_to_plan`, `logic.carriable_days`, `logic.brand_rank_for`, `logic.category_for`, `ensure_categories`.
- Produces: `ensure_rows_for_asins(...) -> list[str]`, `close_plan(...) -> dict`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_shipment_close_and_history.py`:

```python
async def test_an_orphan_asin_gets_a_row_at_to_ship_zero(db, plan_factory):
    """Packed units for an ASIN the new plan lacks must still get a plan row.

    `packed_units_by_asin` aggregates by ASIN and never consults plan items, while the
    invoice bridge builds its lines FROM plan items. So an orphaned ASIN means real
    boxes ship with no GST line against them — the same understatement the
    excluded-but-packed 409 exists to prevent. A row at To Ship 0 shows as over-packed,
    reaches the invoice payload, and gets a line.
    """
    old = await plan_factory(items=[
        {"asin": "B0GONE00001", "item": "Discontinued Sattu", "weight": 0.5,
         "brand": "MF", "fba_sku": "MF-GONE-500G", "shipment_plan": 100},
    ])
    new = await plan_factory(items=[
        {"asin": "B0AAA00001", "item": "Chana Sattu", "weight": 1.0,
         "brand": "MF", "fba_sku": "MF-CH-1KG", "shipment_plan": 500},
    ], status=repository.STATUS_DRAFT)
    await repository.save_packing_entries(
        db, old.id, "2026-08-19", [{"asin": "B0GONE00001", "units": 60}], cartons=3,
    )

    result = await repository.close_plan(db, old.id, new.id)

    assert result["orphan_asins"] == ["B0GONE00001"]
    items = await repository.load_plan_items(db, new.id)
    orphan = next(i for i in items if i.asin == "B0GONE00001")
    assert orphan.shipment_plan == 0
    assert orphan.item == "Discontinued Sattu", "the product name did not travel"
    assert orphan.fba_sku == "MF-GONE-500G", (
        "the merchant SKU did not travel — Amazon keys on it and the invoice needs it"
    )


async def test_close_refuses_and_moves_nothing_when_a_day_is_blocked(db, plan_factory):
    """A refusal that half-applied is worse than either outcome.

    If the close moved three days and then refused on the fourth, the owner is left
    with boxes split across two plans and no single screen showing them.
    """
    old = await plan_factory()
    new = await plan_factory(status=repository.STATUS_DRAFT)
    await repository.save_packing_entries(
        db, old.id, "2026-08-19", [{"asin": "B0AAA00001", "units": 400}], cartons=9,
    )
    await repository.save_packing_entries(
        db, old.id, "2026-08-20", [{"asin": "B0AAA00001", "units": 50}], cartons=2,
    )
    blocked_day = await repository.get_day(db, old.id, "2026-08-20")
    blocked_day.inbound_plan_id = "wf-half-created"
    await db.commit()

    result = await repository.close_plan(db, old.id, new.id)

    assert result["closed"] is False
    assert len(result["blocked"]) == 1
    assert result["blocked"][0]["pack_date"] == "2026-08-20"
    # Nothing moved, and the plan is still active.
    assert [d["pack_date"] for d in await repository.load_days_with_entries(db, old.id)] == [
        "2026-08-19", "2026-08-20",
    ]
    refreshed = await repository.get_plan(db, old.id)
    assert refreshed.status == repository.STATUS_ACTIVE


async def test_closing_stamps_closed_at_and_leaves_shipped_days_behind(db, plan_factory):
    """Shipped days stay on the plan they shipped from — that is the history."""
    old = await plan_factory()
    new = await plan_factory(status=repository.STATUS_DRAFT)
    await repository.save_packing_entries(
        db, old.id, "2026-08-18", [{"asin": "B0AAA00001", "units": 296}], cartons=16,
    )
    shipped = await repository.get_day(db, old.id, "2026-08-18")
    shipped.status = logic.STATUS_SHIPPED
    await db.commit()

    result = await repository.close_plan(db, old.id, new.id)

    assert result["closed"] is True
    assert result["carried"] == []
    closed = await repository.get_plan(db, old.id)
    assert closed.status == repository.STATUS_CLOSED
    assert closed.closed_at is not None
    assert [d["pack_date"] for d in await repository.load_days_with_entries(db, old.id)] == [
        "2026-08-18"
    ]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: FAIL — no attribute `close_plan`.

- [ ] **Step 3: Implement `ensure_rows_for_asins`**

In `app/shipment/repository.py`, after `carry_days_to_plan`:

```python
async def ensure_rows_for_asins(
    db: AsyncSession, plan_id: int, asins: list[str], source_plan_id: int
) -> list[str]:
    """Add To-Ship-0 rows so carried units have a plan row to sit on.

    Returns the ASINs actually inserted.

    A carried day can hold units for an ASIN the new plan does not list — the product
    went inactive in the MRP sheet, or the row was excluded. Leaving it orphaned is a
    GST understatement: ``packed_units_by_asin`` aggregates by ASIN and never consults
    plan items, while the invoice bridge builds its lines FROM plan items, so those
    boxes would ship with no line against them.

    Identity fields are copied from the SOURCE plan's item rather than looked up, so
    the row carries the product name and merchant SKU the units were packed under.
    Amazon keys on the merchant SKU and the invoice needs the name; re-deriving either
    from a catalogue that has since dropped the product would produce a blank.
    """
    if not asins:
        return []

    present = {
        i.asin
        for i in (
            await db.execute(
                select(ShipmentPlanItem).where(
                    ShipmentPlanItem.plan_id == plan_id,
                    ShipmentPlanItem.asin.in_(list(asins)),
                )
            )
        ).scalars()
    }
    wanted = [a for a in asins if a not in present]
    if not wanted:
        return []

    source = {
        i.asin: i
        for i in (
            await db.execute(
                select(ShipmentPlanItem).where(
                    ShipmentPlanItem.plan_id == source_plan_id,
                    ShipmentPlanItem.asin.in_(wanted),
                )
            )
        ).scalars()
    }

    added: list[str] = []
    seen_products: dict[str, str] = {}
    for asin in wanted:
        origin = source.get(asin)
        product = (origin.item if origin else "") or ""
        key = product.casefold()[:120]
        if key:
            seen_products.setdefault(key, product)
        db.add(
            ShipmentPlanItem(
                plan_id=plan_id,
                asin=asin,
                fba_sku=(origin.fba_sku if origin else "") or "",
                brand=(origin.brand if origin else "") or "",
                item=product,
                sort_product=key,
                brand_rank=(
                    origin.brand_rank
                    if origin is not None
                    else logic.brand_rank_for(None)
                ),
                weight=(origin.weight if origin else 0) or 0,
                # Zero across the board: this row is not a plan, it is a home for boxes
                # that already exist. It reads as over-packed, which is exactly right —
                # units were packed against no plan.
                sales_7d=0, projection=0, fba_stock=0, deficit=0,
                shipment_plan=0, available=0,
                s=False, m=False, b=False,
            )
        )
        added.append(asin)

    await db.commit()
    await ensure_categories(db, seen_products)
    return sorted(added)
```

- [ ] **Step 4: Implement `close_plan`**

Append to `app/shipment/repository.py`:

```python
async def close_plan(
    db: AsyncSession, plan_id: int, to_plan_id: int | None
) -> dict:
    """Retire a plan, carrying its packed-but-unshipped days forward.

    Returns ``{"closed", "carried", "orphan_asins", "blocked", "shipped_uninvoiced",
    "target_plan_id"}``.

    Distinct from ``finalise_plan``: that promotes a DRAFT and closes whatever was
    active as a side effect. This retires the ACTIVE plan when the owner decides it is
    done, which may be before any replacement exists — the live case is exactly that
    (sales data moved, a new plan is wanted, and one day is packed below the carton
    threshold).

    **Refuses entirely if any day is blocked, before moving anything.** A close that
    carried three days and then refused on the fourth would leave the boxes split
    across two plans with no single screen showing them.

    ``to_plan_id`` of None means "no target yet": the caller is expected to have
    created one. Nothing is carried in that case and the plan closes only if it has
    nothing to carry, because closing while boxes have nowhere to go is how held stock
    becomes lost stock.
    """
    plan = await get_plan(db, plan_id)
    if plan is None:
        return {
            "closed": False, "carried": [], "orphan_asins": [], "blocked": [],
            "shipped_uninvoiced": [], "target_plan_id": None, "missing": True,
        }

    days = await load_days_with_entries(db, plan_id)
    split = logic.carriable_days(days)
    shipped_uninvoiced = [d["pack_date"] for d in split["shipped_uninvoiced"]]

    if split["blocked"]:
        return {
            "closed": False, "carried": [], "orphan_asins": [],
            "blocked": split["blocked"],
            "shipped_uninvoiced": shipped_uninvoiced,
            "target_plan_id": to_plan_id,
        }

    to_carry = [d["pack_date"] for d in split["carry"]]
    if to_carry and to_plan_id is None:
        return {
            "closed": False, "carried": [], "orphan_asins": [],
            "blocked": [{
                "pack_date": ", ".join(to_carry),
                "reason": "there is no plan to carry these days onto",
            }],
            "shipped_uninvoiced": shipped_uninvoiced,
            "target_plan_id": None,
        }

    orphans: list[str] = []
    if to_carry:
        # Rows FIRST, then the days. If the order were reversed a crash between them
        # would leave carried units on a plan with no row to hold them, which is the
        # GST-understatement state; this order leaves at worst an unused zero row.
        carried_asins = sorted({
            entry["asin"]
            for day in split["carry"]
            for entry in (day.get("entries") or [])
            if entry.get("asin")
        })
        orphans = await ensure_rows_for_asins(
            db, to_plan_id, carried_asins, source_plan_id=plan_id
        )
        await carry_days_to_plan(db, plan_id, to_plan_id, to_carry)

    plan.status = STATUS_CLOSED
    plan.closed_at = datetime.utcnow()
    await db.commit()

    return {
        "closed": True,
        "carried": sorted(to_carry),
        "orphan_asins": orphans,
        "blocked": [],
        "shipped_uninvoiced": shipped_uninvoiced,
        "target_plan_id": to_plan_id,
    }
```

- [ ] **Step 5: Run them to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: PASS (14 tests).

- [ ] **Step 6: Verify the refusal is atomic, by mutation**

Temporarily move the `if split["blocked"]:` early return to *after* the `if to_carry:` block and re-run.
Expected: `test_close_refuses_and_moves_nothing_when_a_day_is_blocked` FAILS on the day list assertion. Restore the order.

- [ ] **Step 7: Run the whole suite**

Run: `venv/Scripts/python -m pytest -q`
Expected: PASS, 1057 tests.

- [ ] **Step 8: Commit**

```bash
git add app/shipment/repository.py tests/test_shipment_close_and_history.py
git commit -m "feat: repository.close_plan retires a plan and carries its unshipped days

Refuses entirely before moving anything if any day is blocked: a close that carried
three days then refused on the fourth would split the boxes across two plans with no
single screen showing them.

Orphan ASINs get a To-Ship-0 row, copied from the source plan's item so the product
name and merchant SKU travel. Rows are inserted before the days move, so a crash
leaves at worst an unused zero row rather than carried units with no row to hold
them — which is the GST-understatement state."
```

---

### Task 4: `repository.list_plans` and plan-scoped reads

**Files:**
- Modify: `app/shipment/repository.py`
- Test: `tests/test_shipment_close_and_history.py`

**Interfaces:**
- Produces: `repository.list_plans(db) -> list[dict]` with keys `id, label, status, created_at, closed_at, days, units, cartons, invoice_numbers, carried_in, carried_out`.

  `invoice_numbers` holds the GST numbers as strings ("ST/26-27/077"), never row ids. A
  row id names nothing the owner can look up in his own records — the same mistake
  `_invoice_numbers()` exists to prevent elsewhere in this codebase, where three refusals
  interpolated a raw `invoice_id` and printed "already on invoice #5".

- [ ] **Step 1: Write the failing test**

```python
# ─── History ─────────────────────────────────────────────────────────────────

async def test_the_plan_list_summarises_every_plan_newest_first(db, plan_factory):
    """Enough to choose from without opening each one.

    Totals and invoice numbers are what a reconciliation scans; a bare list of labels
    would mean opening all of them to find the month being checked.
    """
    old = await plan_factory()
    await repository.save_packing_entries(
        db, old.id, "2026-08-18", [{"asin": "B0AAA00001", "units": 296}], cartons=16,
    )
    new = await plan_factory(status=repository.STATUS_DRAFT)

    rows = await repository.list_plans(db)
    by_id = {r["id"]: r for r in rows}

    assert [r["id"] for r in rows] == sorted(by_id, reverse=True), (
        "the list is not newest-first"
    )
    assert by_id[old.id]["days"] == 1
    assert by_id[old.id]["units"] == 296
    assert by_id[old.id]["cartons"] == 16
    assert by_id[new.id]["days"] == 0


async def test_the_plan_list_reports_carry_lineage_in_both_directions(db, plan_factory):
    """A day that moved must be visible from BOTH plans.

    With only one direction, the date appears to vanish from the plan being reconciled.
    """
    old = await plan_factory()
    new = await plan_factory(status=repository.STATUS_DRAFT)
    await repository.save_packing_entries(
        db, old.id, "2026-08-19", [{"asin": "B0AAA00001", "units": 400}], cartons=9,
    )
    await repository.close_plan(db, old.id, new.id)

    by_id = {r["id"]: r for r in await repository.list_plans(db)}
    assert by_id[new.id]["carried_in"] == 1
    assert by_id[old.id]["carried_out"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: FAIL — no attribute `list_plans`.

- [ ] **Step 3: Implement `list_plans`**

Append to `app/shipment/repository.py`:

```python
async def list_plans(db: AsyncSession) -> list[dict]:
    """Every plan, newest first, summarised for the history list.

    Aggregated in SQL rather than by loading each plan's days, because this is the
    screen the owner opens to FIND a plan and it must not cost one query per plan.

    ``carried_in`` counts days whose ``carried_from_plan_id`` points elsewhere;
    ``carried_out`` counts days that ORIGINATED here and now live on another plan. Both
    directions matter: with only one, a carried day looks as though it vanished from the
    plan being reconciled.
    """
    totals = (
        select(
            ShipmentPackingDay.plan_id.label("plan_id"),
            func.count(ShipmentPackingDay.id).label("days"),
            func.coalesce(func.sum(ShipmentPackingDay.total_units), 0).label("units"),
            func.coalesce(func.sum(ShipmentPackingDay.total_cartons), 0).label("cartons"),
        )
        .group_by(ShipmentPackingDay.plan_id)
        .subquery()
    )

    rows = await db.execute(
        select(ShipmentPlan, totals.c.days, totals.c.units, totals.c.cartons)
        .outerjoin(totals, totals.c.plan_id == ShipmentPlan.id)
        .order_by(ShipmentPlan.id.desc())
    )

    # One query for both lineage directions, keyed by the pair, rather than two
    # aggregates that could disagree about a day.
    lineage = await db.execute(
        select(
            ShipmentPackingDay.plan_id,
            ShipmentPackingDay.carried_from_plan_id,
            func.count(ShipmentPackingDay.id),
        )
        .where(ShipmentPackingDay.carried_from_plan_id.isnot(None))
        .group_by(
            ShipmentPackingDay.plan_id, ShipmentPackingDay.carried_from_plan_id
        )
    )
    carried_in: dict[int, int] = {}
    carried_out: dict[int, int] = {}
    for holder, origin, count in lineage:
        carried_in[holder] = carried_in.get(holder, 0) + int(count or 0)
        carried_out[origin] = carried_out.get(origin, 0) + int(count or 0)

    invoices = await db.execute(
        select(ShipmentPackingDay.plan_id, Invoice.invoice_no)
        .join(Invoice, Invoice.id == ShipmentPackingDay.invoice_id)
        .distinct()
    )
    by_plan_invoices: dict[int, list[str]] = {}
    for plan_id, invoice_no in invoices:
        by_plan_invoices.setdefault(plan_id, []).append(invoice_no)

    out: list[dict] = []
    for plan, days, units, cartons in rows:
        out.append({
            "id": plan.id,
            "label": plan.label,
            "status": plan.status,
            "created_at": plan.created_at.isoformat() if plan.created_at else None,
            "closed_at": plan.closed_at.isoformat() if plan.closed_at else None,
            "days": int(days or 0),
            "units": int(units or 0),
            "cartons": int(cartons or 0),
            "invoice_numbers": sorted(by_plan_invoices.get(plan.id, [])),
            "carried_in": carried_in.get(plan.id, 0),
            "carried_out": carried_out.get(plan.id, 0),
        })
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: PASS (16 tests).

- [ ] **Step 5: Run the whole suite and commit**

Run: `venv/Scripts/python -m pytest -q`
Expected: PASS, 1059 tests.

```bash
git add app/shipment/repository.py tests/test_shipment_close_and_history.py
git commit -m "feat: repository.list_plans summarises every plan for the history screen

Aggregated in SQL, not by loading each plan's days: this is the screen used to FIND a
plan and must not cost a query per plan. Carry lineage is reported in both directions,
because with only one a carried day looks as though it vanished from the plan being
reconciled."
```

---

### Task 5: Router — close, history, plan-scoped downloads, and the `attach_invoice` fix

**Files:**
- Modify: `app/routers/shipment.py` (`_document_rows` ~1899, `attach_invoice` ~2578, new routes after `finalise_plan` ~1315)
- Test: `tests/test_shipment_close_and_history.py`

**Interfaces:**
- Consumes: `repository.close_plan`, `list_plans`, `get_draft_plan`, `create_plan`, `_plan_payload`, `_document_rows`.
- Produces: `POST /shipment/plan/{plan_id}/close`, `GET /shipment/plans`, `GET /shipment/plan/{plan_id}/detail`, `?plan_id=` on all five downloads.

This task is split into 5a (the fix that must land first) and 5b (the new surface), because 5a is the bug Close would otherwise create.

- [ ] **Step 1: Write the failing test for the `attach_invoice` bug**

```python
# ─── The bug Close would otherwise create ────────────────────────────────────

async def test_an_invoice_can_be_recorded_against_a_closed_plans_day(
    auth_client, db, plan_factory
):
    """attach_invoice used to resolve the day through get_active_plan.

    So closing a plan with a shipped-but-uninvoiced day made recording its invoice
    impossible through the app — the owner would have to edit the database. Close makes
    that state reachable deliberately, so the lookup must accept a plan id.
    """
    plan = await plan_factory()
    await repository.save_packing_entries(
        db, plan.id, "2026-08-18", [{"asin": "B0AAA00001", "units": 296}], cartons=16,
    )
    day = await repository.get_day(db, plan.id, "2026-08-18")
    day.status = logic.STATUS_SHIPPED
    await db.commit()

    from app.models import Invoice
    invoice = Invoice(invoice_no="ST/26-27/081", invoice_number=81)
    db.add(invoice)
    await db.commit()
    await db.refresh(invoice)

    await repository.close_plan(db, plan.id, None)
    closed = await repository.get_plan(db, plan.id)
    assert closed.status == repository.STATUS_CLOSED, "precondition: the plan closed"

    r = await auth_client.post("/shipment/attach-invoice", json={
        "invoice_id": invoice.id,
        "pack_dates": ["2026-08-18"],
        "plan_id": plan.id,
    })
    assert r.status_code == 200, r.text
    refreshed = await repository.get_day(db, plan.id, "2026-08-18")
    assert refreshed.invoice_id == invoice.id
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: FAIL — 404 `{"error": "No active plan"}`, because `attach_invoice` calls `get_active_plan`.

- [ ] **Step 3: Fix the lookup**

In `app/routers/shipment.py`, in `attach_invoice`, replace:

```python
    plan = await repository.get_active_plan(db)
    if plan is None:
        return JSONResponse({"error": "No active plan"}, status_code=404)
```

with:

```python
    # `plan_id` first, active as the fallback — the same pattern POST /items uses.
    #
    # This route USED to resolve only the active plan, and closing a plan therefore
    # made an invoice unrecordable against its days: the owner's only remedy was
    # editing the database. Close deliberately creates that state (a shipped day with
    # no invoice stays on the plan it shipped from), so the id must be accepted.
    raw_plan_id = body.get("plan_id")
    plan = (
        await repository.get_plan(db, int(raw_plan_id))
        if raw_plan_id
        else await repository.get_active_plan(db)
    )
    if plan is None:
        return JSONResponse(
            {"error": "No such plan, and no active plan."}, status_code=404
        )
```

- [ ] **Step 4: Run to verify it passes, then the whole suite**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: PASS (17 tests).

Run: `venv/Scripts/python -m pytest -q`
Expected: PASS, 1060 tests. `tests/test_invoice_save.py` and the attach-invoice tests must be green — the fallback keeps every existing caller working.

- [ ] **Step 5: Commit**

```bash
git add app/routers/shipment.py tests/test_shipment_close_and_history.py
git commit -m "fix: attach-invoice accepts a plan_id, so a closed plan's day stays recordable

It resolved the day through get_active_plan, so closing a plan with a shipped-but-
uninvoiced day made recording its invoice impossible through the app. Close creates
that state deliberately, so this had to land with it. Falls back to the active plan,
following the pattern POST /items already uses."
```

---

### Task 5b: The close and history routes

**Files:**
- Modify: `app/routers/shipment.py`
- Test: `tests/test_shipment_close_and_history.py`

- [ ] **Step 1: Write the failing tests**

```python
# ─── The routes ──────────────────────────────────────────────────────────────

async def test_the_live_case_end_to_end(auth_client, db, plan_factory):
    """19 Aug: 400 units, 9 cartons, verified, below the carton threshold.

    The single most important assertion here is `remaining == 100`. If the carried
    units had been added to `available` instead of the day being moved, the packer
    would be told to box 500 more on top of the 400 already in cartons.
    """
    old = await plan_factory()
    await repository.save_packing_entries(
        db, old.id, "2026-08-19", [{"asin": "B0AAA00001", "units": 400}], cartons=9,
    )
    day = await repository.get_day(db, old.id, "2026-08-19")
    day.status = logic.STATUS_VERIFIED
    await db.commit()

    new = await plan_factory(status=repository.STATUS_DRAFT)

    r = await auth_client.post(f"/shipment/plan/{old.id}/close")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["carried"] == ["2026-08-19"]
    assert body["target_plan_id"] == new.id

    detail = (await auth_client.get(f"/shipment/plan/{new.id}/detail")).json()
    chana = next(i for i in detail["items"] if i["asin"] == "B0AAA00001")
    assert chana["shipment_plan"] == 500
    assert chana["packed"] == 400, "the carried boxes are not counted as packed"
    assert chana["remaining"] == 100, (
        "the packer is being told to box units that are already in cartons — the "
        "carried units were added as stock instead of the day being moved"
    )
    carried_day = next(d for d in detail["days"] if d["pack_date"] == "2026-08-19")
    assert carried_day["status"] == logic.STATUS_VERIFIED
    assert carried_day["carried_from_plan_id"] == old.id


async def test_close_creates_a_carrier_plan_when_there_is_no_draft(
    auth_client, db, plan_factory
):
    """The owner closes before uploading the new CSV — the live sequence.

    Without this the boxes would have nowhere to go and the close would refuse, which
    is the state that makes held stock become lost stock.
    """
    old = await plan_factory()
    await repository.save_packing_entries(
        db, old.id, "2026-08-19", [{"asin": "B0AAA00001", "units": 400}], cartons=9,
    )

    r = await auth_client.post(f"/shipment/plan/{old.id}/close")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["target_plan_id"] not in (None, old.id)
    assert body["created_carrier_plan"] is True

    carried = await repository.get_day(db, body["target_plan_id"], "2026-08-19")
    assert carried is not None


async def test_close_refuses_a_blocked_day_over_http(auth_client, db, plan_factory):
    old = await plan_factory()
    await repository.save_packing_entries(
        db, old.id, "2026-08-20", [{"asin": "B0AAA00001", "units": 50}], cartons=2,
    )
    day = await repository.get_day(db, old.id, "2026-08-20")
    day.inbound_plan_id = "wf-half-created"
    await db.commit()

    r = await auth_client.post(f"/shipment/plan/{old.id}/close")
    assert r.status_code == 409
    assert "2026-08-20" in r.json()["error"]
    still_active = await repository.get_plan(db, old.id)
    assert still_active.status == repository.STATUS_ACTIVE


async def test_the_history_routes_are_admin_only(ops_client, plan_factory):
    """Plan detail carries projections, which the Accounts preset withholds."""
    plan = await plan_factory()
    for method, path in (
        ("get", "/shipment/plans"),
        ("get", f"/shipment/plan/{plan.id}/detail"),
        ("post", f"/shipment/plan/{plan.id}/close"),
    ):
        r = await getattr(ops_client, method)(path)
        assert r.status_code in (401, 403), f"{path} -> {r.status_code}"


@pytest.mark.parametrize("path", [
    "/shipment/download/plan.xlsx",
    "/shipment/download/packed.xlsx",
    "/shipment/download/remaining.xlsx",
    "/shipment/download/plan.pdf",
    "/shipment/download/packed.pdf",
])
async def test_every_download_can_target_a_closed_plan(
    auth_client, db, plan_factory, path
):
    """Parametrised so a sixth download that forgets ?plan_id= fails here.

    Accounts asking for last month's packed sheet after a new plan was finalised is
    the case that could not be served at all before.
    """
    old = await plan_factory()
    await repository.save_packing_entries(
        db, old.id, "2026-08-18", [{"asin": "B0AAA00001", "units": 296}], cartons=16,
    )
    shipped = await repository.get_day(db, old.id, "2026-08-18")
    shipped.status = logic.STATUS_SHIPPED
    await db.commit()
    await repository.close_plan(db, old.id, None)

    r = await auth_client.get(f"{path}?plan_id={old.id}")
    assert r.status_code == 200, f"{path} could not serve a closed plan: {r.text[:200]}"
    assert len(r.content) > 1000, "the document is suspiciously small"
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: FAIL — 404s on the new routes; the download tests fail with `No active plan to download.`

- [ ] **Step 3: Widen `_document_rows` to accept a plan id**

In `app/routers/shipment.py`, replace the `_document_rows` signature and its plan lookup:

```python
async def _document_rows(db: AsyncSession, plan_id: int | None = None):
    """(plan, item dicts in canonical order, days) or None if there is no such plan.

    ``plan_id`` defaults to the active plan, which is every existing caller's
    behaviour. Passing one is what lets accounts reprint a CLOSED plan's packed sheet —
    before this, every download resolved only the active plan, so the moment a plan
    closed its documents became unavailable with the data still sitting in the table.

    One function, so all five downloads inherit plan targeting together and none can
    drift — the same single-source property load_plan_items has for row order.
    """
    plan = (
        await repository.get_plan(db, plan_id)
        if plan_id
        else await repository.get_active_plan(db)
    )
    if plan is None:
        return None
```

Leave the rest of the body unchanged.

- [ ] **Step 4: Thread `plan_id` through all five download routes**

For each of `download_plan`, `download_packed`, `download_remaining` and `download_shipment_file`, add the query parameter and pass it through. For `download_plan`:

```python
async def download_plan(
    fmt: str,
    request: Request,
    plan_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
```

and change `loaded = await _document_rows(db)` to `loaded = await _document_rows(db, plan_id)`.

Repeat identically for `download_packed`, `download_remaining` and `download_shipment_file`. The `.pdf` and `.xlsx` variants share one route each via `fmt`, so four edits cover all five documents.

- [ ] **Step 5: Add the close and history routes**

In `app/routers/shipment.py`, after `finalise_plan`:

```python
@router.post("/plan/{plan_id}/close")
async def close_plan_route(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Retire a plan, carrying its packed-but-unshipped days onto the next one.

    Distinct from ``/finalise``, which promotes a draft. This is the owner deciding a
    plan is done, which happens BEFORE a replacement exists in the case that prompted
    it: sales data moved, a new plan is wanted, and the last packed day is below the
    carton threshold so it cannot ship on its own.

    The target is the current draft, or a new empty carrier plan when there is none.
    Without the carrier, closing early would refuse and the boxes would have nowhere to
    go — which is how held stock becomes lost stock.

    Refuses with 409, having moved nothing, when a day is still open or has a
    part-created Amazon inbound plan. Shipped-but-uninvoiced days do not block: they
    stay on this plan and are named in the response, because the owner may well have
    invoiced them outside the app.
    """
    plan = await repository.get_plan(db, plan_id)
    if plan is None:
        return JSONResponse({"error": "No such plan."}, status_code=404)
    if plan.status == repository.STATUS_CLOSED:
        return JSONResponse(
            {"error": f"{plan.label or 'That plan'} is already closed."},
            status_code=409,
        )

    days = await repository.load_days_with_entries(db, plan_id)
    split = logic.carriable_days(days)

    # The target is resolved only when something actually needs carrying, so closing a
    # fully-shipped plan does not leave an empty plan behind.
    target_id = None
    created_carrier = False
    if split["carry"]:
        draft = await repository.get_draft_plan(db)
        if draft is not None:
            target_id = draft.id
        else:
            carrier = await repository.create_plan(
                db, [],
                label=f"Carried from {plan.label or f'plan {plan_id}'}",
                status=repository.STATUS_DRAFT,
            )
            target_id = carrier.id
            created_carrier = True

    result = await repository.close_plan(db, plan_id, target_id)

    if not result["closed"]:
        named = "; ".join(
            f"{b['pack_date']}: {b['reason']}" for b in result["blocked"]
        )
        return JSONResponse(
            {
                "error": f"The plan was not closed and nothing was moved. {named}",
                "blocked": result["blocked"],
            },
            status_code=409,
        )

    result["created_carrier_plan"] = created_carrier
    warnings = []
    if result["shipped_uninvoiced"]:
        warnings.append(
            f"{len(result['shipped_uninvoiced'])} shipped day(s) have no invoice "
            f"({', '.join(result['shipped_uninvoiced'])}). They stay on this plan and "
            "can still be invoiced from Plan history."
        )
    if result["orphan_asins"]:
        warnings.append(
            f"{len(result['orphan_asins'])} carried SKU(s) are not in the new plan and "
            "were added with To Ship 0, so the packed boxes still reach an invoice: "
            f"{', '.join(result['orphan_asins'])}."
        )
    if warnings:
        result["warning"] = " ".join(warnings)
    return JSONResponse(result)


@router.get("/plans")
async def list_plans_route(
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """Every plan, newest first, for the history screen.

    Admin only: the summary carries planned quantities, which are projections, and the
    Accounts preset withholds those for the same reason it withholds purchase costs.
    """
    return JSONResponse({"plans": await repository.list_plans(db)})


@router.get("/plan/{plan_id}/detail")
async def plan_detail(
    plan_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    role: str = Depends(require_admin),
):
    """One plan in full, in the SAME shape /active returns.

    Deliberately identical, so the history screen renders through the existing code
    rather than a second renderer that could disagree with it about order or any
    computed number — the same reason all four documents share ``_document_rows``.
    """
    plan = await repository.get_plan(db, plan_id)
    if plan is None:
        return JSONResponse({"error": "No such plan."}, status_code=404)

    items = await repository.load_plan_items(db, plan_id)
    days = await repository.load_days_with_entries(db, plan_id)
    payload = _plan_payload(plan, items, days)
    payload["role"] = role
    payload["read_only"] = plan.status == repository.STATUS_CLOSED
    payload["plan"]["closed_at"] = (
        plan.closed_at.isoformat() if plan.closed_at else None
    )
    return JSONResponse(payload)
```

- [ ] **Step 6: Run to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: PASS (23 tests).

- [ ] **Step 7: Run the whole suite**

Run: `venv/Scripts/python -m pytest -q`
Expected: PASS, 1066 tests.

- [ ] **Step 8: Commit**

```bash
git add app/routers/shipment.py tests/test_shipment_close_and_history.py
git commit -m "feat: close a plan, read plan history, target any plan from a download

_document_rows takes an optional plan id, so all five downloads inherit plan targeting
from one function — accounts can reprint a closed plan's packed sheet, which was
impossible while every download resolved only the active plan.

/plan/{id}/detail returns the same shape as /active so the history screen reuses the
existing renderer instead of a second one that could disagree about order.

Close resolves its target only when something needs carrying, and creates an empty
carrier plan when no draft exists — the owner closes before uploading the new CSV."
```

---

### Task 6: The screen

**Files:**
- Modify: `templates/shipment.html`
- Test: `tests/test_shipment_close_and_history.py`

- [ ] **Step 1: Write the failing tests**

```python
# ─── The screen ──────────────────────────────────────────────────────────────

def _shipment_source() -> str:
    from pathlib import Path
    return (
        Path(__file__).resolve().parent.parent / "templates" / "shipment.html"
    ).read_text(encoding="utf-8")


def _close_plan_body() -> str:
    """Just the body of closePlan(), so assertions cannot pass on unrelated code.

    A whole-file substring search is nearly useless here: "confirm(" and "carried"
    already appear elsewhere in this template, so a file-wide assertion stays green
    even if closePlan's confirm is empty. Slicing to the function is what makes the
    test able to fail.
    """
    source = _shipment_source()
    start = source.index("async function closePlan(")
    # The next top-level function declaration ends the body. Both spellings, because
    # the following function may or may not be async.
    rest = source[start + 1:]
    ends = [i for i in (rest.find("\nasync function "), rest.find("\nfunction ")) if i != -1]
    return rest[: min(ends)] if ends else rest


def test_the_close_confirm_names_the_dates_and_quantities_that_will_move():
    """A confirm that says only "are you sure" teaches the owner to click through it.

    Naming the dates and their units is what makes the 19 Aug carry a decision rather
    than a surprise found later on the wrong plan. Asserted against closePlan's OWN
    body, and on the interpolations rather than on prose, so rewording the sentence
    does not fail the test but dropping the data does.
    """
    body = _close_plan_body()
    assert "confirm(" in body, "the close does not confirm at all"
    assert "d.pack_date" in body, "the confirm does not name the dates being carried"
    assert "d.total_units" in body, "the confirm does not say how many units move"
    assert "d.total_cartons" in body, "the confirm does not say how many cartons move"
    # The shipped-but-uninvoiced warning is the other half: those days do NOT move,
    # and staying silent about them is the held-days bug again.
    assert 'status === "shipped"' in body, (
        "the confirm does not warn about shipped days with no invoice"
    )


def test_the_history_panel_does_not_add_a_second_item_renderer():
    """One renderer, so history cannot disagree with /active about row order.

    A second render path is how the screen and the downloads drifted apart before,
    which is the complaint the single ORDER BY exists to answer.

    Asserted by counting the calls that BUILD an item row. An earlier version of this
    test counted `function renderItems(` — a function this template does not have, so
    the count was 0 and the assertion was permanently true.
    """
    source = _shipment_source()
    assert "/shipment/plans" in source
    assert "/detail" in source
    # openPlanDetail renders DAYS only. If it grew an item loop it would be a second
    # renderer of the ordered rows, which is the drift being prevented.
    detail_start = source.index("async function openPlanDetail(")
    detail_body = source[detail_start:detail_start + 2000]
    assert "data.items.map(" not in detail_body, (
        "openPlanDetail renders items itself, which can disagree with /active about "
        "row order — render days here and reuse the existing item renderer"
    )


def test_a_carried_day_is_badged_on_its_card():
    """Otherwise a date appears on a plan that never opened it, with no explanation."""
    source = _shipment_source()
    assert "carried_from_plan_id" in source


def test_the_history_panel_escapes_labels_and_coerces_ids():
    """Both halves of the rule this file already follows.

    `esc` on the label: plan labels are owner-typed and product names come from the MRP
    sheet, which is a spreadsheet anyone can type into.

    `Number` on the plan id: it lands inside an `onclick` attribute, so a string there
    is executable content rather than data. It is also the `undefined` guard — `e.cartons`
    printed "100/undefined" on the owner's screen because JS renders a missing field
    silently, which is what tests/test_shipment_admin_ui.py greps for.
    """
    source = _shipment_source()
    assert "openPlanDetail(${Number(p.id)})" in source, (
        "a plan id is interpolated into an onclick without Number()"
    )
    assert "${esc(p.label)}" in source, "a plan label is interpolated unescaped"
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py -q`
Expected: FAIL — `ValueError: substring not found` from `_close_plan_body()`, because
`closePlan` does not exist yet. That the helper *raises* rather than returning an empty
string is deliberate: a helper that silently returned "" would make every assertion in
the test pass vacuously once the slice broke.

- [ ] **Step 3: Add the Close button next to Finalise**

In `templates/shipment.html`, after the `finalise-btn` button (~line 344):

```html
  <button class="btn" id="close-btn" onclick="closePlan()" style="display:none">Close plan</button>
```

- [ ] **Step 4: Add the history panel markup**

After the packing-days card's closing `</div>`, add:

```html
<div class="card" id="history-card">
  <h3>Plan history</h3>
  <p class="subtitle">Closed plans stay readable: day-wise packing, invoice numbers and
    Amazon shipment ids, for reconciliation.</p>
  <div id="history-list"></div>
  <div id="history-detail"></div>
</div>
```

`test_template_render_targets.py` checks every `getElementById` target exists, so all three ids must be declared here.

- [ ] **Step 5: Add the JS**

Before `load();` at the end of the script block.

**Two escaping rules, both already established in this file and both load-bearing:**

1. **Every interpolated string goes through `esc()`.** Plan labels are owner-typed and
   product names come from the MRP sheet, which is a spreadsheet anyone can type into.
   `templates/pricing.html` has a test (`test_product_names_are_escaped`) for exactly
   this reason.
2. **Every interpolated number goes through `Number()`.** Not for escaping — for the
   `undefined` failure. `e.cartons` printed "100/undefined" on the owner's screen
   because JS renders a missing field silently rather than complaining, and
   `tests/test_shipment_admin_ui.py` greps for that specific bug. `Number(x)` on a
   missing field yields `NaN`, which is visible.

`plan_id` values reach `onclick` attributes, so they are coerced with `Number()` at the
point of interpolation — a string there would be executable content in an attribute.

```javascript
// ─── Close, and history ─────────────────────────────────────────────────────

async function closePlan(){
  if(!plan) return;
  // The dates are NAMED in the confirm. "Are you sure?" teaches the owner to click
  // through it, and a carried day found later on the wrong plan reads as a bug.
  let question = `Close ${plan.label || "this plan"}?`;
  const carriable = days.filter(d =>
    ["held","submitted","verified"].includes(d.status)
    && !d.invoice_id && d.total_units > 0);
  if(carriable.length){
    question += `\n\n${carriable.length} packed day(s) will move to the next plan `
      + `so they are not packed again:\n`
      + carriable.map(d => `  ${d.pack_date} — ${d.total_units} units, `
          + `${d.total_cartons} cartons`).join("\n");
  }
  const shippedNoInvoice = days.filter(d => d.status === "shipped" && !d.invoice_id);
  if(shippedNoInvoice.length){
    question += `\n\n⚠ ${shippedNoInvoice.length} shipped day(s) have no invoice `
      + `(${shippedNoInvoice.map(d => d.pack_date).join(", ")}). They stay on this `
      + `plan and can still be invoiced from Plan history.`;
  }
  if(!confirm(question)) return;

  try{
    const r = await fetch(`/shipment/plan/${plan.id}/close`, {method:"POST"});
    const data = await r.json();
    if(!r.ok) throw new Error(data.error || "Could not close the plan");
    let note = "Plan closed";
    if(data.carried && data.carried.length){
      note += ` — ${data.carried.length} day(s) carried forward`;
    }
    toast(note, "success");
    if(data.warning) message(data.warning, "warn");
    await load();
    await loadHistory();
  }catch(e){ toast(e.message, "error"); }
}

async function loadHistory(){
  try{
    const r = await fetch("/shipment/plans");
    if(!r.ok) return;
    const rows = (await r.json()).plans || [];
    $("history-list").innerHTML = rows.map(p => {
      const lineage = [];
      if(p.carried_in)  lineage.push(`${Number(p.carried_in)} carried in`);
      if(p.carried_out) lineage.push(`${Number(p.carried_out)} carried out`);
      // Number() on the id: it lands inside an onclick attribute, where a string
      // would be executable content rather than data.
      return `<div class="history-row">
        <button class="btn-link" onclick="openPlanDetail(${Number(p.id)})">${esc(p.label)}</button>
        <span class="tag">${esc(p.status)}</span>
        <span>${Number(p.days)} day(s) · ${Number(p.units)} units · ${Number(p.cartons)} cartons</span>
        <span>${esc((p.invoice_numbers || []).join(", "))}</span>
        <span>${esc(lineage.join(" · "))}</span>
      </div>`;
    }).join("") || '<p class="subtitle">No plans yet.</p>';
  }catch(e){ /* history is a convenience; a failure here must not break the page */ }
}

async function openPlanDetail(planId){
  try{
    const r = await fetch(`/shipment/plan/${planId}/detail`);
    const data = await r.json();
    if(!r.ok) throw new Error(data.error || "Could not load that plan");
    // Rendered through the SAME payload shape /active returns, so history cannot
    // disagree with the live screen about row order or any computed number.
    $("history-detail").innerHTML =
      `<h4>${esc(data.plan.label)}</h4>`
      + `<p class="subtitle">${data.days.length} packing day(s)`
      + (data.plan.closed_at ? ` · closed ${esc(data.plan.closed_at.slice(0,10))}` : "")
      + `</p>`
      + data.days.map(d => `<div class="history-day">
          <strong>${esc(d.pack_date)}</strong> ${esc(d.status)}
          · ${Number(d.total_units)} units · ${Number(d.total_cartons)} cartons
          ${d.invoice_no ? " · invoice " + esc(d.invoice_no) : ""}
          ${d.shipment_confirmation_id ? " · " + esc(d.shipment_confirmation_id) : ""}
          ${d.carried_from_plan_id ? ' <span class="tag">carried in</span>' : ""}
        </div>`).join("")
      + `<p><a href="/shipment/download/packed.xlsx?plan_id=${Number(planId)}">Download packed sheet</a></p>`;
  }catch(e){ toast(e.message, "error"); }
}
```

In the existing render function that shows/hides `finalise-btn` (~line 526), add alongside it:

```javascript
  document.getElementById("close-btn").style.display =
    (plan && plan.status === "active") ? "" : "none";
```

And in the day-card renderer, where the status badge is built, add the badge:

```javascript
    ${d.carried_from_plan_id ? '<span class="tag">carried in</span>' : ""}
```

Finally, add `loadHistory();` immediately after the existing `load();` call.

- [ ] **Step 6: Add the CSS, using theme variables only**

In the `<style>` block:

```css
.history-row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;
  padding:6px 0;border-bottom:1px solid var(--border);font-size:12.5px}
.history-day{padding:4px 0;font-size:12.5px;color:var(--text-muted)}
```

No hex or rgba values — `tests/test_theme.py` fails on any.

- [ ] **Step 7: Run the template tests**

Run: `venv/Scripts/python -m pytest tests/test_shipment_close_and_history.py tests/test_theme.py tests/test_template_render_targets.py tests/test_shipment_admin_ui.py -q`
Expected: PASS.

- [ ] **Step 8: Run the whole suite and commit**

Run: `venv/Scripts/python -m pytest -q`
Expected: PASS, 1070 tests.

```bash
git add templates/shipment.html tests/test_shipment_close_and_history.py
git commit -m "feat: Close plan button, plan history panel, carried-in badge

The confirm NAMES the dates that will move and the shipped days with no invoice. A
bare 'are you sure' teaches the owner to click through it, and a carried day found
later on the wrong plan reads as a bug rather than a decision.

History renders through the same payload shape /active returns, so it cannot disagree
with the live screen about row order — the drift the single ORDER BY exists to prevent."
```

---

### Task 7: Deploy rehearsal and manual verification

**Files:** none modified — this task is verification only.

- [ ] **Step 1: Rehearse the migration against a copy of production**

```bash
scp -i "C:\Users\LENOVO\Desktop\old downloads\amazon-tracker-key.pem" ubuntu@13.233.144.148:/opt/amazon-tracker/tracker.db ./tracker-prod-copy.db
venv/Scripts/python -c "import sqlite3; c=sqlite3.connect('tracker-prod-copy.db'); print('plans', c.execute('select count(*) from shipment_plans').fetchone()); print('days', c.execute('select count(*) from shipment_packing_days').fetchone()); print('entries', c.execute('select count(*) from shipment_packing_entries').fetchone())"
```

Record the three counts. Then run the migration against the copy and re-check them:

```bash
DATABASE_URL="sqlite+aiosqlite:///./tracker-prod-copy.db" venv/Scripts/alembic upgrade head
DATABASE_URL="sqlite+aiosqlite:///./tracker-prod-copy.db" venv/Scripts/alembic downgrade -1
DATABASE_URL="sqlite+aiosqlite:///./tracker-prod-copy.db" venv/Scripts/alembic upgrade head
```

Expected: all three counts unchanged, and the downgrade/upgrade cycle clean. **Delete the copy from both machines afterwards** — it contains real business data.

- [ ] **Step 2: Confirm the detector answers the new head on the real schema**

```bash
venv/Scripts/python -m pytest tests/test_schema_migrations.py::test_the_deploy_detector_reports_the_head_for_a_head_schema -q
```

Expected: PASS. This is the guard against the backwards-stamp that has already failed a deploy once.

- [ ] **Step 3: Manual rehearsal on localhost**

Start the server with `preview_start` (name `tracker`, port 8020) and walk the live case:

1. Open the Shipment tab. Confirm 19 Aug shows VERIFIED, 400 units, 9 cartons.
2. Press **Close plan**. Read the confirm — it must name 19 Aug with its units and cartons.
3. Accept. Confirm the toast reports one day carried.
4. Confirm 19 Aug now appears on the new plan with a **carried in** badge.
5. Confirm the Chana Sattu row reads **Still to pack 100**, not 500. *This is the assertion that catches the whole design being wrong.*
6. Open **Plan history**. Confirm 10–18 Aug are listed with their invoice numbers (ST/26-27/077 … 080).
7. Click the closed plan and download its packed sheet. Confirm it opens and carries the right dates.
8. Confirm every page is still light and legible.

- [ ] **Step 4: Deploy**

```bash
ssh -i "C:\Users\LENOVO\Desktop\old downloads\amazon-tracker-key.pem" ubuntu@13.233.144.148
cd /opt/amazon-tracker && ./deploy/update-ec2.sh
```

If the deploy fails on the baseline detector, break the self-perpetuating rollback loop first:

```bash
git fetch origin claude/stoic-allen-bb3a55 && git checkout origin/claude/stoic-allen-bb3a55 -- deploy/update-ec2.sh
```

then deploy again.

- [ ] **Step 5: Update CLAUDE.md**

Add to the Shipment System section, after "Plan lifecycle: draft → active → closed":

```markdown
### Closing a plan, and carrying its boxes forward
`POST /plan/{id}/close` retires the active plan. Packed-but-unshipped days (`held`,
`submitted` or `verified`, uninvoiced) **move to the next plan** — `plan_id` is updated
and `carried_from_plan_id` stamped.

**The day moves; its units are never copied.** `logic.remaining_for` ignores
`available` by design, so adding carried units there would tell the packer to box 400
already-packed units a second time. Because every aggregation reaches days through
`load_days(plan_id)`, moving that one column makes the new plan count them correctly
with no new arithmetic.

**Shipped days never carry.** `parse_stock_csv` sums three `afn-inbound-*` columns into
`fba_stock`, so a shipped day is already inside `deficit = projection − fba_stock`.

Close **refuses (409) having moved nothing** when a day is `open` or has an
`inbound_plan_id` with no confirmation. It **warns** about shipped-but-uninvoiced days.

`GET /plans` and `GET /plan/{id}/detail` make a closed plan readable, and all five
downloads take `?plan_id=`. `attach-invoice` also takes one — it resolved only the
active plan, so closing used to make an invoice unrecordable against a shipped day.
```

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record the plan close, carry-forward and history behaviour"
```

---

## Self-Review

**Spec coverage** — every section maps to a task:

| Spec section | Task |
|---|---|
| Decision 1 (only packed-but-unshipped carries) | 2 (`CARRIABLE_STATUSES`, shipped/invoiced tests) |
| Decision 2 (carry the DAY, not a quantity) | 3 + 5b (`remaining == 100` assertion) |
| Decision 3 (`/close`, refusals, warnings, orphan rows, `attach_invoice` fix) | 3b, 5a, 5b |
| Decision 4 (history, `?plan_id=`, lineage both ways, access) | 4, 5b |
| Schema + detector branch | 1 |
| UI | 6 |
| Verification (all 8 bullets) | 2, 3, 3b, 5a, 5b, 6, 7 |
| Out of scope | Nothing implements un-close, partial picking, or draft history |

**Placeholder scan** — no TBD/TODO; every code step carries complete code; every test step carries a real command and expected result.

**Type consistency** — `carriable_days` returns `carry` / `blocked` / `shipped_uninvoiced` and is read with those keys in Task 3b and 5b. `close_plan` returns `closed` / `carried` / `orphan_asins` / `blocked` / `shipped_uninvoiced` / `target_plan_id`, consumed identically in 5b. `carried_from_plan_id` is spelled the same in the model (1), the payload (3 Step 4), the router (5b) and the template (6). `list_plans` emits `invoice_numbers`, read as `invoice_numbers` in Task 6.
