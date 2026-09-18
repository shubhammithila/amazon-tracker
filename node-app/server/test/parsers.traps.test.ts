/**
 * The traps the three saved pages CANNOT exercise, on minimal synthetic HTML.
 *
 * **Both of these were found by mutation, after the differential test passed.** That test compares
 * against the Python parser on three real pages and is the right acceptance bar, but a fixture can
 * only prove what it happens to contain:
 *
 * * Dropping `.first()` from the price chain SURVIVED, because on both OK pages the winning
 *   selector matches exactly ONE node (measured: 1, 1) so concatenation changes nothing — and the
 *   page that does match 21 nodes is intercepted by the unavailable guard before price is read.
 * * Removing the `NO_OF_` placeholder check SURVIVED, because none of the three pages carries the
 *   unsubstituted screen-reader text.
 *
 * So these are not duplicates of the differential test. They cover shapes that exist on Amazon and
 * are absent from the fixtures, which is the honest reason to write synthetic HTML rather than the
 * usual reason (convenience).
 */
import { describe, expect, it } from "vitest";
import * as cheerio from "cheerio";

import { extractDeal, extractPrice, parseProductPage } from "../src/scraper/parsers.js";
import { detectBotInterstitial, detectUnavailable } from "../src/scraper/guards.js";

/** A buyable page: the guards must let it through so the extractors actually run. */
function buyable(inner: string): string {
  return `<html><body>
    <span id="productTitle">A Product</span>
    <button id="add-to-cart-button">Add to Cart</button>
    ${inner}
  </body></html>`;
}

describe("cheerio's .text() concatenates, so every selection takes .first()", () => {
  it("reads ONE price when the winning selector matches several nodes", () => {
    // The real shape of the bug: `priceToPay` appears twice (Amazon renders a mobile and a desktop
    // block on some layouts). Without `.first()` the whole part becomes "181181" and the fraction
    // "0000" — and `₹181181.0000` is obviously wrong, while on a page with a smaller second match
    // the result is merely a plausible WRONG number.
    const html = buyable(`
      <span class="priceToPay"><span class="a-price-whole">181</span><span class="a-price-fraction">00</span></span>
      <span class="priceToPay"><span class="a-price-whole">181</span><span class="a-price-fraction">00</span></span>
    `);
    expect(extractPrice(cheerio.load(html))).toBe("₹181.00");
  });

  it("does not let a recommended product's price win", () => {
    // Two DIFFERENT prices, the second belonging to a carousel item. Concatenation would produce
    // "₹181450.0000"; taking the wrong one would produce "₹450.00". Only the first is right.
    const html = buyable(`
      <span class="priceToPay"><span class="a-price-whole">181</span><span class="a-price-fraction">00</span></span>
      <span class="a-price"><span class="a-price-whole">450</span><span class="a-price-fraction">00</span></span>
    `);
    expect(extractPrice(cheerio.load(html))).toBe("₹181.00");
  });

  it("strips the trailing separator the whole-part span carries", () => {
    // Measured on real pages: the whole-part text is "294." including the separator, so a naive
    // join yields "₹294..00".
    const html = buyable(`
      <span class="priceToPay"><span class="a-price-whole">294.</span><span class="a-price-fraction">00</span></span>
    `);
    expect(extractPrice(cheerio.load(html))).toBe("₹294.00");
  });

  it("falls through the chain IN ORDER, because order is the rule", () => {
    // Only the sixth selector matches. It must still be tried, and the first-matching one wins.
    const html = buyable(`
      <span class="a-price"><span class="a-price-whole">99</span><span class="a-price-fraction">50</span></span>
    `);
    expect(extractPrice(cheerio.load(html))).toBe("₹99.50");
  });
});

describe("the deal badge: structure, never vocabulary", () => {
  it("rejects the UNSUBSTITUTED screen-reader text", () => {
    // The `aok-hidden` spans hold the countdown template because the JavaScript that fills it in
    // never runs here. "Freedom Sale Deal NO_OF_HOURS hours" is not a rendered badge, and reading
    // it would also match a page where the template shipped with no deal active.
    //
    // Same trap as the FBA transportation quote coming back as the literal string "$cost.amount".
    const html = buyable(
      `<span id="dealBadgeSupportingText">Freedom Sale Deal NO_OF_HOURS hours NO_OF_MINUTES minutes</span>`,
    );
    expect(extractDeal(cheerio.load(html))).toBe("No");
  });

  it("accepts a badge whose name nobody has seen before", () => {
    // The whole point of keying on structure: an earlier version matched a hardcoded phrase list
    // and reported "No" for every product during the Freedom Sale, silently.
    const html = buyable(
      `<span id="dealBadgeSupportingText">Some Future Sale Nobody Has Named Yet Deal</span>`,
    );
    expect(extractDeal(cheerio.load(html))).toBe("Yes");
  });

  it("is not fooled by the always-present badge container", () => {
    // `dealBadge_feature_div` is on EVERY product page, deal or not. Its presence cannot be the
    // test — that is the trap the old countdown-element check fell into.
    const html = buyable(`<div id="dealBadge_feature_div"></div>`);
    expect(extractDeal(cheerio.load(html))).toBe("No");
  });

  it("ignores a deal painted on a DIFFERENT product elsewhere on the page", () => {
    // Measured: 7 such mentions on one real page, from ad carousels.
    const html = buyable(`
      <div id="similarities_feature_div">
        <span data-csa-c-painter="dp-deal">someone else's deal</span>
      </div>
    `);
    expect(extractDeal(cheerio.load(html))).toBe("No");
  });

  it("accepts the painter marker when it IS in the badge region", () => {
    const html = buyable(`
      <div data-feature-name="dealBadge">
        <span data-csa-c-painter="dp-deal"></span>
      </div>
    `);
    expect(extractDeal(cheerio.load(html))).toBe("Yes");
  });
});

describe("detectUnavailable", () => {
  it("fires on the words", () => {
    const html = `<html><body>
      <span id="productTitle">A Product</span>
      <div id="availability">Currently unavailable.</div>
      <button id="add-to-cart-button">x</button>
    </body></html>`;
    expect(detectUnavailable(cheerio.load(html))).toBe(true);
  });

  it("fires on a title with NO buy box, which is the case the words miss", () => {
    // `B0D817HX57` in miniature: no availability message at all, just an absent buy box. This is
    // the signal that stops a carousel price being reported as the product's own.
    const html = `<html><body><span id="productTitle">A Product</span></body></html>`;
    expect(detectUnavailable(cheerio.load(html))).toBe(true);
  });

  it("does NOT fire on a normal buyable page", () => {
    const html = buyable(
      `<span class="priceToPay"><span class="a-price-whole">181</span></span>`,
    );
    expect(detectUnavailable(cheerio.load(html))).toBe(false);
  });

  it("treats a live price block as a buy box even with no add-to-cart button", () => {
    // Found by mutation: removing `priceToPay` from the buy-box test survived every other test,
    // because all three saved pages carry an add-to-cart button and so never exercise this branch.
    //
    // It matters because the three signals are alternatives, not a set. Amazon renders layouts
    // where the cart control is injected by JavaScript that never runs here while the price block
    // is in the served HTML — and treating that page as unavailable would null a price that is
    // genuinely current, which is the opposite of the bug the guard exists to prevent.
    const html = `<html><body>
      <span id="productTitle">A Product</span>
      <span class="priceToPay"><span class="a-price-whole">181</span><span class="a-price-fraction">00</span></span>
    </body></html>`;
    const $ = cheerio.load(html);
    expect(detectUnavailable($)).toBe(false);
    // ...and the price is therefore reported rather than cleared.
    expect(parseProductPage(html, "B0TEST00004").price).toBe("₹181.00");
  });

  it("treats buy-now as a buy box too", () => {
    // The second alternative, for the same reason.
    const html = `<html><body>
      <span id="productTitle">A Product</span>
      <button id="buy-now-button">Buy now</button>
    </body></html>`;
    expect(detectUnavailable(cheerio.load(html))).toBe(false);
  });

  it("clears price, seller, fulfillment and deal but KEEPS title and rating", () => {
    // The asymmetry is the design: an unavailable listing still has a real name and real reviews,
    // but its price, seller and deal are not current and must not persist as though they were.
    const html = `<html><body>
      <span id="productTitle">A Product</span>
      <div id="availability">Currently unavailable.</div>
      <span id="acrPopover" title="4.3 out of 5 stars"></span>
      <span class="a-price"><span class="a-price-whole">450</span></span>
      <div id="merchant-info">Sold by Someone</div>
      <span id="dealBadgeSupportingText">Limited time deal</span>
    </body></html>`;
    const row = parseProductPage(html, "B0TEST00001");
    expect(row.status).toBe("Unavailable");
    expect(row.price).toBeNull();
    expect(row.seller).toBeNull();
    expect(row.fulfillment).toBeNull();
    expect(row.deal).toBe("No");
    expect(row.title).toBe("A Product");
    expect(row.rating).toBe("4.3");
  });
});

describe("Amazon's bot interstitial", () => {
  // Measured on this machine during the port's differential test: the SECOND rapid request returns
  // HTTP 200 with 3,793 bytes reading "Click the button below to continue shopping". Both apps hit
  // it; the Python reports "Parse Error (no title)", which reads as a broken parser when the real
  // cause is rate limiting.
  const interstitial =
    "<html><head><title>Amazon.in</title></head><body>" +
    "<p>Click the button below to continue shopping</p>" +
    "<button>Continue shopping</button>" +
    "<a>Conditions of Use &amp; Sale</a><a>Privacy Notice</a>" +
    "</body></html>";

  it("is reported as rate limiting, not as a parse error", () => {
    const row = parseProductPage(interstitial, "B0CWGXYLT6");
    expect(row.status).toBe("Rate limited");
  });

  it("is detected by the phrase AND the small size together", () => {
    const $ = cheerio.load(interstitial);
    expect(detectBotInterstitial($, interstitial.length)).toBe(true);
    // A big page carrying the same words in a footer is NOT the interstitial. Either signal alone is
    // insufficient — the same mistake the always-present deal-badge container taught.
    expect(detectBotInterstitial($, 2_400_000)).toBe(false);
  });

  it("does not fire on a real product page", () => {
    const real = buyable(
      "<p>continue shopping</p>" +
        "<span class='priceToPay'><span class='a-price-whole'>181</span></span>",
    );
    expect(detectBotInterstitial(cheerio.load(real), real.length)).toBe(false);
    expect(parseProductPage(real, "B0CWGXYLT6").status).toBe("OK");
  });

  it("is checked BEFORE the no-title guard, or the cause is misreported", () => {
    // The interstitial has no title either, so a later check would claim it first and send the
    // reader to the selectors instead of to the rate limit.
    const row = parseProductPage(interstitial, "B0CWGXYLT6");
    expect(row.status).not.toBe("Parse Error (no title)");
  });
});

describe("the other two guards", () => {
  it("reports a CAPTCHA rather than a product with no data", () => {
    const html = `<html><body>
      <form action="/errors/validateCaptcha"><input name="field-keywords"></form>
    </body></html>`;
    const row = parseProductPage(html, "B0TEST00002");
    expect(row.status).toBe("Blocked (CAPTCHA)");
    expect(row.title).toBeNull();
  });

  it("reports a missing title as a parse error, not as an empty product", () => {
    // Every real page has #productTitle, so its absence after the CAPTCHA and dog-page checks
    // means the markup changed or the fetch was intercepted. A row of nulls would look like a
    // product that simply has no data.
    const row = parseProductPage("<html><body><p>nothing</p></body></html>", "B0TEST00003");
    expect(row.status).toBe("Parse Error (no title)");
  });
});
