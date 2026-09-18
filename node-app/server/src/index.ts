/**
 * The server: Express routes, a WebSocket for progress, and a health check.
 *
 * Deliberately small. Everything with a decision in it lives in a module with its own tests; this
 * file only wires them together, so there is nothing here that can be wrong in an interesting way.
 */
import { createServer } from "node:http";

import express from "express";
import Redis from "ioredis";
import { WebSocketServer } from "ws";

import { config } from "./config.js";
import { closePool, getPool } from "./db/pool.js";
import { attachProgressBridge, greet } from "./realtime/progress.js";
import { createScrapeRouter } from "./routes/scrape.js";
import { readProgress } from "./scraper/progress.js";

const app = express();
app.use(express.json({ limit: "1mb" }));

// Two clients on purpose: a subscriber cannot issue normal commands. See realtime/progress.ts.
const redis = new Redis({ host: config.REDIS_HOST, port: config.REDIS_PORT });
const subscriber = new Redis({ host: config.REDIS_HOST, port: config.REDIS_PORT });

app.get("/health", async (_req, res) => {
  // Checks its DEPENDENCIES, not just that the process is alive. A health check that only proves
  // Node is running is what let a totally locked database report `active` while every page 500'd.
  const checks: Record<string, string> = {};
  let ok = true;
  try {
    await getPool().query("SELECT 1");
    checks["postgres"] = "ok";
  } catch (error) {
    ok = false;
    checks["postgres"] = error instanceof Error ? error.message : "failed";
  }
  try {
    await redis.ping();
    checks["redis"] = "ok";
  } catch (error) {
    ok = false;
    checks["redis"] = error instanceof Error ? error.message : "failed";
  }
  res.status(ok ? 200 : 503).json({ ok, checks });
});

app.use("/api", createScrapeRouter({ redis }));

const server = createServer(app);
const wss = new WebSocketServer({ server, path: "/ws/progress" });
const detachBridge = attachProgressBridge(subscriber, wss);

wss.on("connection", (socket) => {
  // The current state immediately, so a client that connects mid-scrape does not sit at 0% until
  // the next page completes.
  void readProgress(redis).then((progress) => greet(socket, progress));
});

server.listen(config.PORT, () => {
  console.log(`[server] listening on http://localhost:${config.PORT}`);
  console.log(`[server] websocket at ws://localhost:${config.PORT}/ws/progress`);
});

/** Close the pools on shutdown, so a restart does not leave connections behind. */
async function shutdown(signal: string): Promise<void> {
  console.log(`[server] ${signal} — shutting down`);
  await detachBridge();
  await new Promise<void>((resolve) => server.close(() => resolve()));
  await Promise.allSettled([closePool(), redis.quit(), subscriber.quit()]);
  process.exit(0);
}

process.on("SIGINT", () => void shutdown("SIGINT"));
process.on("SIGTERM", () => void shutdown("SIGTERM"));
