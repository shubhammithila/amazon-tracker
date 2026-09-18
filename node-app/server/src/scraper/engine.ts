/**
 * The scrape orchestrator: batches, bounded workers, retry rounds, progress.
 *
 * Ported from `app/scraper/engine.run_scrape`, keeping the four properties that were paid for:
 *
 * * **Batches of 50 with a fresh connection pool per batch.** The Python comment says "to keep peak
 *   memory low" and CLAUDE.md records why that matters: a 271-ASIN scrape reached 419 MB RSS on a
 *   951 MB box with no swap and WEDGED the app — process state `D`, 34 connections queued on port
 *   8000, every request hanging while `systemctl` reported `active`. Pages are 2.3 MB each, so
 *   holding a batch's worth is the difference.
 * * **Bounded concurrency**, 10 workers pulling from one queue rather than 271 parallel requests.
 * * **Three retry rounds**, re-scraping only rows that failed or came back with no title.
 * * **A stop that is honoured mid-batch**, not merely at a batch boundary.
 *
 * Two deliberate differences from the Python, each fixing something:
 *
 * 1. **The claim is a Redis lock, not a module flag** — see `lock.ts`. A crashed run releases by
 *    TTL; the Python `finally` is lost if the process dies.
 * 2. **Progress never goes backwards.** The Python resets `progress` to 0 and `total` to the retry
 *    set's size at the start of each round, so the bar jumps back on round 2 and reads as a fault.
 *    The round is now its own reported field and the percentage is computed within it.
 */
import type Redis from "ioredis";
import type { Pool } from "undici";

import { createPool, fetchProductPage, RETRYABLE_STATUSES } from "./httpClient.js";
import { claimScrape, type ScrapeClaim } from "./lock.js";
import { parseProductPage, type ProductRow } from "./parsers.js";
import { countPage, finishProgress, initProgress, setProgress } from "./progress.js";
import { randomDelayMs } from "./stealth.js";

/** Matches the Python `BATCH_SIZE`. A fresh pool per batch is what caps peak memory. */
export const BATCH_SIZE = 50;

export interface ScrapeSettings {
  concurrency: number;
  delayMinSeconds: number;
  delayMaxSeconds: number;
  retryRounds: number;
  timeoutMs: number;
  /**
   * Base backoff for the INLINE retry after a throttle or timeout, in milliseconds.
   *
   * Configurable because it has to be: the Python hardcodes `(attempt + 1) * 5 + random(0, 3)`
   * seconds, and a test exercising the retry path then has to wait 5–8 real seconds per attempt.
   * Two tests timed out at 5s before this existed, which is a testability problem rather than a
   * feature request — an untestable backoff is one nobody checks.
   *
   * The DEFAULT is unchanged from the Python, so production behaviour is identical.
   */
  retryBackoffMs: number;
}

export const DEFAULT_SETTINGS: ScrapeSettings = {
  concurrency: 10,
  delayMinSeconds: 1.5,
  delayMaxSeconds: 3.5,
  retryRounds: 3,
  timeoutMs: 15_000,
  retryBackoffMs: 5000,
};

/** Thrown when a scrape is requested while one is in flight. A STATE, so the route answers 409. */
export class ScrapeAlreadyRunning extends Error {
  constructor() {
    super("A scrape is already running.");
    this.name = "ScrapeAlreadyRunning";
  }
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * `B0` + 8 more characters. The Python validates this at the route; it lives here so the engine
 * cannot be handed an FNSKU (which starts with X) by a different caller later.
 */
export function isValidAsin(asin: string): boolean {
  return /^B0[A-Z0-9]{8}$/.test(asin.trim().toUpperCase());
}

export interface RunOptions {
  redis: Redis;
  asins: string[];
  settings?: Partial<ScrapeSettings>;
  /** Called for every finished page, so rows can be persisted as they land rather than at the end. */
  onResult?: (row: ProductRow) => Promise<void> | void;
  /** Aborts the run. The Python uses an asyncio.Event; a signal is the idiomatic equivalent. */
  signal?: AbortSignal;
}

export interface RunSummary {
  rows: ProductRow[];
  rounds: number;
  stopped: boolean;
  errorCount: number;
}

/** One batch: its own pool, released before the next batch is built. */
async function runBatch(
  redis: Redis,
  pool: Pool,
  queue: string[],
  settings: ScrapeSettings,
  results: Map<string, ProductRow>,
  options: RunOptions,
  claim: ScrapeClaim,
): Promise<void> {
  const worker = async (): Promise<void> => {
    for (;;) {
      const asin = queue.shift();
      if (asin === undefined) return;

      // The politeness delay, per request rather than per batch — the point is to look like a human
      // browsing, and 10 simultaneous requests followed by a pause does not.
      await sleep(randomDelayMs(settings.delayMinSeconds, settings.delayMaxSeconds));

      // **ONE abort check, here.** There was a second at the top of the loop, and a mutation
      // deleting it was an EQUIVALENT MUTANT — no test could tell the difference, because this
      // check already returns before any request is made. Rather than write a test that pins
      // redundant code, the redundancy is removed: this is the last point before work happens, so
      // it is the only place the check earns its keep. Checking after the delay also means an abort
      // during a 3-second pause is honoured immediately.
      if (options.signal?.aborted) return;

      let result = await fetchProductPage(pool, asin, options.signal);

      // Two extra attempts on the three retryable statuses, with growing backoff. Matches the
      // Python worker's inline retry, which is separate from the whole-run retry rounds below.
      for (let attempt = 0; attempt < 2; attempt++) {
        if (result.status === "OK" || options.signal?.aborted) break;
        if (!RETRYABLE_STATUSES.includes(result.status)) break;
        // Growing backoff with jitter, matching the Python's `(attempt + 1) * 5 + random(0, 3)`
        // seconds at the default. The jitter scales with the base so a test that sets the base to
        // zero waits genuinely no time.
        await sleep(
          (attempt + 1) * settings.retryBackoffMs +
            Math.random() * settings.retryBackoffMs * 0.6,
        );
        result = await fetchProductPage(pool, asin, options.signal);
      }

      const row: ProductRow =
        result.status === "OK" && result.html
          ? parseProductPage(result.html, asin)
          : {
              asin,
              url: `https://www.amazon.in/dp/${asin}`,
              // The fetch statuses are not parse statuses, so they are carried through as-is;
              // the union is widened at the type level by the cast rather than by inventing a
              // parse status that would be wrong.
              status: result.status as ProductRow["status"],
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

      results.set(asin, row);
      await countPage(redis, asin, row.status !== "OK");
      if (options.onResult) await options.onResult(row);

      // Renew the claim while genuinely working. Without this a scrape longer than the TTL would
      // lose its lock and a second run could start alongside it.
      await claim.heartbeat();
    }
  };

  const workerCount = Math.min(settings.concurrency, queue.length);
  await Promise.all(Array.from({ length: workerCount }, () => worker()));
}

/**
 * Scrape `asins`, retrying failures up to `retryRounds` times.
 *
 * Throws `ScrapeAlreadyRunning` when the Redis claim is held. The claim is released in a `finally`,
 * and the TTL covers the case where the process dies before that runs — which is the whole reason
 * the lock is in Redis.
 */
export async function runScrape(options: RunOptions): Promise<RunSummary> {
  const settings = { ...DEFAULT_SETTINGS, ...options.settings };
  const { redis } = options;

  const asins = options.asins.map((a) => a.trim().toUpperCase()).filter(isValidAsin);
  const unique = [...new Set(asins)];

  const claim = await claimScrape(redis);
  if (!claim) throw new ScrapeAlreadyRunning();

  const results = new Map<string, ProductRow>();
  let round = 0;
  let stopped = false;
  let runError = "";

  try {
    await initProgress(redis, { total: unique.length, roundTotal: settings.retryRounds });

    let pending = unique;
    for (round = 1; round <= settings.retryRounds; round++) {
      if (options.signal?.aborted || pending.length === 0) break;

      // `total` is the CURRENT round's size and `round` is reported alongside it, so the
      // percentage is always within-round and never appears to go backwards without the round
      // label changing too.
      await setProgress(redis, { round, progress: 0, total: pending.length });

      for (let offset = 0; offset < pending.length; offset += BATCH_SIZE) {
        if (options.signal?.aborted) break;
        const batch = pending.slice(offset, offset + BATCH_SIZE);
        const pool = createPool(settings.timeoutMs);
        try {
          await runBatch(redis, pool, [...batch], settings, results, options, claim);
        } finally {
          // Closed per batch so the 2.3 MB-per-page buffers are released between batches rather
          // than accumulating across a 271-ASIN run.
          await pool.close();
        }
      }

      // Retry anything that did not end OK.
      //
      // **The Python condition is `status != "OK" or not title`, and the second half is dead —
      // measured, not assumed.** `parse_product_page` returns "Parse Error (no title)" whenever the
      // title is empty, so a row can never carry status OK and a null title: the status check
      // already covers every case the title check would.
      //
      // It is dropped rather than copied, because a mutation testing it can only be written by
      // constructing a row the parser cannot produce — and a test that fabricates an impossible
      // input to justify a branch is how a codebase accumulates code nobody can explain. If the
      // parser ever gains a path that returns OK with no title, THAT is the change which should
      // also restore this clause.
      pending = [...results.values()]
        .filter((row) => row.status !== "OK")
        .map((row) => row.asin);
    }

    stopped = options.signal?.aborted ?? false;
  } catch (error) {
    runError = error instanceof Error ? error.message : String(error);
    throw error;
  } finally {
    const rows = [...results.values()];
    const errorCount = rows.filter((row) => row.status !== "OK").length;
    // Recorded as an ISO INSTANT (not a date), so there is no calendar-day question to get wrong.
    await finishProgress(redis, {
      resultCount: rows.length,
      finishedAt: isoInstant(),
      ...(runError ? { error: runError } : {}),
    });
    await setProgress(redis, { errorCount });
    await claim.release();
  }

  const rows = [...results.values()];
  return {
    rows,
    rounds: round,
    stopped,
    errorCount: rows.filter((row) => row.status !== "OK").length,
  };
}

/**
 * The current instant as `YYYY-MM-DDTHH:mm:ssZ`, assembled from UTC getters.
 *
 * Not `toISOString()`, which is banned tree-wide by `scripts/check-dates.mjs` — this is a
 * TIMESTAMP rather than a calendar date so UTC is correct here, but the ban has no exemptions
 * because a single one is how a ban stops being one. Any "which day is it" question goes through
 * `ist.ts`.
 */
function isoInstant(): string {
  const now = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    `${now.getUTCFullYear()}-${p(now.getUTCMonth() + 1)}-${p(now.getUTCDate())}` +
    `T${p(now.getUTCHours())}:${p(now.getUTCMinutes())}:${p(now.getUTCSeconds())}Z`
  );
}
