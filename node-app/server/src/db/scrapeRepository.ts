/**
 * Persisting scrape results. The only place the scraper writes SQL.
 *
 * Ported from `save_results_to_db` in `app/routers/scrape.py`, keeping three rules that are easy to
 * "tidy" into bugs:
 *
 * 1. **`Unavailable` rows ARE saved, with a NULL price.** Not skipped. The Python comment says
 *    "to clear stale price/deal data", and that is the whole point: a listing that has gone
 *    unavailable must not keep showing yesterday's price as though it were current. Dropping the
 *    row would leave the last good price as the newest one — the same class of defect as the Ads
 *    tab's stale bid, where the screen showed 13.86 while Amazon held 15.25.
 * 2. **Title and use-by are only OVERWRITTEN by a truthy value** (`r.get("title") or product.title`).
 *    A page that parsed but yielded no title must not blank a name we already have.
 * 3. **History rows are only written when there is a value.** A BSR of null is not rank 0, and a
 *    missing rating is not 0.0 stars — the Portfolio tab's `_ratio` returns None for exactly this
 *    reason, because 0% would rank an unadvertised product as the most efficient in the portfolio.
 *
 * Everything happens in ONE transaction per batch, so a crash cannot leave a price recorded against
 * a product row that was never created.
 */
import type pg from "pg";

type PoolClient = pg.PoolClient;

import type { ProductRow } from "../scraper/parsers.js";
import { query, transaction } from "./pool.js";

/** The statuses that produce a write. Anything else is a failed fetch with nothing to record. */
const PERSISTABLE = new Set(["OK", "Unavailable"]);

export interface SaveSummary {
  productsInserted: number;
  productsUpdated: number;
  priceRows: number;
  bsrRows: number;
  ratingRows: number;
  skipped: number;
}

/**
 * `"₹1,234.50"` -> `1234.5`, or null.
 *
 * Strips everything that is not a digit or a dot, exactly as the Python `re.sub(r"[^\d.]", "")`
 * does — the rupee symbol and the thousands separators both have to go, and doing it by character
 * class rather than by replacing specific symbols means a currency prefix change cannot break it.
 */
export function parsePrice(text: string | null): number | null {
  if (!text) return null;
  const cleaned = text.replace(/[^\d.]/g, "");
  if (!cleaned) return null;
  const value = Number(cleaned);
  // `Number("1.2.3")` is NaN. Guarded rather than trusted, because a NaN reaching a NUMERIC column
  // is a database error at the end of a long scrape.
  return Number.isFinite(value) ? value : null;
}

/** A rating or count as a number, or null. Never 0 for "missing". */
function parseNumber(text: string | null): number | null {
  if (!text) return null;
  const value = Number(text.replace(/,/g, ""));
  return Number.isFinite(value) ? value : null;
}

async function upsertProduct(
  client: PoolClient,
  row: ProductRow,
  now: Date,
): Promise<{ id: number; inserted: boolean }> {
  const existing = await client.query<{ id: number }>(
    "SELECT id FROM products WHERE asin = $1",
    [row.asin],
  );

  const found = existing.rows[0];
  if (!found) {
    const inserted = await client.query<{ id: number }>(
      `INSERT INTO products (asin, title, use_by, first_seen, last_scraped, is_active)
       VALUES ($1, $2, $3, $4, $4, true)
       RETURNING id`,
      [row.asin, row.title, row.useBy, now],
    );
    // `noUncheckedIndexedAccess` makes this explicit rather than an assumed non-null.
    const created = inserted.rows[0];
    if (!created) throw new Error(`INSERT ... RETURNING gave no row for ${row.asin}`);
    return { id: created.id, inserted: true };
  }

  // COALESCE keeps the existing value when the new one is null — rule 2. Written in SQL rather
  // than in JavaScript so there is no read-modify-write window between two statements.
  await client.query(
    `UPDATE products
        SET title = COALESCE($2, title),
            use_by = COALESCE($3, use_by),
            last_scraped = $4
      WHERE id = $1`,
    [found.id, row.title, row.useBy, now],
  );
  return { id: found.id, inserted: false };
}

/**
 * Save a batch of scraped rows. Returns what it actually wrote.
 *
 * The counts are returned rather than logged because the route reports them, and a figure the
 * screen shows must come from the code that did the work — this codebase has shipped "86 orders
 * beside 87 lines" from two places computing one number.
 */
export async function saveScrapeResults(rows: ProductRow[]): Promise<SaveSummary> {
  const summary: SaveSummary = {
    productsInserted: 0,
    productsUpdated: 0,
    priceRows: 0,
    bsrRows: 0,
    ratingRows: 0,
    skipped: 0,
  };
  if (rows.length === 0) return summary;

  // One timestamp for the whole batch, so every history row from one scrape shares a `scraped_at`
  // and a chart cannot show the same run as a smear across several seconds.
  const now = new Date();

  await transaction(async (client) => {
    for (const row of rows) {
      if (!PERSISTABLE.has(row.status)) {
        summary.skipped++;
        continue;
      }

      const { id, inserted } = await upsertProduct(client, row, now);
      if (inserted) summary.productsInserted++;
      else summary.productsUpdated++;

      if (row.status === "Unavailable") {
        // **Rule 1: a deliberate NULL-price row.** This is what stops a delisted product showing
        // its last known price as current.
        await client.query(
          `INSERT INTO price_history (product_id, price, seller, fulfillment, is_deal, scraped_at)
           VALUES ($1, NULL, NULL, NULL, false, $2)`,
          [id, now],
        );
        summary.priceRows++;
      } else {
        const price = parsePrice(row.price);
        if (price !== null) {
          await client.query(
            `INSERT INTO price_history
               (product_id, price, seller, fulfillment, is_deal, scraped_at)
             VALUES ($1, $2, $3, $4, $5, $6)`,
            [id, price, row.seller, row.fulfillment, row.deal === "Yes", now],
          );
          summary.priceRows++;
        }
      }

      // Rule 3: only when there is a value. `bsrNumeric` of 0 is not a real rank either, which is
      // why the Python tests truthiness rather than null.
      if (row.bsrNumeric) {
        await client.query(
          `INSERT INTO bsr_history (product_id, bsr_rank, bsr_category, scraped_at)
           VALUES ($1, $2, $3, $4)`,
          [id, row.bsrNumeric, row.bsrCategory, now],
        );
        summary.bsrRows++;
      }

      const rating = parseNumber(row.rating);
      if (rating !== null) {
        await client.query(
          `INSERT INTO rating_history (product_id, rating, rating_count, scraped_at)
           VALUES ($1, $2, $3, $4)`,
          [id, rating, parseNumber(row.ratingCount), now],
        );
        summary.ratingRows++;
      }
    }
  });

  return summary;
}

export interface ProductListRow extends Record<string, unknown> {
  asin: string;
  title: string | null;
  price: number | null;
  seller: string | null;
  fulfillment: string | null;
  is_deal: boolean | null;
  bsr_rank: number | null;
  bsr_category: string | null;
  rating: number | null;
  rating_count: number | null;
  use_by: string | null;
  last_scraped: Date | null;
}

/**
 * Every product with its LATEST price, BSR and rating.
 *
 * `DISTINCT ON` per history table rather than a window function over a join: joining three history
 * tables first multiplies the rows (3 prices x 2 ratings = 6 rows for one product) and then the
 * "latest" of each is taken from a set that has already been distorted. This is the same trap as
 * `load_snapshot` filtering `seller_sku IS NULL` — without it the channel rows summed on top of
 * their own ASIN row and the dashboard reported roughly double.
 */
export async function listProducts(limit = 500): Promise<ProductListRow[]> {
  return query<ProductListRow>(
    `
    WITH latest_price AS (
      SELECT DISTINCT ON (product_id)
             product_id, price, seller, fulfillment, is_deal
        FROM price_history
       ORDER BY product_id, scraped_at DESC, id DESC
    ),
    latest_bsr AS (
      SELECT DISTINCT ON (product_id) product_id, bsr_rank, bsr_category
        FROM bsr_history
       ORDER BY product_id, scraped_at DESC, id DESC
    ),
    latest_rating AS (
      SELECT DISTINCT ON (product_id) product_id, rating, rating_count
        FROM rating_history
       ORDER BY product_id, scraped_at DESC, id DESC
    )
    SELECT p.asin, p.title, p.use_by, p.last_scraped,
           lp.price, lp.seller, lp.fulfillment, lp.is_deal,
           lb.bsr_rank, lb.bsr_category,
           lr.rating, lr.rating_count
      FROM products p
      LEFT JOIN latest_price  lp ON lp.product_id = p.id
      LEFT JOIN latest_bsr    lb ON lb.product_id = p.id
      LEFT JOIN latest_rating lr ON lr.product_id = p.id
     ORDER BY p.last_scraped DESC NULLS LAST, p.asin
     LIMIT $1
  `,
    [limit],
  );
}
