"""The shared light theme: one stylesheet, no per-page drift, readable contrast.

The request was "the UI of the app is very dark. make it light easy on the eyes.
and easy for a normal 10th pass user to use."

"Easy on the eyes" is the part a test can actually defend, so the contrast ratios
here are computed rather than eyeballed — a colour that looks fine on this
monitor can be unreadable on a warehouse phone in daylight, and pale-text-on-pale-
tint was a real failure in the first draft of the conversion (#bbf7d0 on a pale
green banner is close to invisible).

The structural tests matter as much. Colour lived in seven separate inline
``:root`` blocks, which is how the app drifted into looking like seven apps; a
template that re-declares its own would silently take back control of colour on
that page only, and nothing else would notice.

Printed documents are deliberately NOT part of this. app/shipment/documents.py
and app/invoice/generator.py use a dark header band on a white page — the
accounting-document convention, correct on paper whatever the screen does. A test
below asserts they were left alone, because "make it lighter" applied wholesale
would have bleached the invoice headers too.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = sorted(p for p in (REPO_ROOT / "templates").glob("*.html"))
THEME = REPO_ROOT / "static" / "theme.css"

#: nav.html is a fragment included by the others — it has no <head> of its own,
#: so it neither can nor should link the stylesheet.
FRAGMENTS = {"nav.html"}
PAGES = [p for p in TEMPLATES if p.name not in FRAGMENTS]

pytestmark = pytest.mark.regression


@pytest.fixture(scope="module")
def theme() -> str:
    return THEME.read_text(encoding="utf-8")


def _relative_luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]

    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(foreground: str, background: str) -> float:
    """WCAG 2.1 contrast ratio. 4.5:1 is the AA threshold for body text."""
    a, b = _relative_luminance(foreground), _relative_luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def _vars(theme: str) -> dict[str, str]:
    """The `:root` custom properties whose values are literal colours."""
    block = re.search(r":root\s*\{(.*?)\}", theme, re.S)
    assert block, "theme.css has no :root block"
    found = {}
    for name, value in re.findall(r"--([a-z0-9-]+)\s*:\s*([^;]+);", block.group(1)):
        value = value.strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            found[name] = value
    return found


# ─── One stylesheet, linked everywhere ───────────────────────────────────────

def test_the_theme_stylesheet_exists():
    assert THEME.is_file(), "static/theme.css is missing"


@pytest.mark.parametrize("template", PAGES, ids=lambda p: p.name)
def test_every_page_links_the_shared_theme(template):
    """Parametrised so a page ADDED later without the link fails here.

    That is the actual drift risk: the nav-link bug earlier in this project came
    from exactly this shape of omission, seven copies of something where one
    copy was missing a line.
    """
    assert "/static/theme.css" in template.read_text(encoding="utf-8"), (
        f"{template.name} does not link the shared theme, so it will keep "
        "whatever colours it declares inline and look like a different app"
    )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_template_declares_its_own_root_block(template):
    """Colour has exactly one home.

    A page re-declaring `:root` takes back control of colour for itself only,
    which is invisible until someone opens two tabs side by side.
    """
    body = template.read_text(encoding="utf-8")
    assert not re.search(r":root\s*\{", body), (
        f"{template.name} declares its own :root — theme.css owns colour now"
    )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_template_hardcodes_a_colour(template):
    """Every colour must come from a variable, or the next theme change misses it.

    Hex literals outside :root are precisely what made the dark theme a
    seven-file edit: `#fff` on a button and `rgba(99,102,241,.08)` on a hover row
    are invisible to a variable swap, and rgba tints computed for a dark
    background read as mud on white.

    **A colour COMPOSED from variables is not a literal**, and the distinction had to be
    added when the derived tints arrived: `rgb(var(--green-rgb) / var(--tint-soft))` is
    exactly what this test wants — one source of truth, reachable by a variable swap —
    while `rgb(20 108 52 / .1)` is the thing it forbids. The old pattern stopped at the
    first `)`, so it flagged the good form as `rgb(var(--green-rgb)` and would have pushed
    the next person to inline a pastel instead.
    """
    body = template.read_text(encoding="utf-8")
    # Jinja/HTML/JS comments are prose; a hex code mentioned in one is harmless.
    for pattern in (r"\{#.*?#\}", r"<!--.*?-->", r"/\*.*?\*/"):
        body = re.sub(pattern, " ", body, flags=re.S)

    literals = re.findall(r"#[0-9a-fA-F]{6}\b", body)
    # Balanced enough for one nested `var(...)`, then kept only if it names no variable.
    for call in re.findall(r"rgba?\((?:[^()]|\([^()]*\))*\)", body):
        if "var(" not in call:
            literals.append(call)

    assert not literals, (
        f"{template.name} hardcodes colour(s): {sorted(set(literals))[:6]} — use a "
        "theme variable so the next change reaches them"
    )


# ─── It is actually light ────────────────────────────────────────────────────

def test_the_page_background_is_light(theme):
    """The literal request. A dark --bg would pass every other test here."""
    variables = _vars(theme)
    assert _relative_luminance(variables["bg"]) > 0.8, (
        f"--bg is {variables['bg']}, which is not a light background"
    )
    assert _relative_luminance(variables["surface"]) > 0.85


def test_the_text_is_dark(theme):
    variables = _vars(theme)
    assert _relative_luminance(variables["text"]) < 0.15, (
        f"--text is {variables['text']} — light text on a light page"
    )


# ─── Readable: contrast is measured, not assumed ─────────────────────────────

#: (foreground var, background var). Every pair a user actually reads.
TEXT_PAIRS = [
    ("text", "surface"), ("text", "bg"),
    ("text-muted", "surface"), ("text-muted", "surface2"),
    ("accent", "surface"), ("accent", "accent-soft"),
    ("green", "surface"), ("green", "green-soft"),
    ("red", "surface"), ("red", "red-soft"),
    ("yellow", "surface"), ("yellow", "yellow-soft"),
    ("orange", "surface"), ("orange", "orange-soft"),
    ("blue", "surface"), ("blue", "blue-soft"),
    ("on-accent", "accent"), ("on-green", "green"),
]


@pytest.mark.parametrize("fg,bg", TEXT_PAIRS, ids=lambda v: v)
def test_body_text_pairs_meet_wcag_aa(theme, fg, bg):
    """4.5:1 minimum.

    The pale-on-pale failure this catches is not theoretical: the first pass at
    the light theme kept the dark theme's pale banner text (#bbf7d0) on top of a
    new pale green tint, which measured about 1.3:1 — effectively invisible, and
    it looked "fine" at a glance on a bright laptop screen.
    """
    variables = _vars(theme)
    for name in (fg, bg):
        assert name in variables, f"--{name} is not defined in theme.css"

    ratio = contrast(variables[fg], variables[bg])
    assert ratio >= 4.5, (
        f"--{fg} on --{bg} is {ratio:.2f}:1, below the 4.5:1 AA minimum "
        f"({variables[fg]} on {variables[bg]})"
    )


def test_large_text_pairs_meet_at_least_aa_large(theme):
    """--text-dim and --on-yellow are only ever used on large or bold text.

    Held to 3:1 rather than 4.5:1, and named here so the exemption is a decision
    rather than an oversight.
    """
    variables = _vars(theme)
    assert contrast(variables["text-dim"], variables["surface"]) >= 3.0
    assert contrast(variables["on-yellow"], variables["yellow"]) >= 3.0


def test_the_status_colours_are_distinguishable_from_each_other(theme):
    """Red, amber and green must not be near-identical in luminance.

    Roughly 8% of men have some red-green colour deficiency. If the three status
    colours differ only in hue, a held day and a verified day look the same, and
    the badges carry text as well for exactly that reason — but the colours
    should still help rather than mislead.
    """
    variables = _vars(theme)
    luminances = {
        name: _relative_luminance(variables[name]) for name in ("red", "yellow", "green")
    }
    values = sorted(luminances.values())
    assert values[-1] - values[0] > 0.04, (
        f"status colours are too close in luminance to tell apart: {luminances}"
    )


# ─── Printed documents keep their dark headers ───────────────────────────────

@pytest.mark.parametrize(
    "path,marker",
    [
        ("app/shipment/documents.py", "HEADER_RGB = (0.2, 0.3, 0.3)"),
        ("app/invoice/generator.py", '"2F4F4F"'),
    ],
)
def test_printed_documents_were_not_lightened(path, marker):
    """A dark header band on white paper is the accounting convention.

    These are GST invoices and warehouse clipboard sheets — they are printed, and
    "the app looks too dark" is a statement about screens. Applying the light
    theme here would bleach an invoice header to near-white on white.
    """
    body = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert marker in body, (
        f"{path} no longer uses its dark document header ({marker}) — printed "
        "output should not follow the screen theme"
    )


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
        parts = tokens[channel].split()
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


# ─── The tokens actually resolve, per page ───────────────────────────────────


def _resolve(value: str, tokens: dict[str, str]) -> str:
    """A CSS value with every `var(--token, fallback)` substituted, as a browser would.

    Written because the preview browser cached `theme.css` across restarts and reported every
    token as empty, which is indistinguishable from a genuinely broken stylesheet. Resolving the
    served files here answers the same question deterministically, and keeps answering it.
    """
    for _ in range(4):
        match = re.search(r"var\((--[a-z0-9-]+)(?:,\s*([^)]+))?\)", value)
        if not match:
            break
        name = match.group(1).lstrip("-")
        replacement = tokens.get(name, (match.group(2) or "").strip())
        value = value[:match.start()] + replacement + value[match.end():]
    return value.strip()


@pytest.mark.parametrize(
    "template,selector,expected",
    [
        # The dense grids must all land on the SAME sizes — that is what a scale buys.
        ("portfolio.html", "thead th", "11px"),
        ("portfolio.html", "tbody td", "13px"),
        ("ads.html", "thead th", "11px"),
        ("ads.html", "tbody td", "13px"),
        ("orders.html", "thead th", "11px"),
        ("orders.html", "tbody td", "13px"),
        ("ops.html", "thead th", "11px"),
        ("shipment.html", "thead th", "11px"),
        ("projections.html", "thead th", "11px"),
    ],
)
def test_every_dense_grid_resolves_to_the_same_size(template, selector, expected):
    """**One rhythm across the pages, asserted on the RESOLVED value.**

    Before the refresh these were 10px, 10.5px, 11px and 12.5px depending on which page you were
    looking at — 19 distinct font sizes across the templates, because each page picked its own. A
    token in the source is not proof of a shared size: it could carry a wrong fallback, or name a
    token that does not exist, and either way the page renders differently from its neighbours
    while the source LOOKS consistent. So this resolves the value the way a browser does.
    """
    theme = (REPO_ROOT / "static" / "theme.css").read_text(encoding="utf-8")
    root = re.search(r":root\s*\{(.*?)\}", theme, re.S).group(1)
    tokens = {name: value.strip()
              for name, value in re.findall(r"--([a-z0-9-]+)\s*:\s*([^;]+);", root)}

    css = re.search(r"<style[^>]*>(.*?)</style>",
                    (REPO_ROOT / "templates" / template).read_text(encoding="utf-8"), re.S).group(1)
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)

    match = re.search(re.escape(selector) + r"\{[^}]*font-size:\s*([^;}]+)", css)
    assert match, f"{template} no longer sets a font-size on {selector}"
    assert _resolve(match.group(1), tokens) == expected, (
        f"{template} renders {selector} at {_resolve(match.group(1), tokens)}, not {expected} — "
        f"the pages have drifted apart again"
    )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_every_size_token_carries_a_fallback(template):
    """**A stale `theme.css` must degrade to today's layout, not collapse.**

    Measured during the portfolio conversion: with the stylesheet cached and the tokens undefined,
    `var(--fs-md)` fell back to inherited 15px and the whole grid reflowed — rows went 33px to 36px
    and tags 10px to 15px. `theme.css` is now load-bearing for LAYOUT rather than only colour, and
    a browser or proxy serving one version behind should not rearrange a page of money.

    Every fallback equals the token's own value, so this costs nothing while the stylesheet is
    fresh, and is invisible insurance when it is not.
    """
    body = template.read_text(encoding="utf-8")
    for pattern in (r"\{#.*?#\}", r"<!--.*?-->", r"/\*.*?\*/"):
        body = re.sub(pattern, " ", body, flags=re.S)

    bare = re.findall(r"var\((--(?:fs|sp|radius)-?[a-z0-9]*)\)(?!\s*,)", body)
    assert not bare, (
        f"{template.name} uses {sorted(set(bare))} with no fallback — a stale theme.css would "
        f"collapse these to an inherited size and reflow the page"
    )


# ─── No new hardcoded sizes ──────────────────────────────────────────────────

#: Whole pages allowed to hold raw sizes, with the reason. **Asserted in both directions below**,
#: so the list cannot silently grow — the pattern `test_unauthenticated_access.py` uses for its
#: five public routes. Empty on purpose: every page converted, including `login.html`, which the
#: plan had expected to exempt.
SIZE_EXEMPT: dict[str, str] = {}

#: **Specific VALUES that are exempt everywhere, rather than pages that are exempt entirely.**
#:
#: The plan proposed exempting `ops.html` wholesale for its iOS input guard. Measured while
#: converting it, that would have left **26 of its font-size declarations unguarded to protect 4**
#: — an 85% loophole on the page most likely to be edited under time pressure. Exempting the value
#: keeps the other 22 honest.
#:
#: It also named the wrong value. `ops.html` states the rule itself: "iOS silently zooms the whole
#: page in on focus for anything under 16px", so the packing inputs are **17px** — deliberately
#: above the threshold rather than sitting on it. `--fs-xl` is 16px and would put them back on the
#: boundary.
SIZE_EXEMPT_VALUES = {
    "17px": "iOS zooms the page for an input under 16px; the packing inputs sit above it at 17",
    "9px": "the aria-hidden sort glyph is decoration, not type — 11px makes it compete with its header",
    "34px": "the no-access padlock is an illustration; the scale tops out at 20px because that is "
            "the largest a NUMBER needs to be, and an icon has no such ceiling",
}


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_template_hardcodes_a_size(template):
    """**The colour test's twin, for the values that actually drifted.**

    Counted before the refresh: **19 distinct font-size values** across the templates (13, 12,
    12.5, 11, 14, 10, 15, 16, 13.5, 10.5, 11.5, 9, 21, 19, 14.5, 17...), **22 paddings** and **11
    radii** — while `box-shadow` never drifted, because it was tokenised from the start, and colour
    never drifted, because this file forbids it.

    **Everything with a token AND a test held. Everything without one drifted.** That is the whole
    argument for this test, and it is why the fix was not merely "use the tokens once".

    Verified to bite before it was written: run against the unconverted templates it failed on 10
    pages covering 410 values. A guard that cannot fail proves nothing.
    """
    if template.name in SIZE_EXEMPT:
        pytest.skip(f"{template.name}: {SIZE_EXEMPT[template.name]}")

    body = template.read_text(encoding="utf-8")
    # Comments are prose, and several legitimately QUOTE a pixel value while explaining why it is
    # not used — portfolio.html's body comment says "theme.css sets body{font-size:15px}". Stripping
    # them is the same courtesy the colour test extends, and the converter had to learn it too.
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
    assert SIZE_EXEMPT == {}, (
        "a page-wide exemption was added — prefer exempting the VALUE, which keeps the rest of "
        "that page's sizes guarded"
    )
    assert set(SIZE_EXEMPT_VALUES) == {"17px", "9px", "34px"}, (
        "the value-exemption list changed — a new exempt value is a new hole in this guard"
    )


def test_the_ios_input_guard_still_protects_only_what_it_is_for():
    """The 17px exemption is for the packing INPUTS, not a general licence.

    Without this, `17px` becomes a free size anywhere, and the reason it is allowed — iOS zooming a
    focused input — stops being visible to whoever next reaches for it.
    """
    ops = (REPO_ROOT / "templates" / "ops.html").read_text(encoding="utf-8")
    assert re.search(r"input\.qty\{[^}]*font-size:\s*17px", ops), (
        "ops.html's packing input is no longer 17px — retire the value exemption rather than "
        "leaving it open"
    )
    assert "zoom" in ops.lower(), (
        "the 17px rule must carry its reason, or a later reader will 'tidy' it onto the type scale "
        "and reintroduce the iOS zoom mid-count"
    )


def test_the_sort_glyph_exemption_is_only_used_on_sort_glyphs():
    """`9px` is allowed for the ▲/▼ arrow and nothing else.

    Both pages that sort a dense table use it; asserting the SELECTOR keeps the exemption from
    becoming a way to smuggle unreadably small text past the scale.
    """
    for name in ("portfolio.html", "ads.html"):
        body = (REPO_ROOT / "templates" / name).read_text(encoding="utf-8")
        for match in re.finditer(r"font-size:\s*9px", body):
            rule_start = body.rfind("}", 0, match.start()) + 1
            selector = body[rule_start:match.start()].split("{")[0]
            assert "arrow" in selector, (
                f"{name} uses 9px on '{selector.strip()}' — the exemption is for the aria-hidden "
                f"sort glyph, not for small text generally"
            )
