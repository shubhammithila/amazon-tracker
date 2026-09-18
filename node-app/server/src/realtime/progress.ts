/**
 * Live progress over WebSocket, fanned out through Redis pub/sub.
 *
 * The Python app pushes progress from the scraping process directly to its own WebSocket clients
 * (`app/routers/ws.py`), which works because there is exactly one process. Pub/sub removes that
 * assumption: whichever process runs the scrape publishes, and every process with connected clients
 * forwards. A browser therefore sees progress even when its socket is served by a different worker
 * from the one doing the work.
 *
 * **A separate Redis connection is required for subscribing.** ioredis puts a client into subscriber
 * mode and it can then only issue subscribe/unsubscribe commands — sharing the command client would
 * break every other query in the process with a confusing "only (P)SUBSCRIBE / ... allowed in this
 * context" error at some unrelated call site.
 */
import type Redis from "ioredis";
import type { WebSocket, WebSocketServer } from "ws";

import type { ScrapeProgress } from "../scraper/progress.js";

const CHANNEL = "tracker:scrape:progress";

/** Publish a progress snapshot. Failures are logged, never thrown. */
export async function publishProgress(
  redis: Redis,
  progress: ScrapeProgress,
): Promise<void> {
  try {
    await redis.publish(CHANNEL, JSON.stringify(progress));
  } catch (error) {
    // A broken progress push must not fail the scrape. Losing a frame costs a slightly stale bar;
    // failing the run costs the work.
    console.error("[ws] failed to publish progress:", error);
  }
}

/**
 * Forward every published snapshot to all connected clients.
 *
 * Returns a cleanup function, so a test or a shutdown can close the subscriber rather than leaking
 * a connection — this codebase has a documented incident where a leaked SQLite transaction locked
 * the whole app and `systemctl` still reported it healthy.
 */
export function attachProgressBridge(
  subscriber: Redis,
  wss: WebSocketServer,
): () => Promise<void> {
  void subscriber.subscribe(CHANNEL).catch((error: unknown) => {
    console.error("[ws] subscribe failed:", error);
  });

  const onMessage = (channel: string, message: string): void => {
    if (channel !== CHANNEL) return;
    for (const client of wss.clients) {
      // `readyState === OPEN` is 1. Sending to a CLOSING socket throws, and an unhandled throw here
      // would take down the bridge for every other client.
      if (client.readyState === 1) {
        try {
          client.send(message);
        } catch {
          // One dead client must not stop the fan-out.
        }
      }
    }
  };

  subscriber.on("message", onMessage);

  return async () => {
    subscriber.off("message", onMessage);
    await subscriber.unsubscribe(CHANNEL).catch(() => {});
  };
}

/** Send the current state to a client the moment it connects, so the bar is never blank. */
export function greet(socket: WebSocket, progress: ScrapeProgress): void {
  if (socket.readyState === 1) {
    socket.send(JSON.stringify(progress));
  }
}

export const PROGRESS_CHANNEL = CHANNEL;
