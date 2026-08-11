import re
from lxml import html
from typing import Optional


def extract_title(tree: html.HtmlElement) -> Optional[str]:
    el = tree.xpath('//*[@id="productTitle"]/text()')
    if el:
        return el[0].strip()
    el = tree.xpath('//span[@id="productTitle"]/text()')
    if el:
        return el[0].strip()
    return None


def extract_price(tree: html.HtmlElement) -> Optional[str]:
    selectors = [
        '//span[contains(@class,"priceToPay")]//span[contains(@class,"a-price-whole")]/text()',
        '//*[@id="corePrice_feature_div"]//span[contains(@class,"a-price-whole")]/text()',
        '//span[@id="priceblock_ourprice"]/text()',
        '//span[@id="priceblock_dealprice"]/text()',
        '//*[@id="apex_offerDisplay_desktop"]//span[contains(@class,"a-price-whole")]/text()',
        '//span[contains(@class,"a-price")]//span[contains(@class,"a-price-whole")]/text()',
    ]
    for sel in selectors:
        result = tree.xpath(sel)
        if result:
            price_str = result[0].strip().replace(",", "")
            if price_str:
                fraction = tree.xpath(sel.replace("a-price-whole", "a-price-fraction"))
                if fraction:
                    return f"₹{price_str}.{fraction[0].strip()}"
                return f"₹{price_str}"
    return None


def extract_rating(tree: html.HtmlElement) -> Optional[str]:
    el = tree.xpath('//*[@id="acrPopover"]/@title')
    if el:
        match = re.search(r"([\d.]+)", el[0])
        if match:
            return match.group(1)
    el = tree.xpath('//span[@data-hook="rating-out-of-text"]/text()')
    if el:
        match = re.search(r"([\d.]+)", el[0])
        if match:
            return match.group(1)
    el = tree.xpath('//i[contains(@class,"a-icon-star")]//span/text()')
    if el:
        match = re.search(r"([\d.]+)", el[0])
        if match:
            return match.group(1)
    return None


def extract_rating_count(tree: html.HtmlElement) -> Optional[str]:
    selectors = [
        '//*[@id="acrCustomerReviewText"]/text()',
        '//*[@id="acrCustomerReviewLink"]//span/text()',
        '//span[@data-hook="total-review-count"]/text()',
        '//*[@id="acrPopover"]/..//span[contains(@class,"a-size-base")]/text()',
    ]
    for sel in selectors:
        els = tree.xpath(sel)
        for el in els:
            match = re.search(r"([\d,]+)", el)
            if match:
                return match.group(1).replace(",", "")
    return None


def extract_bsr(tree: html.HtmlElement) -> Optional[str]:
    sales_rank = tree.xpath('//*[@id="SalesRank"]//text()')
    if sales_rank:
        text = " ".join(t.strip() for t in sales_rank if t.strip())
        match = re.search(r"#([\d,]+)\s+in\s+(.+?)(?:\(|$)", text)
        if match:
            rank = match.group(1).replace(",", "")
            category = match.group(2).strip()
            return f"#{rank} in {category}"

    tables = tree.xpath(
        '//table[contains(@id,"productDetails")]//tr | '
        '//div[@id="detailBulletsWrapper_feature_div"]//li'
    )
    for row in tables:
        text = " ".join(row.xpath('.//text()')).strip()
        if "Best Sellers Rank" in text or "best seller" in text.lower():
            match = re.search(r"#([\d,]+)\s+in\s+(.+?)(?:\(|$)", text)
            if match:
                rank = match.group(1).replace(",", "")
                category = match.group(2).strip()
                return f"#{rank} in {category}"

    detail_bullets = tree.xpath('//*[@id="detailBulletsWrapper_feature_div"]//text()')
    if detail_bullets:
        full_text = " ".join(t.strip() for t in detail_bullets)
        match = re.search(r"#([\d,]+)\s+in\s+(.+?)(?:\(|$)", full_text)
        if match:
            rank = match.group(1).replace(",", "")
            category = match.group(2).strip()
            return f"#{rank} in {category}"

    return None


def extract_bsr_numeric(tree: html.HtmlElement) -> Optional[int]:
    bsr = extract_bsr(tree)
    if bsr:
        match = re.search(r"#([\d,]+)", bsr)
        if match:
            return int(match.group(1).replace(",", ""))
    return None


def extract_bsr_category(tree: html.HtmlElement) -> Optional[str]:
    bsr = extract_bsr(tree)
    if bsr:
        match = re.search(r"in\s+(.+)", bsr)
        if match:
            return match.group(1).strip()
    return None


def extract_seller(tree: html.HtmlElement) -> Optional[str]:
    el = tree.xpath('//*[@id="sellerProfileTriggerId"]/text()')
    if el:
        return el[0].strip()
    el = tree.xpath('//*[@id="merchant-info"]//a/text()')
    if el:
        return el[0].strip()
    merchant_info = tree.xpath('//*[@id="merchant-info"]//text()')
    if merchant_info:
        text = " ".join(t.strip() for t in merchant_info if t.strip())
        if "amazon" in text.lower():
            return "Amazon"
    el = tree.xpath('//div[@tabular-attribute-name="Sold by"]//span/text()')
    if el:
        return el[0].strip()
    return None


def extract_fulfillment(tree: html.HtmlElement) -> Optional[str]:
    fulfillment_area = tree.xpath(
        '//*[@id="tabular-buybox"]//text() | '
        '//*[@id="merchant-info"]//text() | '
        '//*[contains(@class,"offer-display-feature-text")]//text()'
    )
    text = " ".join(t.strip().lower() for t in fulfillment_area if t.strip())

    if not text:
        return None

    ships_from_amazon = bool(re.search(r"ships?\s*from[\s\S]{0,30}amazon", text))
    sold_by_amazon = bool(re.search(r"sold\s*by[\s\S]{0,30}amazon", text))

    if ships_from_amazon or sold_by_amazon:
        return "FBA"

    if "easy ship" in text or "easyship" in text:
        return "Easy Ship"

    if re.search(r"ships?\s*from", text) and not ships_from_amazon:
        return "FBM"

    if "fulfilled by amazon" in text or "fulfilment by amazon" in text:
        return "FBA"

    return "FBM"


def extract_deal(tree: html.HtmlElement) -> str:
    """Is the red deal badge on the page? Returns "Yes" / "No".

    **Structure, not vocabulary.** The previous version matched a hardcoded list of
    phrases — 'limited time deal', 'lightning deal', 'prime day deal' — and reported
    "No" for every product during the Freedom Sale, because the badge read
    **"Freedom Sale Deal"** and that string was not on the list. Amazon names each
    sale differently ("Great Indian Festival", "Freedom Sale", whatever is next), so
    a keyword list is guaranteed to go stale and it fails SILENTLY: every row reads
    No, which is indistinguishable from genuinely having no deals.

    Two structural signals are used instead, both taken from the live DOM:

    1. ``#dealBadgeSupportingText`` — the span holding the badge's VISIBLE text. On a
       page with a deal it contains exactly "Freedom Sale Deal"; on one without, the
       span does not exist at all.
    2. ``data-csa-c-painter="dp-deal"`` — Amazon's own marker that the detail page
       painted a deal. Absent when there is no deal.

    Verified against real pages of both kinds:

        with a deal    (B0CY84RYRG): supporting text present, painter present
        without a deal (0143448145): dealBadge div present but BOTH absent

    Note the div itself is always present, which is why its mere existence cannot be
    the test — that is the trap the old countdown-element check fell into.

    **The screen-reader spans are deliberately excluded.** They are `aok-hidden` and
    contain the badge text with UNSUBSTITUTED placeholders —
    "Freedom Sale Deal NO_OF_HOURS hours NO_OF_MINUTES minutes" — because the
    countdown is filled in by JavaScript that never runs here. Reading those would
    work today and would also match a page where the template shipped but no deal was
    active, so the visible span is the honest source.
    """
    # 1. The visible badge text. Most direct: if it is there, the shopper sees a badge.
    visible = " ".join(
        t.strip()
        for t in tree.xpath('//*[@id="dealBadgeSupportingText"]//text()')
        if t.strip()
    )
    # Guard against the placeholder text leaking in through a markup change; a badge
    # whose text is still a template is not a rendered badge.
    if visible and "NO_OF_" not in visible:
        return "Yes"

    # 2. Amazon's own "this page painted a deal" marker, scoped to the badge region so
    #    a deal on a *recommended* product elsewhere on the page cannot trigger it.
    if tree.xpath(
        '//*[@data-feature-name="dealBadge"]//*[@data-csa-c-painter="dp-deal"] | '
        '//span[contains(@class,"dealBadge")][@data-csa-c-painter="dp-deal"]'
    ):
        return "Yes"

    return "No"


def extract_use_by(tree: html.HtmlElement) -> Optional[str]:
    # Primary: Check expiryDate_feature_div (Amazon's dedicated expiry widget)
    expiry_div = tree.xpath('//*[@id="expiryDate_feature_div"]//text()')
    if expiry_div:
        expiry_text = " ".join(t.strip() for t in expiry_div if t.strip())
        # Extract date after "Use by:" or similar label
        match = re.search(
            r"(?:use\s*by|best\s*before|expiry|expiration)[:\s]*(\d{1,2}\s*\w{3,9}\s*\d{2,4})",
            expiry_text, re.IGNORECASE
        )
        if match:
            return match.group(1).strip()
        # If no label prefix, just grab any date-like pattern
        match = re.search(r"(\d{1,2}\s+[A-Z]{3}\s+\d{4})", expiry_text)
        if match:
            return match.group(1).strip()

    # Secondary: Check freshShelfLifeMessage div
    shelf_div = tree.xpath('//*[contains(@id,"freshShelfLife")]//text()')
    if shelf_div:
        shelf_text = " ".join(t.strip() for t in shelf_div if t.strip())
        match = re.search(
            r"(?:use\s*by|best\s*before|expiry)[:\s]*(\d{1,2}[\s/-]\w{3,9}[\s/-]\d{2,4})",
            shelf_text, re.IGNORECASE
        )
        if match:
            return match.group(1).strip()

    # Fallback: Check product detail tables
    detail_texts = tree.xpath(
        '//table[contains(@id,"productDetails")]//text() | '
        '//*[@id="detailBulletsWrapper_feature_div"]//text() | '
        '//*[@id="productDetails_techSpec_section_1"]//text() | '
        '//*[@id="productDetails_detailBullets_sections1"]//text()'
    )
    full_text = " ".join(t.strip() for t in detail_texts if t.strip())

    patterns = [
        r"(?:use\s*by|best\s*before|expiry|expiration|exp\.?\s*date)[:\s]*(\d{1,2}[\s/-]\w{3,9}[\s/-]\d{2,4})",
        r"(?:use\s*by|best\s*before|expiry|expiration)[:\s]*(\d{4}[-/]\d{2}[-/]\d{2})",
        r"(?:use\s*by|best\s*before|expiry|expiration)[:\s]*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, full_text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def detect_captcha(tree: html.HtmlElement) -> bool:
    captcha_indicators = [
        '//form[@action="/errors/validateCaptcha"]',
        '//img[contains(@src,"captcha")]',
        '//*[contains(text(),"Enter the characters you see below")]',
        '//*[contains(text(),"Type the characters")]',
    ]
    for sel in captcha_indicators:
        if tree.xpath(sel):
            return True
    return False


def detect_dog_page(tree: html.HtmlElement) -> bool:
    dog_indicators = [
        '//*[contains(text(),"looking for was not found")]',
        '//img[contains(@alt,"sorry")]',
        '//*[contains(@class,"a-spacing-base") and contains(text(),"no results")]',
    ]
    for sel in dog_indicators:
        if tree.xpath(sel):
            return True
    return False


def detect_unavailable(tree: html.HtmlElement) -> bool:
    """Detect 'Currently unavailable' / 'out of stock' / suppressed listings."""
    unavail_indicators = [
        '//*[@id="availability"]//text()',
        '//*[@id="availability_feature_div"]//text()',
        '//*[@id="outOfStock"]//text()',
    ]
    for sel in unavail_indicators:
        texts = tree.xpath(sel)
        combined = " ".join(t.strip().lower() for t in texts if t.strip())
        if any(phrase in combined for phrase in (
            "currently unavailable",
            "out of stock",
            "we don't know when or if",
            "unavailable",
        )):
            return True

    # Also check if there's no buybox at all (no "Add to Cart" button and no price)
    has_buybox = bool(
        tree.xpath('//*[@id="add-to-cart-button"]') or
        tree.xpath('//*[@id="buy-now-button"]') or
        tree.xpath('//span[contains(@class,"priceToPay")]')
    )
    # If there's a title but absolutely no buybox, treat as unavailable
    title = tree.xpath('//*[@id="productTitle"]/text()')
    if title and not has_buybox:
        return True

    return False


def parse_product_page(raw_html: str, asin: str) -> dict:
    tree = html.fromstring(raw_html)

    if detect_captcha(tree):
        return {
            "asin": asin,
            "url": f"https://www.amazon.in/dp/{asin}",
            "status": "Blocked (CAPTCHA)",
        }

    if detect_dog_page(tree):
        return {
            "asin": asin,
            "url": f"https://www.amazon.in/dp/{asin}",
            "status": "Not Found",
        }

    title = extract_title(tree)
    if not title:
        return {
            "asin": asin,
            "url": f"https://www.amazon.in/dp/{asin}",
            "status": "Parse Error (no title)",
        }

    # If listing exists but is currently unavailable — keep title/ratings/BSR
    # but explicitly clear price/deal/seller so stale data doesn't persist
    if detect_unavailable(tree):
        return {
            "asin": asin,
            "url": f"https://www.amazon.in/dp/{asin}",
            "title": title,
            "rating": extract_rating(tree),
            "rating_count": extract_rating_count(tree),
            "bsr": extract_bsr(tree),
            "bsr_numeric": extract_bsr_numeric(tree),
            "bsr_category": extract_bsr_category(tree),
            "price": None,       # explicitly clear
            "seller": None,      # explicitly clear
            "fulfillment": None, # explicitly clear
            "deal": "No",        # explicitly clear
            "use_by": extract_use_by(tree),
            "status": "Unavailable",
        }

    return {
        "asin": asin,
        "url": f"https://www.amazon.in/dp/{asin}",
        "title": title,
        "rating": extract_rating(tree),
        "rating_count": extract_rating_count(tree),
        "bsr": extract_bsr(tree),
        "bsr_numeric": extract_bsr_numeric(tree),
        "bsr_category": extract_bsr_category(tree),
        "price": extract_price(tree),
        "seller": extract_seller(tree),
        "fulfillment": extract_fulfillment(tree),
        "deal": extract_deal(tree),
        "use_by": extract_use_by(tree),
        "status": "OK",
    }
