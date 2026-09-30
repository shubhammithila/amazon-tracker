# Portfolio: choose, hide and reorder the table's columns

**Date:** 2026-09-30 · **Status:** approved in brainstorming, awaiting spec review · **Tab:** Portfolio only

## Why

Asked for as *"make every column except the Sales, ad spend, units and weights… hiddenable, i.e.
customisable"*, then *"also make the columns slidable — which one first, which later, or last"*.

Today there is one on/off switch (`showExtra`, the `+ More columns` button) that hides Returns, and
**three hand-written row builders** — `dataCells` (product rows), `detailCells` (size and flavour
rows), `totalsRow` — each check it separately. That is manageable for one column. With seven
independent hide switches plus a free order, three builders each checking seven flags is where the
heading and the cells beneath it get out of line; CLAUDE.md records that failure more than once
(`shownColumns()` returning every column, 11 headings over 8 cells). So the substance of this change
is **one column list that every part of the table is built from**, with the picker on top of it.

## Decisions (the owner's)

| Question | Decision |
|---|---|
| Where the layout is saved | **With the login** — follows a named account to any device |
| Shared-password sessions (no account name) | fall back to **this browser** (`localStorage`) — nothing else to save against |
| Does the Excel follow the screen? | **No** — the export always has every column, in the standard order |
| Storage | **A preferences column on `users`** |
| Where columns are rearranged | **Inside the `Columns ▾` menu**, not by dragging headings |

Why not header-drag: clicking a heading already sorts, so every click would be ambiguous between
sort and move, a near-miss drag would re-sort 34 products, and it is unreliable on the warehouse
tablet.

## Columns

| id | Label | Hideable | Movable |
|---|---|---|---|
| `product` | Product | **no** | **no — always first** (it is the row name and the expand control) |
| `verdict` | Verdict | yes | yes |
| `sales` | Sales | **no** | yes |
| `ad_spend` | Ad spend | **no** | yes |
| `tacos` | TACOS | yes | yes |
| `acos` | ACOS | yes | yes |
| `net_pct` | Net % | yes | yes |
| `units` | Units | **no** | yes |
| `weight_kg` | Weight | **no** | yes |
| `returns_pct` | Returns | yes | yes |
| `rating` | Rating | yes | yes |
| `decision` | Decision | yes | yes |

**Default layout** = the order above with only `returns_pct` hidden — exactly today's screen. A login
that has never chosen sees it, and **Reset to default** restores it.

## What the owner sees

`+ More columns` is replaced, in the same place, by **`Columns ▾`**, opening a panel:

```
Columns                          Reset to default
─────────────────────────────────────────────────
  Product                        (always first)
⠿ [✓] Verdict                          ↑  ↓
⠿ [✓] Sales            always shown    ↑  ↓
⠿ [✓] Ad spend         always shown    ↑  ↓
⠿ [✓] TACOS                            ↑  ↓
⠿ [✓] ACOS                             ↑  ↓
⠿ [✓] Net %                            ↑  ↓
⠿ [✓] Units            always shown    ↑  ↓
⠿ [✓] Weight           always shown    ↑  ↓
⠿ [ ] Returns                          ↑  ↓
⠿ [✓] Rating                           ↑  ↓
⠿ [✓] Decision                         ↑  ↓
```

- **Changes apply instantly** — untick hides, drag (⠿) or ↑ ↓ moves. No Save button.
- The four protected columns show a **disabled** tick box: they can move, never hide.
- ↑ ↓ exist because dragging inside a small panel is fiddly on a phone or tablet; drag is for the
  mouse. Both are keyboard reachable. ↑ is disabled on the first movable row and ↓ on the last.
- The panel closes on Escape and on a click outside it, and the button carries `aria-expanded`.

**Behaviour around the table:**

- **Products and SKUs share one layout.**
- **Sorting by a column that is then hidden resets the sort to Sales, descending.** A table ordered
  by a column the owner cannot see reads as randomly ordered.
- **A custom filter on a hidden column keeps applying**, and its row in the filter builder stays
  visible. A filter must not silently switch off because its column was hidden.
- **The Excel export is unaffected** — every column, standard order.

## Storage

A new nullable column, `users.preferences_json` (Text). **NULL means "never chosen"** and yields the
default layout — deliberately NULL rather than `'{}'`-by-default, since "no choice made" and "chose
the default" are different facts, and the NULL is what lets a later default change reach everyone
who never customised.

Namespaced by screen, so another tab can store its own choice later with no further migration:

```json
{"portfolio_columns": {"order": ["verdict", "sales", "net_pct", "..."], "hidden": ["acos", "returns_pct"]}}
```

`order` holds every movable id; `product` is never stored (it is always first). Written by merging
into the existing JSON, so a future sibling key is never overwritten.

**Migration** adds the column, and **must add a branch to `deploy/update-ec2.sh`'s baseline
detector**, newest first, keyed on `preferences_json in cols("users")`. CLAUDE.md records a stale
detector stamping production backwards; `tests/test_schema_migrations.py` runs it and fails if the
branch is missing.

## The server owns the column vocabulary

`app/portfolio/logic.py` declares the ids, labels, protected flags and default order
(`PORTFOLIO_COLUMNS`, `DEFAULT_COLUMN_LAYOUT`) and `GET /portfolio` ships them, the same way
`group_order` and `verdict_groups` travel. **The template holds no second copy of which columns are
protected** — a copy is a second thing to keep in step, and the server is what refuses an invalid
save.

### `normalise_column_layout(saved) -> {"order": [...], "hidden": [...]}`

One pure function, applied **on read AND on write** (the `good_rating: 99` lesson: a value already
stored or edited by hand must not keep breaking the screen):

1. anything that is not a well-formed dict/list → the default layout;
2. unknown ids dropped (a renamed or removed column);
3. duplicates dropped, first occurrence kept;
4. **a known movable id missing from `order` is inserted at its default position** — relative to the
   ids around it in the default order — so a column added next month *appears* for an existing user
   instead of silently never showing. This is the rule most likely to be got wrong, and it is tested
   directly;
5. `product` removed from `order` (it is always first, never stored);
6. protected ids removed from `hidden`; `hidden` limited to known ids.

## Routes

- **`GET /portfolio`** gains `columns` (the vocabulary), `column_layout` (the owner's normalised
  layout, or the default), and **`column_scope`: `"account"` or `"browser"`**.
- **`PUT /portfolio/column-prefs`** with `{order, hidden}` — normalises, merges into the calling
  user's `preferences_json`, returns the normalised layout. **The account comes from the session
  (`get_current_username`), never from the request body**, so it can only ever write the person
  signed in. Requires the `portfolio` area like every other Portfolio route.
- A shared-password session (no username) gets `column_scope: "browser"` on GET; the page then reads
  and writes `localStorage` and never calls PUT. A PUT from such a session is refused (409) rather
  than silently discarded, so a client bug is visible.

## The page

`COLUMNS` becomes a list of column **definitions** keyed by `id`, each with its own cell renderers:

```js
{id: "tacos", label: "TACOS", num: true, sortKey: "tacos",
 row:    r => `...`,   // product row and SKU row
 detail: r => `...`,   // size row and flavour-group row
 total:  t => `...`}   // totals row
```

and **one** function, `visibleColumns()`, returns the definitions in the saved order with hidden ones
removed. The header, `dataCells`, `detailCells` and `totalsRow` each **map over that one list** — the
per-builder `showExtra` checks are deleted, not kept alongside. `showExtra`, `remembered("showExtra")`
and the `cols-btn` toggle go with them.

- **Every `th` and `td` carries `data-col="<id>"`**, so alignment can be checked by NAME, not only by
  count. Swapped TACOS/ACOS cells have the right count and the wrong figures — the failure that
  matters.
- **Full-width rows** (the verdict reason, the SKU channel note, empty notes) take
  `colspan = visibleColumns().length`, never a literal.
- **The size row's "rating is per product" cell no longer spans.** It spanned Rating AND Decision,
  which may no longer be adjacent. The size row renders a dim `per product` (reason on hover) in the
  Rating column and an empty cell under Decision.
- **Table `min-width` is computed from the visible columns** and set inline, replacing the fixed
  `1120px`. A fixed floor would pad a 6-column table out to 12 columns' width; the floor still exists,
  so a full table scrolls inside `.table-wrap` rather than squeezing `nowrap` money cells over their
  gridlines (CLAUDE.md, "the table needs a min-width").
- **Saving:** a change re-renders immediately, then saves in the background (PUT, or `localStorage`
  when `column_scope` is `"browser"`). A failed save leaves the screen as the owner set it and shows a
  small "couldn't save — this layout will reset when you reload" note. The screen never reverts on its
  own.
- The sort-reset and filter rules above are applied in the same change handler.

## Testing

**Executed, not grepped.** CLAUDE.md records that source-level tests over this template missed real
render bugs (the TDZ "Loading…" hang, the phantom banner). The alignment guarantee is therefore
tested by **running the page's own render functions under Node** — the approach verified on the
banners — with the page's one-line helpers copied verbatim rather than stubbed:

- a shuffled order with three columns hidden: the header, a product row, a size row, a flavour-group
  row and the totals row list **the same `data-col` ids in the same order**, and each row's colspans
  sum to the header count;
- the empty layout, the all-shown layout, and a saved layout naming an id that no longer exists.

**Server (pytest):**
- every `normalise_column_layout` rule, including the missing-new-column insertion at its default
  position;
- PUT writes only the session's own account, ignores any `username` in the body, merges rather than
  overwrites sibling keys, and refuses a shared-password session;
- GET returns the default for a NULL preference and `column_scope: "browser"` for a shared session;
- the Excel export's header row is identical with and without a saved layout;
- the migration and its detector branch.

**Mutations** (`scripts/mutate_portfolio_columns.py`), each must be caught: the totals row ignoring
the order; the size row ignoring it; a protected column made hideable; the missing-column rule
removed (a new column vanishes for existing users); PUT taking the user from the body; the sort not
resetting when its column is hidden; the export following the layout; `min-width` reverting to a
literal; the detector branch removed.

**Browser, on real data:** hide three, reorder, reload (the layout returns), sign out and back in
(still there), repeat on a 375px viewport (↑ ↓ usable, no document-level sideways scroll).

Also re-run the existing harnesses — `mutate_portfolio_ui.py`, `mutate_portfolio_active_weight.py`,
`mutate_portfolio_sb.py` — since this rewrites the builders their targets sit in. A `SKIP target not
found` there means a find-string must be re-pointed at the new code, not that the harness is stale.

## Out of scope

- Column choice on any other tab (the namespaced key makes it a later change, not a redesign).
- Saving sort order, window or filters to the account — they stay in `sessionStorage` as today.
- Column widths / resizing.
- Dragging headings in the table.
- The Excel export's columns or order.
