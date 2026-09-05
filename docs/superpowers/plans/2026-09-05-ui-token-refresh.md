# UI token refresh — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the app a type, spacing and radius scale plus a derived colour ramp so every page shares one rhythm, and fix the three UI defects measured in a browser — headers that scroll away, numerals that misalign off Windows, and a nav that overflows a phone.

**Architecture:** Three phases. Phase 1 adds tokens and the three fixes to `static/theme.css` alone, so all 12 pages benefit with no template edits. Phase 2 converts templates to the tokens one commit per page, densest-first. Phase 3 adds a test banning new hardcoded sizes, mirroring the colour test that already prevents colour drift.

**Tech Stack:** Jinja2 templates · hand-written CSS custom properties · pytest (no CSS build step, no preprocessor, no framework)

**Spec:** `docs/superpowers/specs/2026-09-05-ui-token-refresh-design.md` (commit `c4ef0c7`)

## Global Constraints

- **Density is a hard constraint.** No table may lose rows per screen. Verified per page by measuring row height in a browser, not by inspection.
- **All 2,144 tests stay green**, including the 10 `test_theme.py` guardrails.
- **WCAG contrast is computed, not eyeballed.** `test_body_text_pairs_meet_wcag_aa` and `test_large_text_pairs_meet_at_least_aa_large` compute real ratios. The colour ramp must not reduce them.
- **No template may declare `:root`** or **hardcode a colour** — both already enforced by `test_theme.py`.
- **No behaviour changes.** No route, no number, no JavaScript logic. Presentation only.
- **Printed documents are untouched.** `app/shipment/documents.py` and `app/invoice/generator.py` keep their dark header bands (accounting convention, already guarded).
- Run tests with `venv/Scripts/python -m pytest`. Add `-p no:randomly` when asserting on a single test.
- Tint values are **0.10 (soft) / 0.16 (hover)** — not Materio's 0.08, which is near-invisible for our darker greens and reds on `#f6f7f9`.

## Facts verified before writing this plan

| Fact | Value | Why it matters |
|---|---|---|
| **`theme.css` loads AFTER the inline `<style>` in all 12 pages** | e.g. `portfolio.html`: style line 37, theme line 229 | Shared rules win at equal specificity. The whole plan depends on this. |
| `.nav-links{display:flex}` is declared per-template | **10 templates**, not in `theme.css` | The wrap fix must survive 10 local `display:flex` declarations — it does, by load order |
| `.nav-links a` IS in `theme.css` (line 112) | — | Type/colour for nav is already shared |
| `ops.html` z-index stack | `th.freeze:5`, `thead th:4`, `td.freeze:3` | The shared sticky rule must sit BELOW all of these |
| `ops.html` and `shipment.html` already set `thead th{position:sticky}` | — | Their local rules stay as deliberate overrides |
| Hardcoded sizes today | **10 pages, 410 values** | Proves the Phase 3 test bites |
| `--radius: 8px` already exists | `theme.css:88` | Most radius call sites need no change |
| Existing `*-soft` variables | **6** (accent, green, red, yellow, orange, blue) | Kept as aliases in Phase 1 so nothing breaks |
| `body` font-size | **15px** with a comment explaining why | NOT changed — it is a deliberate warehouse-phone decision |
| Portfolio measured | 33px rows, 30px header, 90 rows, 11 cols, 21 rows/screen | The density baseline to preserve |

---

### Task 1: The token layer and the three fixes (`theme.css` only)

Everything in this task lands in one file. No template changes, so nothing can regress visually except through `theme.css` itself.

**Files:**
- Modify: `static/theme.css` (`:root` ends at line 89; `thead th` at 158; `.nav-links a` at 112)
- Test: `tests/test_theme.py`

**Interfaces:**
- Consumes: nothing.
- Produces: CSS custom properties `--fs-xs|sm|md|lg|xl|2xl`, `--sp-1..6`, `--radius-sm|lg|pill`, `--{accent,green,red,yellow,orange,blue}-rgb`, `--tint-soft`, `--tint-hover`. Existing `--radius` and all six `--*-soft` variables keep their current values.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_theme.py`:

```python
# ─── The token scales ────────────────────────────────────────────────────────
#
# **`_vars()` cannot be reused here, and that is deliberate.** It filters to values matching
# `#rrggbb` — correct for the colour tests it was written for, and it would silently DROP every
# `--fs-*`, `--sp-*` and `--tint-*` value, so a test built on it would pass while asserting nothing.
# `_all_vars()` is its sibling for non-colour tokens. Note both return keys WITHOUT the `--` prefix.


def _all_vars(theme: str) -> dict[str, str]:
    """Every `:root` custom property, colour or not. Keys have no leading `--`."""
    block = re.search(r":root\s*\{(.*?)\}", theme, re.S)
    assert block, "theme.css has no :root block"
    return {
        name: value.strip()
        for name, value in re.findall(r"--([a-z0-9-]+)\s*:\s*([^;]+);", block.group(1))
    }



def test_the_type_scale_exists_and_is_ordered(theme):
    """**19 distinct font sizes were counted across the templates before this.**

    Including both `12.5px` and `13.5px` — not a decision, accumulated per-page guessing. Six tokens
    absorb all 19; the sizes were chosen by frequency, so `--fs-md: 13px` is what 62 declarations
    already used.

    Asserted as an ORDERED scale rather than as six independent values, because a scale whose steps
    are out of order is worse than no scale: it reads as licence to invent a seventh.
    """
    tokens = _all_vars(theme)
    sizes = []
    for name in ("fs-xs", "fs-sm", "fs-md", "fs-lg", "fs-xl", "fs-2xl"):
        assert name in tokens, f"{name} is missing from the type scale"
        sizes.append(float(tokens[name].replace("px", "")))
    assert sizes == sorted(sizes), f"the type scale is not ascending: {sizes}"
    assert sizes[0] >= 11, "nothing below 11px: these are read all day on a warehouse phone"


def test_the_spacing_scale_is_a_4px_base(theme):
    """22 distinct padding values became 6. A 4px base, which our top three (4/8/12) already used."""
    tokens = _all_vars(theme)
    for index, name in enumerate(("sp-1", "sp-2", "sp-3", "sp-4", "sp-5", "sp-6"), 1):
        assert name in tokens, f"{name} is missing from the spacing scale"
        assert float(tokens[name].replace("px", "")) == index * 4, (
            f"{name} breaks the 4px base — the point of a base is that it is predictable"
        )


def test_the_radius_scale_exists_and_keeps_the_existing_value(theme):
    """11 radii became 4. `--radius` KEEPS 8px so most call sites need no edit at all."""
    tokens = _all_vars(theme)
    assert tokens["radius"] == "8px", "changing --radius would silently restyle every existing card"
    for name in ("radius-sm", "radius-lg", "radius-pill"):
        assert name in tokens, f"{name} is missing from the radius scale"


def test_every_semantic_colour_has_an_rgb_channel_for_derived_tints(theme):
    """**Materio's one genuinely good idea, and the reason it is worth taking.**

    Today each colour needs a hand-picked partner (`--green` + `--green-soft`): six independent
    choices that can drift, plus a seventh to match by eye when Sponsored Display arrives. With a
    channel, every tint is DERIVED from the one hex.

    The `*-soft` variables are asserted to still exist, because Phase 1 must not break the templates
    that use them — they are retired per page in Phase 2, not all at once.
    """
    tokens = _all_vars(theme)
    for colour in ("accent", "green", "red", "yellow", "orange", "blue"):
        channel = f"{colour}-rgb"
        assert channel in tokens, f"{channel} is missing — soft tints cannot be derived without it"
        parts = theme[channel].split()
        assert len(parts) == 3, f"{channel} must be space-separated 'R G B' for rgb(... / alpha)"
        assert all(0 <= int(p) <= 255 for p in parts), f"{channel} is not a valid RGB triplet"
        assert f"{colour}-soft" in tokens, (
            f"--{colour}-soft was removed too early — templates still reference it in Phase 1"
        )

    for tint in ("tint-soft", "tint-hover"):
        assert tint in tokens, f"{tint} is missing"
        assert 0 < float(tokens[tint]) < 1, f"{tint} must be an alpha between 0 and 1"
    assert float(tokens["tint-soft"]) < float(tokens["tint-hover"]), (
        "the hover tint must be stronger than the resting one, or hover is invisible"
    )


def test_the_rgb_channel_matches_its_own_hex(theme):
    """A channel that disagrees with its hex is worse than no channel: the tint and the text it sits
    behind would come from two different colours, and nothing would look obviously broken."""
    tokens = _all_vars(theme)
    for colour in ("accent", "green", "red", "yellow", "orange", "blue"):
        hex_value = tokens[colour].lstrip("#")
        expected = [int(hex_value[i:i + 2], 16) for i in (0, 2, 4)]
        actual = [int(p) for p in tokens[f"{colour}-rgb"].split()]
        assert actual == expected, (
            f"--{colour}-rgb is {actual} but --{colour} is #{hex_value} = {expected}"
        )


# ─── The three measured fixes ────────────────────────────────────────────────


def test_dense_table_headers_are_sticky_below_the_frozen_column_layer():
    """**Measured on /portfolio-page: 3,766px tall, and after 900px of scroll the headers sat at
    -289px.** 90 rows and 11 money columns, with no way to tell ACOS from TACOS.

    `z-index: 2` is deliberate and not arbitrary. `ops.html` runs a four-layer stack —
    `th.freeze: 5` (the corner), `thead th: 4`, `td.freeze: 3` — so a shared value of 3 or higher
    would cover the frozen column where they intersect.
    """
    css = (REPO_ROOT / "static" / "theme.css").read_text(encoding="utf-8")
    block = re.search(r"thead th\s*\{([^}]*)\}", css, re.S)
    assert block, "thead th is no longer styled in theme.css"
    body = block.group(1)
    assert "position: sticky" in body, "dense table headers must stay put while the rows scroll"
    assert re.search(r"z-index:\s*2\b", body), (
        "thead th must be z-index 2 — below ops.html's frozen-column stack (3/4/5)"
    )
    assert "background:" in body, (
        "a sticky header needs an opaque background or the body rows show through it"
    )


def test_numeric_cells_use_tabular_figures():
    """**Measured drift over seven digits at our 13.5px table size:**

        Segoe UI / system-ui   0px   <- the owner's Windows box
        SF Pro, Inter, Roboto  3px   <- iPad, iPhone
        Arial, Helvetica       6px   <- Android, Linux

    So money columns align for the person who looks most and misalign in the warehouse on a tablet.
    Nearly recorded as a universal bug; measuring per font is what caught that it is conditional.

    Scoped to numeric cells and NEVER to `body`: tabular figures are slightly wider and worse for
    prose, the same distinction `.chan` already makes on the Portfolio tab.
    """
    css = (REPO_ROOT / "static" / "theme.css").read_text(encoding="utf-8")
    rule = re.search(r"([^}]*)\{[^}]*font-variant-numeric:\s*tabular-nums", css)
    assert rule, "no tabular-nums rule — money columns will not align off Windows"
    selector = rule.group(1)
    assert ".num" in selector, "the rule must target numeric cells (.num)"
    assert not re.search(r"(^|[\s,{])body\s*[,{]", selector), (
        "tabular-nums must not apply to body — it is worse for reading sentences"
    )


def test_the_nav_wraps_rather_than_overflowing_a_phone():
    """**Measured at 375px: 9 links totalling 587px in a nowrap row, overflowing by 212px.**

    The last item sat at x=747 and the whole document scrolled sideways by 424px. CLAUDE.md records
    this as known and unfixed.

    This rule lives in `theme.css` even though `display:flex` is declared in 10 TEMPLATES, because
    `theme.css` loads after every inline `<style>` block (verified across all 12 pages) and so wins at
    equal specificity.
    """
    css = (REPO_ROOT / "static" / "theme.css").read_text(encoding="utf-8")
    block = re.search(r"\.nav-links\s*\{([^}]*)\}", css, re.S)
    assert block, ".nav-links is not styled in theme.css, so the wrap fix has no home"
    assert "flex-wrap: wrap" in block.group(1), (
        "9 nav links in a nowrap row overflow a 375px phone by 212px"
    )
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `venv/Scripts/python -m pytest tests/test_theme.py -q -p no:randomly`
Expected: **8 failures**, the first being `KeyError: '--fs-xs'` or an assertion that it is missing.

> If the `theme` fixture does not expose variables as a dict, read the existing
> `test_the_page_background_is_light` (line 131) to see its exact shape and adapt these tests to it
> before continuing. Do not change the fixture.

- [ ] **Step 3: Add the scales to `:root`**

In `static/theme.css`, replace the single line `  --radius: 8px;` (line 88) with:

```css
  /* ── Radius ─────────────────────────────────────────────────────────────
     11 distinct radii were counted across the templates (8, 6, 10, 20, 99, 4,
     5, 7, 12...). Four tokens absorb them. `--radius` KEEPS its 8px, so the
     existing call sites need no edit. */
  --radius-sm: 4px;
  --radius: 8px;
  --radius-lg: 12px;
  --radius-pill: 999px;

  /* ── Type scale ─────────────────────────────────────────────────────────
     **19 distinct font sizes were counted across the templates**, including
     both 12.5px and 13.5px — accumulated per-page guessing, not a decision.
     Six tokens absorb all 19, and the values were chosen by FREQUENCY rather
     than invented: 13px had 62 uses, 12px 49, 11px 43.

     `body` stays at 15px and is deliberately not on this scale — see its own
     comment below. These are for the dense grids. */
  --fs-xs:  11px;   /* ALL-CAPS column headers */
  --fs-sm:  12px;   /* dense cells, tags */
  --fs-md:  13px;   /* the real body default of every table */
  --fs-lg:  14px;   /* card titles, nav */
  --fs-xl:  16px;   /* KPI figures */
  --fs-2xl: 20px;   /* the one headline figure on a KPI strip */

  /* ── Spacing ────────────────────────────────────────────────────────────
     22 distinct padding values became 6, on a 4px base. Not an imported
     convention: 4, 8 and 12 were already our three most common values. */
  --sp-1: 4px;
  --sp-2: 8px;
  --sp-3: 12px;
  --sp-4: 16px;
  --sp-5: 20px;
  --sp-6: 24px;

  /* ── Derived tints ──────────────────────────────────────────────────────
     **Each colour carries its own RGB channel so soft variants are COMPUTED
     rather than hand-picked.** Today every colour needs a matching pastel
     chosen by eye (`--green` + `--green-soft`): six independent choices that
     can drift apart, plus a seventh to match when Sponsored Display arrives.

         background: rgb(var(--green-rgb) / var(--tint-soft));
         color: var(--green);

     Adapted from Materio's 0.08/0.16/0.24/0.32/0.38 ramp, with two changes.
     Only two steps, because only two are used. And 0.10 rather than 0.08 for
     the resting tint: their ramp is tuned to a purple hue on #F4F5FA, and 8%
     of our darker green (#146c34) is close to invisible on #f6f7f9. The exact
     figures are settled by this file's WCAG contrast tests, not by taste. */
  --accent-rgb: 29 78 216;
  --green-rgb:  20 108 52;
  --red-rgb:    198 40 40;
  --yellow-rgb: 161 98 7;
  --orange-rgb: 194 65 12;
  --blue-rgb:   29 78 216;
  --tint-soft:  0.10;
  --tint-hover: 0.16;
```

> **The six `--*-soft` variables above are left exactly as they are.** Ten templates reference them,
> and removing them here would break every page in the same commit that introduces the ramp. They are
> retired per page in Phase 2 and only then deleted.

- [ ] **Step 4: Make dense table headers sticky**

In `static/theme.css`, replace the `thead th` block (line 158) with:

```css
thead th {
  background: var(--surface2);
  color: var(--text-muted);
  border-bottom: 1px solid var(--border-strong);
  /* 11px: these are ALL-CAPS abbreviations, which are harder to read than
     sentence case at the same size. */
  font-size: var(--fs-xs);
  /* **Measured: /portfolio-page is 3,766px tall and after 900px of scroll the
     headers sat at -289px** — 90 rows, 11 money columns, and no way to tell
     ACOS from TACOS. The opaque background above is what makes this safe; a
     transparent sticky header shows the body rows through it.

     `z-index: 2` is deliberate. `ops.html` runs a four-layer stack —
     `th.freeze: 5` (the corner cell), `thead th: 4`, `td.freeze: 3` — so a
     shared value of 3 or more would cover the frozen column at the join.
     ops.html and shipment.html keep their own higher values, which now read
     as deliberate overrides of a shared default. */
  position: sticky;
  top: 0;
  z-index: 2;
}
```

- [ ] **Step 5: Add tabular figures and the nav wrap**

In `static/theme.css`, immediately after the `td { font-size: 13px; }` rule (line 183):

```css
/* **Money columns must align digit-for-digit, and whether they do depends on the
   platform.** Measured drift across seven digits at 13.5px:

       Segoe UI / system-ui   0px   <- the owner's Windows box
       SF Pro, Inter, Roboto  3px   <- iPad, iPhone
       Arial, Helvetica       6px   <- Android, Linux

   So they align for the person who looks most and misalign in the warehouse on
   a tablet. Deliberately NOT on `body`: tabular figures are slightly wider and
   worse for prose, the same numbers-versus-prose split `.chan` already makes. */
td.num, th.num, .kpi-value { font-variant-numeric: tabular-nums; }
```

And add a `.nav-links` block immediately before the existing `.nav-links a` rule (line 112):

```css
/* **Measured at 375px: nine links totalling 587px in a nowrap row, overflowing
   by 212px** — the last item at x=747, and the whole document scrolling sideways
   by 424px. CLAUDE.md records it as known and unfixed.

   This lives here even though `display:flex` is declared in ten TEMPLATES,
   because theme.css loads AFTER every inline <style> block (verified across all
   12 pages), so it wins at equal specificity. Only the wrap behaviour is shared;
   each page keeps its own gap and margin. */
.nav-links {
  flex-wrap: wrap;
  row-gap: var(--sp-1);
}
```

- [ ] **Step 6: Run the theme tests**

Run: `venv/Scripts/python -m pytest tests/test_theme.py -q -p no:randomly`
Expected: PASS, all of them — including the 10 pre-existing guardrails.

> **If a WCAG contrast test fails here, that is the tint values talking, not a bug in the test.**
> Raise `--tint-soft` toward 0.12 rather than lowering it, and re-run. Do not weaken the assertion.

- [ ] **Step 7: Run the full suite**

Run: `venv/Scripts/python -m pytest -q`
Expected: 2,144 passed, 17 skipped.

- [ ] **Step 8: Verify in a browser — this is the step that matters**

```bash
# Start the app with preview_start (name: tracker), sign in with APP_PASSWORD from .env
```

On `/portfolio-page`, check all four and record the numbers:

1. **Row height is unchanged at ~33px** and rows-per-screen is still 21. This is the density constraint; a change here means something in Task 1 leaked into layout.
2. **The header pins.** Scroll 900px and confirm `thead th`'s top is 0, not -289.
3. **Numerals align.** No visual change on Windows (already 0px drift) — the fix is for other platforms.
4. **At 375px the nav wraps** onto multiple rows and the page no longer scrolls sideways.

Then `/ops-page`: confirm the frozen first column still sits **above** the scrolling body cells and **below** the corner header cell. This is the z-index claim, and ops is the page that would expose it.

- [ ] **Step 9: Commit**

```bash
git add static/theme.css tests/test_theme.py
git commit -m "feat(ui): a type/spacing/radius scale, derived tints, and three measured fixes

19 font sizes, 22 paddings and 11 radii existed across the templates; shadows
never drifted because they were tokenised, and colour never drifted because a
test forbids it. This adds the missing scales.

Fixes, all measured in a browser: portfolio headers sat at -289px after 900px of
scroll; digits drift 6px on Android and 3px on iPad but 0 on Windows, so columns
misalign only for the warehouse tablet; the nav overflowed a 375px phone by 212px.

Tints are derived from an RGB channel per colour rather than hand-picked pastels.
The six --*-soft variables stay as aliases until each page is converted."
```

---

### Task 2: Convert `portfolio.html`

The widest table (11 columns), the most drift (64 hardcoded sizes), and the page whose headers the sticky fix was measured on.

**Files:**
- Modify: `templates/portfolio.html` (inline `<style>` from line 37)

**Interfaces:**
- Consumes: every token from Task 1.
- Produces: nothing new. A precedent for Tasks 3–8.

- [ ] **Step 1: Record the before state**

In a browser on `/portfolio-page`, note and keep these four numbers:

```
row height px, rows per screen, table width px, distinct font sizes in the file
```

Get the last one with:

```bash
grep -oE "font-size:\s*[0-9.]+px" templates/portfolio.html | sort -u
```

- [ ] **Step 2: Replace sizes with type tokens**

In `templates/portfolio.html`'s `<style>` block, map every hardcoded `font-size` to a token. The full mapping, which is the same for every template in this plan:

| Hardcoded | Token |
|---|---|
| `10px`, `11px` | `var(--fs-xs)` |
| `12px`, `12.5px` | `var(--fs-sm)` |
| `13px`, `13.5px` | `var(--fs-md)` |
| `14px` | `var(--fs-lg)` |
| `15px`, `16px` | `var(--fs-xl)` |
| `18px`, `20px`, `22px` | `var(--fs-2xl)` |

`12.5px` and `13.5px` fold into the neighbouring token — the half-pixel is not perceptible and having a
scale is the point.

- [ ] **Step 3: Replace padding and radius**

| Hardcoded padding | Token |
|---|---|
| `2px`, `3px`, `4px`, `5px` | `var(--sp-1)` |
| `6px`, `7px`, `8px`, `9px` | `var(--sp-2)` |
| `10px`, `11px`, `12px`, `14px` | `var(--sp-3)` |
| `16px`, `18px` | `var(--sp-4)` |
| `20px` | `var(--sp-5)` |
| `24px` and above | `var(--sp-6)` |

| Hardcoded radius | Token |
|---|---|
| `4px`, `5px` | `var(--radius-sm)` |
| `6px`, `7px`, `8px` | `var(--radius)` |
| `10px`, `12px` | `var(--radius-lg)` |
| `20px`, `99px`, `999px` | `var(--radius-pill)` |

**Multi-value shorthands keep their shape:** `padding: 4px 8px` becomes
`padding: var(--sp-1) var(--sp-2)`, not a single token.

- [ ] **Step 4: Switch soft backgrounds to derived tints**

Replace each `background: var(--X-soft)` with the derived form:

```css
/* before */
background: var(--green-soft);
/* after */
background: rgb(var(--green-rgb) / var(--tint-soft));
```

Row-hover rules move to `var(--tint-hover)` instead:

```css
tbody tr:hover { background: rgb(var(--accent-rgb) / var(--tint-hover)); }
```

- [ ] **Step 5: Mark numeric cells**

The `tabular-nums` rule from Task 1 targets `td.num` / `th.num`. Confirm the money, percentage and unit
columns carry `class="num"`:

```bash
grep -c 'class="num"' templates/portfolio.html
```

If a money column renders without it, add `num` to that cell's class list. **Do not** add `num` to the
product-name column or to the `.chan` merchant/FBA sentence — those are prose.

- [ ] **Step 6: Run the guardrails**

```bash
venv/Scripts/python -m pytest tests/test_theme.py tests/test_local_dates.py tests/test_nav_consistency.py tests/test_template_render_targets.py -q -p no:randomly
```
Expected: PASS. `test_no_template_hardcodes_a_colour` is the one that catches a mistyped `rgb(...)`.

- [ ] **Step 7: Verify in a browser, and compare against Step 1**

On `/portfolio-page`:

- **Row height and rows-per-screen match Step 1.** If rows-per-screen dropped, a padding token was
  mapped too generously — find it and step down. **This is the density constraint and it is not
  negotiable.**
- Verdict chips still read as tinted, not flat grey — a broken `rgb(var(--x-rgb) / ...)` renders as
  transparent, which looks plausible at a glance.
- Expand a parent row: the `.chan` sentence still wraps while numbers stay on one line.
- Sort a column by clicking and by keyboard (Enter/Space) — unchanged behaviour.
- At 375px, no sideways page scroll.

- [ ] **Step 8: Commit**

```bash
git add templates/portfolio.html
git commit -m "refactor(ui): portfolio.html onto the shared token scales

64 hardcoded sizes replaced by tokens; soft backgrounds now derived from the RGB
channels. Row height and rows-per-screen verified unchanged in a browser."
```

---

### Task 3: Convert `ops.html`

**Second on purpose, moved up from the spec's ordering.** This is the warehouse tablet — the device
where the 6px digit drift actually bites — and it holds the most intricate z-index stack, so proving
the shared sticky rule here de-risks every remaining page.

**Files:**
- Modify: `templates/ops.html` (inline `<style>` from line 52; 232 lines, 38 hardcoded sizes)

**Interfaces:**
- Consumes: Task 1's tokens.
- Produces: the confirmed precedent that a page may keep higher `z-index` values as deliberate overrides.

- [ ] **Step 1: Record the before state**

On `/ops-page` (sign in with `OPS_PASSWORD` or an admin account), note: row height, rows per screen,
and that the frozen first column overlaps the scrolling cells correctly.

- [ ] **Step 2: Apply the same three mappings**

Use the identical font-size, padding and radius tables from Task 2, Steps 2–3.

- [ ] **Step 3: Keep the four-layer z-index stack, and say why**

`ops.html` line 167 already sets `thead th{...position:sticky;top:0;z-index:4}` and line 212–213 set the
frozen column. **Leave every z-index value as it is** and add this comment above the `thead th` rule:

```css
/* z-index 4 deliberately OVERRIDES theme.css's shared `thead th{z-index:2}`.
   This page has a frozen first column, so it needs four layers rather than two:
   th.freeze (5) > thead th (4) > td.freeze (3) > body cells. The shared default
   is intentionally the lowest of these so it can never cover the frozen column
   at the join. */
```

- [ ] **Step 4: Keep the 16px inputs, and say why**

`ops.html`'s inputs are 16px on purpose. **Do not map those four declarations to a token** — but do
convert this page's other 22 font-sizes, because the Phase 3 guard exempts the VALUE `16px`, not this
page. Confirm the existing comment explains it; if not, add:

```css
/* 16px is NOT on the type scale, deliberately: iOS zooms the whole page for any
   input smaller than this, and the packer loses their place mid-count. A device
   constraint, not a style choice. */
```

- [ ] **Step 5: Run the guardrails**

```bash
venv/Scripts/python -m pytest tests/test_theme.py tests/test_shipment_admin_ui.py -q -p no:randomly
```
Expected: PASS.

- [ ] **Step 6: Verify in a browser at tablet width**

Resize to 768px and check:

- The frozen first column still sits above the scrolling cells; the corner header cell sits above both.
- The sticky header pins when scrolling a long packing list.
- **Numeric columns align** — this is the platform where it mattered.
- Tapping a quantity input does **not** zoom the page.
- Row height matches Step 1.

- [ ] **Step 7: Commit**

```bash
git add templates/ops.html
git commit -m "refactor(ui): ops.html onto the token scales, keeping its z-index stack and 16px inputs

The four-layer stack (th.freeze 5 > thead th 4 > td.freeze 3) now reads as a
deliberate override of theme.css's shared z-index 2. The 16px inputs stay off the
type scale because iOS zooms the page below that and the packer loses their place."
```

---

### Task 4: Convert `ads.html`

The money-spending screen, and the 1,425-row preview where density matters most.

**Files:**
- Modify: `templates/ads.html` (inline `<style>` from line 35; 137 lines, 51 hardcoded sizes)

**Interfaces:**
- Consumes: Task 1's tokens.
- Produces: nothing new.

- [ ] **Step 1: Record the before state**

On `/ads-page`, run a preview (`spend > 1000`, 30d) and note row height and rows-per-screen **in the
preview table**, which is the dense one.

- [ ] **Step 2: Apply the three mappings**

Identical tables to Task 2, Steps 2–3.

- [ ] **Step 3: Switch the tag and banner tints**

`ads.html` has verdict-style tags and banners using `--*-soft`. Convert each to
`rgb(var(--X-rgb) / var(--tint-soft))`, and the `.tag.changed` / state pills likewise.

**The new pause-run cells added last week** (`serving → PAUSED`) share the bid cells' classes, so they
need no separate treatment — but confirm both render after conversion.

- [ ] **Step 4: Run the guardrails plus the ads UI tests**

```bash
venv/Scripts/python -m pytest tests/test_theme.py tests/test_ads_ui_pause.py tests/test_local_dates.py -q -p no:randomly
```
Expected: PASS. `test_ads_ui_pause.py` asserts on the template's script and markup, so it catches an
edit that breaks the state controls.

- [ ] **Step 5: Verify in a browser, both run kinds**

- A **bid** preview renders `₹14.00 → ₹12.60` under a `Bid` header.
- A **pause** preview renders `serving → PAUSED` under a `State` header, with the Suggested column
  dashed.
- Zero JS errors in the console.
- Row height and rows-per-screen match Step 1.

- [ ] **Step 6: Commit**

```bash
git add templates/ads.html
git commit -m "refactor(ui): ads.html onto the token scales

51 hardcoded sizes replaced. Both run kinds verified in a browser: a bid preview
still shows the bid pair, a pause preview the state pair."
```

---

### Task 5: Convert `shipment.html`

The largest inline block (257 lines, 79 hardcoded sizes) and read by two roles.

**Files:**
- Modify: `templates/shipment.html` (inline `<style>` from line 24)

**Interfaces:**
- Consumes: Task 1's tokens.
- Produces: nothing new.

- [ ] **Step 1: Record the before state**

On `/shipment-page` with an active plan, note row height and rows-per-screen on the plan-items table.

- [ ] **Step 2: Apply the three mappings**

Identical tables to Task 2, Steps 2–3.

- [ ] **Step 3: Keep this page's own sticky header as an override**

`shipment.html` already sets `thead th{...sticky...}`. Leave its z-index as it is and add:

```css
/* Overrides theme.css's shared `thead th{z-index:2}` for this page's own
   stacking needs. The shared default is the lowest value on purpose. */
```

- [ ] **Step 4: Convert the day-status tints**

The day cards use `--yellow-soft` / `--orange-soft` for held days and `--green-soft` for verified.
Convert each to the derived form. **Held versus verified must stay visually distinct** — that
distinction gates a GST invoice.

- [ ] **Step 5: Run the guardrails**

```bash
venv/Scripts/python -m pytest tests/test_theme.py tests/test_shipment_admin_ui.py tests/test_template_render_targets.py -q -p no:randomly
```
Expected: PASS.

- [ ] **Step 6: Verify in a browser**

- Held days are still visibly amber and verified days green, side by side.
- The plan table's sticky header pins.
- Row height matches Step 1.
- The invoice-bar renders (`#invoice-bar` exists — `test_template_render_targets.py` covers the id, but
  look at it).

- [ ] **Step 7: Commit**

```bash
git add templates/shipment.html
git commit -m "refactor(ui): shipment.html onto the token scales

79 hardcoded sizes replaced. Held-versus-verified day colours verified still
distinct in a browser, since that distinction gates a GST invoice."
```

---

### Task 6: Convert `orders.html` and `index.html`

Two pages in one task: both are KPI-strip-plus-table with no unusual stacking, so a reviewer would
accept or reject them together.

**Files:**
- Modify: `templates/orders.html` (line 50; 135 lines, 48 sizes)
- Modify: `templates/index.html` (line 7; 124 lines, 42 sizes)

**Interfaces:**
- Consumes: Task 1's tokens.
- Produces: nothing new.

- [ ] **Step 1: Record the before state**

`/orders-page` (all three tabs) and `/` — row heights and rows-per-screen.

- [ ] **Step 2: Apply the three mappings to both files**

Identical tables to Task 2, Steps 2–3.

- [ ] **Step 3: Put the KPI figures on `--fs-2xl` and add `.kpi-value`**

Both pages have a KPI strip. Give each headline number `font-size: var(--fs-2xl)` and confirm the
element carries `class="kpi-value"` so Task 1's `tabular-nums` rule reaches it:

```bash
grep -c "kpi-value" templates/orders.html templates/index.html
```

If the class is absent, add it to the value element — not to its label.

- [ ] **Step 4: Run the guardrails**

```bash
venv/Scripts/python -m pytest tests/test_theme.py tests/test_local_dates.py tests/test_nav_consistency.py -q -p no:randomly
```
Expected: PASS.

- [ ] **Step 5: Verify in a browser**

- Orders: all three tabs render; the KPI strip stays visible across tab switches; typing in a
  packed-units box does not re-render the row (caret preserved).
- Dashboard: price charts still render (Chart.js is untouched, but confirm).
- Row heights match Step 1 on both.

- [ ] **Step 6: Commit**

```bash
git add templates/orders.html templates/index.html
git commit -m "refactor(ui): orders.html and index.html onto the token scales

90 hardcoded sizes replaced across the two KPI-strip pages. KPI figures now carry
.kpi-value so they get tabular figures."
```

---

### Task 7: Convert the remaining six templates

`invoice`, `users`, `projections`, `pricing`, `no_access`, `login` — 126 hardcoded sizes between them,
none with unusual stacking or density.

**Files:**
- Modify: `templates/invoice.html` (25 sizes) · `templates/users.html` (38) ·
  `templates/projections.html` (33) · `templates/pricing.html` (23) ·
  `templates/no_access.html` (7) · `templates/login.html` (standalone)

**Interfaces:**
- Consumes: Task 1's tokens.
- Produces: a fully converted template set, which Task 8's test then locks.

- [ ] **Step 1: Apply the three mappings to each file**

Identical tables to Task 2, Steps 2–3.

- [ ] **Step 2: Remove `invoice.html`'s now-redundant local rule**

`invoice.html:38` has `.weight-note td.num { text-align: right; font-variant-numeric: tabular-nums; }`
— the app's only pre-existing use. The `tabular-nums` half is now inherited from `theme.css`, so drop
it and keep the alignment:

```css
.weight-note td.num { text-align: right; }
```

- [ ] **Step 3: Run the guardrails**

```bash
venv/Scripts/python -m pytest tests/test_theme.py tests/test_local_dates.py tests/test_invoice_save.py -q -p no:randomly
```
Expected: PASS. `test_invoice_save.py` guards the GST number sequence — untouched here, but it is the
suite that must never go red.

- [ ] **Step 4: Verify in a browser**

- `/invoice`: parse a shipment file, confirm the totals table aligns and the weight note still reads
  correctly.
- `/users-page`: the permissions grid renders.
- `/projections-page`: the Ideal WH column keeps its tint and the table default-sorts by it.
- `/login` and `/no-access` at 375px.

- [ ] **Step 5: Commit**

```bash
git add templates/invoice.html templates/users.html templates/projections.html templates/pricing.html templates/no_access.html templates/login.html
git commit -m "refactor(ui): the remaining six templates onto the token scales

126 hardcoded sizes replaced. invoice.html's local tabular-nums rule is dropped —
theme.css now provides it for every numeric cell."
```

---

### Task 8: The test that stops the drift returning

Without this, 19 font sizes come back one feature at a time. The precedent already works: colour never
drifted because `test_no_template_hardcodes_a_colour` forbids it.

**Files:**
- Modify: `tests/test_theme.py`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: Tasks 2–7 (every template converted).
- Produces: `test_no_template_hardcodes_a_size`.

- [ ] **Step 1: Write the test**

Append to `tests/test_theme.py`:

```python
#: Whole pages allowed to hold raw sizes, with the reason. **Asserted in both directions below**, so
#: the list cannot silently grow — the pattern `test_unauthenticated_access.py` uses for its five
#: public routes.
SIZE_EXEMPT = {
    "login.html": "standalone page — no nav, no tables, nothing shared to drift against",
}

#: **A specific VALUE that is exempt everywhere, rather than a page that is exempt entirely.**
#:
#: `ops.html` needs 16px inputs because iOS zooms the whole page for anything smaller and the packer
#: loses their place mid-count. Exempting the PAGE for that reason would have left its other **26
#: font-size declarations** unguarded to protect **4** legitimate ones — an 85% loophole on the very
#: page most likely to be edited under time pressure. Exempting the value keeps the other 22 honest.
SIZE_EXEMPT_VALUES = {
    "16px": "iOS zooms the page for any input below 16px, and the packer loses their place mid-count",
}


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_template_hardcodes_a_size(template):
    """**The colour test's twin, for the values that actually drifted.**

    Counted before the refresh: **19 distinct font-size values** across the templates (13, 12, 12.5,
    11, 14, 10, 15, 16, 13.5...), **22 paddings** and **11 radii** — while `box-shadow` never drifted,
    because it was tokenised from the start, and colour never drifted, because this file forbids it.

    **Everything with a token AND a test held. Everything without one drifted.** That is the whole
    argument for this test, and it is why the fix is not merely "use the tokens once".

    Verified to bite before it was written: run against the unconverted templates it failed on 10
    pages covering 410 values. A guard that cannot fail proves nothing.
    """
    if template.name in SIZE_EXEMPT:
        pytest.skip(f"{template.name}: {SIZE_EXEMPT[template.name]}")

    body = template.read_text(encoding="utf-8")
    # Comments are prose; a size mentioned in one is harmless. Same stripping as the colour test.
    for pattern in (r"\{#.*?#\}", r"<!--.*?-->", r"/\*.*?\*/"):
        body = re.sub(pattern, " ", body, flags=re.S)

    offenders = []
    for prop, token in (("font-size", "--fs-*"), ("border-radius", "--radius*")):
        for match in re.findall(rf"{prop}:\s*([0-9.]+)px", body):
            if f"{match}px" in SIZE_EXEMPT_VALUES:
                continue
            offenders.append(f"{prop}: {match}px (use {token})")

    assert not offenders, (
        f"{template.name} hardcodes {len(offenders)} size(s): {sorted(set(offenders))[:6]} — "
        f"use a theme token so the next change reaches them"
    )


def test_the_size_exemption_lists_are_exactly_what_is_exempt():
    """Asserted in BOTH directions so an exemption cannot be added silently.

    A one-way check lets someone quiet a failure by appending a filename; this makes the lists
    themselves the thing under review.
    """
    names = {p.name for p in TEMPLATES}
    for exempt in SIZE_EXEMPT:
        assert exempt in names, f"{exempt} is exempted but no longer exists"
    assert set(SIZE_EXEMPT) == {"login.html"}, (
        "the page-exemption list changed — every entry needs a reason a reviewer accepts"
    )
    assert set(SIZE_EXEMPT_VALUES) == {"16px"}, (
        "the value-exemption list changed — a new exempt value is a new hole in this guard"
    )


def test_the_16px_exemption_still_protects_only_what_it_is_for():
    """The exemption is for INPUTS on the packing screen, not a general licence.

    Without this, `16px` becomes a free size anywhere — and the reason it is allowed (iOS zoom on a
    focused input) would stop being visible to whoever next reaches for it.
    """
    ops = (REPO_ROOT / "templates" / "ops.html").read_text(encoding="utf-8")
    assert re.search(r"font-size:\s*16px", ops), (
        "ops.html no longer uses 16px — retire the value exemption rather than leaving it open"
    )
    assert "zoom" in ops.lower(), (
        "the 16px rule in ops.html must carry its reason, or a later reader will 'tidy' it onto the "
        "type scale and reintroduce the iOS zoom"
    )
```

- [ ] **Step 2: Run it**

Run: `venv/Scripts/python -m pytest tests/test_theme.py -q -p no:randomly`
Expected: PASS, with 1 skip (`login.html`). `ops.html` is NOT skipped — only its `16px` value is exempt.

> **If a converted page fails here, that is the test doing its job** — a hardcoded value was missed.
> Fix the template, never the test. Do not add to `SIZE_EXEMPT` to make it green.

- [ ] **Step 3: Prove it still bites**

The test is worthless if it cannot fail. Verify:

```bash
# Temporarily reintroduce a raw size, confirm the test catches it, then revert.
venv/Scripts/python - <<'PY'
import pathlib
p = pathlib.Path("templates/portfolio.html")
original = p.read_text(encoding="utf-8")
p.write_text(original.replace("font-size: var(--fs-md)", "font-size: 12.5px", 1), encoding="utf-8")
PY
venv/Scripts/python -m pytest tests/test_theme.py -q -p no:randomly -k "hardcodes_a_size and portfolio"
# Expected: FAIL naming "font-size: 12.5px (use --fs-*)"
git checkout templates/portfolio.html
```

- [ ] **Step 4: Record it in CLAUDE.md**

Add to `CLAUDE.md`, after the paragraph about `static/theme.css` owning colour:

```markdown
**Sizes are tokenised too, and for the reason colour already was.** Counted before the refresh: **19
distinct font-size values** across the templates (including both `12.5px` and `13.5px`), **22
paddings** and **11 radii** — while `box-shadow` never drifted, because it was tokenised from the
start, and colour never drifted, because a test forbade it. **Everything with a token AND a test held;
everything without one drifted.** `theme.css` now carries `--fs-*`, `--sp-*` and `--radius-*`, and
`test_no_template_hardcodes_a_size` keeps them honest.

> **The exemption is a VALUE, not a page, and that distinction was a real flaw caught in review.**
> `ops.html` needs 16px inputs because iOS zooms the page below that and the packer loses their place
> mid-count — but exempting the whole PAGE for that reason would have left its other **26 font-size
> declarations unguarded to protect 4**, an 85% loophole on the page most likely to be edited under
> time pressure. So `16px` is exempt everywhere and `ops.html` is converted like any other page. Only
> `login.html` is page-exempt (standalone: no nav, no tables).

**Soft colours are DERIVED, not hand-picked.** Each semantic colour carries an RGB channel, so a tint
is `rgb(var(--green-rgb) / var(--tint-soft))` rather than a matching pastel chosen by eye. Six
independent choices became one per colour, and adding Sponsored Display later is one line. The tint
steps are 0.10 and 0.16 — Materio's ramp starts at 0.08, which is near-invisible for our darker greens
and reds on `#f6f7f9`; the figures were settled by this file's WCAG contrast tests, not by preference.

> **Three UI defects were found by measuring in a browser rather than reading the CSS**, and one of
> them was conditional in a way that reading could not reveal. `/portfolio-page` is 3,766px tall and
> its headers sat at **-289px** after 900px of scroll. The nav overflowed a 375px phone by **212px**
> (recorded here as known-and-unfixed for months). And numerals drifted **6px on Android/Linux and 3px
> on iPad but 0px on Windows** — so money columns aligned for the owner and misaligned on the warehouse
> tablet. Any of the three would have read as fine from the source.
```

- [ ] **Step 5: Run everything**

```bash
venv/Scripts/python -m pytest -q
venv/Scripts/python scripts/mutate_ads_state.py
```
Expected: 2,146+ passed (the new tests), 19 skipped; `all 15 mutations caught`.

- [ ] **Step 6: Commit**

```bash
git add tests/test_theme.py CLAUDE.md
git commit -m "test(ui): ban new hardcoded sizes, mirroring the colour test

Colour never drifted because a test forbade it; sizes drifted to 19 font sizes,
22 paddings and 11 radii because nothing did. Two exemptions with a reason each,
and the list is asserted in both directions so it cannot grow silently.

Verified to bite: against the unconverted templates it failed on 10 pages
covering 410 values."
```

---

## Final verification

- [ ] **Full suite**: `venv/Scripts/python -m pytest -q` → 0 failures
- [ ] **All three mutation harnesses** still green (15 / 4 / 14) — none touch CSS, so this confirms no collateral damage
- [ ] **Every page at desktop and 375px**, with no sideways page scroll anywhere
- [ ] **Density audit** — the constraint this whole plan is bounded by:

```javascript
// Run in the browser console on each table page. Record and compare with Task N Step 1.
(() => {
  const rows = [...document.querySelectorAll("tbody tr")];
  const h = rows.slice(0, 20).map(r => r.getBoundingClientRect().height);
  const avg = h.reduce((a, b) => a + b, 0) / h.length;
  return { page: location.pathname, avgRowHeight: +avg.toFixed(1),
           rowsPerScreen: Math.floor(window.innerHeight / avg),
           stickyHeader: getComputedStyle(document.querySelector("thead th")).position };
})()
```

Expected on `/portfolio-page`: `avgRowHeight ≈ 33`, `rowsPerScreen 21`, `stickyHeader "sticky"`.
**A lower `rowsPerScreen` than the Task 2 baseline fails this plan** and the offending padding token
must be stepped down.

- [ ] **Deploy** — no migration in this change, so the standard path applies:

```bash
ssh -i "<key>" ubuntu@13.233.144.148
cd /opt/amazon-tracker && bash deploy/update-ec2.sh   # answer y to the hsn_master.json stash
```

- [ ] **Manual, on production**: open every tab, confirm the sticky headers pin, and check `/ops-page` on the actual warehouse tablet if one is to hand — that is the device the numeral fix was for.

## Out of scope

The sidebar shell and breadcrumbs (rejected: ~260px of horizontal room from tables that already need
1,193px, and the free template's icon-collapse is paid-only). Skeleton loaders — our long jobs already
report true percentages, and a skeleton would imply "nearly there" where a percentage tells the truth.
Dark mode. Any change to routes, numbers, or JavaScript behaviour. The `--shadow*` tokens, which never
drifted and stay as they are.
