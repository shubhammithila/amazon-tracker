"""The Product column stays in view while the figures scroll sideways.

Asked for as *"while scrolling left the first column i.e. the product names should also be frozen"*.
Asserted on the CSS because the behaviour is the stylesheet; checked in a browser as well.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.regression
SRC = (Path(__file__).parent.parent / "templates" / "portfolio.html").read_text(encoding="utf-8")
CSS = SRC[SRC.index("<style>"):SRC.index("</style>")]


def _rule(selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert m, f"no rule for {selector}"
    return m.group(1)


def test_every_product_cell_sticks_to_the_left_with_an_opaque_background():
    rule = _rule('[data-col="product"]')
    assert "position:sticky" in rule and "left:0" in rule
    # Transparent would let the figures slide THROUGH the names.
    assert "background:var(--surface)" in rule


def test_the_corner_cells_sit_above_both_the_headings_and_the_column():
    rule = _rule('thead [data-col="product"],thead tr.totals td[data-col="product"]')
    assert "z-index:3" in rule, "a scrolled figure heading would paint over 'Product'"


def test_tinted_rows_keep_their_tint_in_the_frozen_cell():
    assert "background:var(--surface2)" in _rule(
        'tr.size td[data-col="product"],tr.flav td[data-col="product"]')
    assert "background:var(--accent-soft)" in _rule(
        'tbody tr.parent:hover td[data-col="product"]')


def test_every_row_renderer_marks_its_product_cell():
    """The rule reaches only cells carrying data-col="product"."""
    for marker in ('<th scope="col" data-col="${c.id}"', '<td data-col="product">All',
                   '<td class="p-name" data-col="product">', '<td data-col="product">${esc(g.flavour)}',
                   '<td data-col="product">${esc(sizeName(s))}'):
        assert marker in SRC, marker
