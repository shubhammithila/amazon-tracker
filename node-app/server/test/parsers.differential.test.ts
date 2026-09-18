/**
 * **The acceptance bar for the port: the Node parser must agree with the PYTHON one, field by
 * field, on real Amazon pages.**
 *
 * Not "the Node tests pass". The goal is to replace the Python app, so self-consistency proves
 * nothing — a parser that satisfies its own expectations while disagreeing with the original is
 * precisely the silent failure the Freedom Sale deal badge was, where every row read "No" and that
 * was indistinguishable from having no deals.
 *
 * Ground truth is `test/fixtures/python_expected.json`, produced by running
 * `app/scraper/parsers.parse_product_page` over the same three saved pages. The pages are real,
 * fetched live (2.2–2.3 MB each), and deliberately cover three different outcomes:
 *
 *     B0CWGXYLT6  OK, has a deal
 *     B0CY84RYRG  OK, has a deal, different price shape
 *     B0D817HX57  UNAVAILABLE — no buy box, and the price chain would otherwise return
 *                 ₹294 belonging to a DIFFERENT product in an ad carousel
 *
 * The third is the one that matters and is why the fixtures are worth their size. It is also the
 * page that exposed both spike findings: cheerio's `.text()` concatenating 21 matches, and the
 * guards having to run before the extractors.
 *
 * The `.html` files are gitignored (2.3 MB each, re-fetchable) while this test and the ground
 * truth are tracked. If the pages are absent the test SKIPS with an explanation rather than
 * passing vacuously — a green run that checked nothing is worse than a red one.
 */
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { parseProductPage } from "../src/scraper/parsers.js";
import {
  detectUnavailable,
  detectCaptcha,
  detectDogPage,
} from "../src/scraper/guards.js";
import * as cheerio from "cheerio";

// `fileURLToPath`, not `url.pathname`: the repo path contains a space, which a URL
// percent-encodes to %20 and `readFileSync` then cannot find.
const HERE = dirname(fileURLToPath(import.meta.url));
const PAGES = join(HERE, "..", "..", "..", "spike");
const EXPECTED = JSON.parse(
  readFileSync(join(HERE, "fixtures", "python_expected.json"), "utf8"),
) as Record<string, Record<string, unknown>>;

/** Python's snake_case field -> the TypeScript camelCase one. */
const FIELD_MAP: Record<string, string> = {
  status: "status",
  title: "title",
  price: "price",
  rating: "rating",
  rating_count: "ratingCount",
  bsr: "bsr",
  bsr_numeric: "bsrNumeric",
  bsr_category: "bsrCategory",
  seller: "seller",
  fulfillment: "fulfillment",
  deal: "deal",
  use_by: "useBy",
};

function pagePath(asin: string): string {
  return join(PAGES, `${asin}.html`);
}

const available = Object.keys(EXPECTED).filter((asin) => existsSync(pagePath(asin)));

describe("the Node parser agrees with the Python parser on real pages", () => {
  if (available.length === 0) {
    it.skip(
      "SKIPPED: the saved pages are absent (gitignored, 2.3 MB each). " +
        "Re-fetch them into spike/ to run the differential test.",
      () => {},
    );
    return;
  }

  for (const asin of available) {
    const want = EXPECTED[asin]!;

    describe(`${asin} (python status: ${String(want["status"])})`, () => {
      const html = readFileSync(pagePath(asin), "utf8");
      const got = parseProductPage(html, asin) as unknown as Record<string, unknown>;

      // `status` first: if the two disagree about whether the page is usable, every other field
      // comparison is meaningless, and the Python status carries the guard's verdict.
      it("agrees on status", () => {
        // Python's "Unavailable" branch is reported through its own status value here; the Python
        // dict omits `status` on that path and the port names it explicitly. Both must agree that
        // the page is or is not a normal OK product page.
        const pythonStatus = String(want["status"] ?? "Unavailable");
        expect(got["status"]).toBe(pythonStatus);
      });

      for (const [pyField, tsField] of Object.entries(FIELD_MAP)) {
        if (pyField === "status") continue;
        if (!(pyField in want)) continue;

        it(`agrees on ${pyField}`, () => {
          const expectedValue = want[pyField];
          const actual = got[tsField];
          // Compared as strings because Python writes the numeric BSR as a number and the rating
          // as a string; the QUESTION is whether the two parsers read the same value off the page,
          // not whether they chose the same JSON type for it.
          const norm = (v: unknown) =>
            v === null || v === undefined ? null : String(v);
          expect(norm(actual)).toBe(norm(expectedValue));
        });
      }
    });
  }
});

describe("the guards, on the page that proved they are needed", () => {
  const asin = "B0D817HX57";
  const path = pagePath(asin);

  it.skipIf(!existsSync(path))(
    "detects the unavailable listing rather than reading a carousel price",
    () => {
      const html = readFileSync(path, "utf8");
      const $ = cheerio.load(html);

      expect(detectUnavailable($)).toBe(true);
      expect(detectCaptcha($)).toBe(false);
      expect(detectDogPage($)).toBe(false);

      // The whole point: price must be NULL, not the ₹294 that a rewrite without the guard
      // returns — a real price belonging to a different product.
      const row = parseProductPage(html, asin);
      expect(row.price).toBeNull();
      expect(row.seller).toBeNull();
      expect(row.fulfillment).toBeNull();
      expect(row.deal).toBe("No");

      // ...while the fields that ARE still meaningful survive, which is why this is not simply a
      // rejected page.
      expect(row.title).not.toBeNull();
      expect(row.rating).not.toBeNull();
    },
  );

  it.skipIf(!existsSync(path))(
    "the page really does have no buy box, so the guard is not firing on the words alone",
    () => {
      const $ = cheerio.load(readFileSync(path, "utf8"));
      expect($("#corePrice_feature_div").length).toBe(0);
      expect($('span[class*="priceToPay"]').length).toBe(0);
      expect($("#add-to-cart-button").length).toBe(0);
      // And the last-resort price selector matches MANY nodes — the concatenation trap.
      expect(
        $('span[class*="a-price"] span[class*="a-price-whole"]').length,
      ).toBeGreaterThan(5);
    },
  );
});
