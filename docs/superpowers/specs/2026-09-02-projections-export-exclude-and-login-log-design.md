# Projections: reorder-level export, table highlight, row removal — and a Users login log

## The requests

1. *"give a button to download reorder level i.e. ideal warehouse stock in excel and pdf. just
   the product, brand and the reorder level."*
2. *"also highlight it in the live table"*
3. *"give option here to remove a product from the list by selecting and removing it."*
4. *"In the users section make a page to see user log, what did they do when they logged in to
   check what different users are doing."*

Four independent, small changes — not one feature — so this spec covers all four with their own
sections rather than forcing a shared narrative.

## Decisions taken (yours)

- **Removing a product is a soft hide (`excluded_at`), reversible** — the exact mechanism
  `ShipmentPlanItem` already uses, not a hard delete. A hard delete on an active parent would be
  undone at the very next page load anyway (`build_current_rows` recreates any row missing for a
  currently-active sheet parent), so a delete button that silently fails to delete is worse than
  one that is honest about what it does.
- **The reorder-level export gets the same priority as Shipment's "To buy" list**: filtered to
  rows that need action, never a document full of zeros.
- **The login log is login events only** — sign-in time, IP, success/failure, username tried.
  Not a general activity trail. This app has zero audit history before today; the log starts
  recording from the moment this ships, and says so on screen rather than implying history that
  does not exist.

## 1. Reorder-level export (Excel + PDF)

### What is there to build from
`build_simple_xlsx`/`build_simple_pdf` (`app/shipment/documents.py`) are hardwired to the
8-column shipment identity shape (`IDENTITY_HEADERS`, `_totals_row` summing every column past
those 8) — reusing them for a 3-column Product/Brand/reorder-level report would mean either
faking 5 empty identity columns or rewriting `_totals_row`'s assumption that every trailing
column is summable, which `build_portfolio_xlsx`'s own docstring already explains is exactly the
wrong fix (it broke once, for the Portfolio export, because percentages and an em dash are not
summable either). `build_portfolio_xlsx` is the closer precedent — a **sibling** builder, not a
parameter on the shipment one — but it does not filter or omit a "nothing to report" state.

**`build_tobuy_xlsx`** (same file) is the actual precedent to copy: it already answers "which
products need action, as Product/Brand/one-number rows, filtered to only the ones that do," for
a nearly identical purchasing question (raw material shortfall vs. warehouse reorder level). Its
three rules, restated here for the new export:

1. **Filtered, not sorted.** A product with `ideal_wh_stock <= 0` is ABSENT, never shown as `0`.
   Same reasoning `build_tobuy_xlsx`'s docstring states: this file is read by someone deciding
   what to buy, and a row reading "ABC Sattu … 0" invites ordering zero of it.
2. **Nothing-to-report says so in words.** An empty grid reads as a failed download; a sentence
   ("Every product is above its reorder level.") does not.
3. **A totals row**, since unlike the to-buy list this one has a genuinely summable single
   quantity column (kg) — matching `build_simple_xlsx`'s pattern, not `build_tobuy_xlsx`'s
   (which also totals, in fact — both already total their one numeric column).

### New builders, not reused ones
Two new functions in `app/shipment/documents.py`, following the existing "sibling function"
precedent (`build_portfolio_xlsx` beside `build_simple_xlsx`) rather than widening either:

- `build_reorder_xlsx(rows: list[dict], subtitle: str) -> io.BytesIO` — `rows` already filtered
  by the caller (the router) to `ideal_wh_stock > 0`, each `{"product": str, "brand": str,
  "reorder_level_kg": float}`. Three columns: Product, Brand, Reorder Level (kg). A totals row
  sums the third column, matching `build_simple_xlsx`'s `_totals_row` treatment but hand-rolled
  here since `_totals_row` is keyed to `IDENTITY_HEADERS`, which this document does not have.
- `build_reorder_pdf(rows: list[dict], subtitle: str) -> io.BytesIO` — same three columns,
  portrait A4, reusing `_pdf_document`/`_pdf_table_style`/`_head_cell` (the generic pieces) but
  not `build_simple_pdf` itself, for the identical `IDENTITY_HEADERS`-coupling reason.

Both take the **already-filtered** rows rather than filtering internally, so the router decides
the business rule (`> 0`) once and both formats agree by construction — the same discipline
CLAUDE.md already states for `_document_rows()` on the Shipment tab: "a download cannot disagree
with the screen about order or any computed number — one code path produces both."

### Router
`GET /projections/download/reorder.xlsx` and `GET /projections/download/reorder.pdf` in
`app/routers/projections.py`. Both call `build_current_rows` + `_calculate_with_settings` (the
same pair `/download` already uses), filter to `ideal_wh_stock > 0`, sort descending by
`ideal_wh_stock` (matching the live table's own default sort, so the document and the screen
read in the same order), and hand the filtered rows to the matching builder. A subtitle states
how many products were omitted for being covered, mirroring `build_tobuy_xlsx`'s subtitle
convention.

**Excluded rows (see section 3) are never in these downloads.** `build_current_rows` already
returns only non-excluded rows to every caller — no separate filter needed, the same way an
excluded Shipment line never reaches its four downloads.

## 2. Highlight the reorder level in the live table

`Ideal WH` is already the first calculated column after `Daily (kg/d)`, bold, per the prior
change. "Highlight" here is read as: make it visually distinct from the surrounding numbers, not
merely bold-among-other-bold-cells (the Forecast and Ideal FBA cells are also currently rendered
`<strong>`, so bold alone does not single it out).

Give `Ideal WH`'s `<td>` its own CSS class (`.ideal-wh-cell`) with a background tint
(`var(--accent-soft)`, the same token the sortable-header-hover and chip-active states already
use elsewhere in this app, so no new colour is introduced) and keep the existing diverged-buffer
tooltip on the same cell. This is a template-only change — no new data, no new column.

## 3. Remove a product from the table (soft hide, reversible)

### The mechanism, copied from Shipment
`ShipmentPlanItem.excluded_at` (`app/models.py`): a nullable `DateTime`, set to the current time
to exclude, cleared to `None` to restore. Its own comment states the reasoning this reuses
verbatim: a `DateTime` rather than a `Boolean` so every pre-migration row is included with no
backfill (`WHERE excluded_at IS NULL`), and reversibility is the point — "an accidental
multi-row exclude is one click back."

`ProjectionRow` gets the identical column, `excluded_at`, via migration. `build_current_rows`'s
existing query already goes through `repository.load_rows`; that function (and every other
reader) filters to `excluded_at IS NULL` by default, matching `load_plan_items(...,
include_excluded=False)`'s default.

### Why this cannot be a permanent delete for an active parent, and why that has to be visible
Stated in the decisions above: `build_current_rows` recreates a bare row (Global Defaults,
`needs_review=True`) for every currently-active sheet parent that has no stored row — that is
the entire mechanism behind "Triphala Sattu never goes missing again" from the prior change. A
hard `DELETE` on an active parent's row is therefore undone at the very next page load, silently
looking like the removal "did not work." The soft-hide has the same practical limit — an
excluded row for a still-active parent will NOT stay hidden past the next weekly refresh, because
`upsert_sheet_rows` only skips rows with `sales_source == "manual"`, and exclusion is orthogonal
to that flag.

**This is stated on screen, not left to be discovered.** The remove-selected button's confirm
dialog and the hidden-rows note both say: *"only lasts until the next weekly refresh, if this
product is still active in the MRP sheet."* For a genuinely discontinued parent (inactive in the
sheet), the hide is effectively permanent, because nothing recreates it.

### Selection UI
A leading checkbox column added to the table (new leftmost `<th>`/`<td>`, before `#`), with a
header checkbox that selects/deselects all currently-filtered rows (matching the search/brand
filter already applied — selecting "all" must not silently select a hidden-by-filter row). A
"Remove selected (N)" button appears in the existing button row only when at least one row is
selected, next to "Reset all manual edits", following that button's `btn-danger` styling since
this is also a destructive-feeling action even though it is reversible.

### Repository and router
`app/projections/repository.py` gains `set_excluded(db, parent_products: list[str], excluded:
bool) -> list[str]`, mirroring `app.shipment.repository.set_item_excluded`'s exact signature
shape and behaviour (idempotent — excluding an already-excluded row is a no-op, not an error;
returns the names actually changed).

`app/routers/projections.py` gains `POST /projections/exclude` (body:
`{"parent_products": [...], "excluded": bool}`), mirroring
`POST /shipment/plan/{plan_id}/items/exclude`'s body shape. **Unlike the Shipment version, there
is no packed-units guard to check** — a projection row has no "already committed" state analogous
to packed cartons, so nothing here can refuse an exclude the way Shipment's 409 does.

`GET /projections/last`'s `catalogue` report gains `excluded_count` and `excluded_names` (capped
at 8, matching `hidden_names`'s own cap and the reasoning behind it — "a parent silently missing
… is indistinguishable from a bug").

## 4. Users: a login-event log

### What exists today, and why it is not enough
`User.last_login_at` (a single `DateTime`, overwritten on every successful login in
`app/users.py`'s `authenticate()`) answers "when did they last sign in," not "who signed in when,
how often, or who TRIED and failed." No audit table exists anywhere in this codebase — grepped
across `app/` and `tests/`, confirmed. `AdsMutation` is bid-change-specific and not a general
precedent for "who did what."

**This is a new capability, not a report on history that does not exist.** The panel says so:
*"Recording starts now — nothing before today is in this log."*

### New table: `UserLoginEvent`
```
id            Integer, PK
username      String(32)   — the username TYPED, not resolved. A failed attempt against a
                              username that does not exist is still worth recording (a wrong
                              guess, or evidence of probing), and there is no user row to
                              foreign-key to in that case.
user_id       Integer, nullable, FK -> users.id — set only on a SUCCESSFUL named-user login.
                              Nullable because a shared-password login has no user row, and a
                              failed attempt may have no matching user either.
success       Boolean, nullable=False
via           String(10)   — "named" | "app_password" | "ops_password" | "unknown". Which of
                              the three login paths this attempt took, so a shared-password
                              sign-in shows up distinctly from a named one on the same log.
ip_address    String(64)   — from X-Forwarded-For (Caddy sits in front of uvicorn on this box,
                              per CLAUDE.md's deployment section) with a fallback to the raw
                              client address for local/dev runs where there is no proxy.
created_at    DateTime, default utcnow
```

Indexed on `created_at` (the log is read newest-first and eventually needs a retention sweep —
out of scope for this change, noted below) and on `user_id` (a per-person view is the natural
follow-up question even though this change ships an all-users table first).

### Where it is written
Both branches of `POST /login` in `app/routers/auth.py` — the named-user branch and the two
shared-password branches — call a new `app.users.record_login_event(db, *, username, user_id,
success, via, ip_address)` helper, including the failure path (`_reject()`). **Every attempt is
recorded, not only successes** — a log that only shows successful logins cannot answer "is
someone trying my password," which is the more common reason to want this page at all.

This is the one place in the app that touches ~40 existing routes' worth of blast radius the
"full activity trail" option would have needed — and it touches none of them, because it is
scoped to the single `POST /login` route, which already branches on exactly the three paths this
log needs to distinguish.

### The page
A new tab within the existing Users page, not a new nav link — administrators already reach
`/users-page`; a second top-level nav entry for a sub-view of the same admin area would duplicate
`nav.html`'s existing per-area gating for no benefit, and CLAUDE.md's nav-consistency test
(`CANONICAL_NAV`) only tracks the seven area tabs, not admin-only sub-pages (Users itself is not
in that list either, precedent already established).

`GET /admin/login-log?limit=200` (query param, capped server-side at 500 — this table has no
retention policy yet and could grow unbounded) returns the newest events, newest first, each
carrying the resolved username, success, via, IP and timestamp. Rendered as a new card on
`users.html`, below "People," reusing that page's existing table/pill CSS rather than introducing
new classes — a failed attempt gets a red pill, a success a muted one, consistent with the
Admin/Disabled pill pattern already on that page.

## What does not change

- Every existing document builder, route, and test for Shipment/Portfolio/Ads stays untouched —
  the new export builders are new functions, not edits to shared ones.
- The weekly Projections refresh job, the blend settings, the reorder-point formula itself — all
  from the two prior changes this session, untouched.
- `require_admin_grant`/`require_user_admin` — the login log's new route uses the same
  `require_user_admin` dependency every other `/admin/users/*` route already uses; no new guard
  needed.
- Login itself is unchanged in behaviour — only a side-effect (one INSERT) is added to a route
  that already runs a DB query per attempt.

## Files expected to change

New: `alembic/versions/<new>_projection_row_excluded_at.py`,
`alembic/versions/<new>_user_login_event.py`, `tests/test_projections_exclude.py`,
`tests/test_login_log.py`.

Changed: `app/models.py` (+`excluded_at` on `ProjectionRow`, +`UserLoginEvent`),
`app/projections/repository.py` (+`set_excluded`, `load_rows` default filter),
`app/routers/projections.py` (+`/exclude`, +2 download routes, `catalogue` report gains excluded
fields), `app/shipment/documents.py` (+`build_reorder_xlsx`, +`build_reorder_pdf`),
`app/users.py` (+`record_login_event`), `app/routers/auth.py` (call the new helper on every
login attempt), `app/routers/admin_users.py` (+`GET /admin/login-log`),
`templates/projections.html` (checkbox column, remove-selected button, highlight class, 2
download buttons), `templates/users.html` (new login-log card), `CLAUDE.md`.

## Verification

**Automated**
- A row with `ideal_wh_stock == 0` is absent from both export formats; a row `> 0` is present in
  both, in the same order.
- Excel and PDF exports never include an excluded row.
- `set_excluded` is idempotent (excluding twice changes nothing the second time) and reversible
  (`excluded=False` restores exactly the rows most recently excluded).
- `/projections/last` never returns an excluded row in `products`, and does list it in
  `excluded_names`.
- A failed login attempt (bad password, unknown username, disabled account) is recorded with
  `success=False`; a successful one with `success=True` and the correct `via`.
- `GET /admin/login-log` requires `require_user_admin` — a non-admin session gets 403/redirect,
  matching every other `/admin/users/*` route.
- Mutation harness additions for: the export filter's `> 0` boundary, `set_excluded`'s
  idempotency, and the login route recording a failure.

**Manual, on the running app**
Projections: select 3 rows, click Remove selected, confirm the dialog names the "until the next
refresh" caveat, confirm the 3 rows vanish from the table AND both downloads, confirm a
Restore/undo path exists and works. Download reorder.xlsx and reorder.pdf, confirm exactly 3
columns and a covered-product count matching the to-buy list's own wording style. Confirm the
Ideal WH cells are visibly tinted against the surrounding calculated columns.

Users: attempt a login with a wrong password, then a correct one, then open the login log and
confirm both attempts appear with the right outcome, IP and timestamp, newest first.
