/**
 * The scrape claim, as a Redis lock — replacing a module-level singleton.
 *
 * **This is a real improvement over the Python design, not a translation of it.**
 * `app/scraper/engine.ScrapeState` is a module-level object guarded by an `asyncio.Lock`, and its
 * own comment records the bug that forced the lock: the manual `/scrape` route and the 06:00
 * scheduled job both did `if running:` then acted, so an overlap started two runs against one
 * shared state and whichever finished first cleared the other's results.
 *
 * That lock works only because there is exactly one uvicorn process. It cannot survive a restart,
 * and two workers would give two independent scrape states — so the constraint is invisible until
 * the day someone scales the app.
 *
 * A Redis lock fixes the class rather than the instance:
 *
 * * **Atomic claim.** `SET key value NX PX ttl` tests and sets in one round trip, so there is no
 *   window between checking and claiming. This is the same fix as `claim_for_new_run`, but it holds
 *   across processes.
 * * **Survives a restart.** The TTL means a crashed run releases its claim by itself, where the
 *   Python `finally: running = False` is lost entirely if the process dies — and CLAUDE.md records
 *   that `finally` being load-bearing, because `except Exception` does not catch
 *   `asyncio.CancelledError` at shutdown and the flag then stayed True for the life of the process,
 *   silently refusing every later refresh including the nightly one.
 * * **Released only by its owner.** Each claim carries a token and the release is a Lua script that
 *   compares before deleting. Without that, a run whose TTL expired mid-work would delete the NEXT
 *   run's lock on its way out — the classic distributed-lock bug, and the reason a bare `DEL` is
 *   wrong.
 */
import type Redis from "ioredis";
import { randomUUID } from "node:crypto";

/** One lock per job kind, so a product scrape and a keyword track do not block each other. */
export const SCRAPE_LOCK_KEY = "tracker:lock:product-scrape";

/**
 * How long a claim lives without being renewed.
 *
 * A real 271-ASIN scrape takes minutes, so this is generous — but it is a CEILING on how long a
 * dead process can block the next run, not an estimate of the work. `heartbeat` extends it while
 * the run is genuinely alive, which is what makes a long scrape safe under a short TTL.
 */
export const LOCK_TTL_MS = 120_000;

/**
 * Release only if we still own it. `DEL` alone would let a run whose TTL expired delete a
 * successor's lock.
 */
const RELEASE_SCRIPT = `
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
else
  return 0
end
`;

/** Extend only if we still own it — same reasoning as release. */
const RENEW_SCRIPT = `
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("pexpire", KEYS[1], ARGV[2])
else
  return 0
end
`;

export interface ScrapeClaim {
  readonly token: string;
  /** Extend the TTL. Returns false once the claim has been lost, which the caller must honour. */
  heartbeat(): Promise<boolean>;
  /** Release the claim if still owned. Safe to call twice. */
  release(): Promise<void>;
}

/**
 * Claim the scraper, or return null because someone else holds it.
 *
 * Null rather than throwing: "a scrape is already running" is a STATE the route reports as a 409,
 * not an error — the same distinction `SpApiNotConfigured` draws in the Python client.
 */
export async function claimScrape(
  redis: Redis,
  key: string = SCRAPE_LOCK_KEY,
  ttlMs: number = LOCK_TTL_MS,
): Promise<ScrapeClaim | null> {
  const token = randomUUID();
  const result = await redis.set(key, token, "PX", ttlMs, "NX");
  if (result !== "OK") return null;

  let released = false;
  return {
    token,
    async heartbeat(): Promise<boolean> {
      if (released) return false;
      const renewed = await redis.eval(RENEW_SCRIPT, 1, key, token, String(ttlMs));
      return renewed === 1;
    },
    async release(): Promise<void> {
      if (released) return;
      released = true;
      await redis.eval(RELEASE_SCRIPT, 1, key, token);
    },
  };
}

/** Whether a scrape is currently claimed, for a read-only status route. */
export async function isScrapeRunning(
  redis: Redis,
  key: string = SCRAPE_LOCK_KEY,
): Promise<boolean> {
  return (await redis.exists(key)) === 1;
}
