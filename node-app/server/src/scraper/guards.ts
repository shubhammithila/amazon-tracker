/**
 * The four guards that decide whether a page's fields mean anything — ported BEFORE the extractors.
 *
 * **That order is the finding of the parser spike, not a preference.** The Python file reads
 * extractors-first with `parse_product_page` last, and porting it top-down produced a rewrite that
 * got 11 of 12 fields right on three real pages and the important one wrong: on `B0D817HX57` it
 * returned `₹294.00` where Python returned `null`. That ₹294 is a REAL price — belonging to a
 * different product, in an ad carousel — because the page has no buy box and the six-selector
 * price chain falls through to whatever else on the page looks like a price.
 *
 * `detectUnavailable` is what stops that: it clears price, seller, fulfillment and deal so stale
 * or foreign data cannot persist as though it were current. A rewrite without the guards writes a
 * neighbouring product's price against an unavailable ASIN and **the row looks entirely normal**.
 *
 * All four use cheerio with CSS selectors. The spike measured lxml's XPath as unavailable in Node:
 * `@xmldom/xmldom` dies on real Amazon HTML (`tag mismatch: "div" != "br"` — an unclosed `<br>`,
 * which every page has) and `jsdom` costs 2,240 ms and +511 MB per page against cheerio's 274 ms
 * and +39 MB, on a box with 951 MB running ten concurrent workers.
 */
import type { CheerioAPI } from "cheerio";

/**
 * Amazon is asking for a CAPTCHA, so nothing on this page is product data.
 *
 * `text()` on the whole document is deliberately avoided — it is a 2.3 MB string on these pages.
 * The phrase checks are scoped to the elements that carry them.
 */
export function detectCaptcha($: CheerioAPI): boolean {
  if ($('form[action="/errors/validateCaptcha"]').length > 0) return true;
  if ($('img[src*="captcha"]').length > 0) return true;

  // The two phrase checks from the Python version. Scoped to form and heading regions rather than
  // the document, because a 2.3 MB `:contains()` scan is the slowest possible way to ask.
  const captchaText = $("form, h4, p, div.a-box-inner").text();
  if (
    captchaText.includes("Enter the characters you see below") ||
    captchaText.includes("Type the characters")
  ) {
    return true;
  }
  return false;
}

/**
 * Amazon's "dogs of Amazon" 404 page — the ASIN does not exist.
 *
 * Distinguished from unavailable on purpose: a dog page means the listing is gone, while
 * unavailable means it exists and cannot currently be bought. They lead to different rows.
 */
export function detectDogPage($: CheerioAPI): boolean {
  if ($('img[alt*="sorry" i]').length > 0) return true;
  const body = $("h1, h2, h3, p, .a-spacing-base").text();
  if (body.includes("looking for was not found")) return true;
  if (body.toLowerCase().includes("no results")) return true;
  return false;
}

/** The phrases Amazon uses for a listing that exists but cannot be bought. */
const UNAVAILABLE_PHRASES = [
  "currently unavailable",
  "out of stock",
  "we don't know when or if",
  // Kept LAST and kept deliberately: it is a substring of the others, so ordering does not matter
  // for correctness, but it also matches "temporarily unavailable" and similar wordings Amazon
  // introduces without warning. A narrower list fails silently — the deal-badge lesson.
  "unavailable",
];

/**
 * The listing exists but cannot be bought, so **price, seller, fulfillment and deal must be
 * cleared rather than read**.
 *
 * Two independent signals, and the second is the one that matters:
 *
 * 1. The availability regions say so in words.
 * 2. **There is a title but NO buy box at all.** Measured on `B0D817HX57`: no
 *    `#corePrice_feature_div`, no `priceToPay`, no add-to-cart. The price chain's last-resort
 *    selector then matches **21** nodes belonging to recommended products, and the first of them
 *    is a plausible ₹294.
 *
 * Without signal 2 the page reads as a normal product at someone else's price.
 */
export function detectUnavailable($: CheerioAPI): boolean {
  const availability = $(
    "#availability, #availability_feature_div, #outOfStock",
  )
    .text()
    .toLowerCase();
  if (UNAVAILABLE_PHRASES.some((phrase) => availability.includes(phrase))) {
    return true;
  }

  const hasBuyBox =
    $("#add-to-cart-button").length > 0 ||
    $("#buy-now-button").length > 0 ||
    $('span[class*="priceToPay"]').length > 0;

  // A title with no buy box anywhere. The title check is what keeps this from firing on a
  // CAPTCHA or error page, which have no buy box either and are already handled above.
  const hasTitle = $("#productTitle").length > 0;
  return hasTitle && !hasBuyBox;
}

/**
 * The page rendered a product at all.
 *
 * A missing title is the honest signal for "this is not a product page and I do not know why" —
 * every real page has `#productTitle`, so its absence after the CAPTCHA and dog-page checks means
 * the markup changed or the fetch was intercepted. Returning a row of nulls instead would look
 * like a product with no data.
 */
export function hasTitle($: CheerioAPI): boolean {
  return $("#productTitle").first().text().trim().length > 0;
}
