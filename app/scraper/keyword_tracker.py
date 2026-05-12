import asyncio
import logging
import re
from urllib.parse import quote_plus
from typing import Optional

import httpx
from lxml import html

from app.scraper.http_client import create_client
from app.scraper.stealth import get_random_delay, get_random_headers
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def parse_search_results(raw_html: str) -> list[dict]:
    tree = html.fromstring(raw_html)
    items = tree.xpath('//div[@data-asin and @data-asin!=""][@data-component-type="s-search-result"]')
    results = []
    for item in items:
        asin = item.get("data-asin", "").strip()
        if not asin or len(asin) != 10:
            continue

        is_sponsored = bool(
            item.xpath('.//span[contains(text(),"Sponsored")]') or
            item.xpath('.//*[contains(@class,"s-sponsored-label")]')
        )

        title_el = item.xpath('.//h2//span/text()')
        title = title_el[0].strip() if title_el else None

        price_el = item.xpath('.//span[contains(@class,"a-price-whole")]/text()')
        price = price_el[0].strip().replace(",", "") if price_el else None

        results.append({
            "asin": asin,
            "title": title,
            "price": price,
            "is_sponsored": is_sponsored,
        })
    return results


async def track_keyword_rankings(
    keyword: str,
    target_asins: set[str],
    max_pages: int = 4,
) -> list[dict]:
    client = create_client(timeout=15)
    rankings = []
    position_offset = 0

    try:
        for page in range(1, max_pages + 1):
            url = f"https://www.amazon.in/s?k={quote_plus(keyword)}&page={page}"
            delay = get_random_delay(2.0, 4.0)
            await asyncio.sleep(delay)

            try:
                client.headers.update(get_random_headers())
                response = await client.get(url)
                if response.status_code != 200:
                    logger.warning(f"Keyword search page {page} returned {response.status_code}")
                    break

                items = parse_search_results(response.text)
                if not items:
                    break

                for idx, item in enumerate(items):
                    absolute_position = position_offset + idx + 1
                    if item["asin"] in target_asins:
                        rankings.append({
                            "asin": item["asin"],
                            "keyword": keyword,
                            "rank_position": absolute_position,
                            "page_number": page,
                            "is_sponsored": item["is_sponsored"],
                            "title": item["title"],
                        })

                position_offset += len(items)

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                logger.warning(f"Keyword tracking page {page} failed: {e}")
                continue

    finally:
        await client.aclose()

    return rankings


async def track_multiple_keywords(
    keywords: list[str],
    target_asins: set[str],
    max_pages: int = 4,
) -> dict[str, list[dict]]:
    results = {}
    for keyword in keywords:
        rankings = await track_keyword_rankings(keyword, target_asins, max_pages)
        results[keyword] = rankings
        await asyncio.sleep(get_random_delay(3.0, 6.0))
    return results
