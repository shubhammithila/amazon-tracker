# Portfolio Column Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner hide and reorder the Portfolio table's columns from a `Columns ▾` panel, saved per login.

**Architecture:** The server owns the column vocabulary and a pure `normalise_column_layout` that cleans any saved layout. The layout is stored in a new `users.preferences_json` column (namespaced key `portfolio_columns`), served on `GET /portfolio` and written by `PUT /portfolio/column-prefs`. In the template, every column becomes a definition with its own cell renderers, and the header, product rows, size rows and totals row all map over ONE `visibleColumns()` list — replacing today's three hand-written builders and the `showExtra` toggle.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, SQLite; vanilla JS in `templates/portfolio.html`; pytest; Node (only to execute the template's render functions in tests).

**Spec:** `docs/superpowers/specs/2026-09-30-portfolio-column-picker-design.md`

## Global Constraints

- Column ids, verbatim: `product, verdict, sales, ad_spend, tacos, acos, net_pct, units, weight_kg, returns_pct, rating, decision`.
- Protected (never hideable): `product, sales, ad_spend, units, weight_kg`. `product` is always first and never stored in `order`.
- Default layout: the order above, `hidden = ["returns_pct"]` — today's screen.
- Storage key inside `users.preferences_json`: `"portfolio_columns"`, value `{"order": [...], "hidden": [...]}`. NULL column = never chosen.
- The account written is ALWAYS the session's (`get_current_username`), never anything in the request body.
- Shared-password sessions (no username): `column_scope = "browser"`, layout in `localStorage` key `pf.columnLayout`; a PUT from them returns 409.
- The Excel export is unchanged — every column, standard order.
- Hiding the column the table is sorted by resets sort to `{key: "sales", dir: -1}`.
- Every `th`/`td` in the Portfolio table carries `data-col="<id>"`.
- Run tests with `venv/Scripts/python -m pytest` from the worktree root.
- Every migration adds a branch to `deploy/update-ec2.sh`'s baseline detector, newest first.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `alembic/versions/c3d8e1f5a702_user_preferences.py` (create) | add `users.preferences_json` |
| `app/models.py` (modify) | `User.preferences_json` |
| `deploy/update-ec2.sh` (modify) | detector branch |
| `app/portfolio/columns.py` (create) | the vocabulary + `normalise_column_layout` — pure, no I/O |
| `app/users.py` (modify) | `load_preference` / `save_preference` — merge into the JSON |
| `app/routers/portfolio.py` (modify) | ship vocabulary + layout + scope; `PUT /portfolio/column-prefs` |
| `templates/portfolio.html` (modify) | column definitions, `visibleColumns()`, `normaliseLayout`, the panel |
| `tests/test_portfolio_columns.py` (create) | server: normaliser, routes, export, migration |
| `tests/js_harness.py` (create) | extract template functions and run them under Node |
| `tests/test_portfolio_columns_render.py` (create) | executed alignment + client/server normaliser parity |
| `tests/test_portfolio_groups.py`, `tests/test_portfolio_screen.py`, `tests/test_portfolio_ui_fixes.py`, `tests/test_portfolio_api.py` (modify) | retire `showExtra`-shaped assertions |
| `scripts/mutate_portfolio_columns.py` (create) | mutation harness |
| `scripts/mutate_portfolio_active_weight.py`, `scripts/mutate_portfolio_groups.py`, `scripts/mutate_portfolio_ui.py` (modify if a target moved) | re-point find-strings |
| `CLAUDE.md` (modify) | record it |

`columns.py` is its own module rather than more of `logic.py` (1,500+ lines): it has one job, no I/O, and the router, the users repo and the tests all import it.

---

### Task 1: The storage column and the detector branch

**Files:**
- Create: `alembic/versions/c3d8e1f5a702_user_preferences.py`
- Modify: `app/models.py` (class `User`, after `must_change_password`)
- Modify: `deploy/update-ec2.sh` (the `BASELINE` detector, top of the `elif` chain)
- Test: `tests/test_portfolio_columns.py`

**Interfaces:**
- Produces: `User.preferences_json: Text | None`; Alembic head `c3d8e1f5a702`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_portfolio_columns.py`:

```python
"""The Portfolio column picker: hide and reorder columns, saved per login.

Asked for as "make every column except the Sales, ad spend, units and weights… hiddenable", then
"also make the columns slidable — which one first, which later, or last". See
docs/superpowers/specs/2026-09-30-portfolio-column-picker-design.md.
"""
import pytest

pytestmark = pytest.mark.regression


def test_users_has_a_nullable_preferences_column():
    """NULL means "never chosen", deliberately distinct from "chose the default": it is what lets a
    later change of default reach everyone who never customised."""
    from app.models import User

    column = User.__table__.c.preferences_json
    assert column.nullable is True
    assert column.server_default is None
```

- [ ] **Step 2: Run it to see it fail**

Run: `venv/Scripts/python -m pytest tests/test_portfolio_columns.py -q -p no:randomly`
Expected: FAIL — `AttributeError ... preferences_json`

- [ ] **Step 3: Add the model column**

In `app/models.py`, inside `class User`, directly after the `must_change_password = Column(...)` line:

```python
    #: Personal display choices, as JSON text, namespaced by screen — today only
    #: `{"portfolio_columns": {"order": [...], "hidden": [...]}}`. **NULL means "never chosen"**,
    #: which is a different fact from "chose the default": it is what lets a later change of
    #: default reach everyone who never customised. Written by `app.users.save_preference`, which
    #: MERGES so one screen's choice can never overwrite another's. Never a permission.
    preferences_json = Column(Text, nullable=True)
```

(`Text` is already imported in `app/models.py`.)

- [ ] **Step 4: Write the migration**

Create `alembic/versions/c3d8e1f5a702_user_preferences.py`:

```python
"""users: personal display preferences (Portfolio column layout)

Asked for as "make every column … hiddenable" and "make the columns slidable", saved "with your
login" so the layout follows the owner across devices. One nullable JSON text column, namespaced by
screen, so another tab can store its own choice later with no further migration.

NULL, not `server_default='{}'`: "never chosen" and "chose the default" are different facts, and
only NULL lets a later default change reach users who never customised — the
`over_pack_approved_units` precedent rather than the `from_stock` one.

Revision ID: c3d8e1f5a702
Revises: b91d4a7c3e26
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa

revision = "c3d8e1f5a702"
down_revision = "b91d4a7c3e26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table: SQLite cannot ALTER in place; a no-op on Postgres.
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("preferences_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("preferences_json")
```

- [ ] **Step 5: Add the detector branch**

In `deploy/update-ec2.sh`, replace:

```
elif "sb_spend" in cols("economics_daily"):
    print("b91d4a7c3e26")                           # head: SB spend attributed per ASIN per day
```

with:

```
elif "preferences_json" in cols("users"):
    print("c3d8e1f5a702")                           # head: per-login display preferences
elif "sb_spend" in cols("economics_daily"):
    print("b91d4a7c3e26")                           # SB spend attributed per ASIN per day
```

- [ ] **Step 6: Run the tests**

Run: `venv/Scripts/python -m pytest tests/test_portfolio_columns.py tests/test_schema_migrations.py -q -p no:randomly`
Expected: all PASS (the schema-migration test runs the detector against a fresh head DB and would fail if Step 5 were missing).

Then migrate the local DB: `venv/Scripts/python -m alembic upgrade head` → `Running upgrade b91d4a7c3e26 -> c3d8e1f5a702`.

- [ ] **Step 7: Commit**

```bash
git add app/models.py alembic/versions/c3d8e1f5a702_user_preferences.py deploy/update-ec2.sh tests/test_portfolio_columns.py
git commit -m "feat(users): a per-login preferences column, for the Portfolio column layout

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: The column vocabulary and the normaliser

**Files:**
- Create: `app/portfolio/columns.py`
- Test: `tests/test_portfolio_columns.py` (append)

**Interfaces:**
- Produces:
  - `COLUMNS: list[dict]` — each `{"id": str, "label": str, "locked": bool}`, in default order, `product` first.
  - `DEFAULT_LAYOUT: dict` — `{"order": [...every id except product...], "hidden": ["returns_pct"]}`.
  - `PREFERENCE_KEY = "portfolio_columns"`.
  - `normalise_column_layout(saved: object) -> dict` — always returns `{"order": list[str], "hidden": list[str]}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_portfolio_columns.py`:

```python
from app.portfolio import columns as C

ALL = ["verdict", "sales", "ad_spend", "tacos", "acos", "net_pct",
       "units", "weight_kg", "returns_pct", "rating", "decision"]


def test_the_vocabulary_is_exactly_the_agreed_twelve_columns():
    assert [c["id"] for c in C.COLUMNS] == ["product"] + ALL
    locked = {c["id"] for c in C.COLUMNS if c["locked"]}
    assert locked == {"product", "sales", "ad_spend", "units", "weight_kg"}


def test_the_default_is_todays_screen():
    assert C.DEFAULT_LAYOUT == {"order": ALL, "hidden": ["returns_pct"]}


@pytest.mark.parametrize("junk", [None, "", "[]", 42, [], {"order": "x"}, {"order": [1, 2]}])
def test_anything_malformed_becomes_the_default(junk):
    assert C.normalise_column_layout(junk) == C.DEFAULT_LAYOUT


def test_a_valid_layout_survives_untouched():
    layout = {"order": list(reversed(ALL)), "hidden": ["acos", "rating"]}
    assert C.normalise_column_layout(layout) == layout


def test_unknown_ids_and_duplicates_are_dropped():
    out = C.normalise_column_layout(
        {"order": ["gone", "sales", "sales"] + ALL, "hidden": ["gone", "acos", "acos"]}
    )
    assert out["order"] == ALL
    assert out["hidden"] == ["acos"]


def test_product_is_never_stored_in_the_order():
    out = C.normalise_column_layout({"order": ["product"] + ALL, "hidden": []})
    assert "product" not in out["order"]


def test_a_protected_column_cannot_be_hidden():
    out = C.normalise_column_layout(
        {"order": ALL, "hidden": ["sales", "ad_spend", "units", "weight_kg", "product", "acos"]}
    )
    assert out["hidden"] == ["acos"]


def test_a_NEW_column_appears_at_its_default_position_for_an_existing_user():
    """**The rule most likely to be got wrong.** A column added next month is absent from every
    saved layout; dropping it would make it silently never show for anyone who customised. It goes
    after the nearest default-order predecessor the user still has."""
    saved = [c for c in reversed(ALL) if c != "tacos"]      # user reversed everything; no tacos
    out = C.normalise_column_layout({"order": saved, "hidden": []})
    # In the default order tacos follows ad_spend, so it lands right after ad_spend here.
    assert out["order"].index("tacos") == out["order"].index("ad_spend") + 1
    assert sorted(out["order"]) == sorted(ALL)


def test_a_missing_FIRST_column_goes_to_the_front():
    saved = [c for c in ALL if c != "verdict"]
    out = C.normalise_column_layout({"order": list(reversed(saved)), "hidden": []})
    assert out["order"][0] == "verdict"


def test_the_normaliser_is_idempotent():
    layout = {"order": ["rating", "gone", "sales"], "hidden": ["sales", "decision"]}
    once = C.normalise_column_layout(layout)
    assert C.normalise_column_layout(once) == once
```

- [ ] **Step 2: Run to see them fail**

Run: `venv/Scripts/python -m pytest tests/test_portfolio_columns.py -q -p no:randomly`
Expected: FAIL — `ModuleNotFoundError: app.portfolio.columns`

- [ ] **Step 3: Implement `app/portfolio/columns.py`**

```python
"""The Portfolio table's columns: which exist, which may be hidden, and the default layout.

The server owns this list and ships it with `GET /portfolio`, the way `group_order` and
`verdict_groups` travel, so the template holds no second copy of which columns are protected — a
copy is a second thing to keep in step, and the server is what refuses an invalid save.

`normalise_column_layout` runs on READ and on WRITE, so a value already stored, or edited by hand,
can never break the table: the `good_rating: 99` lesson from the verdict thresholds.
"""
from __future__ import annotations

#: The key under `users.preferences_json`. Namespaced so another screen can store its own choice
#: later without touching this one.
PREFERENCE_KEY = "portfolio_columns"

#: In DEFAULT order. `locked` = cannot be hidden. `product` is additionally pinned first: it is the
#: row's name and its expand control, so a row without it is not a row.
COLUMNS: list[dict] = [
    {"id": "product",     "label": "Product",  "locked": True},
    {"id": "verdict",     "label": "Verdict",  "locked": False},
    {"id": "sales",       "label": "Sales",    "locked": True},
    {"id": "ad_spend",    "label": "Ad spend", "locked": True},
    {"id": "tacos",       "label": "TACOS",    "locked": False},
    {"id": "acos",        "label": "ACOS",     "locked": False},
    {"id": "net_pct",     "label": "Net %",    "locked": False},
    {"id": "units",       "label": "Units",    "locked": True},
    {"id": "weight_kg",   "label": "Weight",   "locked": True},
    {"id": "returns_pct", "label": "Returns",  "locked": False},
    {"id": "rating",      "label": "Rating",   "locked": False},
    {"id": "decision",    "label": "Decision", "locked": False},
]

PINNED_FIRST = "product"
_MOVABLE = [c["id"] for c in COLUMNS if c["id"] != PINNED_FIRST]
_LOCKED = {c["id"] for c in COLUMNS if c["locked"]}

#: Today's screen: everything in default order, Returns hidden.
DEFAULT_LAYOUT: dict = {"order": list(_MOVABLE), "hidden": ["returns_pct"]}


def _default() -> dict:
    return {"order": list(DEFAULT_LAYOUT["order"]), "hidden": list(DEFAULT_LAYOUT["hidden"])}


def normalise_column_layout(saved: object) -> dict:
    """Any saved value -> a valid ``{"order": [...], "hidden": [...]}``. Never raises.

    1. malformed -> the default;
    2. unknown ids dropped (a renamed or removed column);
    3. duplicates dropped, first occurrence kept;
    4. **a known column missing from `order` is inserted after its nearest default-order
       predecessor that IS present** (or at the front if none is), so a column added later APPEARS
       for a user who customised, instead of silently never showing;
    5. `product` removed from `order` — it is pinned first and never stored;
    6. protected ids removed from `hidden`.
    """
    if not isinstance(saved, dict):
        return _default()
    order = saved.get("order")
    hidden = saved.get("hidden", [])
    if not isinstance(order, list) or not isinstance(hidden, list):
        return _default()
    if not all(isinstance(x, str) for x in order) or not all(isinstance(x, str) for x in hidden):
        return _default()

    known = set(_MOVABLE)
    clean: list[str] = []
    for col in order:
        if col in known and col not in clean:
            clean.append(col)

    for index, col in enumerate(_MOVABLE):
        if col in clean:
            continue
        predecessors = [p for p in _MOVABLE[:index] if p in clean]
        at = clean.index(predecessors[-1]) + 1 if predecessors else 0
        clean.insert(at, col)

    hide: list[str] = []
    for col in hidden:
        if col in known and col not in _LOCKED and col not in hide:
            hide.append(col)

    return {"order": clean, "hidden": hide}
```

- [ ] **Step 4: Run the tests**

Run: `venv/Scripts/python -m pytest tests/test_portfolio_columns.py -q -p no:randomly`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/portfolio/columns.py tests/test_portfolio_columns.py
git commit -m "feat(portfolio): the column vocabulary and a normaliser that never breaks the table

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Read and write the preference; the routes

**Files:**
- Modify: `app/users.py` (append two functions)
- Modify: `app/routers/portfolio.py` (`get_portfolio`, new `put_column_prefs`)
- Test: `tests/test_portfolio_columns.py` (append)

**Interfaces:**
- Consumes: `columns.normalise_column_layout`, `columns.COLUMNS`, `columns.PREFERENCE_KEY`; `auth.get_current_username(request) -> str | None`.
- Produces:
  - `users.load_preference(db, username: str, key: str) -> object | None`
  - `users.save_preference(db, username: str, key: str, value: object) -> bool` (False when no such user)
  - `GET /portfolio` payload gains `columns`, `column_layout`, `column_scope`.
  - `PUT /portfolio/column-prefs` body `{order, hidden}` → `200 {"column_layout": {...}}`; `409` for a shared session; `400` for a non-JSON body.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_portfolio_columns.py`:

```python
import json

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.auth import SESSION_COOKIE, serializer


async def _named_client(db, username="owner"):
    from app import users as users_repo
    await users_repo.create(db, username=username, full_name=username, is_admin=True,
                            created_by="test")
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test", follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, serializer.dumps(
        {"authenticated": True, "username": username, "role": "admin"}))
    return client


async def _stored(db, username):
    from sqlalchemy import select
    from app.models import User
    raw = (await db.execute(select(User.preferences_json).where(User.username == username))).scalar()
    return json.loads(raw) if raw else None


async def test_GET_serves_the_vocabulary_and_the_default_for_a_new_login(db):
    async with await _named_client(db) as client:
        body = (await client.get("/portfolio")).json()
    assert [c["id"] for c in body["columns"]] == ["product"] + ALL
    assert body["column_layout"] == C.DEFAULT_LAYOUT
    assert body["column_scope"] == "account"


async def test_a_saved_layout_comes_back_normalised(db):
    async with await _named_client(db) as client:
        r = await client.put("/portfolio/column-prefs",
                             json={"order": ["rating", "gone"], "hidden": ["sales", "acos"]})
        assert r.status_code == 200
        saved = r.json()["column_layout"]
        assert saved["order"][0] == "rating" and "gone" not in saved["order"]
        assert saved["hidden"] == ["acos"]
        assert (await client.get("/portfolio")).json()["column_layout"] == saved


async def test_PUT_writes_ONLY_the_session_account_whatever_the_body_says(db):
    from app import users as users_repo
    await users_repo.create(db, username="victim", full_name="v", is_admin=False,
                            created_by="test")
    async with await _named_client(db, "owner") as client:
        await client.put("/portfolio/column-prefs",
                         json={"username": "victim", "order": ALL, "hidden": ["acos"]})
    assert await _stored(db, "victim") is None
    assert (await _stored(db, "owner"))[C.PREFERENCE_KEY]["hidden"] == ["acos"]


async def test_saving_MERGES_so_another_screens_preference_survives(db):
    from sqlalchemy import update
    from app.models import User
    async with await _named_client(db) as client:
        await db.execute(update(User).where(User.username == "owner")
                         .values(preferences_json=json.dumps({"orders_tab": {"x": 1}})))
        await db.commit()
        await client.put("/portfolio/column-prefs", json={"order": ALL, "hidden": []})
    stored = await _stored(db, "owner")
    assert stored["orders_tab"] == {"x": 1}
    assert stored[C.PREFERENCE_KEY] == {"order": ALL, "hidden": []}


async def test_a_shared_password_session_is_told_to_use_the_browser(auth_client):
    body = (await auth_client.get("/portfolio")).json()
    assert body["column_scope"] == "browser"
    assert body["column_layout"] == C.DEFAULT_LAYOUT
    r = await auth_client.put("/portfolio/column-prefs", json={"order": ALL, "hidden": []})
    assert r.status_code == 409


async def test_a_non_JSON_body_is_a_400(db):
    async with await _named_client(db) as client:
        r = await client.put("/portfolio/column-prefs", content=b"not json",
                             headers={"content-type": "application/json"})
    assert r.status_code == 400


async def test_a_corrupt_stored_value_still_renders_the_default(db):
    from sqlalchemy import update
    from app.models import User
    async with await _named_client(db) as client:
        await db.execute(update(User).where(User.username == "owner")
                         .values(preferences_json="{not json"))
        await db.commit()
        assert (await client.get("/portfolio")).json()["column_layout"] == C.DEFAULT_LAYOUT


async def test_the_EXCEL_ignores_the_saved_layout(db):
    """The file leaves the app; a sheet missing TACOS because it was hidden on screen weeks ago is
    a misleading document. Header row identical with and without a layout."""
    import io
    from openpyxl import load_workbook

    async with await _named_client(db) as client:
        before = (await client.get("/portfolio/download.xlsx")).content
        await client.put("/portfolio/column-prefs",
                         json={"order": list(reversed(ALL)), "hidden": ["tacos", "acos"]})
        after = (await client.get("/portfolio/download.xlsx")).content

    def header(content):
        rows = list(load_workbook(io.BytesIO(content)).active.iter_rows(values_only=True))
        return next(r for r in rows if r and "Sales" in [str(c) for c in r])
    assert header(before) == header(after)
```

- [ ] **Step 2: Run to see them fail**

Run: `venv/Scripts/python -m pytest tests/test_portfolio_columns.py -q -p no:randomly`
Expected: the new async tests FAIL (`KeyError: 'columns'`, 404/405 on PUT).

- [ ] **Step 3: Add the two repo functions to `app/users.py`**

Append (and ensure `import json`, `from sqlalchemy import select`, `from app.models import User` are present at the top — add any that are missing):

```python
async def load_preference(db, username: str, key: str):
    """One namespaced value from `users.preferences_json`, or None.

    **Never raises on a corrupt value.** A hand-edited or truncated JSON cell returns None, and the
    caller normalises None to its default — a display preference must not be able to 500 a page.
    """
    raw = (await db.execute(
        select(User.preferences_json).where(User.username == username)
    )).scalar()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data.get(key) if isinstance(data, dict) else None


async def save_preference(db, username: str, key: str, value) -> bool:
    """Set ONE namespaced value, MERGED into the existing JSON so another screen's key survives.

    Returns False when no such user exists. `username` must come from the session — this function
    trusts its caller, so the route is what guarantees a person can only write themselves.
    """
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None:
        return False
    try:
        data = json.loads(user.preferences_json) if user.preferences_json else {}
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[key] = value
    user.preferences_json = json.dumps(data)
    await db.commit()
    return True
```

- [ ] **Step 4: Wire the routes in `app/routers/portfolio.py`**

Change the imports:

```python
from app import permissions, users as users_repo
from app.portfolio import columns, economics, logic, refresh, repository
from app.routers.auth import get_current_username, require_area
```

Add this helper above `get_portfolio`:

```python
async def _column_payload(request: Request, db: AsyncSession) -> dict:
    """The vocabulary, this person's normalised layout, and WHERE it is saved.

    `"account"` for a named login, `"browser"` for a shared-password session — which has no user
    row to save against, so the page keeps its layout in localStorage instead.
    """
    username = get_current_username(request)
    saved = (await users_repo.load_preference(db, username, columns.PREFERENCE_KEY)
             if username else None)
    return {
        "columns": columns.COLUMNS,
        "column_layout": columns.normalise_column_layout(saved) if saved is not None
                         else columns.normalise_column_layout(columns.DEFAULT_LAYOUT),
        "column_scope": "account" if username else "browser",
    }
```

In `get_portfolio`, add `**(await _column_payload(request, db)),` inside the returned dict, directly after `**data,`.

Add the new route after `get_portfolio`:

```python
@router.put("/column-prefs")
async def put_column_prefs(
    request: Request,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.PORTFOLIO)),
):
    """Save the caller's column layout. `{"order": [...], "hidden": [...]}`.

    **The account is the SESSION's, never the body's** — a `username` in the body is ignored, so a
    person can only ever write their own layout. Normalised before storing, so what is saved is
    exactly what will be read back.

    A shared-password session has no row to save against: 409 rather than a silent no-op, so a client
    that forgot `column_scope` is a visible bug rather than a layout that quietly never sticks.
    """
    username = get_current_username(request)
    if not username:
        return JSONResponse(
            {"error": "Shared-password sessions keep their column layout in this browser."},
            status_code=409,
        )
    try:
        body = await request.json()
    except Exception:                       # noqa: BLE001 - a malformed body is a 400
        return JSONResponse({"error": "Expected a JSON body."}, status_code=400)

    layout = columns.normalise_column_layout(
        {"order": (body or {}).get("order"), "hidden": (body or {}).get("hidden", [])}
    )
    if not await users_repo.save_preference(db, username, columns.PREFERENCE_KEY, layout):
        return JSONResponse({"error": "Your account was not found."}, status_code=404)
    return JSONResponse({"column_layout": layout})
```

- [ ] **Step 5: Run the tests**

Run: `venv/Scripts/python -m pytest tests/test_portfolio_columns.py tests/test_portfolio_api.py -q -p no:randomly`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add app/users.py app/routers/portfolio.py tests/test_portfolio_columns.py
git commit -m "feat(portfolio): serve and save the column layout, per login

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: A harness that EXECUTES the template's JavaScript

**Files:**
- Create: `tests/js_harness.py`
- Test: `tests/test_portfolio_columns_render.py` (a smoke test only, in this task)

**Interfaces:**
- Produces: `run_portfolio_js(body: str) -> list` — runs the named template code plus `body` under Node and returns the JSON value `body` prints via `emit(...)`. Skips the test when `node` is not installed.

Why this exists: CLAUDE.md records that source-level tests over this template missed real render bugs (the TDZ "Loading…" hang, a phantom banner). Alignment of cells under headings is only provable by running the builders. **The page's own one-line helpers are copied verbatim, never stubbed** — a stub that disagrees with the code invents defects (the `n()` formatter lesson).

- [ ] **Step 1: Write the harness**

Create `tests/js_harness.py`:

```python
"""Run pieces of templates/portfolio.html under Node, so render output is TESTED rather than grepped.

Extraction is by NAME: top-level `function NAME(` blocks and `const NAME = ` statements are copied
verbatim from the template. Only the DOM and `data` are faked. A stub for one of the page's own
helpers is exactly how a probe once reported a phantom banner — so helpers are copied, never written.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).parent.parent / "templates" / "portfolio.html"

#: Everything the column builders read, copied verbatim from the template.
FUNCTIONS = [
    "stars", "money", "pct", "kg", "acosCell", "verdictClass", "groupFlag", "sizeName",
    "normaliseLayout", "visibleColumns", "headerHtml", "dataCells", "detailCells",
    "computeTotals", "totalsRow", "sizeRowHtml", "tableMinWidth",
]
CONSTS = ["n", "esc", "ico", "COLUMN_DEFS"]


def _script() -> str:
    source = TEMPLATE.read_text(encoding="utf-8")
    return source[source.rindex("<script>") + len("<script>"): source.rindex("</script>")]


def _function(script: str, name: str) -> str:
    start = script.index(f"function {name}(")
    rest = script[start:]
    end = rest.find("\nfunction ", 1)
    block = rest if end == -1 else rest[:end]
    # stop at the first top-level statement that is not part of the function
    for marker in ("\nconst ", "\nlet ", "\n$(", "\ndocument.", "\nload()"):
        cut = block.find(marker)
        if cut != -1:
            block = block[:cut]
    return block


def _const(script: str, name: str) -> str:
    """`const NAME = …;` up to the first `;` at bracket depth 0 and OUTSIDE any string.

    String-aware on purpose: `ico` is a multi-line arrow returning a template literal, and a `;`
    inside a string or `${…}` must not end the statement. Template `${` raises depth like `{`.
    Every const this harness extracts ends with `;` in the template — keep it that way.
    """
    start = script.index(f"const {name} = ")
    depth, i, quote = 0, start, None
    while i < len(script):
        ch = script[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if quote == "`" and script.startswith("${", i):
                depth += 1
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:                       # closing a `${` back into its template string
                depth, quote = 0, "`"
        elif ch == ";" and depth == 0:
            return script[start:i + 1]
        i += 1
    raise AssertionError(f"could not extract const {name}")


def run_portfolio_js(body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the render tests need it")
    script = _script()
    parts = [
        "let data = {}; let sort = {key: 'sales', dir: -1}; let layout = null;",
        "const $ = () => null;",
        "function emit(v){ console.log(JSON.stringify(v)); }",
    ]
    parts += [_const(script, name) for name in CONSTS]
    parts += [_function(script, name) for name in FUNCTIONS]
    parts.append(body)
    path = os.path.join(tempfile.gettempdir(), "pf_render_test.js")
    Path(path).write_text("\n".join(parts), encoding="utf-8")
    result = subprocess.run([node, path], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])
```

Note: check each name against the template before running — a helper declared as
`const money = …` rather than `function money(` belongs in `CONSTS`, not `FUNCTIONS`
(`grep -n "function money(\|const money = " templates/portfolio.html`). Move names between the two
lists to match; never write a stand-in.

Note: `FUNCTIONS` and `CONSTS` name code that Task 5 creates (`normaliseLayout`, `visibleColumns`, `computeTotals`, `tableMinWidth`, `COLUMN_DEFS`). The smoke test below therefore fails until Task 5 — that is intended; run it at the end of Task 5.

- [ ] **Step 2: Write the smoke test**

Create `tests/test_portfolio_columns_render.py`:

```python
"""The Portfolio table's column alignment, proven by EXECUTING the page's render functions.

A table where TACOS and ACOS have swapped cells has the right COUNT of columns and the wrong figure
under each heading — so every cell carries `data-col`, and these tests compare ids, in order.
"""
import pytest

from tests.js_harness import run_portfolio_js

pytestmark = pytest.mark.regression


def test_the_harness_can_run_the_page_code():
    assert run_portfolio_js("emit(normaliseLayout(null, data.columns).order.length)") == 11
```

(`data.columns` is set by each test's body; the smoke test relies on `normaliseLayout` falling back to its built-in default when `data.columns` is undefined — Task 5 implements that.)

- [ ] **Step 3: Commit** (tests will be run green at the end of Task 5)

```bash
git add tests/js_harness.py tests/test_portfolio_columns_render.py
git commit -m "test: a harness that executes the Portfolio template's render functions under Node

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: One column list drives every row

**Files:**
- Modify: `templates/portfolio.html` — `COLUMNS` (≈line 1198), `shownColumns`/`isShown` (≈1224–1232), `headerHtml`, `detailCells`, `dataCells`, `totalsRow`, `sizeRowHtml`, flavour-row block in `renderTable`, the two `colspan="${shownColumns().length}"` rows, the `<table>` tag, `table{…min-width:1120px}` CSS, `let showExtra` (≈447)
- Test: `tests/test_portfolio_columns_render.py` (append)

**Interfaces:**
- Consumes: `data.columns`, `data.column_layout`, `data.column_scope` (Task 3).
- Produces (all top-level in the template's `<script>`):
  - `COLUMN_DEFS: {[id]: {num?, sortKey?, tip?, width, row(r), detail(r), total(t)}}` — each renderer returns the cell's INNER html.
  - `normaliseLayout(saved, vocab) -> {order, hidden}` — the client twin of the server normaliser, used for browser-scope and optimistic re-render.
  - `visibleColumns() -> [{id, label, ...COLUMN_DEFS[id]}]` — `product` first, then `layout.order` minus `layout.hidden`.
  - `cell(col, inner, extraClass?) -> "<td data-col=…>"`.
  - `computeTotals(list) -> t` and `totalsRow(list, isSkus)`.
  - `tableMinWidth() -> number` (px).
  - global `let layout` — the current `{order, hidden}`.

- [ ] **Step 1: Write the failing render tests**

Append to `tests/test_portfolio_columns_render.py`:

```python
VOCAB = """
data.columns = [
  {id:"product",label:"Product",locked:true},{id:"verdict",label:"Verdict",locked:false},
  {id:"sales",label:"Sales",locked:true},{id:"ad_spend",label:"Ad spend",locked:true},
  {id:"tacos",label:"TACOS",locked:false},{id:"acos",label:"ACOS",locked:false},
  {id:"net_pct",label:"Net %",locked:false},{id:"units",label:"Units",locked:true},
  {id:"weight_kg",label:"Weight",locked:true},{id:"returns_pct",label:"Returns",locked:false},
  {id:"rating",label:"Rating",locked:false},{id:"decision",label:"Decision",locked:false}];
data.group_flags = {};
const ROW = {product:"Chana Sattu", verdict:"SCALE", sales:1000, ad_spend:200, tacos:0.2,
  acos:0.5, net:300, net_pct:0.3, units:10, weight_kg:5, returns_pct:0.01, rating:4.2,
  rating_count:40, decision:"keep", asin:"B0X", parent_asin:"B0P", units_ordered:10,
  units_refunded:0, ads_cost:200, ad_attributed_sales:400, size:"1 kg"};
const ids = html => [...html.matchAll(/<t[hd][^>]*data-col="([^"]+)"/g)].map(m => m[1]);
const span = html => [...html.matchAll(/<td([^>]*)>/g)]
  .reduce((a, m) => a + (+((/colspan="(\\d+)"/.exec(m[1]) || [0, 1])[1])), 0);
"""

LAYOUTS = {
    "default": "null",
    "shuffled_three_hidden":
        '{order:["decision","net_pct","sales","rating","verdict","units","acos","weight_kg",'
        '"ad_spend","returns_pct","tacos"], hidden:["acos","verdict","returns_pct"]}',
    "all_shown": '{order:[], hidden:[]}',
    "stale_id": '{order:["gone","rating","sales"], hidden:["gone"]}',
}


@pytest.mark.parametrize("name", LAYOUTS)
def test_header_product_size_flavour_and_totals_rows_list_the_SAME_columns_in_the_SAME_order(name):
    out = run_portfolio_js(VOCAB + f"""
layout = normaliseLayout({LAYOUTS[name]}, data.columns);
const head = ids(headerHtml());
emit({{head,
  product: ids(`<td data-col="product"></td>` + dataCells(ROW)),
  size: ids(sizeRowHtml(ROW, false)),
  flavour: ids(`<td data-col="product"></td>` + detailCells(ROW)),
  totals: ids(totalsRow([ROW], false)),
  sizeSpan: span(sizeRowHtml(ROW, false)), headCount: head.length}});
""")
    assert out["head"][0] == "product"
    for row in ("product", "size", "flavour", "totals"):
        assert out[row] == out["head"], f"{name}: the {row} row is out of line with the header"
    assert out["sizeSpan"] == out["headCount"]


def test_hidden_columns_are_absent_and_the_locked_ones_cannot_be_hidden():
    out = run_portfolio_js(VOCAB + """
layout = normaliseLayout({order:[], hidden:["sales","units","acos","decision"]}, data.columns);
emit(ids(headerHtml()));
""")
    assert "acos" not in out and "decision" not in out
    assert {"sales", "units", "ad_spend", "weight_kg"} <= set(out)


def test_the_size_rows_rating_cell_no_longer_spans_into_decision():
    """It spanned Rating AND Decision; once those can be separated, a span would put a cell under
    the wrong heading."""
    out = run_portfolio_js(VOCAB + """
layout = normaliseLayout({order:["rating","sales","decision"], hidden:[]}, data.columns);
emit(sizeRowHtml(ROW, false));
""")
    assert "colspan" not in out


def test_min_width_follows_the_visible_columns():
    out = run_portfolio_js(VOCAB + """
layout = normaliseLayout({order:[], hidden:[]}, data.columns); const all = tableMinWidth();
layout = normaliseLayout({order:[], hidden:["verdict","tacos","acos","net_pct","rating"]},
                         data.columns);
emit([all, tableMinWidth()]);
""")
    assert out[0] > out[1] > 0


@pytest.mark.parametrize("saved", [
    None, {"order": ["gone", "rating", "rating"], "hidden": ["sales", "acos"]},
    {"order": ["decision"], "hidden": []}, {"order": "x"},
])
def test_the_client_normaliser_agrees_with_the_server(saved):
    """Two normalisers exist — the server's, and the client's twin for browser-scope and optimistic
    re-render. Two copies of a rule drift; this pins them to the same answers."""
    import json

    from app.portfolio import columns as C

    client = run_portfolio_js(VOCAB + f"emit(normaliseLayout({json.dumps(saved)}, data.columns));")
    assert client == C.normalise_column_layout(saved)
```

- [ ] **Step 2: Run to see them fail**

Run: `venv/Scripts/python -m pytest tests/test_portfolio_columns_render.py -q -p no:randomly`
Expected: FAIL — `could not extract` / `normaliseLayout is not defined`.

- [ ] **Step 3: Replace `COLUMNS`, `shownColumns`, `isShown` with the definitions**

Replace the whole `const COLUMNS = [ … ];` block and the `shownColumns()` and `isShown()` functions with:

```js
/* ── Columns: one definition each, and ONE list every row is built from ──────────────────────
   Asked for as "make every column except the Sales, ad spend, units and weights… hiddenable" and
   "make the columns slidable". Which columns exist and which are protected comes from the SERVER
   (`data.columns`); this holds only how each one RENDERS.

   Each renderer returns a cell's INNER html for one row type: `row` (product and SKU rows),
   `detail` (size and flavour rows), `total` (the tfoot, given `computeTotals`' result). The header,
   `dataCells`, `detailCells` and `totalsRow` all map over `visibleColumns()` — there is no second
   place a column can be shown or hidden, which is what three hand-written builders each checking
   `showExtra` used to be. `width` feeds `tableMinWidth`. */
const COLUMN_DEFS = {
  verdict:     {width: 120,
                row: r => { const flag = groupFlag(r.verdict);
                  return `<span class="tag ${verdictClass(r.verdict)}">${esc(r.verdict)}</span>${
                    flag ? ` <span class="gflag" title="${esc(flag)}">${ico("alert")}</span>` : ""}`; },
                detail: () => "", total: () => ""},
  sales:       {num: true, sortKey: "sales", width: 100,
                row: r => money(r.sales), detail: r => money(r.sales), total: t => money(t.sales)},
  ad_spend:    {num: true, sortKey: "ad_spend", width: 100,
                row: r => money(r.ad_spend), detail: r => money(r.ad_spend),
                total: t => money(t.spend)},
  tacos:       {num: true, sortKey: "tacos", width: 80, tip: "ad spend over TOTAL sales",
                row: r => pct(r.tacos), detail: r => pct(r.tacos),
                total: t => pct(t.ratio(t.spend, t.sales))},
  acos:        {num: true, sortKey: "acos", width: 80,
                tip: "ad spend over ad-ATTRIBUTED sales — shown for context; no verdict is decided on it",
                row: r => acosCell(r), detail: r => acosCell(r),
                total: t => acosCell({acos: t.adsCost ? t.ratio(t.adsCost, t.attributed) : null,
                                      acos_infinite: t.infinite})},
  net_pct:     {num: true, sortKey: "net_pct", width: 80,
                row: r => pct(r.net_pct, {sign: true}), detail: r => pct(r.net_pct, {sign: true}),
                total: t => pct(t.ratio(t.net, t.sales), {sign: true})},
  units:       {num: true, sortKey: "units", width: 80,
                row: r => n(r.units).toLocaleString("en-IN"),
                detail: r => n(r.units).toLocaleString("en-IN"),
                total: t => n(t.units).toLocaleString("en-IN")},
  weight_kg:   {num: true, sortKey: "weight_kg", width: 90,
                tip: "units × pack size, from the MRP sheet — a dash where the sheet has no weight",
                row: r => kg(r.weight_kg), detail: r => kg(r.weight_kg),
                total: t => (t.weighed ? kg(t.weight) : '<span class="dim">—</span>') + (t.weightUnknown
                  ? `<span class="tnote">${n(t.weightUnknown)} row(s) have no pack weight in the
                     sheet and are excluded</span>` : "")},
  returns_pct: {num: true, sortKey: "returns_pct", width: 80,
                row: r => r.returns_pct ? pct(r.returns_pct) : '<span class="dim">—</span>',
                detail: r => r.returns_pct ? pct(r.returns_pct) : '<span class="dim">—</span>',
                total: t => pct(t.ratio(t.refunded, t.ordered))},
  // A size row carries no rating of its own: Amazon pools reviews per family, so a per-size star
  // would be the parent's number repeated, implying a precision the review data does not have. It
  // used to SPAN Rating and Decision; those can now be separated, so it is its own cell.
  rating:      {sortKey: "rating", width: 110,
                row: r => stars(r.rating, r.rating_count),
                detail: () => '<span class="dim" style="font-size:11px" title="Amazon pools reviews across every size of a product">per product</span>',
                total: t => t.rating === null ? '<span class="dim">—</span>'
                  : `${stars(t.rating, t.reviews)}<span class="tnote">weighted by reviews</span>`},
  decision:    {width: 100,
                row: r => r.decision
                  ? `<span class="tag d-${esc(r.decision)}">${esc(r.decision.toUpperCase())}</span>`
                  : '<span class="dim">—</span>',
                detail: () => "",
                total: t => t.decided ? `<span class="dim">${n(t.decided)} decided</span>`
                  : '<span class="dim">—</span>'},
};

//: Used only when `data.columns` has not arrived (and by the render tests' smoke check).
const FALLBACK_ORDER = ["verdict", "sales", "ad_spend", "tacos", "acos", "net_pct", "units",
                        "weight_kg", "returns_pct", "rating", "decision"];

/* The client twin of `app/portfolio/columns.normalise_column_layout`, rule for rule. It exists for
   the browser-scope layout (shared-password sessions) and to re-render instantly before the save
   returns. `test_the_client_normaliser_agrees_with_the_server` pins the two to the same answers. */
function normaliseLayout(saved, vocab){
  const movable = (vocab && vocab.length ? vocab.map(c => c.id) : ["product"].concat(FALLBACK_ORDER))
    .filter(id => id !== "product");
  const locked = new Set((vocab || []).filter(c => c.locked).map(c => c.id)
    .concat(vocab && vocab.length ? [] : ["product", "sales", "ad_spend", "units", "weight_kg"]));
  const def = {order: movable.slice(), hidden: ["returns_pct"]};
  if(!saved || typeof saved !== "object" || Array.isArray(saved)) return def;
  const order = saved.order, hidden = saved.hidden === undefined ? [] : saved.hidden;
  if(!Array.isArray(order) || !Array.isArray(hidden)) return def;
  if(!order.every(x => typeof x === "string") || !hidden.every(x => typeof x === "string")) return def;
  const known = new Set(movable);
  const clean = [];
  order.forEach(id => { if(known.has(id) && !clean.includes(id)) clean.push(id); });
  movable.forEach((id, i) => {
    if(clean.includes(id)) return;
    const pred = movable.slice(0, i).filter(p => clean.includes(p));
    clean.splice(pred.length ? clean.indexOf(pred[pred.length - 1]) + 1 : 0, 0, id);
  });
  const hide = [];
  hidden.forEach(id => { if(known.has(id) && !locked.has(id) && !hide.includes(id)) hide.push(id); });
  return {order: clean, hidden: hide};
}

/* The columns actually rendered, in order: Product pinned first, then the layout minus hidden. */
function visibleColumns(){
  const vocab = data.columns || [];
  const label = id => (vocab.find(c => c.id === id) || {}).label || id;
  const current = layout || normaliseLayout(null, vocab);
  return [{id: "product", label: label("product")}].concat(
    current.order.filter(id => !current.hidden.includes(id) && COLUMN_DEFS[id])
      .map(id => Object.assign({id, label: label(id)}, COLUMN_DEFS[id])));
}

/* One cell, tagged with its column id so alignment can be checked by NAME: swapped TACOS/ACOS cells
   have the right count and the wrong figure under each heading. */
function cell(col, inner, extraClass){
  const cls = [col.num ? "num" : "", extraClass || ""].filter(Boolean).join(" ");
  return `<td data-col="${col.id}"${cls ? ` class="${cls}"` : ""}>${inner}</td>`;
}

/* The table's floor width, from what is SHOWN. A fixed floor padded a narrow table out to twelve
   columns' width; no floor lets the nowrap money cells overflow their gridlines. */
function tableMinWidth(){
  return visibleColumns().reduce((a, c) => a + (c.id === "product" ? 260 : (c.width || 90)), 0);
}
```

- [ ] **Step 4: Rewrite `headerHtml`**

Replace the body of `headerHtml()` so it maps over `visibleColumns()` and tags each heading (the sortable/aria behaviour is kept verbatim):

```js
function headerHtml(){
  return visibleColumns().map(c => {
    const key = c.sortKey;
    if(!key) return `<th scope="col" data-col="${c.id}"${c.num ? ' class="num"' : ""}>${esc(c.label)}</th>`;
    const on = sort.key === key;
    const aria = on ? (sort.dir < 0 ? "descending" : "ascending") : "none";
    const name = c.tip ? `${c.label} — ${c.tip}` : c.label;
    return `<th scope="col" data-col="${c.id}" class="sortable${c.num ? " num" : ""}${on ? " sorted" : ""}"
      data-sort="${key}" tabindex="0" role="button" aria-sort="${aria}"
      aria-label="Sort by ${esc(name)}"${c.tip ? ` title="${esc(c.tip)}"` : ""}>${esc(c.label)}${
      on ? `<span class="arrow">${ico("chevron", sort.dir < 0 ? "ico-r90" : "ico-r270")}</span>` : ""}</th>`;
  }).join("");
}
```

- [ ] **Step 5: Rewrite `detailCells`, `dataCells`, `sizeRowHtml` and the flavour row**

Replace `detailCells` and `dataCells` entirely:

```js
/* The cells AFTER Product on a size or flavour row. Product itself is built by the caller, because
   each row type labels it differently. */
function detailCells(row){
  return visibleColumns().slice(1).map(c => cell(c, c.detail(row))).join("");
}

/* The cells AFTER Product on a product or SKU row. */
function dataCells(r){
  return visibleColumns().slice(1).map(c => cell(c, c.row(r))).join("");
}
```

In `sizeRowHtml`, replace the row's first two cells so Product is tagged and the old empty Verdict `<td></td>` is gone (Verdict is now a column like any other, and its `detail` renderer emits the empty cell):

```js
function sizeRowHtml(s, nested){
  return `<tr class="size${nested ? " nested" : ""}">
    <td data-col="product">${esc(sizeName(s))} <span class="asin">${esc(s.asin)}</span></td>
    ${detailCells(s)}
  </tr>`;
}
```

In `renderTable`'s flavour-group block, replace

```js
          html += `<tr class="flav">
            <td>${esc(g.flavour)}
              <span class="fcount">${g.sizes.length} size(s)</span></td>
            <td></td>
            ${detailCells(g)}
          </tr>`;
```

with

```js
          html += `<tr class="flav">
            <td data-col="product">${esc(g.flavour)}
              <span class="fcount">${g.sizes.length} size(s)</span></td>
            ${detailCells(g)}
          </tr>`;
```

In the SKU-view and product-view parent rows, add `data-col="product"` to the first cell: `<td class="p-name">` → `<td class="p-name" data-col="product">` (two places).

Replace both `colspan="${shownColumns().length}"` with `colspan="${visibleColumns().length}"`.

- [ ] **Step 6: Split `totalsRow` into `computeTotals` + cells**

Keep every line of arithmetic and every comment in the current `totalsRow` up to (not including) `return \`<tr class="totals">`, but wrap it as:

```js
function computeTotals(list){
  /* …the existing sum/weight/ratio/rating/decided/infinite code, unchanged… */
  return {sales, spend, net, adsCost, attributed, units, ordered, refunded,
          weighed: weighed.length, weight, weightUnknown, ratio,
          rating, reviews, decided, infinite};
}

function totalsRow(list, isSkus){
  const t = computeTotals(list);
  return `<tr class="totals">
    <td data-col="product">All ${n(list.length).toLocaleString("en-IN")} ${isSkus ? "pack size(s)" : "product(s)"} shown
      <span class="tnote">money and units summed; percentages recomputed from those sums, not averaged</span></td>
    ${visibleColumns().slice(1).map(c => cell(c, c.total(t))).join("")}
  </tr>`;
}
```

- [ ] **Step 7: The table width, and retire `showExtra`**

Change the `<table>` in `renderTable` to `<table style="min-width:${tableMinWidth()}px">`.
Change the CSS rule `table{width:100%;border-collapse:separate;border-spacing:0;min-width:1120px}` to `table{width:100%;border-collapse:separate;border-spacing:0}` and add to its comment: *the floor is now `tableMinWidth()`, computed from the visible columns and set inline.*

Replace `let showExtra = remembered("showExtra", false);    // the Returns column` with:

```js
// The column layout {order, hidden}. Set from the server's `column_layout` (a named login) or
// localStorage (a shared-password session) on every load — see `applyLayoutFrom`.
let layout = null;
```

Update the comment directly below it that says "for the reason `showExtra` records" to "for the reason recorded here: `remembered` is a `const` arrow function…".

In `load()`, directly after `data = body;`, add:

```js
    applyLayoutFrom(data);
```

and add this function after `load()`:

```js
/* Where the layout comes from: the account for a named login, this browser otherwise. */
function applyLayoutFrom(d){
  if(d.column_scope === "browser"){
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem("pf.columnLayout") || "null"); } catch(e){}
    layout = normaliseLayout(saved, d.columns);
  } else {
    layout = normaliseLayout(d.column_layout, d.columns);
  }
}
```

Delete the old `$("cols-btn").addEventListener("click", …)` block and `renderColsButton()` (and its call in `render()`) — Task 6 replaces them. Temporarily set the button text statically: in the HTML, `<button class="btn-outline" id="cols-btn">Columns ▾</button>`.

- [ ] **Step 8: Run the render tests and the portfolio suite**

Run: `venv/Scripts/python -m pytest tests/test_portfolio_columns_render.py -q -p no:randomly`
Expected: all PASS.

Run: `venv/Scripts/python -m pytest tests/ -k portfolio -q -p no:randomly`
Expected failures ONLY in tests that pin the retired shape — expected set:
`test_portfolio_groups.py::test_the_hidden_columns_are_gated_in_ALL_THREE_places`,
`test_portfolio_groups.py::test_showExtra_is_declared_AFTER_the_helper_it_calls`,
`test_portfolio_screen.py::test_the_totals_row_has_one_cell_per_column`,
`test_portfolio_screen.py::test_units_and_weight_are_OUTSIDE_the_showExtra_gate_in_ALL_THREE_functions`,
`test_portfolio_ui_fixes.py::test_the_money_columns_are_all_tagged_num_in_all_three_render_functions`,
and any `test_portfolio_api.py` assertion naming `showExtra`/`shownColumns`/`min-width:1120px`.
Any OTHER failure is a regression to fix before continuing.

- [ ] **Step 9: Rewrite the retired-shape tests to assert the new invariant**

Each of those tests was right for three hand-written builders. Replace each one's body with an assertion of the property it protected, now held by construction — do NOT delete a test without a replacement:

- `test_the_hidden_columns_are_gated_in_ALL_THREE_places` → assert `headerHtml`, `dataCells`, `detailCells` and `totalsRow` each contain `visibleColumns()` and none contains `showExtra`; the executed alignment is in `test_portfolio_columns_render.py` (reference it in the docstring).
- `test_showExtra_is_declared_AFTER_the_helper_it_calls` → rename `test_layout_state_is_declared_AFTER_the_helper_it_calls`; assert `source.index("const remembered =") < source.index("let layout = null")`.
- `test_the_totals_row_has_one_cell_per_column` → assert `totalsRow` maps `visibleColumns().slice(1)` and emits exactly one hand-written `<td` (the Product cell).
- `test_units_and_weight_are_OUTSIDE_the_showExtra_gate_in_ALL_THREE_functions` → assert `units` and `weight_kg` are `locked: True` in `app.portfolio.columns.COLUMNS` and each `COLUMN_DEFS` entry for them has `row`, `detail` and `total`.
- `test_the_money_columns_are_all_tagged_num_in_all_three_render_functions` → assert every numeric column id (`sales, ad_spend, tacos, acos, net_pct, units, weight_kg, returns_pct`) has `num: true` in `COLUMN_DEFS`, and `cell()` emits `class="num"` when `col.num` (executed via `run_portfolio_js`).

Run: `venv/Scripts/python -m pytest -q`
Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
git add templates/portfolio.html tests/
git commit -m "refactor(portfolio): every row is built from ONE column list

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: The `Columns ▾` panel

**Files:**
- Modify: `templates/portfolio.html` — the `cols-btn` markup, new `#cols-panel`, CSS, handlers
- Test: `tests/test_portfolio_columns_render.py` (append)

**Interfaces:**
- Consumes: `layout`, `normaliseLayout`, `visibleColumns`, `data.columns`, `data.column_scope`, `sort`, `remember`, `render()`.
- Produces: `columnsPanelHtml() -> string`, `moveColumn(id, delta)`, `setHidden(id, hidden)`, `applyLayout(next)`, `saveLayout()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_portfolio_columns_render.py`:

```python
PANEL = VOCAB + """
layout = normaliseLayout(null, data.columns);
let saved = []; function saveLayout(){ saved.push(JSON.parse(JSON.stringify(layout))); }
function render(){} function remember(){}
"""


def test_the_panel_lists_every_movable_column_and_disables_the_locked_ticks():
    html = run_portfolio_js(PANEL + "emit(columnsPanelHtml());")
    for col in ("verdict", "sales", "tacos", "rating", "decision"):
        assert f'data-col-row="{col}"' in html
    assert 'data-col-row="product"' not in html, "Product is pinned and is not listed as movable"
    import re
    sales = re.search(r'data-col-row="sales".*?</li>', html, re.S).group(0)
    assert "disabled" in sales, "a protected column's tick box must be disabled"


def test_moving_a_column_changes_the_order_and_saves():
    out = run_portfolio_js(PANEL + """
moveColumn("decision", -1);
emit({order: layout.order, saves: saved.length});
""")
    assert out["order"].index("decision") == out["order"].index("rating") - 1
    assert out["saves"] == 1


def test_moving_past_either_end_is_a_no_op():
    out = run_portfolio_js(PANEL + """
const first = layout.order[0], last = layout.order[layout.order.length - 1];
moveColumn(first, -1); moveColumn(last, +1);
emit({first: layout.order[0], last: layout.order[layout.order.length - 1], saves: saved.length});
""")
    assert out["saves"] == 0


def test_hiding_the_sorted_column_resets_the_sort_to_sales():
    out = run_portfolio_js(PANEL + """
sort = {key: "tacos", dir: 1};
setHidden("tacos", true);
emit({sort, hidden: layout.hidden});
""")
    assert out["sort"] == {"key": "sales", "dir": -1}
    assert "tacos" in out["hidden"]


def test_a_locked_column_cannot_be_hidden_even_by_calling_the_handler():
    out = run_portfolio_js(PANEL + 'setHidden("sales", true); emit(layout.hidden);')
    assert "sales" not in out
```

- [ ] **Step 2: Run to see them fail**

Run: `venv/Scripts/python -m pytest tests/test_portfolio_columns_render.py -q -p no:randomly`
Expected: FAIL — `columnsPanelHtml is not defined`. (Add `"columnsPanelHtml", "moveColumn", "setHidden", "applyLayout"` to `FUNCTIONS` in `tests/js_harness.py` first.)

- [ ] **Step 3: Markup and CSS**

Replace the `<button class="btn-outline" id="cols-btn">…</button>` line (and its comment) with:

```html
    <!-- Choose, hide and reorder columns. Saved with the login (or this browser for a shared
         password). Sales, Ad spend, Units and Weight can move but not hide; Product is pinned. -->
    <span class="cols-wrap">
      <button class="btn-outline" id="cols-btn" aria-expanded="false" aria-controls="cols-panel">Columns ▾</button>
      <div class="cols-panel" id="cols-panel" hidden></div>
    </span>
```

Add to the `<style>` block (theme variables only — `tests/test_theme.py` bans literal colours):

```css
.cols-wrap{position:relative;display:inline-block}
.cols-panel{position:absolute;right:0;top:calc(100% + 4px);z-index:20;min-width:290px;
  background:var(--surface);border:1px solid var(--border-strong);border-radius:8px;
  box-shadow:0 6px 24px var(--shadow, transparent);padding:8px 0}
.cols-head{display:flex;justify-content:space-between;align-items:center;padding:4px 12px 8px;
  font-weight:600;border-bottom:1px solid var(--border)}
.cols-list{list-style:none;margin:0;padding:4px 0}
.cols-list li{display:flex;align-items:center;gap:8px;padding:4px 12px}
.cols-list li.dragging{opacity:.4}
.cols-list .grip{cursor:grab;color:var(--text-dim);user-select:none}
.cols-list label{flex:1;display:flex;gap:6px;align-items:center}
.cols-list .lock{color:var(--text-dim);font-size:11px}
.cols-list button{background:none;border:1px solid var(--border);border-radius:4px;
  padding:0 6px;cursor:pointer;color:var(--text)}
.cols-list button:disabled{opacity:.35;cursor:default}
.cols-note{padding:4px 12px;font-size:11.5px;color:var(--red)}
```

If `var(--shadow)` does not exist in `static/theme.css`, drop the `box-shadow` declaration instead of inventing a colour.

- [ ] **Step 4: The panel functions**

Add after `applyLayoutFrom`:

```js
/* ── The Columns ▾ panel ─────────────────────────────────────────────────────────────────────
   Every change applies INSTANTLY and saves in the background — no Save button. ↑ ↓ exist because
   dragging inside a small panel is fiddly on a phone or the warehouse tablet; drag is for the mouse.
   Header drag was rejected: a heading click already SORTS, so every click would be ambiguous. */
function columnsPanelHtml(){
  const vocab = data.columns || [];
  const locked = new Set(vocab.filter(c => c.locked).map(c => c.id));
  const label = id => (vocab.find(c => c.id === id) || {}).label || id;
  const last = layout.order.length - 1;
  return `<div class="cols-head"><span>Columns</span>
      <button type="button" class="btn-link" data-cols-reset>Reset to default</button></div>
    <ul class="cols-list">
      <li><span class="grip" aria-hidden="true"></span><label>${esc(label("product"))}</label>
        <span class="lock">always first</span></li>
      ${layout.order.map((id, i) => `
      <li draggable="true" data-col-row="${id}">
        <span class="grip" aria-hidden="true">⠿</span>
        <label><input type="checkbox" data-col-toggle="${id}"
          ${layout.hidden.includes(id) ? "" : "checked"}${locked.has(id) ? " disabled" : ""}/>
          ${esc(label(id))}</label>
        ${locked.has(id) ? '<span class="lock">always shown</span>' : ""}
        <button type="button" data-col-move="${id}" data-delta="-1" aria-label="Move ${esc(label(id))} left"
          ${i === 0 ? "disabled" : ""}>↑</button>
        <button type="button" data-col-move="${id}" data-delta="1" aria-label="Move ${esc(label(id))} right"
          ${i === last ? "disabled" : ""}>↓</button>
      </li>`).join("")}
    </ul>
    <div class="cols-note" id="cols-note" hidden></div>`;
}

/* The one place a layout changes: normalise, apply the sort rule, re-render, save. */
function applyLayout(next){
  const before = JSON.stringify(layout);
  layout = normaliseLayout(next, data.columns);
  if(JSON.stringify(layout) === before) return;
  // Sorted by a column that is now hidden -> back to Sales. A table ordered by a column the owner
  // cannot see reads as randomly ordered.
  const sortedCol = Object.keys(COLUMN_DEFS).find(id => COLUMN_DEFS[id].sortKey === sort.key);
  if(sortedCol && layout.hidden.includes(sortedCol)){
    sort = {key: "sales", dir: -1};
    remember("sort", sort);
  }
  render();
  saveLayout();
}

function moveColumn(id, delta){
  const order = layout.order.slice();
  const i = order.indexOf(id), j = i + delta;
  if(i < 0 || j < 0 || j >= order.length) return;
  [order[i], order[j]] = [order[j], order[i]];
  applyLayout({order, hidden: layout.hidden});
}

function setHidden(id, hide){
  const hidden = layout.hidden.filter(x => x !== id);
  if(hide) hidden.push(id);
  applyLayout({order: layout.order, hidden});      // the normaliser refuses a locked id
}

/* Account or browser. A failed save leaves the screen AS SET and says so — it never reverts. */
async function saveLayout(){
  const note = $("cols-note");
  if(data.column_scope === "browser"){
    try { localStorage.setItem("pf.columnLayout", JSON.stringify(layout)); }
    catch(e){ if(note){ note.textContent = "Couldn't save — this layout will reset when you reload."; note.hidden = false; } }
    return;
  }
  try{
    const r = await fetch("/portfolio/column-prefs", {method: "PUT",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(layout)});
    if(!r.ok) throw new Error(String(r.status));
    if(note) note.hidden = true;
  }catch(err){
    if(note){ note.textContent = "Couldn't save — this layout will reset when you reload."; note.hidden = false; }
  }
}

function renderColumnsPanel(){
  const panel = $("cols-panel");
  if(panel && !panel.hidden) panel.innerHTML = columnsPanelHtml();
}
```

Add `renderColumnsPanel();` to `render()` in place of the removed `renderColsButton();`.

- [ ] **Step 5: The event wiring**

Add after the functions above (top level, near the other listeners):

```js
$("cols-btn").addEventListener("click", ev => {
  ev.stopPropagation();
  const panel = $("cols-panel");
  panel.hidden = !panel.hidden;
  $("cols-btn").setAttribute("aria-expanded", String(!panel.hidden));
  if(!panel.hidden) panel.innerHTML = columnsPanelHtml();
});
$("cols-panel").addEventListener("click", ev => {
  ev.stopPropagation();
  const move = ev.target.closest("[data-col-move]");
  if(move){ moveColumn(move.dataset.colMove, Number(move.dataset.delta)); return; }
  if(ev.target.closest("[data-cols-reset]")) applyLayout(null);
});
$("cols-panel").addEventListener("change", ev => {
  const box = ev.target.closest("[data-col-toggle]");
  if(box) setHidden(box.dataset.colToggle, !box.checked);
});
// Drag to reorder, mouse only; ↑ ↓ are the touch and keyboard path.
let dragId = null;
$("cols-panel").addEventListener("dragstart", ev => {
  const li = ev.target.closest("[data-col-row]"); if(!li) return;
  dragId = li.dataset.colRow; li.classList.add("dragging");
  ev.dataTransfer.effectAllowed = "move";
});
$("cols-panel").addEventListener("dragover", ev => {
  if(dragId && ev.target.closest("[data-col-row]")) ev.preventDefault();
});
$("cols-panel").addEventListener("drop", ev => {
  const li = ev.target.closest("[data-col-row]"); if(!li || !dragId) return;
  ev.preventDefault();
  const order = layout.order.filter(id => id !== dragId);
  order.splice(order.indexOf(li.dataset.colRow) + (layout.order.indexOf(dragId) <
    layout.order.indexOf(li.dataset.colRow) ? 1 : 0), 0, dragId);
  dragId = null;
  applyLayout({order, hidden: layout.hidden});
});
$("cols-panel").addEventListener("dragend", () => { dragId = null; });
document.addEventListener("click", () => {
  const panel = $("cols-panel");
  if(!panel.hidden){ panel.hidden = true; $("cols-btn").setAttribute("aria-expanded", "false"); }
});
document.addEventListener("keydown", ev => {
  if(ev.key === "Escape" && !$("cols-panel").hidden){
    $("cols-panel").hidden = true;
    $("cols-btn").setAttribute("aria-expanded", "false");
    $("cols-btn").focus();
  }
});
```

- [ ] **Step 6: Run everything**

Run: `venv/Scripts/python -m pytest tests/test_portfolio_columns_render.py tests/test_portfolio_columns.py tests/test_theme.py tests/test_template_render_targets.py -q -p no:randomly`
Expected: all PASS (`test_template_render_targets.py` checks every `$(...)` written to has a matching id — `cols-panel` and `cols-note` must exist).

Run: `venv/Scripts/python -m pytest -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add templates/portfolio.html tests/
git commit -m "feat(portfolio): a Columns panel to hide and reorder columns, saved per login

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Mutation harness, existing harnesses, browser check, docs

**Files:**
- Create: `scripts/mutate_portfolio_columns.py`
- Modify (only if a find-string no longer matches): `scripts/mutate_portfolio_ui.py`, `scripts/mutate_portfolio_active_weight.py`, `scripts/mutate_portfolio_groups.py`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Write the harness**

Create `scripts/mutate_portfolio_columns.py` using the same runner as `scripts/mutate_portfolio_sb.py` (copy its `main()` verbatim, with this `TESTS` and `MUTATIONS`):

```python
TESTS = ["tests/test_portfolio_columns.py", "tests/test_portfolio_columns_render.py",
         "tests/test_schema_migrations.py"]

T, COLS, ROUTER = "templates/portfolio.html", "app/portfolio/columns.py", "app/routers/portfolio.py"

MUTATIONS = [
    (T, '    ${visibleColumns().slice(1).map(c => cell(c, c.total(t))).join("")}',
     '    ${normaliseLayout(null, data.columns).order.map(id => cell(Object.assign({id}, COLUMN_DEFS[id]), COLUMN_DEFS[id].total(t))).join("")}',
     "the totals row ignores the saved order and hidden set"),
    (T, 'return visibleColumns().slice(1).map(c => cell(c, c.detail(row))).join("");',
     'return normaliseLayout(null, data.columns).order.map(id => cell(Object.assign({id}, COLUMN_DEFS[id]), COLUMN_DEFS[id].detail(row))).join("");',
     "size and flavour rows ignore the layout"),
    (COLS, '    {"id": "sales",       "label": "Sales",    "locked": True},',
     '    {"id": "sales",       "label": "Sales",    "locked": False},',
     "a protected column becomes hideable"),
    (COLS, '''        clean.insert(at, col)''', '''        pass''',
     "a NEW column silently never appears for users who customised"),
    (ROUTER, "    username = get_current_username(request)\n    if not username:\n        return JSONResponse(\n            {\"error\": \"Shared-password",
     "    username = get_current_username(request) or (await request.json()).get(\"username\")\n    if not username:\n        return JSONResponse(\n            {\"error\": \"Shared-password",
     "PUT takes the account from the BODY — anyone can overwrite anyone's layout"),
    (T, '    sort = {key: "sales", dir: -1};\n    remember("sort", sort);',
     '    remember("sort", sort);',
     "sorting by a hidden column is not reset"),
    (T, '<table style="min-width:${tableMinWidth()}px">', '<table style="min-width:1120px">',
     "the table floor reverts to a literal and pads a narrow layout"),
    (COLS, '        if col in known and col not in _LOCKED and col not in hide:',
     '        if col in known and col not in hide:',
     "the server normaliser lets a protected column be hidden"),
    (T, '  return `<td data-col="${col.id}"${cls', '  return `<td${cls',
     "cells lose their column id, so alignment can only be checked by count"),
    ("deploy/update-ec2.sh",
     'elif "preferences_json" in cols("users"):\n    print("c3d8e1f5a702")                           # head: per-login display preferences\n',
     "", "the baseline detector is stale and stamps production BACKWARDS"),
]
```

Plus one mutation making the Excel follow the layout: in `download_portfolio`, it is a plain add-only change (none exists), so instead assert it by the test `test_the_EXCEL_ignores_the_saved_layout` — no mutation line needed.

- [ ] **Step 2: Run it, and close any survivor with a test**

Run: `venv/Scripts/python scripts/mutate_portfolio_columns.py`
Expected: `All 10 mutations caught.` For any `SURVIVED`, add the test that would have caught it to the relevant test file, re-run until clean. A `SKIP target not found` means the find-string must be re-pointed at the code as actually written.

- [ ] **Step 3: Re-run the older harnesses**

Run each: `venv/Scripts/python scripts/mutate_portfolio_ui.py`, `venv/Scripts/python scripts/mutate_portfolio_active_weight.py`, `venv/Scripts/python scripts/mutate_portfolio_groups.py`, `venv/Scripts/python scripts/mutate_portfolio_sb.py`.
Expected: all caught. This change rewrote the builders their targets sit in, so a `SKIP target not found` is expected for find-strings quoting the old `dataCells`/`totalsRow`/`showExtra` code — **re-point each one at the new code expressing the same defect** (e.g. the `class="num"` on the weight cell now lives in `cell()` via `COLUMN_DEFS.weight_kg.num`), never delete it.

- [ ] **Step 4: Browser check on real data**

Start the app (`preview_start` name `tracker`), sign in as a named user, open `/portfolio-page`:
1. Open `Columns ▾`; untick ACOS, Verdict, Rating → gone from header, rows and totals.
2. ↑ Decision three times → it moves left in every row.
3. Drag Net % to the top.
4. Reload → the layout returns. Sign out and back in → still there.
5. Measure: `document.querySelectorAll('thead th').length`, a parent row's `td` count, an expanded size row's, and the tfoot's are equal; their `data-col` sequences are identical.
6. Resize to 375px: ↑ ↓ usable, `document.documentElement.scrollWidth - clientWidth` is 0.
7. Reset to default → today's layout.

- [ ] **Step 5: CLAUDE.md**

Under the Portfolio tab section, after "### The size rows are plain…", add a section `### The columns are the owner's to choose, saved with the login` recording: the ask (verbatim); the four protected columns + pinned Product; storage in `users.preferences_json` (NULL = never chosen, namespaced, merged); account vs browser scope and the 409; normalise on read AND write, with the missing-new-column rule; ONE `visibleColumns()` list replacing three builders + `showExtra`, and `data-col` on every cell; the Rating/Decision span removed; `tableMinWidth()`; sort reset on hide; the Excel unaffected; and the render tests that EXECUTE the template under Node. Update the test count on line 10.

- [ ] **Step 6: Full suite, commit, push, deploy**

Run: `venv/Scripts/python -m pytest -q` → all PASS.

```bash
git add scripts/ CLAUDE.md tests/
git commit -m "test(portfolio): mutation harness for the column picker; record it

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git push origin claude/stoic-allen-bb3a55
```

Deploy — it has a migration, so check the script out first (CLAUDE.md):

```bash
ssh -i "/c/Users/LENOVO/Desktop/old downloads/amazon-tracker-key.pem" ubuntu@13.233.144.148 \
  "cd /opt/amazon-tracker && git fetch origin claude/stoic-allen-bb3a55 && git checkout origin/claude/stoic-allen-bb3a55 -- deploy/update-ec2.sh && yes y | bash deploy/update-ec2.sh"
```

Expected: `before: b91d4a7c3e26` → `after: c3d8e1f5a702 (head)`, `Deployed successfully`.
