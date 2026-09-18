#!/usr/bin/env node
/**
 * Mutation harness for the ported scraper. Every mutation MUST be caught.
 *
 * Run: `node scripts/mutate-parser.mjs`
 *
 * Written because the differential test — which compares against the Python parser on three real
 * pages and is the right acceptance bar — passed while TWO of these survived. A fixture proves only
 * what it happens to contain: the winning price selector matches exactly one node on both OK pages,
 * and none of the three carries the unsubstituted `NO_OF_` screen-reader text. The Python side has
 * the same lesson recorded four times over.
 */
import { execFileSync } from "node:child_process";
import { copyFileSync, readFileSync, writeFileSync, unlinkSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// fileURLToPath, not url.pathname — the repo path contains a space.
const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

/** [file, find, replace, why this would be a real bug] */
const MUTATIONS = [
  [
    "src/scraper/parsers.ts",
    "  if (detectUnavailable($)) {",
    "  if (false) {",
    "an unavailable listing reports a CAROUSEL price belonging to another product",
  ],
  [
    "src/scraper/parsers.ts",
    'const whole = $(selector).first().text().trim().replace(/,/g, "");',
    'const whole = $(selector).text().trim().replace(/,/g, "");',
    "cheerio concatenates every match, so two price nodes become one wrong number",
  ],
  [
    "src/scraper/parsers.ts",
    'if (visible && !visible.includes("NO_OF_")) return "Yes";',
    'if (visible) return "Yes";',
    "the unsubstituted countdown template counts as a rendered deal badge",
  ],
  [
    "src/scraper/parsers.ts",
    'const cleaned = whole.replace(/[.,]$/, "");',
    "const cleaned = whole;",
    'the whole-part separator is kept, so a price reads "294..00"',
  ],
  [
    "src/scraper/guards.ts",
    "  return hasTitle && !hasBuyBox;",
    "  return false;",
    "the no-buy-box signal is lost — the words alone miss B0D817HX57 entirely",
  ],
  [
    "src/scraper/guards.ts",
    '    $(\'span[class*="priceToPay"]\').length > 0;',
    "    false;",
    "priceToPay stops counting as a buy box, so buyable pages read as unavailable",
  ],
  // Matched on the SCOPING fragment alone rather than the whole multi-line expression: the full
  // text has to be escaped exactly, and a reformat of the surrounding lines silently turns the
  // mutation into a SKIP — which the harness reports as a survivor, correctly, because a mutation
  // that never applied has proved nothing.
  [
    "src/scraper/parsers.ts",
    '[data-feature-name="dealBadge"] [data-csa-c-painter="dp-deal"]',
    "[data-csa-c-painter='dp-deal']",
    "the painter check is unscoped, so an ad carousel's deal marks this product",
  ],
  [
    "src/scraper/parsers.ts",
    '  if (detectCaptcha($)) return emptyRow(asin, "Blocked (CAPTCHA)");',
    "  if (false) return emptyRow(asin, \"Blocked (CAPTCHA)\");",
    "a CAPTCHA page is parsed as a product with no data",
  ],
  [
    "src/scraper/parsers.ts",
    '  if (!hasTitle($)) return emptyRow(asin, "Parse Error (no title)");',
    '  if (false) return emptyRow(asin, "Parse Error (no title)");',
    "a non-product page yields a row of nulls instead of a named failure",
  ],

  // ── engine.ts ──
  [
    "src/scraper/engine.ts",
    "        .filter((row) => row.status !== \"OK\")",
    "        .filter(() => true)",
    "every round re-scrapes the WHOLE set, not just the failures",
  ],
  [
    "src/scraper/engine.ts",
    "    await claim.release();",
    "    void claim;",
    "the claim is never released, so every later run is refused",
  ],
  [
    "src/scraper/engine.ts",
    "  const unique = [...new Set(asins)];",
    "  const unique = asins;",
    "a repeated ASIN is fetched twice and counted twice",
  ],
  [
    "src/scraper/engine.ts",
    "      if (options.signal?.aborted) return;\n\n      let result = await fetchProductPage",
    "      let result = await fetchProductPage",
    "the stop is ignored, so a cancelled scrape drains the whole queue",
  ],
  [
    "src/scraper/engine.ts",
    "  const asins = options.asins.map((a) => a.trim().toUpperCase()).filter(isValidAsin);",
    "  const asins = options.asins.map((a) => a.trim().toUpperCase());",
    "an FNSKU is requested from a path that cannot serve it",
  ],
  [
    "src/scraper/engine.ts",
    "          await pool.close();",
    "          void pool;",
    "pools are never closed, so 2.3 MB page buffers accumulate across batches",
  ],

  // ── progress.ts ──
  [
    "src/scraper/progress.ts",
    "    state.total > 0\n      ? Math.min(100, Math.round((state.progress / state.total) * 100))\n      : 0;",
    "    Math.round((state.progress / state.total) * 100);",
    "0/0 renders as NaN% — the `100/undefined` defect class",
  ],
  [
    "src/scraper/progress.ts",
    'const tx = redis.multi().hincrby(KEY, "progress", 1).hset(KEY, "currentAsin", asin);',
    'const tx = redis.multi().hset(KEY, "currentAsin", asin);',
    "pages are never counted, so the bar never moves",
  ],
];

function runTests() {
  try {
    execFileSync("npx", ["vitest", "run", "--silent"], {
      cwd: ROOT,
      stdio: "pipe",
      shell: process.platform === "win32",
    });
    return true;
  } catch {
    return false;
  }
}

let survived = 0;
MUTATIONS.forEach(([rel, find, replace, why], index) => {
  const path = join(ROOT, rel);
  const original = readFileSync(path, "utf8");
  const label = `[${String(index + 1).padStart(2)}/${MUTATIONS.length}]`;

  if (!original.includes(find)) {
    console.log(`${label} SKIP  target text not found in ${rel}`);
    console.log(`          looked for: ${JSON.stringify(find.slice(0, 70))}`);
    survived++;
    return;
  }

  const backup = path + ".mutbak";
  copyFileSync(path, backup);
  try {
    writeFileSync(path, original.replace(find, replace), "utf8");
    if (runTests()) {
      console.log(`${label} SURVIVED  ${why}`);
      survived++;
    } else {
      console.log(`${label} caught    ${why}`);
    }
  } finally {
    copyFileSync(backup, path);
    unlinkSync(backup);
  }
});

console.log("");
if (survived > 0) {
  console.error(`${survived} MUTATION(S) SURVIVED — a test is missing.`);
  process.exit(1);
}
console.log(`All ${MUTATIONS.length} mutations caught.`);
