/**
 * Fetching one product page, and turning every failure into a named status.
 *
 * Ported from `app/scraper/engine.fetch_product_page` plus `http_client.create_client`. The
 * status strings are the Python ones VERBATIM — `"Throttled (503)"`, `"Timeout"`,
 * `"Connection Error"` — because the retry logic branches on them and the differential test
 * compares them. Renaming one to read better would silently stop the retry.
 *
 * Uses undici's `Pool` rather than bare `fetch` for the reason the Python code keeps one
 * `AsyncClient` per batch: on the SP-API client, measured, three calls on a reused connection took
 * 2.5s while a fresh client per call cost ~1.2s each — the TCP and TLS handshake was the cost, not
 * the waiting. Amazon product pages are 2.3 MB over TLS, so this matters more here.
 */
import { Pool } from "undici";

import { cookieHeader, randomHeaders } from "./stealth.js";

/** Amazon.in, as one connection pool. */
const ORIGIN = "https://www.amazon.in";

export interface FetchResult {
  /** `"OK"` means a 200 with a body; anything else is a named failure. */
  status: string;
  html: string | null;
}

/**
 * A pool sized to the scrape's concurrency.
 *
 * `connections` mirrors httpx's `max_connections=15`. `pipelining: 1` is deliberate: HTTP/1.1
 * pipelining against a CDN that may reorder responses is a source of mismatched bodies, and one
 * page attributed to the wrong ASIN is far worse than a slower scrape.
 */
export function createPool(timeoutMs = 15_000): Pool {
  return new Pool(ORIGIN, {
    connections: 15,
    pipelining: 1,
    // `connect` is the TCP/TLS budget and `headers`/`body` the response budget. httpx used
    // `Timeout(timeout, connect=10)`, so the same split is kept.
    connectTimeout: 10_000,
    headersTimeout: timeoutMs,
    bodyTimeout: timeoutMs,
  });
}

/**
 * Fetch one product page. **Never throws** — every outcome is a status string.
 *
 * That contract is why the Python worker can treat the result uniformly and retry on three
 * specific statuses. A thrown error here would have to be caught in the worker loop and
 * translated anyway, and a missed translation would abort a whole batch.
 */
export async function fetchProductPage(
  pool: Pool,
  asin: string,
  signal?: AbortSignal,
): Promise<FetchResult> {
  try {
    const response = await pool.request({
      path: `/dp/${asin}`,
      method: "GET",
      headers: { ...randomHeaders(), cookie: cookieHeader() },
      // Amazon redirects /dp/ to a canonical URL, and without following it every page is a 301
      // with no body. httpx had follow_redirects=True; undici needs it asked for explicitly, and
      // a low cap because a redirect loop is a failure rather than something to chase.
      maxRedirections: 5,
      ...(signal ? { signal } : {}),
    });

    if (response.statusCode === 503) {
      // Amazon's throttle. Its body is a real page, so it must be discarded explicitly or the
      // connection is not released back to the pool.
      await response.body.dump();
      return { status: "Throttled (503)", html: null };
    }
    if (response.statusCode === 404) {
      await response.body.dump();
      return { status: "Not Found (404)", html: null };
    }
    if (response.statusCode !== 200) {
      await response.body.dump();
      return { status: `HTTP ${response.statusCode}`, html: null };
    }

    const html = await response.body.text();
    return { status: "OK", html };
  } catch (error) {
    // The statuses the retry loop branches on. Matched on undici's error CODES rather than
    // message text, which is not a stable interface.
    const code = (error as { code?: string }).code ?? "";
    const name = (error as { name?: string }).name ?? "";

    if (
      code === "UND_ERR_HEADERS_TIMEOUT" ||
      code === "UND_ERR_BODY_TIMEOUT" ||
      code === "UND_ERR_CONNECT_TIMEOUT" ||
      name === "TimeoutError"
    ) {
      return { status: "Timeout", html: null };
    }
    if (
      code === "ECONNREFUSED" ||
      code === "ECONNRESET" ||
      code === "ENOTFOUND" ||
      code === "EAI_AGAIN" ||
      code === "UND_ERR_SOCKET"
    ) {
      return { status: "Connection Error", html: null };
    }
    if (name === "AbortError") {
      // A deliberate stop, not a fault. Named separately so a stopped scrape does not inflate the
      // error count and look like Amazon blocking us.
      return { status: "Stopped", html: null };
    }

    const message = error instanceof Error ? error.message : String(error);
    return { status: `Error: ${message.slice(0, 80)}`, html: null };
  }
}

/** The three statuses worth retrying. Verbatim from the Python worker. */
export const RETRYABLE_STATUSES: readonly string[] = [
  "Throttled (503)",
  "Timeout",
  "Connection Error",
];
