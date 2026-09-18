/**
 * Product-page extractors, ported from `app/scraper/parsers.py`.
 *
 * **Selector ORDER is the rule, not an implementation detail.** `extractPrice` tries six selectors
 * and takes the first that matches; reordering them changes which price a page reports. The order
 * below is the Python order, verbatim.
 *
 * **Every selection takes `.first()`, and that is load-bearing.** lxml's `tree.xpath(...)` returns
 * a list and the Python code reads `result[0]`; cheerio's `.text()` concatenates the text of
 * *every* match. Measured on the real `B0D817HX57`, whose last-resort price selector matches 21
 * nodes:
 *
 *     $(sel).text()         -> "294.494.597.35294.494.47.597.175.181.450"
 *     $(sel).first().text() -> "294."
 *
 * `₹294..00` is obviously wrong; on a page with two matches the concatenation produces a
 * plausible number instead, which is the dangerous case.
 *
 * XPath `contains(@class,"x")` becomes the CSS attribute-substring selector `[class*="x"]`, and the
 * one union — `//table[contains(@id,"productDetails")]//tr | //div[@id="…"]//li` — becomes a comma
 * list, verified to return the same 11 rows on a real page.
 */
import * as cheerio from "cheerio";
import type { CheerioAPI } from "cheerio";

import {
  detectCaptcha,
  detectDogPage,
  detectUnavailable,
  hasTitle,
} from "./guards.js";

/** What one scraped page yields. Mirrors the Python dict, with the statuses as a closed union. */
export type ScrapeStatus =
  | "OK"
  | "Blocked (CAPTCHA)"
  | "Not Found"
  | "Parse Error (no title)"
  | "Unavailable";

export interface ProductRow {
  asin: string;
  url: string;
  status: ScrapeStatus;
  title: string | null;
  price: string | null;
  rating: string | null;
  ratingCount: string | null;
  bsr: string | null;
  bsrNumeric: number | null;
  bsrCategory: string | null;
  seller: string | null;
  fulfillment: string | null;
  deal: string;
  useBy: string | null;
}

/** The six price selectors, in the order the Python file tries them. Order IS the rule. */
const PRICE_SELECTORS = [
  'span[class*="priceToPay"] span[class*="a-price-whole"]',
  '#corePrice_feature_div span[class*="a-price-whole"]',
  "span#priceblock_ourprice",
  "span#priceblock_dealprice",
  '#apex_offerDisplay_desktop span[class*="a-price-whole"]',
  'span[class*="a-price"] span[class*="a-price-whole"]',
] as const;

export function extractTitle($: CheerioAPI): string | null {
  const text = $("#productTitle").first().text().trim();
  return text || null;
}

/**
 * The price as `₹123` or `₹123.45`, or null.
 *
 * The rupee symbol and the whole/fraction split are Amazon's markup: the integer part and the
 * decimals live in separate spans, so the fraction is looked up with the SAME selector that
 * matched the whole — swapping the class name, exactly as the Python does.
 */
export function extractPrice($: CheerioAPI): string | null {
  for (const selector of PRICE_SELECTORS) {
    const whole = $(selector).first().text().trim().replace(/,/g, "");
    if (!whole) continue;
    // Some of these selectors match a full price string rather than a whole-part span
    // (`#priceblock_ourprice`), in which case it already carries the symbol.
    if (whole.includes("₹")) return whole;
    const fraction = $(selector.replace("a-price-whole", "a-price-fraction"))
      .first()
      .text()
      .trim();
    // The whole-part span's text often ends in the decimal separator ("294."), so it is stripped
    // before the fraction is appended — otherwise the result reads "₹294..00".
    const cleaned = whole.replace(/[.,]$/, "");
    return fraction ? `₹${cleaned}.${fraction}` : `₹${cleaned}`;
  }
  return null;
}

export function extractRating($: CheerioAPI): string | null {
  const fromTitle = $("#acrPopover").first().attr("title") ?? "";
  const m1 = /([\d.]+)/.exec(fromTitle);
  if (m1?.[1]) return m1[1];

  const outOf = $('span[data-hook="rating-out-of-text"]').first().text();
  const m2 = /([\d.]+)/.exec(outOf);
  if (m2?.[1]) return m2[1];

  const iconText = $('i[class*="a-icon-star"] span').first().text();
  const m3 = /([\d.]+)/.exec(iconText);
  return m3?.[1] ?? null;
}

export function extractRatingCount($: CheerioAPI): string | null {
  const selectors = [
    "#acrCustomerReviewText",
    "#acrCustomerReviewLink span",
    'span[data-hook="total-review-count"]',
  ];
  for (const selector of selectors) {
    // Each MATCH is tested, not just the first, because the Python version loops the node list —
    // `#acrCustomerReviewLink span` matches several spans and only some carry the number.
    const nodes = $(selector).toArray();
    for (const node of nodes) {
      const match = /([\d,]+)/.exec($(node).text());
      if (match?.[1]) return match[1].replace(/,/g, "");
    }
  }
  return null;
}

/** `#1,234 in Category` from any of the three places Amazon puts it. */
export function extractBsr($: CheerioAPI): string | null {
  const pattern = /#([\d,]+)\s+in\s+(.+?)(?:\(|$)/;

  const salesRank = $("#SalesRank").text().replace(/\s+/g, " ").trim();
  if (salesRank) {
    const match = pattern.exec(salesRank);
    if (match?.[1] && match[2]) {
      return `#${match[1].replace(/,/g, "")} in ${match[2].trim()}`;
    }
  }

  // The XPath union, as a CSS comma list. Verified to return the same 11 rows on a real page.
  const rows = $(
    'table[id*="productDetails"] tr, #detailBulletsWrapper_feature_div li',
  ).toArray();
  for (const row of rows) {
    const text = $(row).text().replace(/\s+/g, " ").trim();
    if (
      text.includes("Best Sellers Rank") ||
      text.toLowerCase().includes("best seller")
    ) {
      const match = pattern.exec(text);
      if (match?.[1] && match[2]) {
        return `#${match[1].replace(/,/g, "")} in ${match[2].trim()}`;
      }
    }
  }

  const bullets = $("#detailBulletsWrapper_feature_div")
    .text()
    .replace(/\s+/g, " ")
    .trim();
  if (bullets) {
    const match = pattern.exec(bullets);
    if (match?.[1] && match[2]) {
      return `#${match[1].replace(/,/g, "")} in ${match[2].trim()}`;
    }
  }

  return null;
}

export function extractBsrNumeric($: CheerioAPI): number | null {
  const bsr = extractBsr($);
  if (!bsr) return null;
  const match = /#([\d,]+)/.exec(bsr);
  if (!match?.[1]) return null;
  return Number.parseInt(match[1].replace(/,/g, ""), 10);
}

export function extractBsrCategory($: CheerioAPI): string | null {
  const bsr = extractBsr($);
  if (!bsr) return null;
  const match = /in\s+(.+)/.exec(bsr);
  return match?.[1]?.trim() ?? null;
}

export function extractSeller($: CheerioAPI): string | null {
  const trigger = $("#sellerProfileTriggerId").first().text().trim();
  if (trigger) return trigger;

  const merchantLink = $("#merchant-info a").first().text().trim();
  if (merchantLink) return merchantLink;

  const merchantInfo = $("#merchant-info").text().trim();
  if (merchantInfo.toLowerCase().includes("amazon")) return "Amazon";

  const tabular = $('div[tabular-attribute-name="Sold by"] span')
    .first()
    .text()
    .trim();
  return tabular || null;
}

/**
 * `FBA` / `FBM` / `Easy Ship`, or null when the buy box says nothing.
 *
 * The rule order is the rule: "ships from Amazon" or "sold by Amazon" wins before the Easy Ship
 * check, and a bare "ships from" that is NOT Amazon means FBM. The `[\s\S]{0,30}` gap in the
 * Python regexes matters — the words are separated by markup, so the two are not adjacent.
 */
export function extractFulfillment($: CheerioAPI): string | null {
  const text = $(
    '#tabular-buybox, #merchant-info, [class*="offer-display-feature-text"]',
  )
    .text()
    .replace(/\s+/g, " ")
    .toLowerCase()
    .trim();

  if (!text) return null;

  const shipsFromAmazon = /ships?\s*from[\s\S]{0,30}amazon/.test(text);
  const soldByAmazon = /sold\s*by[\s\S]{0,30}amazon/.test(text);
  if (shipsFromAmazon || soldByAmazon) return "FBA";

  if (text.includes("easy ship") || text.includes("easyship")) return "Easy Ship";
  if (/ships?\s*from/.test(text) && !shipsFromAmazon) return "FBM";
  if (
    text.includes("fulfilled by amazon") ||
    text.includes("fulfilment by amazon")
  ) {
    return "FBA";
  }
  return "FBM";
}

/**
 * Is the red deal badge on the page? `"Yes"` / `"No"`.
 *
 * **Structure, not vocabulary.** An earlier Python version matched a hardcoded phrase list and
 * reported "No" for every product during the Freedom Sale, because the badge read "Freedom Sale
 * Deal" and that string was not on the list. Amazon renames the sale every few months and the
 * failure is SILENT — every row reads No, which is indistinguishable from having no deals.
 *
 * Three traps, all of them live:
 *
 * 1. `#dealBadge_feature_div` is on EVERY product page, deal or not, so its presence cannot be
 *    the test.
 * 2. The `aok-hidden` screen-reader spans hold the badge text with the countdown
 *    unsubstituted — `"Freedom Sale Deal NO_OF_HOURS hours"` — because that JavaScript never runs
 *    here. Text containing `NO_OF_` is rejected. Same trap as the FBA transportation quote coming
 *    back as the literal string `"$cost.amount"`.
 * 3. Ad carousels carry deal markup for OTHER products — 7 such mentions on one measured page — so
 *    the painter check is scoped to the badge region.
 */
export function extractDeal($: CheerioAPI): string {
  const visible = $("#dealBadgeSupportingText").text().replace(/\s+/g, " ").trim();
  if (visible && !visible.includes("NO_OF_")) return "Yes";

  const painter = $(
    '[data-feature-name="dealBadge"] [data-csa-c-painter="dp-deal"], ' +
      'span[class*="dealBadge"][data-csa-c-painter="dp-deal"]',
  );
  if (painter.length > 0) return "Yes";

  return "No";
}

/** The use-by / best-before date as Amazon prints it, or null. */
export function extractUseBy($: CheerioAPI): string | null {
  const expiry = $("#expiryDate_feature_div").text().replace(/\s+/g, " ").trim();
  if (expiry) {
    const labelled =
      /(?:use\s*by|best\s*before|expiry|expiration)[:\s]*(\d{1,2}\s*\w{3,9}\s*\d{2,4})/i.exec(
        expiry,
      );
    if (labelled?.[1]) return labelled[1].trim();
    const bare = /(\d{1,2}\s+[A-Z]{3}\s+\d{4})/.exec(expiry);
    if (bare?.[1]) return bare[1].trim();
  }

  const shelf = $('[id*="freshShelfLife"]').text().replace(/\s+/g, " ").trim();
  if (shelf) {
    const match =
      /(?:use\s*by|best\s*before|expiry)[:\s]*(\d{1,2}[\s/-]\w{3,9}[\s/-]\d{2,4})/i.exec(
        shelf,
      );
    if (match?.[1]) return match[1].trim();
  }

  const details = $(
    'table[id*="productDetails"], #detailBulletsWrapper_feature_div, ' +
      "#productDetails_techSpec_section_1, #productDetails_detailBullets_sections1",
  )
    .text()
    .replace(/\s+/g, " ")
    .trim();

  const patterns = [
    /(?:use\s*by|best\s*before|expiry|expiration|exp\.?\s*date)[:\s]*(\d{1,2}[\s/-]\w{3,9}[\s/-]\d{2,4})/i,
    /(?:use\s*by|best\s*before|expiry|expiration)[:\s]*(\d{4}[-/]\d{2}[-/]\d{2})/i,
    /(?:use\s*by|best\s*before|expiry|expiration)[:\s]*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})/i,
  ];
  for (const pattern of patterns) {
    const match = pattern.exec(details);
    if (match?.[1]) return match[1].trim();
  }
  return null;
}

function emptyRow(asin: string, status: ScrapeStatus): ProductRow {
  return {
    asin,
    url: `https://www.amazon.in/dp/${asin}`,
    status,
    title: null,
    price: null,
    rating: null,
    ratingCount: null,
    bsr: null,
    bsrNumeric: null,
    bsrCategory: null,
    seller: null,
    fulfillment: null,
    deal: "No",
    useBy: null,
  };
}

/**
 * One page to one row. **The guards run first and they decide what the row may contain.**
 *
 * The unavailable branch keeps title, rating and BSR while explicitly nulling price, seller,
 * fulfillment and deal — so a listing that cannot be bought cannot carry a price. Measured: on
 * `B0D817HX57` the price chain would otherwise return a real ₹294 belonging to a *different*
 * product in an ad carousel, and the row would look completely normal.
 */
export function parseProductPage(rawHtml: string, asin: string): ProductRow {
  const $ = cheerio.load(rawHtml);

  if (detectCaptcha($)) return emptyRow(asin, "Blocked (CAPTCHA)");
  if (detectDogPage($)) return emptyRow(asin, "Not Found");
  if (!hasTitle($)) return emptyRow(asin, "Parse Error (no title)");

  const title = extractTitle($);

  if (detectUnavailable($)) {
    return {
      ...emptyRow(asin, "Unavailable"),
      title,
      rating: extractRating($),
      ratingCount: extractRatingCount($),
      bsr: extractBsr($),
      bsrNumeric: extractBsrNumeric($),
      bsrCategory: extractBsrCategory($),
      useBy: extractUseBy($),
    };
  }

  return {
    asin,
    url: `https://www.amazon.in/dp/${asin}`,
    status: "OK",
    title,
    price: extractPrice($),
    rating: extractRating($),
    ratingCount: extractRatingCount($),
    bsr: extractBsr($),
    bsrNumeric: extractBsrNumeric($),
    bsrCategory: extractBsrCategory($),
    seller: extractSeller($),
    fulfillment: extractFulfillment($),
    deal: extractDeal($),
    useBy: extractUseBy($),
  };
}
