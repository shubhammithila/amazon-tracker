/**
 * The scrape lock, against REAL Redis.
 *
 * Not a mock. The whole value of moving off the module-level `ScrapeState` singleton is that the
 * claim is atomic ACROSS PROCESSES and survives a restart, and `ioredis-mock` would answer these
 * questions from a JavaScript object — proving the test's own assumptions rather than Redis's
 * behaviour. The Python suite learned this exact lesson with SQLite: an in-memory database silently
 * refuses WAL mode, so the locking tests had to use a real temporary FILE or they proved nothing.
 *
 * Skips with an explanation when Redis is absent, rather than passing vacuously.
 */
import Redis from "ioredis";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { claimScrape, isScrapeRunning } from "../src/scraper/lock.js";
import type { ScrapeClaim } from "../src/scraper/lock.js";

const KEY = "tracker:test:lock:product-scrape";
let redis: Redis | null = null;

/**
 * Connect at MODULE LOAD, not in `beforeAll`.
 *
 * `describe.skipIf(...)` is evaluated while the file is being collected, which happens before any
 * hook runs — so a flag set in `beforeAll` is still false when the skip decision is made, and every
 * test would be skipped even with Redis up. Awaiting here is legitimate: Vitest supports top-level
 * await in a test file, and the connection attempt is bounded by `retryStrategy: () => null`.
 */
async function connect(): Promise<Redis | null> {
  const client = new Redis({
    host: "127.0.0.1",
    port: 6379,
    lazyConnect: true,
    // Fail fast instead of retrying for a minute when Redis is not running.
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

redis = await connect();

function redisAvailable(): boolean {
  return redis !== null;
}

beforeAll(() => {
  if (!redisAvailable()) {
    // Loud, so a skipped suite is a decision the reader sees rather than a silent gap.
    console.warn(
      "[lock.test] Redis is not reachable on 127.0.0.1:6379 — the lock suite is SKIPPED. " +
        "Start it with `docker compose up -d` from node-app/.",
    );
  }
});

afterAll(async () => {
  if (redis) {
    await redis.del(KEY);
    await redis.quit();
  }
});

beforeEach(async () => {
  if (redis) await redis.del(KEY);
});

/**
 * Whether Redis answered. Evaluated in beforeAll, so the suite SKIPS loudly when Redis is absent
 * rather than each test quietly asserting `true === true` — a green run that checked nothing is
 * worse than a red one, and that pattern is how the Python suite once had a scheduler guard passing
 * vacuously for a whole release.
 */
function redisOrSkip(): Redis {
  if (!redis) throw new Error("unreachable: guarded by describe.skipIf");
  return redis;
}

/**
 * A claim that must have succeeded, narrowed for the compiler.
 *
 * `strict` + `noUncheckedIndexedAccess` are on deliberately — they are the reason TypeScript was
 * chosen for this port — so a test cannot lean on `!` without stating why. Here the null case is a
 * genuine assertion failure, not a possibility to handle.
 */
function claimed(claim: ScrapeClaim | null): ScrapeClaim {
  if (!claim) throw new Error("claim was not granted");
  return claim;
}

describe.skipIf(!redisAvailable())("the scrape lock (real Redis on :6379)", () => {
  it("is claimable when free", async () => {
    const redis = redisOrSkip();
    const claim = claimed(await claimScrape(redis, KEY));
    await claim.release();
  });

  it("refuses a SECOND claim — the bug the Python lock exists to prevent", async () => {
    const redis = redisOrSkip();
    // The manual /scrape route and the 06:00 scheduled job both did `if running:` then acted, so an
    // overlap started two runs against one shared state and whichever finished first cleared the
    // other's results.
    const first = claimed(await claimScrape(redis, KEY));
    // NOT wrapped in `claimed()`: null is the expected answer here, and asserting it is the point
    // of the test.
    const second = await claimScrape(redis, KEY);
    expect(second).toBeNull();
    await first.release();
  });

  it("is claimable again after release", async () => {
    const redis = redisOrSkip();
    const first = claimed(await claimScrape(redis, KEY));
    await first.release();
    const second = claimed(await claimScrape(redis, KEY));
    await second.release();
  });

  it("survives a concurrent stampede: exactly ONE of ten claims wins", async () => {
    const redis = redisOrSkip();
    // The atomicity check. `SET NX` tests and sets in one round trip, so there is no window between
    // checking and claiming — which is the difference between this and `if running:`.
    const claims = await Promise.all(
      Array.from({ length: 10 }, () => claimScrape(redis, KEY)),
    );
    const winners = claims.filter((c) => c !== null);
    expect(winners).toHaveLength(1);
    await winners[0]!.release();
  });

  it("does NOT release a lock it no longer owns", async () => {
    const redis = redisOrSkip();
    // The classic distributed-lock bug: a run whose TTL expired mid-work calls release on its way
    // out and deletes the NEXT run's claim. A bare DEL would do exactly that.
    const first = claimed(await claimScrape(redis, KEY, 60_000));
    // Simulate the first claim's TTL lapsing and a second run taking over.
    await redis.del(KEY);
    const second = claimed(await claimScrape(redis, KEY, 60_000));

    await first.release(); // must be a no-op

    expect(await isScrapeRunning(redis, KEY)).toBe(true);
    expect(await redis.get(KEY)).toBe(second.token);
    await second.release();
  });

  it("expires by itself, so a crashed process cannot block the next run for ever", async () => {
    const redis = redisOrSkip();
    // This is what the Python design cannot do. `finally: running = False` is lost entirely if the
    // process dies — and CLAUDE.md records that `finally` being load-bearing, because
    // `except Exception` does not catch CancelledError at shutdown, leaving the flag True for the
    // life of the process and silently refusing every later refresh.
    // The claim is asserted to have been granted, then deliberately abandoned — simulating a
    // process that died without releasing.
    claimed(await claimScrape(redis, KEY, 120));
    await new Promise((resolve) => setTimeout(resolve, 260));
    expect(await isScrapeRunning(redis, KEY)).toBe(false);
    const next = claimed(await claimScrape(redis, KEY));
    await next.release();
  });

  it("heartbeat extends a claim that is still held", async () => {
    const redis = redisOrSkip();
    // What makes a multi-minute scrape safe under a short TTL: the ceiling bounds how long a DEAD
    // process blocks the next run, while a live one keeps renewing.
    const claim = claimed(await claimScrape(redis, KEY, 400));
    expect(await claim.heartbeat()).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(await claim.heartbeat()).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 250));
    // Without the heartbeat the 400ms TTL would have lapsed by now.
    expect(await isScrapeRunning(redis, KEY)).toBe(true);
    await claim.release();
  });

  it("heartbeat reports FALSE once the claim is lost, so the run can stop", async () => {
    const redis = redisOrSkip();
    const claim = claimed(await claimScrape(redis, KEY, 60_000));
    await redis.del(KEY);
    expect(await claim.heartbeat()).toBe(false);
  });

  it("reports whether a scrape is running, for the status route", async () => {
    const redis = redisOrSkip();
    expect(await isScrapeRunning(redis, KEY)).toBe(false);
    const claim = claimed(await claimScrape(redis, KEY));
    expect(await isScrapeRunning(redis, KEY)).toBe(true);
    await claim.release();
    expect(await isScrapeRunning(redis, KEY)).toBe(false);
  });
});
