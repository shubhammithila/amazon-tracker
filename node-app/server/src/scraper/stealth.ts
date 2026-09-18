/**
 * Header rotation and request pacing, ported from `app/scraper/stealth.py`.
 *
 * The values are copied verbatim rather than refreshed. That is deliberate: this exact set has been
 * fetching real pages from the production box, and "newer user agents must be better" is a guess.
 * If Amazon starts refusing them it will refuse both apps identically, which is the behaviour a
 * replacement port wants — a difference here would show up as a parser difference and cost a day.
 *
 * **What this does NOT fix, and cannot:** Amazon serves datacentre IPs a different offer. Measured
 * across six ASINs, three fetches each, from a laptop and from EC2: deal status and price move
 * TOGETHER, so two ASINs read ₹295/Yes locally and ₹349/No on EC2. A parser fault would disagree
 * about the badge while the price stayed identical. No header rotation changes that — it needs
 * residential egress — and it is why the differential test must run both scrapers from the SAME
 * machine or the comparison is meaningless.
 */

/** Exactly the 20 user agents the Python module carries. */
export const USER_AGENTS = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
  "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
  "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
] as const;

export const ACCEPT_LANGUAGES = [
  "en-IN,en;q=0.9,hi;q=0.8",
  "en-IN,en-GB;q=0.9,en;q=0.8",
  "en-US,en;q=0.9,en-IN;q=0.8",
  "en-IN,en;q=0.9",
  "en-GB,en;q=0.9,en-IN;q=0.7,hi;q=0.7",
] as const;

/**
 * `null` is a real option, not a placeholder: a browser opening a product page directly sends no
 * referer, so always sending one is itself a signal.
 */
export const REFERERS = [
  "https://www.google.com/",
  "https://www.google.co.in/",
  "https://www.amazon.in/",
  null,
] as const;

/**
 * Cookies that make Amazon serve Indian prices in rupees.
 *
 * Not stealth — these change what the page SAYS. Without `i18n-prefs=INR` the price block can come
 * back in a different currency, and the parser's `₹` prefix would then be a lie about the number
 * beside it.
 */
export const LOCALE_COOKIES: Readonly<Record<string, string>> = {
  "i18n-prefs": "INR",
  "lc-acbin": "en_IN",
  "sp-cdn": '"L5Z9:IN"',
};

/** Injectable so tests are deterministic; defaults to `Math.random`. */
export type RandomSource = () => number;

function pick<T>(items: readonly T[], random: RandomSource): T {
  // `items.length - 1` clamped, so a random of exactly 1.0 (permitted by the type, if not by
  // Math.random) cannot index past the end. With noUncheckedIndexedAccess the non-null assertion
  // would otherwise be a lie.
  const index = Math.min(Math.floor(random() * items.length), items.length - 1);
  return items[index] as T;
}

/**
 * One plausible browser's headers.
 *
 * `Sec-Fetch-Site` is `none` for a direct navigation and `cross-site` when a referer is sent — they
 * have to agree, because a request claiming no referer while announcing a cross-site fetch is a
 * contradiction no real browser produces.
 */
export function randomHeaders(random: RandomSource = Math.random): Record<string, string> {
  const headers: Record<string, string> = {
    "User-Agent": pick(USER_AGENTS, random),
    Accept:
      "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": pick(ACCEPT_LANGUAGES, random),
    // No `br` unless the client can actually decompress it. undici handles gzip/deflate/br, so this
    // matches the Python set.
    "Accept-Encoding": "gzip, deflate, br",
    DNT: "1",
    Connection: "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
  };

  const referer = pick(REFERERS, random);
  if (referer) {
    headers["Referer"] = referer;
    headers["Sec-Fetch-Site"] = "cross-site";
  }
  return headers;
}

/** The locale cookies as a `Cookie` header value. */
export function cookieHeader(): string {
  return Object.entries(LOCALE_COOKIES)
    .map(([key, value]) => `${key}=${value}`)
    .join("; ");
}

/**
 * A pause between requests, in milliseconds.
 *
 * The Python version returns SECONDS because `asyncio.sleep` takes seconds; this returns
 * milliseconds because `setTimeout` does. Converting at the boundary rather than leaving the caller
 * to remember which unit it got is the same discipline as `ist.utcHhMm` — a bare number with an
 * implied unit is how a 1.5-second delay becomes a 1.5-millisecond one and the scrape gets banned.
 */
export function randomDelayMs(
  minSeconds = 1.5,
  maxSeconds = 3.5,
  random: RandomSource = Math.random,
): number {
  if (maxSeconds < minSeconds) {
    throw new RangeError(`max delay ${maxSeconds} is below min ${minSeconds}`);
  }
  const seconds = minSeconds + random() * (maxSeconds - minSeconds);
  return Math.round(seconds * 1000);
}
