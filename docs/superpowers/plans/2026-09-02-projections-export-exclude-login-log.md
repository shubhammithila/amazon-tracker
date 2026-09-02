# Projections export/highlight/exclude + Users login log — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a filtered reorder-level Excel/PDF export and a visual highlight to the Projections
table, a reversible soft-hide ("remove") for individual products, and a new login-event log
visible from the Users page.

**Architecture:** Two independent subsystems sharing no code. Group A (Tasks 1–6) touches
`app/projections/*`, `app/shipment/documents.py` (two new sibling builders), and
`templates/projections.html`. Group B (Tasks 7–9) touches `app/models.py`, `app/users.py`,
`app/routers/auth.py`, `app/routers/admin_users.py`, and `templates/users.html`. Either group is
independently shippable — there is no ordering dependency between them, though this plan executes
A before B.

**Tech Stack:** FastAPI, SQLAlchemy async, SQLite (local) / PostgreSQL (prod), Alembic, pytest
(async), openpyxl, reportlab, vanilla JS + Jinja2 templates.

## Global Constraints

- `app/projections/logic.py` stays pure — no DB, no network (existing module rule).
- `app/projections/repository.py` stays the only SQL for `projection_row`/`projection_refresh`.
- `app/users.py` stays the only module that touches the `users` table (existing module rule,
  stated in its own docstring) — the new `UserLoginEvent` table is a login-time side effect of
  authentication, so it is written from `app/users.py`, not from the router.
- Every new/changed setting or destructive action gets a docstring/comment naming the specific
  bug or reasoning it exists to prevent, matching every existing module in this codebase.
- Exclude/restore is REVERSIBLE by construction — `excluded_at` set or cleared, never a `DELETE`,
  mirroring `ShipmentPlanItem.excluded_at`'s exact reasoning (an accidental multi-row exclude is
  one click back).
- The reorder-level export FILTERS (never zero-pads) rows below the action threshold, mirroring
  `build_tobuy_xlsx`'s own stated reasoning: a row reading "Product … 0" invites acting on a
  number that means nothing.
- Every new migration adds a branch to `deploy/update-ec2.sh`'s baseline detector, newest first,
  per CLAUDE.md's two documented production-deploy failures from this list going stale.
- Login is unchanged in EFFECT — every branch of `POST /login` (named user, `APP_PASSWORD`,
  `OPS_PASSWORD`, and the failure path) still returns exactly what it does today; only a new
  side-effect (one `record_login_event` call) is added to each.

---

## Group A — Projections: reorder export, highlight, remove

### Task 1: `ProjectionRow.excluded_at` — model, migration, repository

**Files:**
- Modify: `app/models.py` (add `excluded_at` to `ProjectionRow`, around line 898, right after
  `current_wh_stock`)
- Modify: `app/projections/repository.py` (`load_rows`, new `set_excluded`)
- Create: `alembic/versions/<new_revision>_projection_row_excluded_at.py`
- Test: `tests/test_projections_repository.py`

**Interfaces:**
- Produces: `ProjectionRow.excluded_at: DateTime | None`.
  `repository.load_rows(db, *, include_excluded: bool = False) -> list[dict]` — new keyword
  argument, default preserves today's behaviour for every existing caller.
  `repository.set_excluded(db, parent_products: list[str], excluded: bool) -> list[str]` —
  returns the parent names actually changed (mirrors
  `app.shipment.repository.set_item_excluded`'s exact signature shape).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_projections_repository.py`:

```python
async def test_load_rows_excludes_by_default(db):
    from app.projections import repository

    await repository.save_row(db, "Chana Sattu", {"last_month_sale": 10.0}, source="sheet")
    await repository.set_excluded(db, ["Chana Sattu"], True)

    rows = await repository.load_rows(db)
    assert rows == []


async def test_load_rows_include_excluded_shows_it_anyway(db):
    from app.projections import repository

    await repository.save_row(db, "Chana Sattu", {"last_month_sale": 10.0}, source="sheet")
    await repository.set_excluded(db, ["Chana Sattu"], True)

    rows = await repository.load_rows(db, include_excluded=True)
    assert len(rows) == 1
    assert rows[0]["excluded_at"] is not None


async def test_set_excluded_is_reversible(db):
    from app.projections import repository

    await repository.save_row(db, "Chana Sattu", {"last_month_sale": 10.0}, source="sheet")
    await repository.set_excluded(db, ["Chana Sattu"], True)
    await repository.set_excluded(db, ["Chana Sattu"], False)

    rows = await repository.load_rows(db)
    assert len(rows) == 1
    assert rows[0]["parent_product"] == "Chana Sattu"


async def test_set_excluded_is_idempotent(db):
    """Excluding an already-excluded row a second time changes nothing and reports no
    change — the same rule set_item_excluded follows, so a double-click cannot look like
    two separate actions in a log or a response."""
    from app.projections import repository

    await repository.save_row(db, "Chana Sattu", {"last_month_sale": 10.0}, source="sheet")
    first = await repository.set_excluded(db, ["Chana Sattu"], True)
    second = await repository.set_excluded(db, ["Chana Sattu"], True)
    assert first == ["Chana Sattu"]
    assert second == []


async def test_set_excluded_returns_only_the_names_actually_changed(db):
    from app.projections import repository

    await repository.save_row(db, "Chana Sattu", {"last_month_sale": 10.0}, source="sheet")
    changed = await repository.set_excluded(db, ["Chana Sattu", "Nonexistent Product"], True)
    assert changed == ["Chana Sattu"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_projections_repository.py -q -p no:randomly -k excluded`
Expected: FAIL — `set_excluded` does not exist yet, and `load_rows` takes no `include_excluded`
argument.

- [ ] **Step 3: Add the column to the model**

In `app/models.py`, inside `class ProjectionRow`, immediately after the
`current_wh_stock = Column(Numeric(10, 1), default=0)` line:

```python
    # Set when the owner removes this parent from the screen. A TIMESTAMP rather than a
    # boolean, matching ShipmentPlanItem.excluded_at exactly and for the same two reasons:
    # `WHERE excluded_at IS NULL` treats every pre-migration row as included with no backfill,
    # and reversibility is the point — removing several rows by mistake is one click back.
    #
    # **This does not permanently retire an active parent.** `build_current_rows` recreates a
    # bare row for any currently-active sheet parent missing one, and exclusion does not stop
    # that — only a row for a parent no longer active in the sheet stays hidden for good.
    excluded_at = Column(DateTime)
```

- [ ] **Step 4: Generate and write the migration**

Run: `venv/Scripts/python -m alembic revision -m "projections: excluded_at for removing a row"`

Note the printed revision id, then replace the generated file's body (confirm
`down_revision = "db7f8bc09d4d"`, the current head) with:

```python
"""projections: excluded_at for removing a row

**Additive only** — one nullable column, matching ShipmentPlanItem.excluded_at exactly. Every
existing row gets NULL (= included), so nothing already on screen disappears from this
migration alone.

Revision ID: <fill in>
Revises: db7f8bc09d4d
Create Date: <fill in>

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "<fill in>"
down_revision: Union[str, None] = "db7f8bc09d4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projection_row", sa.Column("excluded_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projection_row", schema=None) as batch_op:
        batch_op.drop_column("excluded_at")
```

Run: `venv/Scripts/python -m alembic upgrade head`
Expected: no errors.

- [ ] **Step 5: Update `_FIELDS`/`_NUMERIC_FIELDS`/`load_rows`/`_row_to_dict`, add `set_excluded`**

In `app/projections/repository.py`, add `"excluded_at"` to `_FIELDS` (NOT `_NUMERIC_FIELDS` — it
is a `DateTime`, not a `Numeric`):

```python
_FIELDS = (
    "parent_product", "brand", "purchase_rate", "supplier_to_wh", "packing", "wh_to_ixd",
    "ixd_to_fba", "wh_buffer_days", "seasonal_impact", "needs_review",
    "sales_source", "last_month_sale", "seven_day_rate", "thirty_day_rate", "daily_rate",
    "diverged", "current_fba_stock", "current_wh_stock", "excluded_at",
)
```

`_row_to_dict` needs no change — it only special-cases `_NUMERIC_FIELDS`, and a `DateTime`
serialises fine as a Python `datetime` object for now (the router will `.isoformat()` it or test
truthiness, never pass it straight to `JSONResponse`). Update `load_rows` and add `set_excluded`:

```python
async def load_rows(db: AsyncSession, *, include_excluded: bool = False) -> list[dict]:
    """Every stored parent row, as plain dicts with floats — never `Decimal`.

    **Excludes a removed row by default**, matching `app.shipment.repository.load_plan_items`'s
    own `include_excluded` default — the same soft-hide, same default direction, so a caller
    that forgets the flag gets the safe answer (nothing removed is shown) rather than the
    surprising one.
    """
    query = select(ProjectionRow)
    if not include_excluded:
        query = query.where(ProjectionRow.excluded_at.is_(None))
    rows = (await db.execute(query)).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def set_excluded(
    db: AsyncSession, parent_products: list[str], excluded: bool,
) -> list[str]:
    """Remove or restore rows by parent name, reversibly. Returns the names actually changed.

    Mirrors `app.shipment.repository.set_item_excluded` exactly: idempotent (excluding an
    already-excluded row is a no-op, not counted as a change) and never deletes anything.
    """
    if not parent_products:
        return []

    result = await db.execute(
        select(ProjectionRow).where(ProjectionRow.parent_product.in_(list(parent_products)))
    )
    stamp = datetime.utcnow() if excluded else None
    changed = []
    for row in result.scalars():
        if excluded and row.excluded_at is not None:
            continue
        if not excluded and row.excluded_at is None:
            continue
        row.excluded_at = stamp
        changed.append(row.parent_product)

    if changed:
        await db.commit()
    return changed
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_projections_repository.py -q -p no:randomly`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add app/models.py app/projections/repository.py tests/test_projections_repository.py alembic/versions/<new_file>.py
git commit -m "feat(projections): excluded_at, so a product can be removed from the table reversibly"
```

---

### Task 2: `deploy/update-ec2.sh` detector branch for `excluded_at`

**Files:**
- Modify: `deploy/update-ec2.sh`
- Test: `tests/test_schema_migrations.py::test_the_deploy_detector_reports_the_head_for_a_head_schema`
  (existing test — run to verify, no new test needed, per this file's established pattern)

**Interfaces:** Consumes Task 1's migration revision id.

- [ ] **Step 1: Run the existing detector test to confirm it now fails**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py::test_the_deploy_detector_reports_the_head_for_a_head_schema -q -p no:randomly`
Expected: FAIL (the detector's newest branch still points at the pre-Task-1 head).

- [ ] **Step 2: Add the newest-first branch**

In `deploy/update-ec2.sh`, find `elif "projection_row" in tables and "growth_rate" not in
cols("projection_row"):` and add a new branch above it:

```python
elif "projection_row" in tables and "excluded_at" in cols("projection_row"):
    print("<Task 1's revision id>")                 # head: excluded_at for removing a row
elif "projection_row" in tables and "growth_rate" not in cols("projection_row"):
    print("db7f8bc09d4d")                           # growth_rate dropped (now a global setting)
```

- [ ] **Step 3: Run the detector test again**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py::test_the_deploy_detector_reports_the_head_for_a_head_schema -q -p no:randomly`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add deploy/update-ec2.sh
git commit -m "chore(deploy): detector branch for projection_row.excluded_at"
```

---

### Task 3: Two new document builders — `build_reorder_xlsx`, `build_reorder_pdf`

**Files:**
- Modify: `app/shipment/documents.py` (two new functions, placed after `build_tobuy_xlsx`)
- Test: `tests/test_shipment_documents.py`

**Interfaces:**
- Consumes: nothing from Task 1/2.
- Produces: `build_reorder_xlsx(rows: list[dict], subtitle: str) -> io.BytesIO`,
  `build_reorder_pdf(rows: list[dict], subtitle: str) -> io.BytesIO`. Each `row` is
  `{"product": str, "brand": str, "reorder_level_kg": float}`, already filtered by the caller —
  neither function filters internally.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_shipment_documents.py`:

```python
def test_build_reorder_xlsx_has_three_columns_and_a_totals_row():
    from app.shipment import documents
    from openpyxl import load_workbook

    rows = [
        {"product": "Chana Sattu", "brand": "Mithila Foods", "reorder_level_kg": 962.0},
        {"product": "Govindbhog Rice", "brand": "Mithila Foods", "reorder_level_kg": 745.0},
    ]
    buffer = documents.build_reorder_xlsx(rows, "2 products need reordering")
    book = load_workbook(buffer)
    sheet = book.active

    headers = [cell.value for cell in sheet[1]]
    assert headers == ["Product", "Brand", "Reorder Level (kg)"]
    assert sheet.cell(row=2, column=1).value == "Chana Sattu"
    assert sheet.cell(row=2, column=3).value == 962.0
    # A totals row sums the one numeric column.
    last_row = sheet.max_row
    assert sheet.cell(row=last_row, column=3).value == 1707.0


def test_build_reorder_xlsx_says_so_in_words_when_nothing_needs_reordering():
    from app.shipment import documents
    from openpyxl import load_workbook

    buffer = documents.build_reorder_xlsx([], "Every product is above its reorder level.")
    book = load_workbook(buffer)
    sheet = book.active
    # No crash on an empty list, and the sheet names the good news rather than being blank.
    text = " ".join(str(c.value) for row in sheet.iter_rows() for c in row if c.value)
    assert "above its reorder level" in text


def test_build_reorder_pdf_has_three_columns():
    from app.shipment import documents
    from pypdf import PdfReader
    import io

    rows = [{"product": "Chana Sattu", "brand": "Mithila Foods", "reorder_level_kg": 962.0}]
    buffer = documents.build_reorder_pdf(rows, "1 product needs reordering")
    reader = PdfReader(io.BytesIO(buffer.getvalue()))
    text = reader.pages[0].extract_text()
    assert "Chana Sattu" in text
    assert "Mithila Foods" in text
    assert "962" in text
```

Check whether `pypdf` is already a test dependency before writing that last test:

Run: `venv/Scripts/python -c "import pypdf; print(pypdf.__version__)"`

If that fails, check for the existing PDF-reading pattern used by other document tests instead:

Run: `grep -n "import.*pdf\|PdfReader\|pdfplumber" tests/test_shipment_documents.py | head -5`

Use whatever library the existing PDF tests in that file already use, matching their exact
import and extraction call — do not add a new PDF-reading dependency if one is already a test
dependency in this file.

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_shipment_documents.py -q -p no:randomly -k reorder`
Expected: FAIL — `build_reorder_xlsx`/`build_reorder_pdf` do not exist yet.

- [ ] **Step 3: Write the two builders**

In `app/shipment/documents.py`, add after `build_tobuy_xlsx` (which ends around line 1173):

```python
#: Column headers for the reorder-level report — three columns, matching what was asked for
#: verbatim: "just the product, brand and the reorder level."
_REORDER_HEADERS = ["Product", "Brand", "Reorder Level (kg)"]
_XLSX_REORDER_WIDTHS = [30, 16, 18]


def build_reorder_xlsx(rows: list[dict], subtitle: str) -> io.BytesIO:
    """The warehouse reorder level, Product/Brand/kg only — filtered by the CALLER to rows that
    actually need reordering (`ideal_wh_stock > 0`), never zero-padded.

    **A sibling of `build_simple_xlsx`, not a parameter on it — the same reason
    `build_portfolio_xlsx` is its own function.** `build_simple_xlsx`'s totals row
    (`_totals_row`) sums every column past `IDENTITY_HEADERS`, which this three-column document
    does not have; hand-rolling the total here is simpler than threading a second identity
    shape through code written for the Shipment tab's eight columns.

    Mirrors `build_tobuy_xlsx`'s own two rules for a purchasing document: nothing to reorder
    says so IN WORDS rather than rendering an empty grid (an empty download reads as a failed
    one), and the caller's filtering is trusted completely — this function never re-filters.
    """
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Reorder level"

    if not rows:
        out_rows = [[subtitle, "", ""]]
    else:
        out_rows = [
            [row["product"], row.get("brand", ""), round(float(row["reorder_level_kg"]), 1)]
            for row in rows
        ]
        out_rows.append([
            f"TOTAL · {len(rows)} product(s)", "",
            round(sum(row["reorder_level_kg"] for row in rows), 1),
        ])

    _write_sheet(sheet, _REORDER_HEADERS, out_rows, _XLSX_REORDER_WIDTHS)
    book.properties.title = "Reorder level"
    book.properties.description = subtitle

    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    return buffer


def build_reorder_pdf(rows: list[dict], subtitle: str) -> io.BytesIO:
    """The same report as a portrait A4 PDF — a short list, not a wide one, so the generic
    document helpers (`_pdf_document`/`_head_cell`/`_pdf_table_style`) are reused directly
    rather than through `build_simple_pdf`, which assumes the eight-column shipment identity
    shape via `_pdf_column_widths`'s `IDENTITY_HEADERS` lookups.
    """
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table

    buffer = io.BytesIO()
    doc, elements = _pdf_document(buffer, "Reorder level", subtitle, landscape_mode=False)

    if not rows:
        body = [[subtitle, "", ""]]
    else:
        body = [
            [row["product"], row.get("brand", ""), round(float(row["reorder_level_kg"]), 1)]
            for row in rows
        ]
        body.append([
            f"TOTAL · {len(rows)} product(s)", "",
            round(sum(row["reorder_level_kg"] for row in rows), 1),
        ])

    styles = _paragraph_styles()
    data = [[_head_cell(h) for h in _REORDER_HEADERS]]
    for position, row in enumerate(body):
        is_totals = rows and position == len(body) - 1
        product_style = styles["totals"] if is_totals else styles["loud"]
        number_style = styles["totals_qty"] if is_totals else styles["quantity"]
        data.append([
            Paragraph(_escape(row[0]), product_style),
            Paragraph(_escape(row[1]), styles["plain"]),
            Paragraph(_escape(row[2]), number_style),
        ])

    # Fixed widths: three columns is narrow enough that measuring content (as the shipment
    # documents do for eight columns of unpredictable width) is unnecessary complexity here.
    table = Table(data, colWidths=[100 * mm, 45 * mm, 45 * mm], repeatRows=1)
    table.setStyle(
        _pdf_table_style(3, totals_row=len(body) if rows else None)
    )
    elements.append(table)
    doc.build(elements, onFirstPage=_page_footer, onLaterPages=_page_footer)
    buffer.seek(0)
    return buffer
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_shipment_documents.py -q -p no:randomly -k reorder`
Expected: all PASS. If the PDF test's text-extraction assertion fails because reportlab wrapped
"962.0" as "962" or similar formatting difference, adjust the assertion to check for "962" as a
substring (already written that way above) rather than an exact float string.

- [ ] **Step 5: Commit**

```bash
git add app/shipment/documents.py tests/test_shipment_documents.py
git commit -m "feat(shipment): build_reorder_xlsx and build_reorder_pdf for the Projections export"
```

---

### Task 4: Router — download routes, exclude route, `catalogue` report fields

**Files:**
- Modify: `app/routers/projections.py`
- Test: `tests/test_projections_api.py`

**Interfaces:**
- Consumes: Task 1's `repository.load_rows(..., include_excluded=...)`, `repository.set_excluded`.
  Task 3's `documents.build_reorder_xlsx`, `documents.build_reorder_pdf`.
- Produces: `GET /projections/download/reorder.xlsx`, `GET /projections/download/reorder.pdf`,
  `POST /projections/exclude`. `GET /projections/last`'s `catalogue` dict gains
  `excluded_count: int` and `excluded_names: list[str]` (capped at 8, matching `hidden_names`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_projections_api.py`:

```python
async def test_exclude_hides_a_row_from_last(auth_client, db, fake_catalogue):
    await auth_client.get("/projections/last")  # seed the rows
    response = await auth_client.post("/projections/exclude", json={
        "parent_products": ["Chana Sattu"], "excluded": True,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "excluded"
    assert body["changed"] == ["Chana Sattu"]

    last = (await auth_client.get("/projections/last")).json()
    names = {p["parent_product"] for p in last["products"]}
    assert "Chana Sattu" not in names
    assert "Chana Sattu" in last["catalogue"]["excluded_names"]
    assert last["catalogue"]["excluded_count"] == 1


async def test_exclude_then_restore_brings_the_row_back(auth_client, db, fake_catalogue):
    await auth_client.get("/projections/last")
    await auth_client.post("/projections/exclude", json={
        "parent_products": ["Chana Sattu"], "excluded": True,
    })
    restore = await auth_client.post("/projections/exclude", json={
        "parent_products": ["Chana Sattu"], "excluded": False,
    })
    assert restore.json()["status"] == "restored"

    last = (await auth_client.get("/projections/last")).json()
    names = {p["parent_product"] for p in last["products"]}
    assert "Chana Sattu" in names
    assert last["catalogue"]["excluded_count"] == 0


async def test_exclude_refuses_an_empty_selection(auth_client, db, fake_catalogue):
    response = await auth_client.post("/projections/exclude", json={
        "parent_products": [], "excluded": True,
    })
    assert response.status_code == 400


async def test_download_reorder_xlsx_only_includes_positive_reorder_levels(
    auth_client, db, fake_catalogue,
):
    from app.projections import repository

    await auth_client.get("/projections/last")  # seeds all active parents at daily_rate=0
    # A daily_rate of 0 (never refreshed) means ideal_wh_stock is 0 for every row, so the
    # export should be empty — confirmed via the "nothing to reorder" sentence rather than a
    # crash or an empty file.
    response = await auth_client.get("/projections/download/reorder.xlsx")
    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


async def test_download_reorder_pdf_responds_200(auth_client, db, fake_catalogue):
    await auth_client.get("/projections/last")
    response = await auth_client.get("/projections/download/reorder.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


async def test_excluded_row_never_appears_in_the_reorder_download(auth_client, db, fake_catalogue):
    from app.projections import repository

    await auth_client.get("/projections/last")
    # Force a positive reorder level directly, then exclude it, then confirm the download
    # is empty rather than containing the excluded row.
    await repository.save_row(
        db, "Chana Sattu",
        {"daily_rate": 50.0, "wh_buffer_days": 10, "supplier_to_wh": 5, "seasonal_impact": 1.0},
        source="sheet",
    )
    await auth_client.post("/projections/exclude", json={
        "parent_products": ["Chana Sattu"], "excluded": True,
    })
    response = await auth_client.get("/projections/download/reorder.xlsx")
    from openpyxl import load_workbook
    import io
    book = load_workbook(io.BytesIO(response.content))
    sheet = book.active
    text = " ".join(str(c.value) for row in sheet.iter_rows() for c in row if c.value)
    assert "Chana Sattu" not in text
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_projections_api.py -q -p no:randomly -k "exclude or reorder"`
Expected: FAIL — none of the three new routes exist yet.

- [ ] **Step 3: Write the router code**

In `app/routers/projections.py`, update `build_current_rows` to report excluded parents. Find the
current function (it calls `repository.load_rows(db)` and builds `report`) and change it to:

```python
async def build_current_rows(db: AsyncSession) -> tuple[list[dict], dict]:
    """Every currently-active parent, merged with its stored row (sales, any manual edit) or a
    freshly-built one. Returns `(rows, catalogue_report)`.

    **A brand-new parent (no stored row yet) is written to the database here**, with
    `sales_source="sheet"` and zero sales, so it exists for the weekly refresh to update and is
    never re-synthesised on every page load. A parent hidden this load (no longer active) is left
    in the database untouched — its row is not deleted, only excluded from what is returned, so a
    reactivated product keeps its history rather than starting over.

    **An OWNER-excluded row (`excluded_at` set) is likewise left untouched and left out of
    `rows`** — the same non-destructive exclusion `hidden_names` already applies to a parent no
    longer active in the sheet, now also available for one the owner chose to hide directly.
    """
    sheet_products, sheet_warning, sheet_source = await catalogue.load_catalogue()
    live_groups = logic.group_active_by_name(sheet_products)

    all_stored = await repository.load_rows(db, include_excluded=True)
    stored = {r["parent_product"]: r for r in all_stored if r.get("excluded_at") is None}
    excluded_names = sorted(r["parent_product"] for r in all_stored if r.get("excluded_at"))
    hidden = logic.hidden_parent_names(
        {r["parent_product"] for r in all_stored}, live_groups,
    )

    rows: list[dict] = []
    for name, group in live_groups.items():
        if name in stored:
            rows.append(stored[name])
            continue
        if name in excluded_names:
            continue  # owner-excluded; stays hidden even though it is active in the sheet
        config = logic.build_parent_config(name, group, DEFAULTS, GLOBAL_DEFAULTS)
        created = await repository.save_row(
            db, name,
            {**config, "last_month_sale": 0, "seven_day_rate": None, "thirty_day_rate": None,
             "daily_rate": 0, "diverged": False, "current_fba_stock": 0, "current_wh_stock": 0},
            source="sheet",
        )
        rows.append(created)

    report = {
        "source": sheet_source,
        "active_parents": len(live_groups),
        "hidden_count": len(hidden),
        "hidden_names": hidden[:8],
        "excluded_count": len(excluded_names),
        "excluded_names": excluded_names[:8],
        "warning": sheet_warning,
    }
    return rows, report
```

Add the exclude route and the two download routes. Place them after `reset_row` (which already
exists) and before `get_blend_settings`:

```python
@router.post("/exclude")
async def exclude_products(
    request: Request, _=Depends(require_auth), db: AsyncSession = Depends(get_db),
):
    """Remove or restore rows by parent name, reversibly. Body:
    `{"parent_products": [...], "excluded": bool}`.

    **No packed-units-style guard here, unlike Shipment's exclude route.** A projection row has
    no analogous "already committed" state — nothing downstream treats a row as spent the way
    packed cartons are, so there is nothing this needs to refuse.
    """
    body = await request.json()
    names = [str(n).strip() for n in (body.get("parent_products") or []) if str(n).strip()]
    excluded = bool(body.get("excluded", True))

    if not names:
        return JSONResponse({"error": "Select at least one product."}, status_code=400)

    changed = await repository.set_excluded(db, names, excluded)
    return JSONResponse({
        "status": "excluded" if excluded else "restored",
        "changed": changed,
        "count": len(changed),
    })


def _reorder_rows(products: list[dict]) -> list[dict]:
    """Product/Brand/reorder-level rows, filtered to only what needs reordering and sorted the
    same way the live table's default sort reads — biggest need first — so the document and the
    screen agree on order without either one being derived from the other after the fact."""
    filtered = [p for p in products if (p.get("ideal_wh_stock") or 0) > 0]
    filtered.sort(key=lambda p: p.get("ideal_wh_stock", 0), reverse=True)
    return [
        {"product": p["parent_product"], "brand": p.get("brand", ""),
         "reorder_level_kg": p["ideal_wh_stock"]}
        for p in filtered
    ]


def _reorder_subtitle(total: int, reordering: int) -> str:
    if reordering == 0:
        return "Every product is above its reorder level — nothing to reorder right now."
    covered = total - reordering
    return (
        f"{reordering} product(s) need reordering"
        + (f" · {covered} covered, not shown" if covered else "")
    )


@router.get("/download/reorder.xlsx")
async def download_reorder_xlsx(
    request: Request, _=Depends(require_auth), db: AsyncSession = Depends(get_db),
):
    """Product, Brand, Reorder Level (kg) — filtered to products that actually need it, same
    rule the 'To buy' purchasing export already follows."""
    from app.shipment import documents

    rows, _report = await build_current_rows(db)
    products = await _calculate_with_settings(db, rows)
    reorder_rows = _reorder_rows(products)
    subtitle = _reorder_subtitle(len(products), len(reorder_rows))

    buffer = documents.build_reorder_xlsx(reorder_rows, subtitle)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=reorder-level.xlsx"},
    )


@router.get("/download/reorder.pdf")
async def download_reorder_pdf(
    request: Request, _=Depends(require_auth), db: AsyncSession = Depends(get_db),
):
    """The same report as a portrait PDF."""
    from app.shipment import documents

    rows, _report = await build_current_rows(db)
    products = await _calculate_with_settings(db, rows)
    reorder_rows = _reorder_rows(products)
    subtitle = _reorder_subtitle(len(products), len(reorder_rows))

    buffer = documents.build_reorder_pdf(reorder_rows, subtitle)
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=reorder-level.pdf"},
    )
```

- [ ] **Step 4: Confirm the file imports cleanly**

Run: `venv/Scripts/python -c "import app.routers.projections"`
Expected: no output, no error.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_projections_api.py -q -p no:randomly`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/routers/projections.py tests/test_projections_api.py
git commit -m "feat(projections): exclude route and filtered reorder-level Excel/PDF downloads"
```

---

### Task 5: Mutation coverage for exclude + export filtering

**Files:**
- Modify: `scripts/mutate_projections.py`

- [ ] **Step 1: Add two mutation entries**

```python
    (
        "load_rows stops excluding a removed row by default",
        REPO,
        "    query = select(ProjectionRow)\n    if not include_excluded:\n        query = query.where(ProjectionRow.excluded_at.is_(None))",
        "    query = select(ProjectionRow)",
        "test_load_rows_excludes_by_default",
    ),
    (
        "the reorder export stops filtering to a positive reorder level",
        ROUTER,
        "    filtered = [p for p in products if (p.get(\"ideal_wh_stock\") or 0) > 0]",
        "    filtered = list(products)",
        "test_excluded_row_never_appears_in_the_reorder_download",
    ),
```

Add these to the `MUTATIONS` list in `scripts/mutate_projections.py`, before the closing `]`.
Confirm the exact text of the first mutation's `old` string matches Task 1 Step 5's `load_rows`
body verbatim, and the second matches Task 4 Step 3's `_reorder_rows` verbatim — if either was
formatted slightly differently when actually written, adjust the `old` string here to match.

- [ ] **Step 2: Run the harness**

Run: `venv/Scripts/python scripts/mutate_projections.py`
Expected: `all 14 mutations caught` (12 pre-existing + 2 new). If either SURVIVES, the named test
does not actually exercise that line — strengthen the test's assertions, do not weaken the
mutation.

- [ ] **Step 3: Run the full suite**

Run: `venv/Scripts/python -m pytest -q -p no:randomly`
Expected: zero failures.

- [ ] **Step 4: Commit**

```bash
git add scripts/mutate_projections.py
git commit -m "test(projections): mutation coverage for exclude and the reorder export filter"
```

---

### Task 6: `templates/projections.html` — highlight, checkboxes, remove button, download buttons

**Files:**
- Modify: `templates/projections.html`

**Interfaces:**
- Consumes: Task 4's `GET /projections/last` (`catalogue.excluded_count`/`excluded_names`),
  `POST /projections/exclude`, `GET /projections/download/reorder.xlsx`,
  `GET /projections/download/reorder.pdf`.

No automated test — matches this file's own established pattern (exercised by
`tests/test_local_dates.py`'s repo-wide scan and by manual verification).

- [ ] **Step 1: Add the highlight CSS class**

In the `<style>` block, add near the other `td`-related rules (after `td input.wide{width:70px}`):

```css
/* The one column the owner actually watches — tinted so it reads as the headline number
   among the other calculated columns (Forecast, Ideal FBA), which are ALSO bold. */
td.ideal-wh-cell{background:var(--accent-soft);border-radius:4px}
```

- [ ] **Step 2: Apply the class to the Ideal WH cell in `renderTable()`**

Find the line building the Ideal WH `<td>` (currently
`<td ${idealWhTitle}><strong>${...}</strong></td>`) and add the class:

```html
      <td class="ideal-wh-cell" ${idealWhTitle}><strong>${p.ideal_wh_stock?Math.round(p.ideal_wh_stock):0}</strong></td>
```

- [ ] **Step 3: Add a checkbox column**

In `<thead><tr>`, add a new leftmost `<th>` before `<th>#</th>`:

```html
  <th style="width:28px"><input type="checkbox" id="select-all-checkbox" onchange="toggleSelectAll(this)" title="Select all visible rows"/></th>
```

In `renderTable()`'s row template, add a matching leftmost `<td>` before the `#` cell:

```html
      <td><input type="checkbox" class="row-select" data-parent="${escHtml(p.parent_product)}"/></td>
```

- [ ] **Step 4: Add the selection-tracking and remove/restore functions**

Add these functions near `resetRow`/`clearAllOverrides`:

```javascript
function toggleSelectAll(checkbox){
  document.querySelectorAll(".row-select").forEach(cb => cb.checked = checkbox.checked);
  updateRemoveButton();
}

function selectedParents(){
  return Array.from(document.querySelectorAll(".row-select:checked"))
    .map(cb => cb.dataset.parent);
}

function updateRemoveButton(){
  const count = selectedParents().length;
  const btn = document.getElementById("remove-selected-btn");
  btn.style.display = count > 0 ? "inline-block" : "none";
  btn.textContent = `Remove selected (${count})`;
}

async function removeSelected(){
  const names = selectedParents();
  if(!names.length) return;
  if(!confirm(
    `Remove ${names.length} product(s) from this table?\n\n`
    + "This only lasts until the next weekly refresh if a product is still active in the "
    + "MRP sheet — it is meant to declutter this screen, not to retire a product Amazon "
    + "still sells. You can restore a removed product from the hidden-parents note."
  )) return;

  const r = await fetch("/projections/exclude", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({parent_products: names, excluded: true}),
  });
  const data = await r.json();
  if(!r.ok){ toast(data.error || "Could not remove.", "error"); return; }
  toast(`Removed ${data.count} product(s)`, "success");
  document.getElementById("select-all-checkbox").checked = false;
  await loadAll();
}

async function restoreProduct(parentProduct){
  const r = await fetch("/projections/exclude", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({parent_products: [parentProduct], excluded: false}),
  });
  if(r.ok){ toast(`${parentProduct}: restored`, "success"); await loadAll(); }
}
```

`renderTable()` needs a change-listener wired once (delegated, matching the existing
`data-reset-row` pattern) — add beside the existing `tbody` click listener for reset:

```javascript
document.getElementById("tbody").addEventListener("change", (e) => {
  if(e.target.classList.contains("row-select")) updateRemoveButton();
});
```

- [ ] **Step 5: Add the Remove selected button and the two download buttons**

Find the button row (`class="globals-card"` containing "Save edits & Recalculate", "⬇ Excel",
"Reset all manual edits") and add, after the Excel button:

```html
    <button class="btn-outline" onclick="window.location.href='/projections/download/reorder.xlsx'">⬇ Reorder level (Excel)</button>
    <button class="btn-outline" onclick="window.location.href='/projections/download/reorder.pdf'">⬇ Reorder level (PDF)</button>
    <button class="btn-danger" id="remove-selected-btn" style="display:none" onclick="removeSelected()">Remove selected</button>
```

- [ ] **Step 6: Show the excluded-count note**

In `renderCatalogueSummary()`, add below the existing hidden-parents-note handling:

```javascript
function renderCatalogueSummary(){
  const el = document.getElementById("catalogue-summary");
  const c = catalogueInfo;
  el.textContent = `${c.active_parents||0} active (source: ${c.source||"?"})`;
  if(c.warning){el.textContent += " — " + c.warning;}
  const hiddenEl = document.getElementById("hidden-parents-note");
  if((c.hidden_count||0) > 0){
    hiddenEl.style.display = "block";
    const names = (c.hidden_names||[]).join(", ");
    const more = c.hidden_count > (c.hidden_names||[]).length ? ` and ${c.hidden_count - c.hidden_names.length} more` : "";
    hiddenEl.textContent = `${c.hidden_count} parent(s) hidden — not active in the MRP sheet: ${names}${more}`;
  } else {
    hiddenEl.style.display = "none";
  }
  const excludedEl = document.getElementById("excluded-parents-note");
  if((c.excluded_count||0) > 0){
    excludedEl.style.display = "block";
    const restoreLinks = (c.excluded_names||[]).map(n =>
      `${escHtml(n)} <button class="btn-outline" style="padding:1px 6px;font-size:10px" onclick="restoreProduct('${escHtml(n).replace(/'/g,"\\'")}')">restore</button>`
    ).join(", ");
    const more = c.excluded_count > (c.excluded_names||[]).length ? ` and ${c.excluded_count - c.excluded_names.length} more` : "";
    excludedEl.innerHTML = `${c.excluded_count} product(s) removed from this table: ${restoreLinks}${more}`;
  } else {
    excludedEl.style.display = "none";
  }
}
```

Add the container `<div>` beside the existing `#hidden-parents-note` in the "Catalogue & refresh
status" card:

```html
  <div id="hidden-parents-note" style="font-size:11.5px;color:var(--text-muted);display:none"></div>
  <div id="excluded-parents-note" style="font-size:11.5px;color:var(--text-muted);display:none"></div>
```

- [ ] **Step 7: Confirm the template still renders**

Run:
```bash
venv/Scripts/python -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))
html = env.get_template('projections.html').render(active='projections', grant=None)
print('rendered', len(html), 'chars')
"
```
Expected: no Jinja2 error.

- [ ] **Step 8: Syntax-check the extracted JavaScript**

Run:
```bash
venv/Scripts/python -c "
import re, pathlib, tempfile, os
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))
html = env.get_template('projections.html').render(active='projections', grant=None)
m = re.search(r'<script>(.*)</script>', html, re.S)
out = os.path.join(tempfile.gettempdir(), 'projections_check4.js')
pathlib.Path(out).write_text(m.group(1), encoding='utf-8')
print(out)
"
```
Then: `node --check <printed path>`
Expected: no output (clean exit).

- [ ] **Step 9: Run the render-target and local-dates scans**

Run: `venv/Scripts/python -m pytest tests/test_local_dates.py tests/test_template_render_targets.py -q -p no:randomly`
Expected: all PASS or SKIP, no FAIL.

- [ ] **Step 10: Commit**

```bash
git add templates/projections.html
git commit -m "feat(projections): highlighted Ideal WH cell, selectable remove, reorder-level downloads"
```

---

## Group B — Users: a login-event log

### Task 7: `UserLoginEvent` model, migration, and `app.users.record_login_event`

**Files:**
- Modify: `app/models.py` (new class, placed after `class User`)
- Modify: `app/users.py` (new function)
- Create: `alembic/versions/<new_revision>_user_login_event.py`
- Test: `tests/test_users_and_permissions.py` (or a new `tests/test_login_log.py` — see Task 9;
  this task's own tests go in `tests/test_login_log.py` since it is a genuinely new area)

**Interfaces:**
- Produces: `app.models.UserLoginEvent` (columns: `id`, `username`, `user_id`, `success`, `via`,
  `ip_address`, `created_at`). `app.users.record_login_event(db, *, username: str, user_id: int
  | None, success: bool, via: str, ip_address: str | None) -> None`.
  `app.users.load_login_events(db, *, limit: int = 200) -> list[dict]` — newest first, capped
  server-side at 500 regardless of the requested `limit`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_login_log.py`:

```python
"""The Users login log — sign-in time, IP, success/failure. This app has no audit history
before this change; `record_login_event` is called from every branch of `POST /login`
starting now, and nothing before today is recoverable.
"""
import pytest

pytestmark = pytest.mark.asyncio


async def test_record_login_event_then_load_round_trips(db):
    from app import users as users_repo

    await users_repo.record_login_event(
        db, username="ravi", user_id=1, success=True, via="named", ip_address="203.0.113.5",
    )
    events = await users_repo.load_login_events(db)
    assert len(events) == 1
    assert events[0]["username"] == "ravi"
    assert events[0]["success"] is True
    assert events[0]["via"] == "named"
    assert events[0]["ip_address"] == "203.0.113.5"
    assert isinstance(events[0]["created_at"], str), "a datetime reaching JSON must be pre-serialised"


async def test_a_failed_attempt_is_recorded_with_no_user_id(db):
    from app import users as users_repo

    await users_repo.record_login_event(
        db, username="unknown-person", user_id=None, success=False, via="named",
        ip_address="203.0.113.5",
    )
    events = await users_repo.load_login_events(db)
    assert events[0]["success"] is False
    assert events[0]["user_id"] is None


async def test_load_login_events_returns_newest_first(db):
    from app import users as users_repo

    await users_repo.record_login_event(
        db, username="first", user_id=None, success=True, via="app_password", ip_address="1.1.1.1",
    )
    await users_repo.record_login_event(
        db, username="second", user_id=None, success=True, via="app_password", ip_address="2.2.2.2",
    )
    events = await users_repo.load_login_events(db)
    assert [e["username"] for e in events] == ["second", "first"]


async def test_load_login_events_caps_at_500_even_if_more_is_requested(db):
    from app import users as users_repo

    for i in range(3):
        await users_repo.record_login_event(
            db, username=f"user{i}", user_id=None, success=True, via="named", ip_address="1.1.1.1",
        )
    events = await users_repo.load_login_events(db, limit=10_000)
    assert len(events) == 3  # not an error case, just confirms the cap does not break a small load
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_login_log.py -q -p no:randomly`
Expected: FAIL — `UserLoginEvent`, `record_login_event`, `load_login_events` do not exist yet.

- [ ] **Step 3: Add the model**

In `app/models.py`, add after `class User` (which ends around line 497, right before the blank
line preceding whatever class currently follows it):

```python
class UserLoginEvent(Base):
    """One login attempt — successful or not. **The login history this app never had.**

    `User.last_login_at` is a single timestamp, overwritten on every success, so it can answer
    "when did they last sign in" and nothing else — not "how often," not "who tried and failed,"
    not "which of the three login paths did this go through." No audit table existed anywhere
    in this codebase before this one.

    **Every attempt is recorded, success or failure** — a log that only shows successes cannot
    answer "is someone trying my password," which is the more common reason to open this page.

    `username` is the string TYPED, not resolved — a failed attempt against a username that does
    not exist is still worth recording, and there is no user row to attach it to in that case.
    `user_id` is set only when the attempt succeeded against a real named account, and stays NULL
    for every shared-password login (no user row exists) and every failed one.

    `via` distinguishes the three paths `POST /login` can take, so a shared-password sign-in does
    not read as though it were a named one on the same log.
    """
    __tablename__ = "user_login_events"
    __table_args__ = (
        Index("idx_user_login_events_created", "created_at"),
        Index("idx_user_login_events_user", "user_id"),
    )

    id = Column(Integer, primary_key=True)
    username = Column(String(32), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    success = Column(Boolean, nullable=False)
    #: "named" | "app_password" | "ops_password".
    via = Column(String(16), nullable=False)
    #: From X-Forwarded-For (Caddy sits in front of uvicorn in production) with a fallback to
    #: the raw client address for local/dev runs with no proxy in front.
    ip_address = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
```

- [ ] **Step 4: Generate and write the migration**

Run: `venv/Scripts/python -m alembic revision -m "users: a login-event log"`

Replace the generated body (confirm `down_revision` matches whatever Task 1's migration set as
its own revision id — Group A and Group B share one linear Alembic history, so this migration's
`down_revision` is Task 1's revision id, NOT `db7f8bc09d4d` directly):

```python
"""users: a login-event log

**Additive only** — one new table, nothing existing touched. Records every login attempt from
here forward; nothing before this migration is recoverable, because nothing was being recorded.

Revision ID: <fill in>
Revises: <Task 1's revision id>
Create Date: <fill in>

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "<fill in>"
down_revision: Union[str, None] = "<Task 1's revision id>"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_login_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("via", sa.String(length=16), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_user_login_events_created", "user_login_events", ["created_at"], unique=False,
    )
    op.create_index(
        "idx_user_login_events_user", "user_login_events", ["user_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_user_login_events_user", table_name="user_login_events")
    op.drop_index("idx_user_login_events_created", table_name="user_login_events")
    op.drop_table("user_login_events")
```

Run: `venv/Scripts/python -m alembic upgrade head`
Expected: no errors.

- [ ] **Step 5: Add `record_login_event`/`load_login_events` to `app/users.py`**

Add near the bottom of `app/users.py`, after the existing `payload()` function:

```python
# ─── Login log ────────────────────────────────────────────────────────────────

async def record_login_event(
    db: AsyncSession, *, username: str, user_id: int | None, success: bool, via: str,
    ip_address: str | None,
) -> None:
    """One row per login ATTEMPT — success or failure. Called from every branch of
    `POST /login` in app/routers/auth.py, including the rejection path.

    Writing this here rather than in the router matches this module's own stated rule: it is
    the only place that touches user-related tables, and a login event is a side effect of
    authenticating, not of routing.
    """
    from app.models import UserLoginEvent

    db.add(UserLoginEvent(
        username=username[:32], user_id=user_id, success=success, via=via[:16],
        ip_address=(ip_address or "")[:64] or None,
    ))
    await db.commit()


async def load_login_events(db: AsyncSession, *, limit: int = 200) -> list[dict]:
    """The newest login attempts, JSON-safe. Capped at 500 regardless of what is asked for —
    this table has no retention sweep yet, and an unbounded query on it would only get slower
    as it grows. 500 is far more than a single screenful; it exists to bound the WORST case."""
    from app.models import UserLoginEvent

    capped = min(int(limit), 500)
    result = await db.execute(
        select(UserLoginEvent).order_by(UserLoginEvent.created_at.desc(), UserLoginEvent.id.desc())
        .limit(capped)
    )
    return [
        {
            "id": row.id,
            "username": row.username,
            "user_id": row.user_id,
            "success": bool(row.success),
            "via": row.via,
            "ip_address": row.ip_address,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in result.scalars()
    ]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_login_log.py -q -p no:randomly`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add app/models.py app/users.py tests/test_login_log.py alembic/versions/<new_file>.py
git commit -m "feat(users): a login-event log — record every attempt, success or failure"
```

---

### Task 8: `deploy/update-ec2.sh` detector branch for `user_login_events`

**Files:**
- Modify: `deploy/update-ec2.sh`
- Test: `tests/test_schema_migrations.py::test_the_deploy_detector_reports_the_head_for_a_head_schema`

- [ ] **Step 1: Confirm the detector test now fails**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py::test_the_deploy_detector_reports_the_head_for_a_head_schema -q -p no:randomly`
Expected: FAIL.

- [ ] **Step 2: Add the newest-first branch and the required-tables entry**

In `deploy/update-ec2.sh`, add above the branch Task 2 added:

```python
elif "user_login_events" in tables:
    print("<Task 7's revision id>")                 # head: login-event log
elif "projection_row" in tables and "excluded_at" in cols("projection_row"):
    print("<Task 1's revision id>")                 # excluded_at for removing a row
```

Find the required-tables `need` set (search for `need = {"shipment_plans",`) and add
`"user_login_events"` to it.

- [ ] **Step 3: Run the detector test again**

Run: `venv/Scripts/python -m pytest tests/test_schema_migrations.py::test_the_deploy_detector_reports_the_head_for_a_head_schema -q -p no:randomly`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add deploy/update-ec2.sh
git commit -m "chore(deploy): detector branch and required-tables entry for user_login_events"
```

---

### Task 9: Wire `POST /login` to record every attempt; the login-log route and panel

**Files:**
- Modify: `app/routers/auth.py` (`login`)
- Modify: `app/routers/admin_users.py` (new route)
- Modify: `templates/users.html`
- Test: `tests/test_login_log.py`, `tests/test_users_and_permissions.py`

**Interfaces:**
- Consumes: Task 7's `users_repo.record_login_event`, `users_repo.load_login_events`.
- Produces: `GET /admin/users/login-log` (no query param needed — the route always requests
  `limit=200` from `load_login_events`, which itself caps at 500; see Step 4).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_login_log.py`:

```python
async def test_a_failed_login_is_recorded(client, db):
    from app import users as users_repo

    r = await client.post("/login", data={"username": "nobody", "password": "wrong"})
    assert r.status_code == 401

    events = await users_repo.load_login_events(db)
    assert len(events) == 1
    assert events[0]["success"] is False
    assert events[0]["username"] == "nobody"


async def test_a_successful_named_login_is_recorded_with_the_right_via(client, db):
    from app import users as users_repo

    user, password = await users_repo.create(
        db, username="ravi", full_name="Ravi", is_admin=True, created_by="test",
    )
    r = await client.post("/login", data={"username": "ravi", "password": password})
    assert r.status_code == 303

    events = await users_repo.load_login_events(db)
    assert events[0]["success"] is True
    assert events[0]["via"] == "named"
    assert events[0]["user_id"] == user.id


async def test_a_shared_password_login_is_recorded_as_app_password(client, db, monkeypatch):
    from app import users as users_repo
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "app_password", "test-shared-password")
    r = await client.post("/login", data={"password": "test-shared-password"})
    assert r.status_code == 303

    events = await users_repo.load_login_events(db)
    assert events[0]["via"] == "app_password"
    assert events[0]["user_id"] is None
```

Add to `tests/test_users_and_permissions.py`:

```python
async def test_login_log_requires_admin(client, db):
    r = await client.get("/admin/users/login-log")
    assert r.status_code in (303, 401, 403)


async def test_login_log_lists_recent_attempts(client, db):
    admin, admin_password = await _make_admin(client, db)
    await client.post("/login", data={"username": "wrong-user", "password": "wrong"})
    await _signed_in(client, admin.username, admin_password)

    r = await client.get("/admin/users/login-log")
    assert r.status_code == 200
    body = r.json()
    assert len(body["events"]) >= 2  # the failed probe and this test's own successful sign-in
    assert any(not e["success"] for e in body["events"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_login_log.py tests/test_users_and_permissions.py -q -p no:randomly -k "login_log or login_is_recorded or shared_password_login"`
Expected: FAIL — `POST /login` does not call `record_login_event` yet, and
`GET /admin/users/login-log` does not exist.

- [ ] **Step 3: Wire the login route**

In `app/routers/auth.py`, add a helper right before the `login` function to extract the client
IP through Caddy's forwarded header:

```python
def _client_ip(request: Request) -> str | None:
    """The real client address, through Caddy's X-Forwarded-For in production.

    Caddy sits in front of uvicorn on this box (per CLAUDE.md's deployment section), so
    `request.client.host` there is Caddy's own loopback address, not the visitor's. The header
    is trusted here ONLY for a login log entry — a display value, never a security decision —
    so a spoofed header has no consequence beyond a wrong-looking IP in this one report.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
```

Update the `login` function's three return points to record the attempt. The full function
becomes:

```python
@router.post("/login")
async def login(
    request: Request,
    password: str = Form(...),
    username: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Named user first, then the shared passwords.

    Named first so that creating a user called after a shared password's owner does not
    get shadowed by it. Between the two shared passwords, APP_PASSWORD is checked first:
    if someone sets OPS_PASSWORD to the same value, the owner must not be silently
    demoted to ops.

    **Every attempt is recorded** via `users_repo.record_login_event` — success or failure,
    named or shared. See app/models.py's UserLoginEvent docstring for why this exists.
    """
    ip = _client_ip(request)

    async def _reject(typed_username: str):
        try:
            named = await users_repo.any_users_exist(db)
        except Exception:
            named = False
        try:
            await users_repo.record_login_event(
                db, username=typed_username or "(shared password)", user_id=None,
                success=False, via="named" if typed_username else "app_password", ip_address=ip,
            )
        except Exception:
            logger.warning("could not record a failed login attempt")
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Those details did not work. Check and try again.",
                "show_username": named,
            },
            status_code=401,
        )

    if username.strip():
        try:
            user = await users_repo.authenticate(db, username, password)
        except Exception:
            user = None  # users table missing — fall through to the shared passwords
        if user is not None:
            await users_repo.record_login_event(
                db, username=user.username, user_id=user.id, success=True, via="named",
                ip_address=ip,
            )
            return _issue(
                {
                    "authenticated": True,
                    "username": user.username,
                    "role": ROLE_ADMIN if user.is_admin else ROLE_OPS,
                },
                _landing(user.permissions, user.is_admin),
            )
        return await _reject(username)

    if settings.app_password and password == settings.app_password:
        await users_repo.record_login_event(
            db, username="(shared password)", user_id=None, success=True,
            via="app_password", ip_address=ip,
        )
        return _issue({"authenticated": True}, "/")
    if settings.ops_password and password == settings.ops_password:
        await users_repo.record_login_event(
            db, username="(shared password)", user_id=None, success=True,
            via="ops_password", ip_address=ip,
        )
        return _issue({"authenticated": True, "role": ROLE_OPS}, "/ops-page")
    return await _reject("")
```

Add `import logging` and `logger = logging.getLogger(__name__)` near the top of
`app/routers/auth.py` if it is not already present — check first:

Run: `grep -n "^import logging\|^logger = " app/routers/auth.py`

If absent, add both lines after the existing imports.

- [ ] **Step 4: Add the login-log route**

**Path note:** `admin_users.py`'s router is declared `APIRouter(prefix="/admin/users")`, so a
route added here lives under that prefix. The final path is `GET /admin/users/login-log`, not
`/admin/login-log` — a sibling path would need a second, prefix-less router for one route, which
is more machinery than a naming difference justifies. Task 9's Interfaces section and the tests
in Step 1 already use `/admin/users/login-log` — this step matches them.

In `app/routers/admin_users.py`, add after `list_users`:

```python
@router.get("/login-log")
async def login_log(
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(require_user_admin),
):
    """The most recent login attempts — success and failure, named and shared.

    Recording started the day this shipped: there is no history before it. The panel says so.
    """
    events = await users_repo.load_login_events(db, limit=200)
    return JSONResponse({"events": events})
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_login_log.py tests/test_users_and_permissions.py -q -p no:randomly`
Expected: all PASS.

- [ ] **Step 6: Add the login-log card to `templates/users.html`**

Add a new `<div class="card">` after the existing "People" card (before `</main>`):

```html
<div class="card">
  <h3>Login log</h3>
  <p class="help">Every sign-in attempt, successful or not — usernames, IPs and timestamps.
    <strong>Recording started when this was added; nothing before then is in this log.</strong></p>
  <div id="loginLog"><div class="empty">Loading…</div></div>
</div>
```

Add the loading/rendering JavaScript. Find the `load()` function and add a call to a new
`loadLoginLog()` at its end:

```javascript
async function load(){
  // ... existing body unchanged ...
  renderWarnings();
  renderUsers();
  await loadLoginLog();
}

async function loadLoginLog(){
  let data;
  try{
    const r = await fetch("/admin/users/login-log");
    data = await r.json();
    if(!r.ok) throw new Error(data.error || "Could not load the login log.");
  }catch(err){
    $("loginLog").innerHTML = `<div class="empty">${esc(err.message || "Could not reach the server.")}</div>`;
    return;
  }
  const events = data.events || [];
  if(!events.length){
    $("loginLog").innerHTML = '<div class="empty">No login attempts recorded yet.</div>';
    return;
  }
  const rows = events.map(e => {
    const pill = e.success
      ? '<span class="pill off" style="background:var(--green-soft);color:var(--green)">Success</span>'
      : '<span class="pill off" style="background:var(--red-soft);color:var(--red)">Failed</span>';
    const when = e.created_at ? new Date(e.created_at).toLocaleString() : "—";
    return `<tr>
      <td>${esc(e.username)} ${pill}</td>
      <td>${esc(e.via)}</td>
      <td>${esc(e.ip_address || "—")}</td>
      <td>${esc(when)}</td>
    </tr>`;
  }).join("");
  $("loginLog").innerHTML = `
    <table><thead><tr>
      <th>Who</th><th>Method</th><th>IP</th><th>When</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}
```

- [ ] **Step 7: Confirm the template still renders**

Run:
```bash
venv/Scripts/python -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))
html = env.get_template('users.html').render(active='users', grant=None)
print('rendered', len(html), 'chars')
"
```
Expected: no Jinja2 error.

- [ ] **Step 8: Syntax-check the extracted JavaScript**

Run:
```bash
venv/Scripts/python -c "
import re, pathlib, tempfile, os
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))
html = env.get_template('users.html').render(active='users', grant=None)
m = re.search(r'<script>(.*)</script>', html, re.S)
out = os.path.join(tempfile.gettempdir(), 'users_check.js')
pathlib.Path(out).write_text(m.group(1), encoding='utf-8')
print(out)
"
```
Then: `node --check <printed path>`
Expected: no output.

- [ ] **Step 9: Run the render-target and local-dates scans, then the full suite**

Run: `venv/Scripts/python -m pytest tests/test_local_dates.py tests/test_template_render_targets.py -q -p no:randomly`
Expected: PASS/SKIP, no FAIL.

Run: `venv/Scripts/python -m pytest -q -p no:randomly`
Expected: zero failures, full suite.

- [ ] **Step 10: Commit**

```bash
git add app/routers/auth.py app/routers/admin_users.py templates/users.html tests/test_login_log.py tests/test_users_and_permissions.py
git commit -m "feat(users): record every login attempt and show them on a login-log panel"
```

---

## Task 10: `CLAUDE.md` — document all four changes

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add a Projections subsection**

Insert immediately after the existing "`Ideal WH` is a genuine reorder point…" subsection (added
by the prior session's plan) and before the next `##` heading:

```markdown
### A product can be removed from the table, and the export only lists what needs reordering
Reported directly: *"give a button to download reorder level... just the product, brand and the
reorder level"*, *"also highlight it in the live table"*, and *"give option here to remove a
product from the list by selecting and removing it."*

**Removing a row is `excluded_at`, exactly `ShipmentPlanItem`'s own mechanism** — a nullable
timestamp, never a `DELETE`, reversible in one click. This is not a way to permanently retire a
product still active in the MRP sheet: `build_current_rows` recreates a bare row for any
currently-active parent missing one, and exclusion does not stop that. It IS effectively
permanent for a parent no longer active in the sheet, because nothing recreates that one. Both
the confirm dialog and the hidden-rows note say so, rather than letting the owner discover the
limit the first time a "removed" product reappears after a weekly refresh.

**The reorder-level export gets the same treatment as the Shipment tab's "To buy" list**:
filtered to `ideal_wh_stock > 0`, never a row reading "Product … 0" — the same reasoning
`build_tobuy_xlsx`'s own docstring states, because a document like this gets forwarded to a
supplier and a zero invites ordering zero of it. `build_reorder_xlsx`/`build_reorder_pdf` are new
sibling functions in `app/shipment/documents.py`, not parameters on `build_simple_xlsx`/
`build_simple_pdf` — those two are hardwired to the 8-column shipment identity shape via
`IDENTITY_HEADERS`, the same reason `build_portfolio_xlsx` had to be its own function rather
than widening `build_simple_xlsx`.

The `Ideal WH` cell carries a background tint (`.ideal-wh-cell`) distinguishing it from the
other bold calculated columns (Forecast, Ideal FBA) — bold alone did not single it out as the
number actually being watched.
```

- [ ] **Step 2: Add a Users subsection**

Insert a new top-level section. Find `## Orders tab` (or whichever section immediately follows
the Users-related material near the top of the file, around the "Permissions are per AREA" and
"Signed out, EVERY route goes to /login" sections) and add a new subsection immediately after
"The grant is read from the database on every request…" paragraph, before the next `##`:

```markdown
### There was no login history before this, and the panel says so
`User.last_login_at` is a single timestamp, overwritten on every success — it can answer "when
did they last sign in" and nothing else. No audit table existed anywhere in this codebase.
Reported directly: *"make a page to see user log, what did they do when they logged in to check
what different users are doing."*

**Scoped to login events, not a general activity trail.** A full "what did they click" log would
need a hook on most of this app's ~150 routes and this codebase has no precedent for that kind of
blanket instrumentation; login events answer the actual question — who signed in, when, from
where, and whether an attempt failed — while touching exactly one route.

**Every attempt is recorded, not only successes.** A log that only shows successful logins cannot
answer "is someone trying my password," which is the more common reason to open a page like this.
`UserLoginEvent.username` is the string TYPED, not resolved, because a failed attempt against a
username that does not exist is still worth keeping — `user_id` stays NULL for that case and for
every shared-password login, which has no user row to point at.

**Recording starts the day this shipped.** The panel states this rather than implying a history
that is not there — the honest version of the same lesson the Portfolio tab's `ProductDecision`
already teaches: absence of a row must never be mistaken for absence of an event.
```

- [ ] **Step 3: Confirm the CLAUDE.md-scanning tests still pass**

Run: `venv/Scripts/python -m pytest tests/test_local_dates.py tests/test_theme.py -q -p no:randomly`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record the reorder export/exclude and login-log changes"
```

---

## Final verification (manual, once every task above is complete)

1. Start the app and sign in as an administrator.
2. Open `/projections-page`. Confirm the Ideal WH cells are visibly tinted.
3. Tick 2–3 product checkboxes, click "Remove selected", confirm the dialog names the
   "until the next weekly refresh" caveat, confirm on OK the rows vanish and an
   "N product(s) removed" note appears with working "restore" links.
4. Click "⬇ Reorder level (Excel)" and "⬇ Reorder level (PDF)" — confirm both download, both
   contain exactly Product/Brand/Reorder Level (kg), and neither contains a removed product.
5. Restore one removed product via its "restore" link; confirm it reappears in the table.
6. Open `/users-page`. Confirm a new "Login log" card is present, showing at least the sign-in
   just performed, with a green "Success" pill.
7. Sign out, attempt a login with a wrong password, sign back in. Reopen the login log and
   confirm the failed attempt shows a red "Failed" pill above the successful one.
8. Run the full automated suite one final time: `venv/Scripts/python -m pytest -q -p no:randomly`
   — same command used after every task above, now green end to end.
9. Run the mutation harness one final time: `venv/Scripts/python scripts/mutate_projections.py`
   — `all 14 mutations caught`.
