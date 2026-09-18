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
  [
    "src/scraper/parsers.ts",
    '  const painter = $(\n    \'[data-feature-name="dealBadge"] [data-csa-c-painter="dp-deal"], \' +\n      \'span[class*="dealBadge"][data-csa-c-painter="dp-deal"]\',\n  );',
    '  const painter = $(\'[data-csa-c-painter="dp-deal"]\');',
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
