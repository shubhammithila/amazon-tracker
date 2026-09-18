/**
 * The scrape routes: start, progress, results, stop.
 *
 * Mirrors `app/routers/scrape.py`. Two properties carried over deliberately:
 *
 * * **A GET never blocks on a fetch.** `/progress` and `/results` read stored state; only `POST
 *   /scrape` starts work, and it returns immediately. The Python app follows the same rule for every
 *   expensive source (`A GET never blocks on a fetch` is stated three times in CLAUDE.md) because a
 *   page that hangs gets clicked again, and two clicks on a scrape is two scrapes.
 * * **"Already running" is a 409, not a 500.** It is a state, not a failure.
 */
import { Router, type Request, type Response } from "express";
import type Redis from "ioredis";

import { config } from "../config.js";
import { saveScrapeResults, listProducts } from "../db/scrapeRepository.js";
import {
  isValidAsin,
  runScrape,
  ScrapeAlreadyRunning,
} from "../scraper/engine.js";
import { isScrapeRunning } from "../scraper/lock.js";
import { readProgress } from "../scraper/progress.js";
import { publishProgress } from "../realtime/progress.js";

export interface ScrapeRouterDeps {
  redis: Redis;
}

/**
 * The in-flight run's abort handle.
 *
 * Module-level, and unlike the Python's `ScrapeState` that is FINE here: this is only the handle for
 * stopping a run that this process started, not the source of truth for whether a run exists. That
 * lives in Redis, so another process asking "is a scrape running" gets the right answer even though
 * it holds no controller. The distinction is the whole reason the lock moved.
 */
let currentRun: AbortController | null = null;

export function createScrapeRouter({ redis }: ScrapeRouterDeps): Router {
  const router = Router();

  router.post("/scrape", async (req: Request, res: Response) => {
    const body = req.body as { asins?: unknown } | undefined;
    const raw = Array.isArray(body?.asins) ? body.asins : [];
    const submitted = raw.map((value) => String(value).trim().toUpperCase());

    // Reported rather than silently dropped. The Python surfaces `missing_sku_count` and names the
    // rows for the same reason: a count that quietly shrinks reads as the feature not working.
    const valid = submitted.filter(isValidAsin);
    const rejected = submitted.filter((asin) => !isValidAsin(asin));

    if (valid.length === 0) {
      res.status(400).json({
        error:
          "No valid ASINs. An ASIN is 10 characters starting with B0 — FNSKUs (starting with X) " +
          "are not product identifiers and Amazon's /dp/ path cannot serve them.",
        rejected,
      });
      return;
    }

    const controller = new AbortController();
    currentRun = controller;

    // Fire and forget, so the POST returns at once. The run's own `finally` releases the Redis
    // claim; a crash is covered by the claim's TTL, which the Python's module flag cannot do.
    void (async () => {
      try {
        const summary = await runScrape({
          redis,
          asins: valid,
          signal: controller.signal,
          settings: {
            concurrency: config.SCRAPE_CONCURRENCY,
            delayMinSeconds: config.SCRAPE_DELAY_MIN,
            delayMaxSeconds: config.SCRAPE_DELAY_MAX,
            retryRounds: config.SCRAPE_RETRY_ROUNDS,
            timeoutMs: config.SCRAPE_TIMEOUT_MS,
          },
          onResult: async () => {
            // Pushed per page so the bar moves without the browser polling. Read from Redis rather
            // than passed through, so the number on screen is the one the server stored.
            await publishProgress(redis, await readProgress(redis));
          },
        });

        // Persisted AFTER the run, in one transaction — matching the Python's `on_complete`. Worth
        // noting as a known limit rather than a choice: a crash mid-scrape loses the batch. Writing
        // incrementally is a real improvement and belongs in its own change, with its own test that
        // a partial run leaves usable rows.
        const saved = await saveScrapeResults(summary.rows);
        console.log(
          `[scrape] ${summary.rows.length} row(s), ${summary.errorCount} error(s), ` +
            `${saved.productsInserted} new product(s), ${saved.priceRows} price row(s)`,
        );
        await publishProgress(redis, await readProgress(redis));
      } catch (error) {
        if (error instanceof ScrapeAlreadyRunning) return;
        console.error("[scrape] run failed:", error);
      } finally {
        if (currentRun === controller) currentRun = null;
      }
    })();

    // 202: accepted and still running. A 200 would imply the work is done.
    res.status(202).json({
      started: true,
      count: valid.length,
      ...(rejected.length > 0 ? { rejected } : {}),
    });
  });

  router.get("/progress", async (_req: Request, res: Response) => {
    // Both the stored figures AND the authoritative "is it running" from the lock. They can
    // disagree: a process killed mid-scrape leaves `running: 1` in the hash while the claim has
    // expired, and the lock is the one that decides.
    const [progress, running] = await Promise.all([
      readProgress(redis),
      isScrapeRunning(redis),
    ]);
    res.json({ ...progress, running });
  });

  router.get("/results", async (req: Request, res: Response) => {
    const limitRaw = Number(req.query["limit"] ?? 500);
    const limit = Number.isFinite(limitRaw)
      ? Math.min(Math.max(Math.trunc(limitRaw), 1), 2000)
      : 500;
    res.json({ products: await listProducts(limit), limit });
  });

  router.post("/stop", async (_req: Request, res: Response) => {
    if (!currentRun) {
      // Honest about the limit: this process can only stop a run it started. Saying so beats
      // reporting success for something that did not happen.
      const running = await isScrapeRunning(redis);
      res.status(running ? 409 : 200).json({
        stopped: false,
        running,
        ...(running
          ? {
              error:
                "A scrape is running but was started by a different process, so this one cannot " +
                "stop it. It will finish or its claim will expire.",
            }
          : {}),
      });
      return;
    }
    currentRun.abort();
    res.json({ stopped: true });
  });

  return router;
}
