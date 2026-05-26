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
    deal_selectors = [
        '//*[@id="dealBadge"]',
        '//*[@id="dealnudge"]',
        '//*[contains(@class,"dealBadge")]',
        '//span[contains(text(),"Limited time deal")]',
        '//span[contains(text(),"Deal of the Day")]',
        '//span[contains(text(),"Lightning Deal")]',
    ]
    for sel in deal_selectors:
        if tree.xpath(sel):
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
