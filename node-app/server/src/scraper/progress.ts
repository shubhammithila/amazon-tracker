/**
 * Scrape progress, in Redis rather than a module-level object.
 *
 * The Python `ScrapeState` holds progress, results and the current ASIN as instance attributes,
 * which means a page load can only see them if it happens to be served by the same process that is
 * scraping. Redis makes the same figures readable from any process and survivable across a restart
 * — so the progress bar keeps working if uvicorn is replaced mid-scrape, which today it does not.
 *
 * **Progress is MONOTONIC by construction.** The Python `run_scrape` resets `progress` to 0 and
 * `total` to the retry set's size at the start of each retry round, so the bar visibly jumps
 * backwards on round 2 — and a bar that goes backwards reads as a fault. The ads refresh already
 * learned this: `PHASE_BOUNDS` exists precisely because the actionable pass can stop at page 2 of 8
 * and the pending pass then starts its own page 1, which would send the bar back. Here the round is
 * reported as its own field and `percent` is computed within the round, so the number never
 * decreases without the round label also changing.
 */
import type Redis from "ioredis";

const KEY = "tracker:scrape:progress";

/** A TTL so an abandoned run's progress does not linger for ever claiming to be live. */
const PROGRESS_TTL_SECONDS = 3600;

export interface ScrapeProgress {
  running: boolean;
  /** Pages completed IN THIS ROUND. */
  progress: number;
  /** Pages expected in this round. */
  total: number;
  round: number;
  roundTotal: number;
  currentAsin: string;
  errorCount: number;
  resultCount: number;
  /** ISO instant of the last completed run, or "" — never a date-only string. */
  lastScrapedAt: string;
  error: string;
  /** 0-100 within the current round. Derived here so the screen cannot compute it differently. */
  percent: number;
}

const EMPTY: ScrapeProgress = {
  running: false,
  progress: 0,
  total: 0,
  round: 1,
  roundTotal: 1,
  currentAsin: "",
  errorCount: 0,
  resultCount: 0,
  lastScrapedAt: "",
  error: "",
  percent: 0,
};

/**
 * `percent` is computed in ONE place, server-side.
 *
 * The Python app has shipped the opposite three times — the Orders tab's "86 orders beside 87
 * lines", the Portfolio parent rows that exist to prevent it, the ads campaign headers whose totals
 * are rolled up in `logic.group_changes` rather than in the template. A figure computed in the
 * browser drifts from the one the server believes.
 */
function withPercent(state: Omit<ScrapeProgress, "percent">): ScrapeProgress {
  const percent =
    state.total > 0
      ? Math.min(100, Math.round((state.progress / state.total) * 100))
      : 0;
  return { ...state, percent };
}

export async function readProgress(redis: Redis): Promise<ScrapeProgress> {
  const raw = await redis.hgetall(KEY);
  if (!raw || Object.keys(raw).length === 0) return EMPTY;

  const num = (key: string): number => Number(raw[key] ?? 0) || 0;
  return withPercent({
    running: raw["running"] === "1",
    progress: num("progress"),
    total: num("total"),
    round: num("round") || 1,
    roundTotal: num("roundTotal") || 1,
    currentAsin: raw["currentAsin"] ?? "",
    errorCount: num("errorCount"),
    resultCount: num("resultCount"),
    lastScrapedAt: raw["lastScrapedAt"] ?? "",
    error: raw["error"] ?? "",
  });
}

/** Replace the whole record — used when a run starts, so no field survives from the last one. */
export async function initProgress(
  redis: Redis,
  fields: { total: number; roundTotal: number },
): Promise<void> {
  await redis
    .multi()
    .del(KEY)
    .hset(KEY, {
      running: "1",
      progress: "0",
      total: String(fields.total),
      round: "1",
      roundTotal: String(fields.roundTotal),
      currentAsin: "",
      errorCount: "0",
      resultCount: "0",
      lastScrapedAt: "",
      error: "",
    })
    .expire(KEY, PROGRESS_TTL_SECONDS)
    .exec();
}

/** Merge a partial update. Numeric fields are set, not incremented, so a retry cannot double them. */
export async function setProgress(
  redis: Redis,
  fields: Partial<Record<keyof ScrapeProgress, string | number | boolean>>,
): Promise<void> {
  const payload: Record<string, string> = {};
  for (const [key, value] of Object.entries(fields)) {
    if (value === undefined) continue;
    payload[key] = typeof value === "boolean" ? (value ? "1" : "0") : String(value);
  }
  if (Object.keys(payload).length === 0) return;
  await redis.hset(KEY, payload);
  await redis.expire(KEY, PROGRESS_TTL_SECONDS);
}

/** One page finished. `INCR` so concurrent workers cannot lose a count to a read-modify-write. */
export async function countPage(
  redis: Redis,
  asin: string,
  failed: boolean,
): Promise<void> {
  const tx = redis.multi().hincrby(KEY, "progress", 1).hset(KEY, "currentAsin", asin);
  if (failed) tx.hincrby(KEY, "errorCount", 1);
  await tx.exec();
}

/** Mark the run finished, keeping the figures so the screen can show what it achieved. */
export async function finishProgress(
  redis: Redis,
  fields: { resultCount: number; error?: string; finishedAt: string },
): Promise<void> {
  await setProgress(redis, {
    running: false,
    currentAsin: "",
    resultCount: fields.resultCount,
    lastScrapedAt: fields.finishedAt,
    error: fields.error ?? "",
  });
}

export const PROGRESS_KEY = KEY;
