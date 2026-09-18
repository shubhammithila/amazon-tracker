/**
 * The scrape orchestrator: batching, retry rounds, the stop, and progress that never goes backwards.
 *
 * Amazon is never contacted. `httpClient` is mocked at the module boundary so the engine's own
 * behaviour is what is under test — but note the limit, because this codebase has been bitten by it
 * twice today: **a fake client answers whatever it is told to**, so it cannot prove anything about
 * Amazon's actual responses. The FBA packing bug survived 2,203 passing tests and 19/19 mutations
 * for exactly that reason, and the SB `daily=True` fetch before it. That is what the differential
 * test against real saved pages is for.
 *
 * Redis is REAL, for the same reason `lock.test.ts` uses it: progress and the claim are the two
 * things that must work across processes.
 */
import Redis from "ioredis";
import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

// Mocked BEFORE the engine is imported, so the engine binds the fake.
const fetchCalls: string[] = [];
let responder: (asin: string) => { status: string; html: string | null } = () => ({
  status: "OK",
  html: "<html><body><span id='productTitle'>X</span><button id='add-to-cart-button'>a</button></body></html>",
});

/**
 * Pools created and closed, so the batching can be asserted rather than assumed.
 *
 * A pool MUST be closed per batch: pages are 2.3 MB and CLAUDE.md records a 271-ASIN scrape
 * reaching 419 MB RSS on a 951 MB box with no swap and wedging the app — process state `D`, 34
 * connections queued on port 8000, `systemctl` still reporting `active`. Leaking one pool per batch
 * is how that comes back, and nothing else in the suite would notice.
 */
const pools: { closed: boolean }[] = [];

vi.mock("../src/scraper/httpClient.js", () => ({
  createPool: () => {
    const pool = { closed: false, close: async () => {} };
    pool.close = async () => {
      pool.closed = true;
    };
    pools.push(pool);
    return pool;
  },
  fetchProductPage: async (_pool: unknown, asin: string) => {
    fetchCalls.push(asin);
    return responder(asin);
  },
  RETRYABLE_STATUSES: ["Throttled (503)", "Timeout", "Connection Error"],
}));

const { runScrape, ScrapeAlreadyRunning, isValidAsin, BATCH_SIZE } = await import(
  "../src/scraper/engine.js"
);
const { readProgress, PROGRESS_KEY } = await import("../src/scraper/progress.js");
const { parseProductPage } = await import("../src/scraper/parsers.js");
const { SCRAPE_LOCK_KEY } = await import("../src/scraper/lock.js");

async function connect(): Promise<Redis | null> {
  const client = new Redis({
    host: "127.0.0.1",
    port: 6379,
    lazyConnect: true,
    retryStrategy: () => null,
    maxRetriesPerRequest: 1,
  });
  try {
    await client.connect();
    await client.ping();
    return client;
  } catch {
    await client.quit().catch(() => {});
    return null;
  }
}

const redis = await connect();
const available = redis !== null;

function db(): Redis {
  if (!redis) throw new Error("unreachable: guarded by describe.skipIf");
  return redis;
}

/** A page that parses to status OK, so a "success" is genuinely a parsed row. */
const okPage =
  "<html><body><span id='productTitle'>A Product</span>" +
  "<button id='add-to-cart-button'>Add</button>" +
  "<span class='priceToPay'><span class='a-price-whole'>181</span>" +
  "<span class='a-price-fraction'>00</span></span></body></html>";

beforeEach(async () => {
  fetchCalls.length = 0;
  pools.length = 0;
  responder = () => ({ status: "OK", html: okPage });
  if (redis) await redis.del(PROGRESS_KEY, SCRAPE_LOCK_KEY);
});

afterAll(async () => {
  if (redis) {
    await redis.del(PROGRESS_KEY, SCRAPE_LOCK_KEY);
    await redis.quit();
  }
});

/** Fast settings: the real delays would make this suite take minutes. */
const fast = {
  delayMinSeconds: 0,
  delayMaxSeconds: 0,
  retryRounds: 1,
  concurrency: 4,
  // The inline retry backoff defaults to 5 SECONDS, matching the Python. Left at the default, two
  // of these tests time out — which is why it is a setting rather than a constant.
  retryBackoffMs: 0,
};

function asins(n: number): string[] {
  return Array.from({ length: n }, (_, i) => `B0${String(i).padStart(8, "0")}`);
}

describe("isValidAsin", () => {
  it("accepts a B0 ASIN", () => {
    expect(isValidAsin("B0CWGXYLT6")).toBe(true);
  });
  it("rejects an FNSKU, which starts with X", () => {
    // The Python route rejects these because Amazon's /dp/ path does not serve them; the check
    // lives in the engine so a different caller cannot bypass it.
    expect(isValidAsin("X001ABCDEF")).toBe(false);
  });
  it("rejects the wrong length and lower case is normalised by the caller", () => {
    expect(isValidAsin("B0CWGXYLT")).toBe(false);
    expect(isValidAsin("B0CWGXYLT66")).toBe(false);
  });
});

describe.skipIf(!available)("runScrape (real Redis, faked HTTP)", () => {
  it("scrapes every ASIN once and reports them", async () => {
    const summary = await runScrape({ redis: db(), asins: asins(5), settings: fast });
    expect(summary.rows).toHaveLength(5);
    expect(fetchCalls).toHaveLength(5);
    expect(summary.errorCount).toBe(0);
  });

  it("de-duplicates the input", async () => {
    // A sheet with a repeated ASIN must not be fetched twice — it is a wasted request against a
    // rate-limited origin, and it would double-count the row.
    const summary = await runScrape({
      redis: db(),
      asins: ["B000000001", "B000000001", "B000000002"],
      settings: fast,
    });
    expect(summary.rows).toHaveLength(2);
    expect(fetchCalls).toHaveLength(2);
  });

  it("drops invalid ASINs instead of requesting them", async () => {
    const summary = await runScrape({
      redis: db(),
      asins: ["B000000001", "X001ABCDEF", "nonsense"],
      settings: fast,
    });
    expect(summary.rows).toHaveLength(1);
    expect(fetchCalls).toEqual(["B000000001"]);
  });

  it("refuses a second concurrent run — the claim is held", async () => {
    // Slow the fake so the first run is genuinely still in flight.
    responder = () => ({ status: "OK", html: okPage });
    const first = runScrape({ redis: db(), asins: asins(8), settings: fast });
    await expect(
      runScrape({ redis: db(), asins: asins(3), settings: fast }),
    ).rejects.toBeInstanceOf(ScrapeAlreadyRunning);
    await first;
  });

  it("releases the claim afterwards, so the next run can start", async () => {
    await runScrape({ redis: db(), asins: asins(2), settings: fast });
    expect(await db().exists(SCRAPE_LOCK_KEY)).toBe(0);
    const again = await runScrape({ redis: db(), asins: asins(2), settings: fast });
    expect(again.rows).toHaveLength(2);
  });

  it("releases the claim even when the run THROWS", async () => {
    // The Python `finally` is load-bearing for exactly this: without it the flag stays set and
    // every later run is silently refused.
    responder = () => {
      throw new Error("boom");
    };
    await expect(
      runScrape({ redis: db(), asins: asins(2), settings: fast }),
    ).rejects.toThrow("boom");
    expect(await db().exists(SCRAPE_LOCK_KEY)).toBe(0);
  });

  it("retries only the rows that failed, not the whole set", async () => {
    // Counted PER ASIN rather than by slicing the call log at a fixed offset. With concurrency > 1
    // the interleaving is not deterministic, so an index-based slice is testing the scheduler
    // rather than the retry rule — my first version did exactly that and failed intermittently.
    const attempts = new Map<string, number>();
    responder = (asin) => {
      const n = (attempts.get(asin) ?? 0) + 1;
      attempts.set(asin, n);
      // One ASIN fails every inline attempt in round 1 (3 calls: initial + 2 retries), then
      // succeeds in round 2.
      if (asin === "B000000001" && n <= 3) return { status: "Timeout", html: null };
      return { status: "OK", html: okPage };
    };
    const summary = await runScrape({
      redis: db(),
      asins: asins(3),
      settings: { ...fast, retryRounds: 2 },
    });

    expect(summary.rows).toHaveLength(3);
    // The failing ASIN is attempted again in round 2; the two that succeeded are NOT.
    expect(attempts.get("B000000001")).toBe(4); // 3 in round 1, 1 in round 2
    expect(attempts.get("B000000000")).toBe(1);
    expect(attempts.get("B000000002")).toBe(1);
  });

  it("retries a page that came back unparseable", async () => {
    // A 200 with markup the parser cannot read is not a finished row. Note this particular input
    // is caught by the STATUS check alone (it parses to "Parse Error (no title)"), which is why the
    // next test exists — found by mutation, when removing `|| !row.title` left this one passing.
    let attempts = 0;
    responder = () => {
      attempts++;
      return attempts === 1
        ? { status: "OK", html: "<html><body>no title here</body></html>" }
        : { status: "OK", html: okPage };
    };
    const summary = await runScrape({
      redis: db(),
      asins: ["B000000001"],
      settings: { ...fast, retryRounds: 2 },
    });
    expect(attempts).toBeGreaterThan(1);
    expect(summary.rows[0]!.title).toBe("A Product");
  });

  it("treats an EMPTY title as a parse failure, so the row is retried", async () => {
    // **Measured, and it is why the Python's `or not title` clause is deliberately NOT ported.**
    //
    // A mutation removing that clause survived every test, so I checked what the parser actually
    // returns for an empty title element: status "Parse Error (no title)", title null. The status
    // check therefore covers every no-title case and the title check is UNREACHABLE — the only way
    // to test it would be to fabricate a row the parser cannot produce.
    //
    // Asserting the STATUS here is what pins that reasoning: if the parser ever gains a path that
    // returns OK with no title, this test fails and the clause must come back.
    const attempts = new Map<string, number>();
    responder = (asin) => {
      const n = (attempts.get(asin) ?? 0) + 1;
      attempts.set(asin, n);
      return n === 1
        ? {
            status: "OK",
            html:
              "<html><body><span id='productTitle'>   </span>" +
              "<button id='add-to-cart-button'>Add</button></body></html>",
          }
        : { status: "OK", html: okPage };
    };
    const summary = await runScrape({
      redis: db(),
      asins: ["B000000001"],
      settings: { ...fast, retryRounds: 2 },
    });
    expect(attempts.get("B000000001")).toBe(2);
    expect(summary.rows[0]!.title).toBe("A Product");
  });

  it("an empty title really does produce a non-OK status", () => {
    // The assumption the test above rests on, asserted directly rather than left implicit.
    const row = parseProductPage(
      "<html><body><span id='productTitle'>   </span>" +
        "<button id='add-to-cart-button'>Add</button></body></html>",
      "B000000001",
    );
    expect(row.title).toBeNull();
    expect(row.status).not.toBe("OK");
  });

  it("stops mid-run when aborted, and does not finish the queue", async () => {
    const controller = new AbortController();
    let seen = 0;
    responder = () => {
      seen++;
      if (seen === 3) controller.abort();
      return { status: "OK", html: okPage };
    };
    const summary = await runScrape({
      redis: db(),
      asins: asins(40),
      settings: fast,
      signal: controller.signal,
    });
    expect(summary.stopped).toBe(true);
    // The point: it stops WITHIN the batch rather than at the next batch boundary.
    expect(fetchCalls.length).toBeLessThan(40);
  });

  it("stops WITHIN a batch, close to the abort — not at the batch boundary", async () => {
    // **Found by mutation: removing the abort check at the top of the worker loop passed every
    // other test.** With 40 ASINs in one batch of 50 and concurrency 4, a worker that only checks
    // before its fetch still drains the whole queue, so `fetchCalls.length < 40` stayed true by a
    // margin large enough to hide it.
    //
    // 120 ASINs spans three batches, and aborting during the first must stop well short of even
    // ONE batch — which is only true if the check is inside the loop that pulls from the queue.
    const controller = new AbortController();
    let seen = 0;
    responder = () => {
      seen++;
      if (seen === 5) controller.abort();
      return { status: "OK", html: okPage };
    };
    const summary = await runScrape({
      redis: db(),
      asins: asins(120),
      settings: fast,
      signal: controller.signal,
    });
    expect(summary.stopped).toBe(true);
    // Allowance for the workers already in flight when the abort landed: concurrency is 4, so at
    // most a handful more can complete. A worker loop that ignores the signal would reach 50+.
    expect(fetchCalls.length).toBeLessThan(20);
  });

  it("calls onResult for every page as it lands, not once at the end", async () => {
    // This is what lets rows be persisted incrementally — a crash then leaves real data.
    const seen: string[] = [];
    await runScrape({
      redis: db(),
      asins: asins(6),
      settings: fast,
      onResult: (row) => {
        seen.push(row.asin);
      },
    });
    expect(seen).toHaveLength(6);
  });

  it("carries a fetch failure through as the row's status", async () => {
    responder = () => ({ status: "Throttled (503)", html: null });
    const summary = await runScrape({
      redis: db(),
      asins: ["B000000001"],
      settings: fast,
    });
    expect(summary.rows[0]!.status).toBe("Throttled (503)");
    expect(summary.rows[0]!.price).toBeNull();
    expect(summary.errorCount).toBe(1);
  });

  it("batches, so peak memory is bounded", async () => {
    // 120 ASINs is 3 batches of 50. The batch size is what stopped a 271-ASIN scrape wedging the
    // app at 419 MB RSS on a 951 MB box.
    const summary = await runScrape({
      redis: db(),
      asins: asins(120),
      settings: fast,
    });
    expect(summary.rows).toHaveLength(120);
    expect(BATCH_SIZE).toBe(50);
    // One pool PER BATCH: ceil(120/50) = 3.
    expect(pools).toHaveLength(3);
  });

  it("CLOSES every pool, so 2.3 MB page buffers do not accumulate", async () => {
    // Found by mutation: replacing `await pool.close()` with a no-op passed every other test,
    // because nothing observed the pool. Leaking one per batch is precisely how the app reached
    // 419 MB RSS and stopped accepting connections while systemctl still said `active`.
    await runScrape({ redis: db(), asins: asins(120), settings: fast });
    expect(pools).toHaveLength(3);
    expect(pools.every((pool) => pool.closed)).toBe(true);
  });

  it("closes the pool even when the batch THROWS", async () => {
    // The `finally` half. A failing batch must not leak its connections either.
    responder = () => {
      throw new Error("boom");
    };
    await expect(
      runScrape({ redis: db(), asins: asins(3), settings: fast }),
    ).rejects.toThrow("boom");
    expect(pools).toHaveLength(1);
    expect(pools[0]!.closed).toBe(true);
  });
});

describe.skipIf(!available)("progress", () => {
  it("reports running=false and a finish time once done", async () => {
    await runScrape({ redis: db(), asins: asins(3), settings: fast });
    const state = await readProgress(db());
    expect(state.running).toBe(false);
    expect(state.resultCount).toBe(3);
    // An ISO INSTANT, never a date-only string — there is no calendar-day question to get wrong.
    expect(state.lastScrapedAt).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  });

  it("counts each page AS IT LANDS, so the bar actually moves", async () => {
    // Found by mutation: removing the `hincrby(progress)` passed every other test, because the
    // final state was only checked for `resultCount`. A progress bar that never moves is the
    // whole feature failing, and it would have shipped.
    const samples: number[] = [];
    await runScrape({
      redis: db(),
      asins: asins(12),
      settings: fast,
      onResult: async () => {
        samples.push((await readProgress(db())).progress);
      },
    });
    // Strictly increasing overall: the last sample must exceed the first, and the final count must
    // equal the number of pages.
    expect(samples.at(-1)!).toBeGreaterThan(samples[0]!);
    expect(Math.max(...samples)).toBe(12);
  });

  it("counts a FAILED page as progress too, and separately as an error", async () => {
    // A failure is still a page finished. Not counting it would stall the bar at the first timeout
    // while the run carried on — which reads as the app having hung.
    responder = (asin) =>
      asin === "B000000001"
        ? { status: "Timeout", html: null }
        : { status: "OK", html: okPage };
    await runScrape({ redis: db(), asins: asins(4), settings: fast });
    const state = await readProgress(db());
    expect(state.errorCount).toBe(1);
    expect(state.resultCount).toBe(4);
  });

  it("never reports a percent above 100 or below 0", async () => {
    await runScrape({ redis: db(), asins: asins(7), settings: fast });
    const state = await readProgress(db());
    expect(state.percent).toBeGreaterThanOrEqual(0);
    expect(state.percent).toBeLessThanOrEqual(100);
  });

  it("computes percent SERVER-side so the screen cannot disagree", async () => {
    // The Orders tab shipped "86 orders beside 87 lines" because two places computed one figure.
    await db().hset(PROGRESS_KEY, { progress: "5", total: "10", running: "1" });
    const state = await readProgress(db());
    expect(state.percent).toBe(50);
  });

  it("reports 0 percent rather than NaN when nothing is expected", async () => {
    // `0/0` is NaN, which renders as "NaN%" — the same class of defect as `e.cartons` printing
    // "100/undefined" on the owner's screen.
    await db().del(PROGRESS_KEY);
    await db().hset(PROGRESS_KEY, { progress: "0", total: "0" });
    const state = await readProgress(db());
    expect(state.percent).toBe(0);
    expect(Number.isNaN(state.percent)).toBe(false);
  });

  it("returns a usable empty state when nothing has ever run", async () => {
    await db().del(PROGRESS_KEY);
    const state = await readProgress(db());
    expect(state.running).toBe(false);
    expect(state.percent).toBe(0);
    expect(state.round).toBe(1);
  });
});
