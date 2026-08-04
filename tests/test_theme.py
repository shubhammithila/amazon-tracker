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
    """
    body = template.read_text(encoding="utf-8")
    # Jinja/HTML/JS comments are prose; a hex code mentioned in one is harmless.
    for pattern in (r"\{#.*?#\}", r"<!--.*?-->", r"/\*.*?\*/"):
        body = re.sub(pattern, " ", body, flags=re.S)

    literals = re.findall(r"#[0-9a-fA-F]{6}\b", body) + re.findall(r"rgba?\([^)]*\)", body)
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
