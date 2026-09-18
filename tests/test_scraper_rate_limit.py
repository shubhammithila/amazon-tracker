"""Amazon's throttle page is an HTTP 200, and it used to be reported as a parse error.

**Found by porting the scraper to another stack and diffing the two on the same ASINs.** One
implementation succeeded and the other reported a parse failure; the difference turned out to be
request timing, not code. Measured on this machine: a burst of requests gets normal 2.4 MB pages,
then every subsequent one comes back as a complete little **3,793-byte** page reading

    Amazon.in  Click the button below to continue shopping  Continue shopping
    Conditions of Use & Sale  Privacy Notice  © 1996-2025, Amazon.com, Inc. or its affiliates

It is not a CAPTCHA, not a 503, and not an error. Before this it landed on the `no title` branch and
was reported as ``Parse Error (no title)`` — true, and actively misleading, because it reads as a
markup change and sends the reader to the XPaths. The cause is rate limiting and the remedy is to
wait, which is a different action entirely.

Worse than the wrong label: ``Parse Error (no title)`` is **not in the retry list**, so a throttled
ASIN was abandoned after a single attempt.

Every test here fails against the code before this change.
"""
from lxml import html

import pytest

from app.scraper.engine import RETRYABLE_STATUSES
from app.scraper.parsers import (
    INTERSTITIAL_MAX_BYTES,
    detect_bot_interstitial,
    detect_captcha,
    detect_dog_page,
    parse_product_page,
)

pytestmark = pytest.mark.regression


#: The real page, reduced to its structure. The phrase and the footer links are verbatim from the
#: captured response; the surrounding chrome is not, because it is not what the guard keys on.
INTERSTITIAL = (
    "<html><head><title>Amazon.in</title></head><body>"
    "<div><p>Click the button below to continue shopping</p>"
    "<button>Continue shopping</button></div>"
    "<div><a>Conditions of Use &amp; Sale</a><a>Privacy Notice</a>"
    "<span>&copy; 1996-2025, Amazon.com, Inc. or its affiliates</span></div>"
    "</body></html>"
)


def _product_page(inner: str = "") -> str:
    """A buyable page, so the guards let it through to the extractors."""
    return (
        "<html><body>"
        '<span id="productTitle">MITHILA FOODS 500 g Chana Sattu</span>'
        '<button id="add-to-cart-button">Add to Cart</button>'
        '<span class="priceToPay"><span class="a-price-whole">181</span>'
        '<span class="a-price-fraction">00</span></span>'
        f"{inner}</body></html>"
    )


def test_the_throttle_page_is_reported_as_rate_limiting():
    """The whole point. Not "Parse Error (no title)", which points at the wrong thing."""
    result = parse_product_page(INTERSTITIAL, "B0CWGXYLT6")
    assert result["status"] == "Rate limited"


def test_it_is_not_reported_as_a_parse_error():
    """Asserted separately, because that is the label this change removes.

    A parse error means "the selectors need looking at". This means "slow down". Conflating them
    sent whoever read the dashboard to the wrong file.
    """
    result = parse_product_page(INTERSTITIAL, "B0CWGXYLT6")
    assert result["status"] != "Parse Error (no title)"


def test_rate_limiting_is_retried():
    """It clears by itself, so it belongs with the throttle and the timeouts.

    Before this, the status it produced was not in the retry list at all — so a throttled ASIN was
    given up on after one attempt, which is the opposite of what a temporary failure deserves.
    """
    assert "Rate limited" in RETRYABLE_STATUSES
    # And the statuses that were already retried must stay retried.
    for status in ("Throttled (503)", "Timeout", "Connection Error"):
        assert status in RETRYABLE_STATUSES


def test_the_guard_needs_BOTH_the_phrase_and_the_small_size():
    """Neither signal alone is sufficient, and that is deliberate.

    "continue shopping" can legitimately appear in a footer or a recommendation strip on a real
    2.4 MB page, and a small page could be any error. The same discipline the deal badge needs,
    where `dealBadge_feature_div` is on every product page and so cannot be the test.
    """
    tree = html.fromstring(INTERSTITIAL)
    assert detect_bot_interstitial(tree, INTERSTITIAL) is True

    # The identical markup, declared to be a 2.4 MB page: not the interstitial.
    padded = INTERSTITIAL + ("<!-- padding -->" * 4000)
    assert len(padded) > INTERSTITIAL_MAX_BYTES
    assert detect_bot_interstitial(html.fromstring(padded), padded) is False


def test_a_real_product_page_mentioning_continue_shopping_is_untouched():
    """The false-positive case that matters: a live page must not be called rate-limited."""
    page = _product_page("<div><a>Continue shopping for more deals</a></div>")
    tree = html.fromstring(page)
    assert detect_bot_interstitial(tree, page) is False

    result = parse_product_page(page, "B0CWGXYLT6")
    assert result["status"] == "OK"
    assert result["price"] == "₹181.00"


def test_a_page_with_a_product_title_is_never_the_interstitial():
    """A third signal, and the cheapest one: the throttle page has no product title at all.

    Checked explicitly so a short but genuine page — an early render, a partial response — cannot be
    mistaken for a throttle and silently retried instead of parsed.
    """
    small_but_real = (
        '<html><body><span id="productTitle">A Product</span>'
        "<p>continue shopping</p></body></html>"
    )
    tree = html.fromstring(small_but_real)
    assert detect_bot_interstitial(tree, small_but_real) is False


def test_the_interstitial_wins_over_the_no_title_branch():
    """ORDER, asserted by BEHAVIOUR rather than by source position.

    The interstitial has no `#productTitle`, so whichever branch returns first claims it — and if the
    no-title one wins, the status is `Parse Error (no title)` again and this change is undone.

    An earlier version of this test compared `source.index(...)` positions, and a mutation moving the
    check later in the function SURVIVED it: the block still sat before the string being searched for,
    so the positions still compared as expected while proving nothing about which branch ran. Asserted
    on the returned status instead, which is the thing that actually matters.

    Both possible collisions are exercised: a page with no title at all, and one whose title element
    exists but is empty — the latter is what `extract_title` returns None for.
    """
    assert parse_product_page(INTERSTITIAL, "B0CWGXYLT6")["status"] == "Rate limited"

    with_empty_title = INTERSTITIAL.replace(
        "<body>", '<body><span id="productTitle">   </span>'
    )
    # A product title present but blank means `extract_title` is None, so the no-title branch is live.
    # The interstitial signal is gone here though (the title element EXISTS), so this must NOT be
    # reported as rate limiting — it is a genuine parse failure.
    assert parse_product_page(with_empty_title, "B0CWGXYLT6")["status"] == (
        "Parse Error (no title)"
    )


def test_it_is_distinguished_from_a_captcha_and_a_dog_page():
    """Three different pages, three different actions.

    A CAPTCHA needs a human. A dog page means the ASIN is gone. This needs patience. Reporting any of
    them as another would send someone to do the wrong thing.
    """
    tree = html.fromstring(INTERSTITIAL)
    assert detect_captcha(tree) is False
    assert detect_dog_page(tree) is False
    assert detect_bot_interstitial(tree, INTERSTITIAL) is True


def test_a_rate_limited_row_is_not_saved_to_the_database():
    """It carries no product data, so there is nothing to record.

    The persistence filter is an allow-list (`status not in ("OK", "Unavailable")`), so this holds by
    construction — asserted anyway, because a future reader widening that tuple to "save everything"
    would write a row of nulls over a real product's price.
    """
    import inspect

    from app.routers import scrape as scrape_router

    source = inspect.getsource(scrape_router.save_results_to_db)
    assert 'status not in ("OK", "Unavailable")' in source, (
        "persistence must stay an allow-list; a rate-limited row has no data to save"
    )
