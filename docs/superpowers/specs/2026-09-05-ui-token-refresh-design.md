# A UI refresh built on tokens, not on a template

## The request

> "This is a github repo for better ui/ux. Check this and incorporate the features which you think
> will improve our layout and ui/ux
> https://github.com/themeselection/materio-mui-nextjs-admin-template-free"

Followed by, when asked what was actually wrong: **"it looks dated/plain next to that template"**, at
**moderate** appetite — refresh the look, keep the density, no sidebar.

## What the template actually offers, and what it does not

Researched against the repo and the live demo rather than the landing page. Two corrections worth
recording, because both change the conclusion:

**The free version is much thinner than it appears.** Five pages, **one table** (with no numeric
columns at all), and **no** pagination, breadcrumbs, skeleton loaders, empty states, sticky headers,
density toggle, or icon-collapsing sidebar. Several patterns worth having are paid-only. It cannot
teach us anything about loading states or long tables because it contains neither.

**Three of its defaults are actively wrong for this app**, measured rather than assumed:

| | Materio | Ours, measured on `/portfolio-page` |
|---|---|---|
| body row height | 50px | **33px** |
| rows visible per 720px screen | ~14 | **21** |
| card treatment | `box-shadow` on every card | borders |
| base font | 15px | 13px |

Adopting their row rhythm would cost **a third of the visible rows** on every table. On the 1,425-row
ads preview that is tens of thousands of pixels of extra scrolling. Their `#8C57FF` purple primary
would put a third saturated hue on screens where red and green already mean money, and shadowing every
card means elevation encodes nothing — Materio itself ships a `bordered` skin for this, hardcoded off
in the free version, which is the variant appropriate here.

### The one idea worth taking

Every semantic colour in Materio carries a **derived opacity ramp of itself** (0.08 / 0.16 / 0.24 /
0.32 / 0.38), so soft and tinted variants are computed from one hex rather than hand-picked. Their
tonal chip is `background: <colour>/0.16; color: <colour>`, and the same ramp drives alert backgrounds,
tinted avatars and progress tracks — one source of truth, four components.

Our `theme.css` hand-picks every pair today (`--green` + `--green-soft`, `--red` + `--red-soft`, …):
eight independent choices that can drift apart, and a ninth to invent by eye when Sponsored Display
arrives.

## The real problem is drift, and it is measurable

The app looks "plain" because nothing shares a rhythm. Counted across the nine templates' own
`<style>` blocks:

| Property | Distinct hardcoded values |
|---|---|
| `font-size` | **19** — 13, 12, 12.5, 11, 14, 10, 15, 16, 13.5 … |
| `padding` | **22** |
| `border-radius` | **11** — 8, 6, 10, 20, 99, 4, 5, 7, 12 … |
| `box-shadow` | 6, and **all already use `var(--shadow*)`** |

**1,404 lines of CSS live inside templates against 299 in the shared sheet** — 82% of the styling is
per-page. That asymmetry is the finding: a refresh confined to `theme.css` would reach only a fifth of
what is on screen.

Note the shadow row. Shadows are the one property that never drifted, because they were tokenised from
the start. Colour never drifted either — `test_theme.py::test_no_template_hardcodes_a_colour` forbids
it. **Everything with a token and a test held; everything without one drifted.** That is the whole
argument for Phase 3.

## Three defects found while measuring

Each was verified in a browser on a running app, not inferred.

1. **Dense table headers scroll away.** `/portfolio-page` is 3,766px tall with 90 rows and 11 money
   columns; `thead th` computes to `position: static`, and after 900px of scroll the headers sit at
   **-289px**. You lose which column is ACOS and which is TACOS. Materio has no sticky header either,
   so this is ours to fix rather than to copy.

2. **Numerals misalign on every platform except Windows.** Nearly reported as universal, then measured
   per family at our table's 13.5px — drift across seven digits:

   | Font | Drift | Who gets it |
   |---|---|---|
   | Segoe UI / system-ui | **0px** | the owner's Windows box |
   | SF Pro, Inter, Roboto | 3px | iPad, iPhone |
   | Arial, Helvetica, `sans-serif` | **6px** | Android, Linux |

   So columns align for the person who looks most and misalign in the warehouse on a tablet. The
   single existing use of `tabular-nums` is a footnote table in `invoice.html`.

3. **The nav overflows a phone by 212px.** Nine links totalling 587px in a `flex-wrap: nowrap` row at
   375px; the last item ("Users") sits at x=747, and the whole document scrolls sideways by 424px.
   CLAUDE.md already records this as known and unfixed.

## Decisions taken (the owner's)

- **Aesthetic refresh, moderate appetite.** Recognisably the same app, visibly newer.
- **Density is preserved.** No table loses rows per screen. This is a hard constraint, and it is
  verified per page rather than promised.
- **No sidebar.** Rejected on cost: it takes ~260px of horizontal room from tables that already need
  1,193px, and the free template's icon-collapse — the thing that would mitigate it — is paid-only.
- **Approach A: tokens first, then per page**, in three phases.

## The rules this must not break

1. **All 2,144 tests stay green**, including the 10 `test_theme.py` guardrails.
2. **WCAG contrast is computed, not eyeballed.** `test_theme.py` already computes real contrast ratios
   for every foreground/background pair. The opacity ramp changes how soft backgrounds are produced,
   so these tests are what prove the refresh did not quietly reduce contrast.
3. **No template declares `:root`** and **no template hardcodes a colour** — both already enforced.
4. **Printed documents keep their dark header bands.** `app/shipment/documents.py` and
   `app/invoice/generator.py` are accounting conventions, already guarded.
5. **No behaviour changes.** No route, no number, no JavaScript logic. This is presentation only.

---

## Phase 1 — The token layer (`static/theme.css` only)

Values chosen to **absorb what the templates already use**, so conversion is renaming rather than
redesigning. Frequency drove every choice.

### Type: 19 sizes become 6

```css
--fs-xs:  11px;   /* ALL-CAPS column headers (43 uses today) */
--fs-sm:  12px;   /* dense cells, tags (49) */
--fs-md:  13px;   /* body default, most cells (62 — our real base) */
--fs-lg:  14px;   /* card titles (20) */
--fs-xl:  16px;   /* KPI figures (14) */
--fs-2xl: 20px;   /* the one headline figure on a KPI strip */
```

`12.5px` (46 uses) and `13.5px` (11) both fold into `--fs-md`. The 0.5px is not perceptible; having a
scale is.

### Spacing on a 4px base

```css
--sp-1: 4px;  --sp-2: 8px;   --sp-3: 12px;
--sp-4: 16px; --sp-5: 20px;  --sp-6: 24px;
```

Materio's base, and it already fits: 4, 8 and 12 are our three most common paddings.

### Radius: 11 become 4

```css
--radius-sm: 4px;  --radius: 8px;  --radius-lg: 12px;  --radius-pill: 999px;
```

`--radius: 8px` already exists and keeps its value, so most call sites need no change.

### The derived opacity ramp

Each semantic colour gains an RGB channel; soft variants are computed rather than picked:

```css
--green-rgb: 20 108 52;      /* the channel behind --green */
--tint-soft:  0.10;          /* chip and banner backgrounds */
--tint-hover: 0.16;          /* row hover, pressed states */

/* a status chip: */
background: rgb(var(--green-rgb) / var(--tint-soft));
color: var(--green);
```

One hex per colour instead of two. Adding Sponsored Display later is one line, not a pastel matched by
eye. **The existing `--*-soft` variables are kept as aliases** so no template breaks on the same
commit that introduces the ramp — they are removed in Phase 2 as pages convert.

> **The tint values are 0.10/0.16, not Materio's 0.08/0.16.** Their ramp is tuned against a
> `#F4F5FA` page and a purple hue. Ours sits on `#f6f7f9` with WCAG-verified pairs, and 0.08 of our
> darker greens and reds is close to invisible on it. The exact figures are settled by
> `test_theme.py`'s contrast maths, not by preference.

### The three fixes

```css
thead th { position: sticky; top: 0; z-index: 2; }
```

Two details, both checked against the source rather than assumed.

`thead th` already carries an opaque `background: var(--surface2)` — required, or body rows show
through a transparent header. That is why this is a two-property change and not a rewrite.

**`ops.html` and `shipment.html` already set `position: sticky` on `thead th` themselves**, and
`ops.html` runs a deliberate four-layer stack that the shared rule must not disturb:

| element | `z-index` in `ops.html` |
|---|---|
| `th.freeze` (the corner cell) | **5** |
| `thead th` | **4** |
| `td.freeze` (frozen body column) | **3** |
| body cells | auto |

The corner cell must outrank both the header row and the frozen column, or one covers the other where
they intersect. So the shared rule uses **`z-index: 2`** — below every layer those two pages
establish — and both keep their own higher values, which now read as deliberate overrides of a shared
default rather than as lone workarounds. Their existing declarations are left in place in Phase 2
rather than deleted, with a comment saying why they are higher.

```css
td.num, th.num, .kpi-value { font-variant-numeric: tabular-nums; }
```

Scoped to numeric cells, never `body`. Tabular figures are slightly wider and worse for prose — the
same prose-versus-numbers distinction `.chan` already makes on the Portfolio tab.

```css
.nav-links { flex-wrap: wrap; row-gap: var(--sp-1); }
```

One property fixes the 212px overflow.

### Deliberately not added

- **Skeleton loaders.** Materio has none, and our long jobs already show true percentages
  (`PHASE_BOUNDS`). A skeleton implies "nearly there" where a percentage tells the truth — it would be
  a downgrade.
- **Shadows on cards.** Elevation that applies to everything communicates nothing; borders already do
  the job. The existing `--shadow*` tokens stay for overlays and the frozen-column edge.
- **A pagination style, breadcrumbs, or a density toggle.** No page needs one, and the template has
  none to copy.

## Phase 2 — Convert the templates, one commit each

Densest and most-used first, because that is where the token layer pays off:

| # | Template | Template CSS | Why here |
|---|---|---|---|
| 1 | `portfolio.html` | 198 lines | 11 columns, most drift, biggest sticky-header gain |
| 2 | `ads.html` | 137 | The money-spending screen; 1,425-row previews |
| 3 | `shipment.html` | 257 | Largest block; two roles read it |
| 4 | `ops.html` | 232 | Warehouse tablet, where misaligned numerals actually bite |
| 5 | `orders.html` | 135 | Three tabs over one KPI strip |
| 6 | `index.html` | 124 | Dashboard |
| 7 | the rest | 321 | `invoice`, `users`, `projections`, `pricing`, `no_access`, `login` |

**Ten pages, 410 hardcoded size values** — counted by running the Phase 3 check against today's
templates. Per page: shipment 79, portfolio 64, ads 51, orders 48, index 42, users 38, projections 33,
invoice 25, pricing 23, `no_access` 7. `nav.html` has no `<style>` block at all and needs no
conversion.

**A conversion is a rename.** `font-size: 12.5px` → `var(--fs-md)`; `padding: 7px` → `var(--sp-2)`.
Where a value does not map cleanly the **token** is questioned, never a twentieth exception invented.

**Each page is verified in a browser, not only by tests:** desktop and 375px, before and after, plus a
**row-count-per-screen check**. If a conversion costs rows per screen it is wrong and is reverted —
that check is the enforcement of the density decision.

**Two values stay hardcoded, with the reason in a comment:**

- `ops.html`'s **16px** inputs. iOS zooms the whole page for anything smaller and the packer loses
  their place mid-count. A device constraint, not a style choice.
- The printed documents' dark bands (`documents.py`, `generator.py`) — accounting convention, already
  guarded by `test_theme.py`.

## Phase 3 — The test that stops the drift returning

Without this, 19 font sizes return one feature at a time. The precedent already works: colour never
drifted because a test forbade it.

`tests/test_theme.py` gains a twin of the colour test:

```python
@pytest.mark.parametrize("template", TEMPLATES)
def test_no_template_hardcodes_a_size(template):
    """The colour test's twin, for the values that actually drifted.

    Measured before the refresh: 19 distinct font-size values across the templates, 22 paddings and
    11 radii — while box-shadow never drifted (tokenised from the start) and colour never drifted
    (this file already forbids it). Everything with a token AND a test held.
    """
```

Three properties keep it honest rather than irritating:

- **The failure names the offending value and the token to use**, so a refusal is actionable.
- **Exemptions are declared with a reason each, and the list is asserted in both directions** so it
  cannot silently grow — the pattern `test_unauthenticated_access.py` uses for its five public routes.
- **Parametrised per template**, so a new page fails on its own line.

Initial exemptions: `ops.html` (the 16px iOS inputs) and `login.html` (standalone — no nav, no tables).

## Files

Changed: `static/theme.css` · `templates/*.html` (9, one commit each) ·
`tests/test_theme.py` · `CLAUDE.md`

New: none.

## Verification

### Automated
- **All 2,144 tests green.**
- **The 10 existing `test_theme.py` guardrails**, especially `test_body_text_pairs_meet_wcag_aa` and
  `test_large_text_pairs_meet_at_least_aa_large`, which compute real contrast ratios. The opacity ramp
  changes how soft backgrounds are produced, so these are the tests that prove contrast did not drop.
- `test_the_status_colours_are_distinguishable_from_each_other` — the ramp must not make green and
  yellow tints converge.
- **The new size test fails against today's templates** — verified by running the check before writing
  it: **10 pages, 410 hardcoded values**. A guard that cannot fail proves nothing, so this was measured
  rather than assumed. It passes page by page as each conversion lands.
- `test_nav_consistency.py`, `test_local_dates.py`, `test_template_render_targets.py`,
  `test_ads_ui_pause.py` unchanged and green.

### Measured targets

| | Now | After |
|---|---|---|
| Portfolio header after 900px scroll | **-289px** (off screen) | pinned at top |
| Nav overflow at 375px | **212px** | 0 |
| Digit drift — Arial / SF Pro | **6px / 3px** | 0 / 0 |
| Distinct font sizes in templates | **19** | 6 |
| Distinct paddings | **22** | 6 |
| Rows per screen, portfolio | 21 | **21 — unchanged** |

The last row is the point: a refresh that costs no data.

### Manual, per converted page
Desktop and 375px, before and after. Confirm the row count per screen has not fallen, the sticky
header pins, and numeric columns align. On `ops.html` specifically, confirm iOS does not zoom on input
focus.

## Out of scope

The sidebar shell and breadcrumbs (rejected on horizontal cost). Skeleton loaders. Any change to
routes, numbers, or JavaScript behaviour. Dark mode — `theme.css` is deliberately one light theme and
nothing has asked for a second. The `.nav-links` markup itself: only its wrap behaviour changes, so
`test_nav_consistency.py` stays meaningful.
