"""The red deal badge: "Freedom Sale Deal", "Limited time deal", whatever is next.

Reported: *"deal tag is showing wrong. NO in everything. earlier code me it was coming
correct."* Every product read **No** while the Freedom Sale was live and the red badge
was plainly on the page.

The cause was a hardcoded phrase list — `'limited time deal'`, `'lightning deal'`,
`'prime day deal'` and so on. The badge said **"Freedom Sale Deal"**, which was not on
it, so the check returned No. Amazon renames the sale every few months ("Great Indian
Festival", "Freedom Sale", …), so that list was guaranteed to go stale, and it failed in
the worst possible way: silently, with every row reading No, which is indistinguishable
from genuinely having no deals.

`extract_deal` now keys on STRUCTURE:

  1. ``#dealBadgeSupportingText`` — the span holding the badge's visible text. Present
     only when a badge is rendered.
  2. ``data-csa-c-painter="dp-deal"`` — Amazon's own marker that the detail page painted
     a deal.

The fixtures below are cut from real pages fetched while writing this, so they carry the
quirks that matter rather than an idealised shape. **`dealBadge_feature_div` exists on
every product page whether or not there is a deal** — that is the trap, and
`test_the_empty_badge_container_is_not_a_deal` is the test that pins it.
"""
import pytest

from lxml import html

from app.scraper.parsers import extract_deal

pytestmark = pytest.mark.regression


# ─── Fixtures cut from real Amazon pages ─────────────────────────────────────

#: B0CY84RYRG during the Freedom Sale. Verbatim, including the `aok-hidden`
#: screen-reader spans whose countdown placeholders were never substituted — the
#: JavaScript that fills them in does not run for us.
WITH_DEAL = """
<div id="dealBadge_feature_div" class="celwidget" data-feature-name="dealBadge"
     data-csa-c-type="widget" data-csa-c-content-id="dealBadge"
     data-csa-c-asin="B0CY84RYRG">
  <span class="dealBadge aok-relative" data-csa-c-type="item" data-csa-c-owner="DealsX"
        data-csa-c-item-id="amzn1.asin.B0CY84RYRG:amzn1.deal.60c63c9a"
        data-csa-c-painter="dp-deal">
    <span id="deals_countdown_timer_from_hours_screen_reader_label" class="aok-hidden">Freedom Sale Deal NO_OF_HOURS hours NO_OF_MINUTES minutes</span>
    <span id="deals_countdown_timer_from_minutes_without_seconds_screen_reader_label" class="aok-hidden">Freedom Sale Deal NO_OF_MINUTES minutes</span>
    <span id="dealBadgeSupportingText" class="a-size-small dealBadgeTextColor a-text-bold">
      <span>Freedom Sale Deal</span>
    </span>
  </span>
</div>
"""

#: A product with no deal (a paperback, 0143448145). The container is STILL THERE and
#: still carries data-feature-name="dealBadge" — it is simply empty of badge content.
NO_DEAL = """
<div id="dealBadge_feature_div" class="celwidget" data-feature-name="dealBadge"
     data-csa-c-type="widget" data-csa-c-content-id="dealBadge"
     data-csa-c-slot-id="dealBadge_feature_div" data-csa-c-asin="0143448145">
</div>
"""


def _tree(fragment: str):
    """Wrap a fragment in a page so the XPaths run against realistic structure."""
    return html.fromstring(f"<html><body><div id='centerCol'>{fragment}</div></body></html>")


# ─── The reported bug ────────────────────────────────────────────────────────

def test_a_freedom_sale_deal_is_detected():
    """The exact case that read No on every row.

    "Freedom Sale Deal" was not in the old phrase list, so the badge was invisible to
    the parser while being plainly visible to the owner.
    """
    assert extract_deal(_tree(WITH_DEAL)) == "Yes"


def test_the_empty_badge_container_is_not_a_deal():
    """The other half, and the reason the container's existence cannot be the test.

    `dealBadge_feature_div` is present on every product page. Treating "the div exists"
    as a deal would report Yes for the entire catalogue — the same silent failure as
    before, in the opposite direction and harder to notice.
    """
    assert extract_deal(_tree(NO_DEAL)) == "No"


def test_a_page_with_no_badge_markup_at_all_is_not_a_deal():
    assert extract_deal(_tree("<div id='x'>nothing here</div>")) == "No"
    assert extract_deal(_tree("")) == "No"


# ─── Why it must not be a phrase list ────────────────────────────────────────

@pytest.mark.parametrize("label", [
    "Freedom Sale Deal",
    "Limited time deal",
    "Great Indian Festival Deal",
    "Lightning Deal",
    "Deal of the Day",
    "Prime Day Deal",
    # The point of the parametrisation: a sale Amazon has not run yet must work too.
    "Some Future Sale Nobody Has Named Yet",
    "छूट",                     # a non-English label, which a keyword list cannot cover
])
def test_any_badge_wording_counts_as_a_deal(label):
    """Structure, not vocabulary.

    Amazon renames the sale every few months. A parser that has to be edited each time
    is a parser that reports No for the first week of every sale — and nobody notices,
    because No is what it always says when it is wrong.
    """
    fragment = f"""
    <div id="dealBadge_feature_div" data-feature-name="dealBadge">
      <span class="dealBadge" data-csa-c-painter="dp-deal">
        <span id="dealBadgeSupportingText"><span>{label}</span></span>
      </span>
    </div>
    """
    assert extract_deal(_tree(fragment)) == "Yes", f"{label!r} was not detected"


# ─── The traps in the real markup ────────────────────────────────────────────

def test_the_unsubstituted_placeholder_text_alone_is_not_a_deal():
    """The screen-reader spans carry the badge text with the countdown UNFILLED:

        "Freedom Sale Deal NO_OF_HOURS hours NO_OF_MINUTES minutes"

    Those are `aok-hidden` templates whose values are substituted by JavaScript that
    never runs for us. Matching them would work on today's pages and would ALSO match a
    page that shipped the template with no active deal — so a badge whose only text is
    still a placeholder must not count.
    """
    fragment = """
    <div id="dealBadge_feature_div" data-feature-name="dealBadge">
      <span class="aok-hidden">Freedom Sale Deal NO_OF_HOURS hours NO_OF_MINUTES minutes</span>
      <span class="aok-hidden">Freedom Sale Deal NO_OF_MINUTES minutes</span>
    </div>
    """
    assert extract_deal(_tree(fragment)) == "No"


def test_placeholder_text_inside_the_supporting_span_is_rejected():
    """Defensive: if a markup change ever put the template into the visible span, that
    is a template, not a rendered badge."""
    fragment = """
    <div id="dealBadge_feature_div" data-feature-name="dealBadge">
      <span id="dealBadgeSupportingText">Freedom Sale Deal NO_OF_HOURS hours</span>
    </div>
    """
    assert extract_deal(_tree(fragment)) == "No"


def test_a_deal_on_a_recommended_product_does_not_count():
    """Carousels of "customers also bought" carry their own deal markup.

    Those are other products. Counting them would mark a non-discounted item as being
    on a deal, which then reaches the dashboard as a pricing decision.
    """
    fragment = """
    <div id="dealBadge_feature_div" data-feature-name="dealBadge"></div>
    <div id="similarities_feature_div">
      <span class="dealBadge" data-csa-c-painter="sims-deal">
        <span>Freedom Sale Deal</span>
      </span>
    </div>
    """
    assert extract_deal(_tree(fragment)) == "No"


def test_the_painter_marker_alone_is_enough():
    """Amazon's own "this page painted a deal" attribute, for the case where the
    supporting-text span is renamed but the marker survives. Two independent signals so
    one markup tweak does not take the feature out entirely."""
    fragment = """
    <div id="dealBadge_feature_div" data-feature-name="dealBadge">
      <span class="dealBadge" data-csa-c-painter="dp-deal">
        <span>some new span name</span>
      </span>
    </div>
    """
    assert extract_deal(_tree(fragment)) == "Yes"


def test_a_plain_mrp_discount_is_not_a_deal():
    """A struck-through MRP and a percentage saving are on almost every listing.

    They are not the red badge, and conflating them would report Yes for the whole
    catalogue — which is what "-51% ₹295 M.R.P.: ₹599" would do to a text-matching
    parser.
    """
    fragment = """
    <div id="corePriceDisplay_desktop_feature_div">
      <span class="savingsPercentage">-51%</span>
      <span class="a-price"><span class="a-offscreen">₹295.00</span></span>
      <span class="basisPrice">M.R.P.: <span class="a-text-price">₹599</span></span>
    </div>
    <div id="dealBadge_feature_div" data-feature-name="dealBadge"></div>
    """
    assert extract_deal(_tree(fragment)) == "No"


def test_javascript_in_the_badge_div_is_not_read_as_a_badge():
    """The container often holds inline script. The old parser filtered JS out of the
    text it scanned; this one does not scan free text at all, which is why that whole
    class of false positive is gone — asserted so it stays gone."""
    fragment = """
    <div id="dealBadge_feature_div" data-feature-name="dealBadge">
      <script>P.when('A').execute(function(A){ var dealBadge = "Limited time deal"; });</script>
    </div>
    """
    assert extract_deal(_tree(fragment)) == "No"


# ─── The whole-page path ─────────────────────────────────────────────────────

def test_the_parsed_row_carries_the_deal():
    """extract_deal is reached through parse_product_page, which is what the engine
    calls. A correct helper wired to nothing would still show No on the dashboard.

    The buy box in this fixture is not decoration. ``detect_unavailable`` treats "a
    title but no buy box at all" as a suppressed listing and then FORCES deal to "No" —
    correctly, since an unavailable item cannot be on a deal. Leaving it out made this
    test fail against working code, which is a fair warning about how narrow the
    available path is.
    """
    from app.scraper.parsers import parse_product_page

    page = f"""
    <html><body>
      <span id="productTitle">MITHILA FOODS 1 kg Roasted Chana</span>
      <div id="centerCol">
        <span class="priceToPay"><span class="a-offscreen">₹295.00</span></span>
        <input id="add-to-cart-button" type="submit"/>
        {WITH_DEAL}
      </div>
    </body></html>
    """
    row = parse_product_page(page, "B0CY84RYRG")
    assert row["deal"] == "Yes", f"the deal did not reach the row: {row.get('deal')}"


def test_an_unavailable_listing_reports_no_deal():
    """A listing with no price cannot be on a deal, and the row explicitly clears the
    field. Two rows in the reported screenshot were Unavailable, so this path is live."""
    from app.scraper.parsers import parse_product_page

    page = """
    <html><body>
      <span id="productTitle">MITHILA FOODS 1.5 Kg Authentic Bihari</span>
      <div id="outOfStock">Currently unavailable.</div>
    </body></html>
    """
    row = parse_product_page(page, "B0CWGY2LCP")
    assert row["deal"] == "No"
