#!/usr/bin/env node
/**
 * One rule, over every source file: dates never go through UTC formatting or date-only parsing.
 *
 * The Python app shipped this defect FOUR times in templates — including on a GST invoice date,
 * which read 2026-08-28 for a shipment on the 29th — and each fix came with its own passing test
 * scoped to the file that broke. A guard scoped to the file that broke cannot stop a habit that
 * spans files, so this runs over the tree from day one.
 *
 * Two patterns, because banning one does not cover the other:
 *
 *   toISOString()          formats through UTC, so a correct local Date answers YESTERDAY for the
 *                          5.5 hours after IST midnight.
 *   new Date("YYYY-MM-DD") a date-only string is parsed as UTC midnight BY SPEC, so it renders as
 *                          05:30 the following morning in IST. This is the Orders-tab bug, and the
 *                          toISOString ban alone does not catch it.
 *
 * Comments and strings are stripped before matching, for the reason the Python equivalent
 * documents: these fixes are EXPLAINED by naming the call they replaced, and an assertion that
 * cannot coexist with its own explanation forces the explanation out — and the explanation is the
 * part that stops the next occurrence.
 *
 * `src/ist.ts` is the single exemption, and it is exempt for `new Date(Date.UTC(...))` only, which
 * is numeric-argument construction rather than string parsing. It may not use `toISOString` either.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// `fileURLToPath`, never `url.pathname`: the repo path contains a space ("Amazon Tracker"), which
// a URL percent-encodes to %20 and `scandir` then cannot find. The obvious version fails only on
// paths with spaces, which is exactly the kind of thing that works on one machine.
const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

const SKIP_DIRS = new Set(["node_modules", "dist", ".git", "coverage"]);
const EXTENSIONS = /\.(ts|tsx|mjs|js|jsx)$/;

/** Strip block comments, line comments and string literals so only real code is matched. */
function code(text) {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "")
    .replace(/(["'`])(?:\\.|(?!\1)[^\\])*\1/g, '""');
}

/**
 * Every `new Date(...)` argument, extracted by MATCHING BRACKETS rather than by regex.
 *
 * `/new Date\(([^)]*)\)/` stops at the first `)`, so `new Date(d.getTime() + 100)` yields
 * `d.getTime(` — which then fails a "looks numeric" test and reports a false positive on correct
 * code. The Python suite has the identical bug in its history: a colour check whose regex stopped
 * at the first `)` and flagged `rgb(var(--green-rgb) / .1)`.
 */
function dateArguments(source) {
  const args = [];
  const pattern = /new\s+Date\s*\(/g;
  let match;
  while ((match = pattern.exec(source)) !== null) {
    let depth = 1;
    let i = match.index + match[0].length;
    const start = i;
    while (i < source.length && depth > 0) {
      const ch = source[i];
      if (ch === "(") depth++;
      else if (ch === ")") depth--;
      if (depth === 0) break;
      i++;
    }
    args.push(source.slice(start, i).trim());
  }
  return args;
}

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (EXTENSIONS.test(entry)) out.push(full);
  }
  return out;
}

const problems = [];
let scanned = 0;

for (const file of walk(ROOT)) {
  const rel = relative(ROOT, file).replace(/\\/g, "/");
  if (rel.endsWith("scripts/check-dates.mjs")) continue; // this file names the patterns
  scanned++;
  const source = code(readFileSync(file, "utf8"));

  if (/\btoISOString\s*\(/.test(source)) {
    problems.push(
      `${rel}: toISOString() formats through UTC — in IST it answers the previous day for ` +
        `5.5 hours out of every 24. Use ist.isoDate()/ist.utcInstantString().`,
    );
  }

  // `new Date(` with anything that is not a numeric expression or Date.UTC(...). A date-only
  // STRING is the dangerous case; `new Date()` and `new Date(ms)` are fine.
  const isIst = rel === "src/ist.ts";
  for (const arg of dateArguments(source)) {
    if (arg === "") continue; // new Date() — now, fine
    if (isIst && arg.startsWith("Date.UTC")) continue; // numeric construction, the one exemption
    if (/^Date\.UTC/.test(arg)) {
      problems.push(
        `${rel}: new Date(Date.UTC(...)) outside ist.ts — build dates through src/ist.ts so the ` +
          `offset lives in one place.`,
      );
      continue;
    }
    // Demonstrably numeric arguments are safe: a millisecond timestamp carries no timezone, so
    // there is nothing to get wrong.
    const numeric =
      /\.getTime\s*\(\s*\)/.test(arg) ||
      /\bDate\.now\s*\(\s*\)/.test(arg) ||
      /^[\d\s()+*/.-]+$/.test(arg);
    if (numeric) continue;

    // **A bare identifier is NOT flagged, and that limit is stated rather than papered over.**
    // `new Date(ms)` and `new Date(isoText)` are indistinguishable in source — deciding between
    // them needs the type, which only the compiler has. Guessing would either miss the real bug
    // (a variable holding "2026-08-25", which is the Orders-tab bug's actual shape) or cry wolf on
    // every millisecond timestamp, and a checker that cries wolf gets switched off.
    //
    // What closes the gap instead is TYPES: `parseIsoDate` is the only way a `YYYY-MM-DD` string
    // becomes a date in this codebase, and it returns `IstDate`, not `Date`. So a string date
    // cannot reach `new Date(...)` without an explicit cast that shows up in review. This check
    // catches the two forms types cannot: UTC formatting, and a string LITERAL.
    if (/^[A-Za-z_$][\w$]*$/.test(arg)) continue;

    problems.push(
      `${rel}: new Date(${arg.slice(0, 40)}) — a date-only string parses as UTC midnight by ` +
        `spec, which renders as 05:30 the NEXT day in IST. Use ist.parseIsoDate().`,
    );
  }
}

console.log(`checked ${scanned} file(s) for UTC date handling`);
if (problems.length) {
  console.error(`\n${problems.length} problem(s):`);
  for (const p of problems) console.error("  - " + p);
  process.exit(1);
}
console.log("no UTC date formatting or date-only string parsing found.");
