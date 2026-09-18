/**
 * Configuration, validated at boot.
 *
 * **Fails at startup rather than at first use**, which is the discipline `app/config.py` learned the
 * hard way: `SECRET_KEY` defaulting to a guessable value was a full authentication bypass, because a
 * session cookie with no role resolves to admin. Failing loudly at startup is the only safe
 * behaviour for anything security- or data-shaped, and `deploy/update-ec2.sh` verifies over HTTP and
 * rolls back, so a missing value surfaces immediately instead of living in a log nobody reads.
 */
import { z } from "zod";

const schema = z.object({
  PORT: z.coerce.number().int().min(1).max(65535).default(8001),

  // Its OWN database, never the Python app's. The two must never write one dataset — this codebase
  // already documents what happens when two writers share a truth.
  DATABASE_URL: z
    .string()
    .default("postgresql://tracker:tracker_local_dev@localhost:5432/tracker"),

  REDIS_HOST: z.string().default("127.0.0.1"),
  REDIS_PORT: z.coerce.number().int().default(6379),

  // Scrape tuning, defaults matching app/config.py exactly so behaviour is comparable.
  SCRAPE_CONCURRENCY: z.coerce.number().int().min(1).max(50).default(10),
  SCRAPE_DELAY_MIN: z.coerce.number().min(0).default(1.5),
  SCRAPE_DELAY_MAX: z.coerce.number().min(0).default(3.5),
  SCRAPE_RETRY_ROUNDS: z.coerce.number().int().min(1).max(10).default(3),
  SCRAPE_TIMEOUT_MS: z.coerce.number().int().min(1000).default(15_000),

  NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
});

export type Config = z.infer<typeof schema>;

function load(): Config {
  const parsed = schema.safeParse(process.env);
  if (!parsed.success) {
    // Every problem at once, not just the first: a boot that fails three times because it reports
    // one missing variable per attempt wastes the reader's time.
    const issues = parsed.error.issues
      .map((issue) => `  ${issue.path.join(".") || "(root)"}: ${issue.message}`)
      .join("\n");
    throw new Error(`Invalid configuration:\n${issues}`);
  }
  const config = parsed.data;
  if (config.SCRAPE_DELAY_MAX < config.SCRAPE_DELAY_MIN) {
    // Range-checked ACROSS fields, not just per field. The Portfolio tab's `good_rating: 99` passed
    // a finite-float check and silently sent BEST BET to zero for ever, because nothing asked
    // whether the value made sense in its own units.
    throw new Error(
      `SCRAPE_DELAY_MAX (${config.SCRAPE_DELAY_MAX}) is below SCRAPE_DELAY_MIN ` +
        `(${config.SCRAPE_DELAY_MIN}) — the random delay would be negative.`,
    );
  }
  return config;
}

export const config = load();
