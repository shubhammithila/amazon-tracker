/* Does cheerio reproduce the Python parser EXACTLY on all 3 real pages?
   The acceptance bar for the port is agreement with the original, not self-consistency. */
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";

const expected = JSON.parse(readFileSync("./python_expected.json", "utf8"));

// Ported from parsers.py, selector order preserved — order IS the rule.
function price($) {
  const chains = [
    'span[class*="priceToPay"] span[class*="a-price-whole"]',
    '#corePrice_feature_div span[class*="a-price-whole"]',
    "span#priceblock_ourprice",
    "span#priceblock_dealprice",
    '#apex_offerDisplay_desktop span[class*="a-price-whole"]',
    'span[class*="a-price"] span[class*="a-price-whole"]',
  ];
  for (const sel of chains) {
    const whole = $(sel).first().text().trim().replace(/,/g, "");
    if (whole) {
      const frac = $(sel.replace("a-price-whole", "a-price-fraction")).first().text().trim();
      return frac ? `₹${whole}.${frac}` : `₹${whole}`;
    }
  }
  return null;
}
function rating($) {
  const t = $("#acrPopover").attr("title") || "";
  let m = t.match(/([\d.]+)/); if (m) return m[1];
  m = ($('span[data-hook="rating-out-of-text"]').first().text() || "").match(/([\d.]+)/);
  return m ? m[1] : null;
}
function ratingCount($) {
  for (const sel of ["#acrCustomerReviewText", "#acrCustomerReviewLink span",
                     'span[data-hook="total-review-count"]']) {
    const m = ($(sel).first().text() || "").match(/([\d,]+)/);
    if (m) return m[1].replace(/,/g, "");
  }
  return null;
}
function deal($) {
  // Structure, not vocabulary — the Freedom Sale lesson.
  const txt = $("#dealBadgeSupportingText").text().trim();
  if (txt && !txt.includes("NO_OF_")) return "Yes";
  return $('#dealBadge_feature_div [data-csa-c-painter="dp-deal"]').length ? "Yes" : "No";
}

let fail = 0;
for (const [asin, want] of Object.entries(expected)) {
  const $ = cheerio.load(readFileSync(`./${asin}.html`, "utf8"));
  const got = { price: price($), rating: rating($), rating_count: ratingCount($), deal: deal($) };
  for (const k of ["price", "rating", "rating_count", "deal"]) {
    const w = want[k] === null ? null : String(want[k]);
    const g = got[k] === null ? null : String(got[k]);
    const ok = w === g;
    if (!ok) fail++;
    console.log(`  ${ok ? "OK  " : "DIFF"} ${asin} ${k.padEnd(13)} python=${w}  node=${g}`);
  }
}
console.log(fail ? `\n${fail} FIELD(S) DISAGREE` : "\nAll fields agree across all pages.");
