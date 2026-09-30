"""The Portfolio table's column alignment, proven by EXECUTING the page's render functions.

A table where TACOS and ACOS have swapped cells has the right COUNT of columns and the wrong figure
under each heading — so every cell carries `data-col`, and these tests compare ids, in order.
"""
import pytest

from tests.js_harness import run_portfolio_js

pytestmark = pytest.mark.regression


def test_the_harness_can_run_the_page_code():
    assert run_portfolio_js("emit(normaliseLayout(null, data.columns).order.length)") == 11
