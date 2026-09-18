/**
 * The Postgres connection pool.
 *
 * One pool per process, created lazily. `pg` keeps connections open, so constructing one per request
 * would repay the TCP handshake every time — the same cost the SP-API client measured at ~1.2s per
 * call before it started reusing a client.
 *
 * **`NUMERIC` is parsed to a JavaScript number here, deliberately, and the limit is stated.**
 * `pg` returns `NUMERIC` as a STRING by default, because a 1000-digit decimal cannot fit a float.
 * Left as strings, every money figure would silently become `"181.00"` and `"181.00" + "19.00"` is
 * `"181.0019.00"` — string concatenation, no error, a plausible-looking wrong total. That is the
 * `e.cartons` defect class in a money column.
 *
 * The widest column in this schema is `NUMERIC(12,2)`: at most 10 integer digits, which is well
 * inside the 15–16 significant digits a double holds exactly. So the conversion is safe FOR THIS
 * SCHEMA, and it is asserted by a test rather than assumed — if a wider column is ever added, that
 * test fails and this decision has to be revisited.
 */
// **A DEFAULT import, not named ones.** `pg` is CommonJS, so `import { Pool } from "pg"` fails at
// RUNTIME with "does not provide an export named 'Pool'" — while typechecking cleanly, because the
// bundled types describe named exports. Node's ESM loader cannot statically detect CJS named
// exports, so this is one of the few things only running the code reveals. Caught on first boot.
import pg from "pg";

import { config } from "../config.js";

const { Pool, types } = pg;
type Pool = pg.Pool;
type PoolClient = pg.PoolClient;

/** `NUMERIC` / `DECIMAL`. */
const OID_NUMERIC = 1700;
/** `INT8` / `BIGINT` — a count, which `COUNT(*)` returns. */
const OID_INT8 = 20;

types.setTypeParser(OID_NUMERIC, (value) => (value === null ? null : Number(value)));
// `COUNT(*)` comes back as a string for the same reason; a count that is a string breaks every
// comparison silently (`"12" > 9` is false).
types.setTypeParser(OID_INT8, (value) => (value === null ? null : Number(value)));

let pool: Pool | null = null;

export function getPool(): Pool {
  if (!pool) {
    pool = new Pool({
      connectionString: config.DATABASE_URL,
      // Sized for a 951 MB box. The Python app uses pool_size=5/max_overflow=10 for non-SQLite
      // URLs, so this matches rather than inventing a number.
      max: 10,
      idleTimeoutMillis: 30_000,
      connectionTimeoutMillis: 10_000,
    });
    pool.on("error", (error) => {
      // An idle client erroring must not take the process down. Logged rather than swallowed,
      // because a silent connection failure is how "the app is not running" gets reported with
      // nothing in the logs — which happened on production with a stale SQLite lock.
      console.error("[db] idle client error:", error.message);
    });
  }
  return pool;
}

export async function closePool(): Promise<void> {
  if (pool) {
    await pool.end();
    pool = null;
  }
}

/** A one-shot query on a pooled connection. */
export async function query<T extends Record<string, unknown>>(
  sql: string,
  params: unknown[] = [],
): Promise<T[]> {
  const result = await getPool().query(sql, params);
  return result.rows as T[];
}

/**
 * Run `fn` inside a transaction, rolling back on any throw.
 *
 * Used by the scrape writer so a product row and its history rows land together. A partial write
 * here would leave a price recorded against no product, and this codebase has a documented incident
 * where that shape of inconsistency cost 400 units of real packed stock.
 */
export async function transaction<T>(
  fn: (client: PoolClient) => Promise<T>,
): Promise<T> {
  const client = await getPool().connect();
  try {
    await client.query("BEGIN");
    const result = await fn(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    // Released in `finally`, always. A leaked client is a connection the pool can never reuse, and
    // with max=10 it takes ten leaks to hang the app with no error anywhere.
    client.release();
  }
}
