/**
 * Parser-library spike: can Node reproduce lxml's XPath on REAL Amazon HTML, and how fast?
 *
 * Not a style preference. `app/scraper/parsers.py` is 394 lines of XPath tuned against live
 * pages, and its value is in the tuning: `extract_price` tries 6 selectors in order,
 * `extract_deal` exists because a phrase list silently reported "No" for every product during
 * the Freedom Sale. Porting that is re-verification, not translation — so the library is chosen
 * on measured evidence against saved real pages, before any of it is rewritten.
 *
 * Three candidates, and the two axes that matter: does it survive Amazon's real-world HTML
 * (2.4 MB, unclosed tags, inline scripts), and what does it cost per page on a 951 MB box.
 */
import { readFileSync } from "node:fs";
import { performance } from "node:perf_hooks";

import * as cheerio from "cheerio";
import { JSDOM } from "jsdom";
import xpath from "xpath";
import { DOMParser } from "@xmldom/xmldom";

const ASINS = ["B0CWGXYLT6", "B0CY84RYRG", "B0D817HX57"];
const pages = new Map(
  ASINS.map((a) => [a, readFileSync(new URL(`./${a}.html`, import.meta.url), "utf8")]),
);

const mb = (n) => `${(n / 1024 / 1024).toFixed(1)} MB`;
const rss = () => process.memoryUsage().rss;

function time(label, fn) {
  const before = rss();
  const t0 = performance.now();
  let out, error = null;
  try {
    out = fn();
  } catch (e) {
    error = e.message.slice(0, 90);
  }
  const ms = performance.now() - t0;
  return { label, ms, deltaRss: rss() - before, out, error };
}

/* The three selectors that decide this. Each is taken verbatim from parsers.py and each
   exercises a different capability:
     - title:  trivial by id, every library can do it (control)
     - price:  descendant + class-contains, the 6-selector fallback chain
     - bsr:    `//table[contains(@id,"x")]//tr | //div[@id="y"]//li` — a UNION with
               contains(). This is the one with no clean CSS equivalent. */
const XP = {
  title: '//*[@id="productTitle"]/text()',
  price:
    '//span[contains(@class,"priceToPay")]//span[contains(@class,"a-price-whole")]/text()',
  bsrUnion:
    '//table[contains(@id,"productDetails")]//tr | //div[@id="detailBulletsWrapper_feature_div"]//li',
  dealText: '//*[@id="dealBadgeSupportingText"]//text()',
  painter: '//*[@data-csa-c-painter="dp-deal"]',
};

console.log("=".repeat(78));
console.log("PARSER LIBRARY SPIKE — real Amazon pages");
console.log("=".repeat(78));
for (const [asin, html] of pages) {
  console.log(`  ${asin}  ${mb(html.length)}`);
}
console.log();

const results = [];

// ── 1. cheerio: CSS only. Needs every selector rewritten. ──
{
  const asin = ASINS[0];
  const r = time("cheerio", () => {
    const $ = cheerio.load(pages.get(asin));
    return {
      title: $("#productTitle").text().trim().slice(0, 40),
      // The rewrite: contains(@class) becomes an attribute-substring selector.
      price: $('span[class*="priceToPay"] span[class*="a-price-whole"]').first().text().trim(),
      // The union rewrite — cheerio takes a comma list, which is the CSS equivalent.
      bsrRows: $('table[id*="productDetails"] tr, #detailBulletsWrapper_feature_div li').length,
      dealText: $("#dealBadgeSupportingText").text().trim().slice(0, 30),
      painter: $('[data-csa-c-painter="dp-deal"]').length,
    };
  });
  results.push(r);
  console.log("── cheerio (CSS, selectors REWRITTEN) ──");
  console.log(r.error ? `  ERROR ${r.error}` : `  ${JSON.stringify(r.out)}`);
  console.log(`  ${r.ms.toFixed(0)} ms   rss +${mb(r.deltaRss)}\n`);
}

// ── 2. xpath + @xmldom/xmldom: keeps the selectors IDENTICAL, but parses XML strictly. ──
{
  const asin = ASINS[0];
  const r = time("xpath+xmldom", () => {
    // Silence the flood of warnings real HTML produces; we only care whether it survives.
    const doc = new DOMParser({
      onError: () => {},
      locator: {},
    }).parseFromString(pages.get(asin), "text/html");
    const sel = (e) => xpath.select(e, doc);
    return {
      title: String(sel(XP.title)[0]?.data ?? "").trim().slice(0, 40),
      price: String(sel(XP.price)[0]?.data ?? "").trim(),
      bsrRows: sel(XP.bsrUnion).length,
      painter: sel(XP.painter).length,
    };
  });
  results.push(r);
  console.log("── xpath + @xmldom/xmldom (selectors IDENTICAL to lxml) ──");
  console.log(r.error ? `  ERROR ${r.error}` : `  ${JSON.stringify(r.out)}`);
  console.log(`  ${r.ms.toFixed(0)} ms   rss +${mb(r.deltaRss)}\n`);
}

// ── 3. jsdom: real HTML parsing + real XPath via document.evaluate. ──
{
  const asin = ASINS[0];
  const r = time("jsdom", () => {
    const dom = new JSDOM(pages.get(asin));
    const doc = dom.window.document;
    const all = (expr) => {
      const it = doc.evaluate(expr, doc, null, 5 /* ORDERED_NODE_ITERATOR */, null);
      const out = [];
      for (let n = it.iterateNext(); n; n = it.iterateNext()) out.push(n);
      return out;
    };
    return {
      title: (all(XP.title)[0]?.nodeValue ?? "").trim().slice(0, 40),
      price: (all(XP.price)[0]?.nodeValue ?? "").trim(),
      bsrRows: all(XP.bsrUnion).length,
      dealText: (all(XP.dealText)[0]?.nodeValue ?? "").trim().slice(0, 30),
      painter: all(XP.painter).length,
    };
  });
  results.push(r);
  console.log("── jsdom (real HTML parser + real XPath) ──");
  console.log(r.error ? `  ERROR ${r.error}` : `  ${JSON.stringify(r.out)}`);
  console.log(`  ${r.ms.toFixed(0)} ms   rss +${mb(r.deltaRss)}\n`);
}

// ── Throughput: the engine runs 10 concurrent workers, so per-page cost is multiplied. ──
console.log("=".repeat(78));
console.log("THROUGHPUT — all 3 pages, 5 rounds (the 10-worker engine multiplies this)");
console.log("=".repeat(78));

function bench(label, parseOne) {
  const t0 = performance.now();
  const before = rss();
  let n = 0;
  for (let round = 0; round < 5; round++) {
    for (const html of pages.values()) {
      try {
        parseOne(html);
        n++;
      } catch {
        /* counted as a failure below */
      }
    }
  }
  const ms = performance.now() - t0;
  console.log(
    `  ${label.padEnd(16)} ${n}/15 pages  ${ms.toFixed(0)} ms total  ` +
      `${(ms / Math.max(n, 1)).toFixed(0)} ms/page  rss +${mb(rss() - before)}`,
  );
}

bench("cheerio", (html) => {
  const $ = cheerio.load(html);
  $("#productTitle").text();
  $('table[id*="productDetails"] tr, #detailBulletsWrapper_feature_div li').length;
});

bench("xpath+xmldom", (html) => {
  const doc = new DOMParser({ onError: () => {}, locator: {} }).parseFromString(html, "text/html");
  xpath.select(XP.title, doc);
  xpath.select(XP.bsrUnion, doc);
});

bench("jsdom", (html) => {
  const doc = new JSDOM(html).window.document;
  doc.evaluate(XP.title, doc, null, 5, null).iterateNext();
  const it = doc.evaluate(XP.bsrUnion, doc, null, 5, null);
  let c = 0;
  for (let x = it.iterateNext(); x; x = it.iterateNext()) c++;
});

console.log();
console.log("Ground truth from the Python parser (spike/python_expected.json):");
const expected = JSON.parse(
  readFileSync(new URL("./python_expected.json", import.meta.url), "utf8"),
);
for (const [asin, row] of Object.entries(expected)) {
  console.log(
    `  ${asin}  price=${row.price}  rating=${row.rating}  deal=${row.deal}  bsr=${row.bsr}`,
  );
}
