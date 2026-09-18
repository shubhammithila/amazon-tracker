/**
 * The scraper page: paste ASINs, watch live progress, read the results.
 *
 * Deliberately plain. This exists to prove the Node stack works end to end — start a scrape, see the
 * bar move over a WebSocket, see rows land in Postgres — not to reproduce the Python app's styling.
 * The visual port is its own decision and the last UI refresh was reverted in full, so nothing here
 * assumes what it should look like.
 *
 * Two rules carried over from the Python templates, both of which shipped as real bugs there:
 *
 * * **No `toISOString()` and no `new Date(dateString)`.** Formatting a date through UTC answers the
 *   previous day for 5.5 hours out of every 24 in IST, and a date-only string is parsed as UTC
 *   midnight by spec. `scripts/check-dates.mjs` enforces this over the tree.
 * * **Every figure comes from the server.** The progress percentage is computed once, server-side, in
 *   `progress.ts` — this codebase has shipped "86 orders beside 87 lines" from two places computing
 *   one number, and the Portfolio parent rows exist to prevent the same thing.
 */
import { useCallback, useEffect, useRef, useState } from "react";

interface Progress {
  running: boolean;
  progress: number;
  total: number;
  round: number;
  roundTotal: number;
  currentAsin: string;
  errorCount: number;
  resultCount: number;
  lastScrapedAt: string;
  error: string;
  percent: number;
}

interface Product {
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
  last_scraped: string | null;
}

const EMPTY: Progress = {
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
 * An ISO instant as local date and time.
 *
 * Built from the LOCAL getters. `toISOString()` is banned tree-wide and a date-only string never
 * reaches `new Date` — the value here is a full timestamp, which is unambiguous.
 */
function formatInstant(iso: string): string {
  if (!iso) return "—";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ` +
    `${pad(at.getHours())}:${pad(at.getMinutes())}`
  );
}

function money(value: number | null): string {
  // A dash, never a zero. A product with no price does not cost ₹0, and "0" would sort and read as
  // the cheapest thing in the list — the reason the Portfolio tab's `_ratio` returns None.
  if (value === null || value === undefined) return "—";
  return `₹${value.toLocaleString("en-IN", { minimumFractionDigits: 2 })}`;
}

export default function App() {
  const [asinText, setAsinText] = useState("");
  const [progress, setProgress] = useState<Progress>(EMPTY);
  const [products, setProducts] = useState<Product[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);

  const loadResults = useCallback(async () => {
    try {
      const response = await fetch("/api/results?limit=200");
      const data = (await response.json()) as { products: Product[] };
      setProducts(data.products);
    } catch {
      setMessage("Could not load results.");
    }
  }, []);

  useEffect(() => {
    void loadResults();

    // Live progress. The socket is the primary channel; the poll below is a fallback, because a
    // dropped socket must not leave the bar frozen with no explanation.
    const socket = new WebSocket(`ws://${window.location.host}/ws/progress`);
    socketRef.current = socket;
    socket.onmessage = (event) => {
      try {
        setProgress(JSON.parse(String(event.data)) as Progress);
      } catch {
        /* a malformed frame is not worth failing the page over */
      }
    };
    return () => socket.close();
  }, [loadResults]);

  useEffect(() => {
    // A slow poll regardless of the socket: if the WebSocket silently dies the page still converges,
    // and when a run finishes the results are reloaded once.
    const timer = setInterval(() => {
      void (async () => {
        const response = await fetch("/api/progress");
        const next = (await response.json()) as Progress;
        setProgress((previous) => {
          if (previous.running && !next.running) void loadResults();
          return next;
        });
      })();
    }, 3000);
    return () => clearInterval(timer);
  }, [loadResults]);

  const start = async () => {
    setBusy(true);
    setMessage("");
    // Split on anything that is not part of an ASIN, so a pasted column, a comma list or a space
    // separated line all work without asking the user which format to use.
    const asins = asinText
      .split(/[^A-Za-z0-9]+/)
      .map((value) => value.trim().toUpperCase())
      .filter(Boolean);
    try {
      const response = await fetch("/api/scrape", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asins }),
      });
      const data = (await response.json()) as {
        started?: boolean;
        count?: number;
        rejected?: string[];
        error?: string;
      };
      if (!response.ok) {
        setMessage(data.error ?? "Could not start the scrape.");
      } else {
        // The rejected ASINs are NAMED, not just counted. A count that silently shrinks reads as
        // the feature not working.
        const rejected = data.rejected?.length
          ? ` ${data.rejected.length} rejected: ${data.rejected.slice(0, 5).join(", ")}`
          : "";
        setMessage(`Started ${data.count} ASIN(s).${rejected}`);
      }
    } catch {
      setMessage("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    const response = await fetch("/api/stop", { method: "POST" });
    const data = (await response.json()) as { stopped: boolean; error?: string };
    setMessage(data.stopped ? "Stopping…" : (data.error ?? "Nothing to stop."));
  };

  return (
    <main style={styles.page}>
      <h1 style={styles.h1}>Scraper <span style={styles.tag}>Node · Postgres · Redis</span></h1>

      <section style={styles.card}>
        <label htmlFor="asins" style={styles.label}>
          ASINs — paste a column, a comma list, or spaces
        </label>
        <textarea
          id="asins"
          value={asinText}
          onChange={(event) => setAsinText(event.target.value)}
          placeholder="B0CWGXYLT6&#10;B0CY84RYRG&#10;B0D817HX57"
          rows={5}
          style={styles.textarea}
        />
        <div style={styles.row}>
          <button onClick={start} disabled={busy || progress.running} style={styles.primary}>
            {progress.running ? "Scraping…" : "Start scrape"}
          </button>
          <button onClick={stop} disabled={!progress.running} style={styles.button}>
            Stop
          </button>
          <button onClick={() => void loadResults()} style={styles.button}>
            Reload results
          </button>
        </div>
        {message && <p style={styles.message}>{message}</p>}
      </section>

      <section style={styles.card}>
        <div style={styles.row}>
          <strong>{progress.running ? "Running" : "Idle"}</strong>
          <span style={styles.muted}>
            round {progress.round} of {progress.roundTotal}
          </span>
          <span style={styles.muted}>
            {progress.progress} / {progress.total} pages
          </span>
          {progress.errorCount > 0 && (
            <span style={styles.warn}>{progress.errorCount} error(s)</span>
          )}
          <span style={styles.muted}>last run {formatInstant(progress.lastScrapedAt)}</span>
        </div>
        <div style={styles.barOuter}>
          <div style={{ ...styles.barInner, width: `${progress.percent}%` }} />
        </div>
        <div style={styles.row}>
          {/* The percentage is the SERVER's, not recomputed here. */}
          <span style={styles.muted}>{progress.percent}%</span>
          {progress.currentAsin && (
            <span style={styles.mono}>{progress.currentAsin}</span>
          )}
        </div>
        {progress.error && <p style={styles.warn}>{progress.error}</p>}
      </section>

      <section style={styles.card}>
        <div style={styles.row}>
          <strong>{products.length} product(s)</strong>
          <span style={styles.muted}>latest price, BSR and rating per ASIN</span>
        </div>
        <div style={{ overflowX: "auto" }}>
          <table style={styles.table}>
            <thead>
              <tr>
                {["ASIN", "Product", "Price", "Deal", "Seller", "Ship", "BSR", "Rating", "Scraped"].map(
                  (heading) => (
                    <th key={heading} style={styles.th}>
                      {heading}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {products.map((product) => (
                <tr key={product.asin}>
                  <td style={{ ...styles.td, ...styles.mono }}>{product.asin}</td>
                  <td style={styles.td}>{product.title ?? "—"}</td>
                  <td style={{ ...styles.td, textAlign: "right" }}>{money(product.price)}</td>
                  <td style={styles.td}>{product.is_deal ? "Yes" : "—"}</td>
                  <td style={styles.td}>{product.seller ?? "—"}</td>
                  <td style={styles.td}>{product.fulfillment ?? "—"}</td>
                  <td style={{ ...styles.td, textAlign: "right" }}>
                    {product.bsr_rank ? `#${product.bsr_rank.toLocaleString("en-IN")}` : "—"}
                  </td>
                  <td style={styles.td}>
                    {product.rating === null
                      ? "—"
                      : `${product.rating}★ (${product.rating_count?.toLocaleString("en-IN") ?? "—"})`}
                  </td>
                  <td style={styles.td}>
                    {product.last_scraped ? formatInstant(product.last_scraped) : "—"}
                  </td>
                </tr>
              ))}
              {products.length === 0 && (
                <tr>
                  <td style={styles.td} colSpan={9}>
                    No products yet. Paste some ASINs above and press Start.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}

const styles = {
  page: {
    fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif",
    maxWidth: 1100,
    margin: "0 auto",
    padding: 24,
    color: "#1c1c1c",
  },
  h1: { fontSize: 22, marginBottom: 16, display: "flex", alignItems: "center", gap: 10 },
  tag: { fontSize: 11, fontWeight: 600, color: "#6b6b6b", letterSpacing: ".04em" },
  card: {
    border: "1px solid #e2e2e2",
    borderRadius: 8,
    padding: 16,
    marginBottom: 16,
    background: "#fff",
  },
  label: { display: "block", fontSize: 11, color: "#6b6b6b", textTransform: "uppercase" as const,
           letterSpacing: ".04em", marginBottom: 6 },
  textarea: {
    width: "100%",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    fontSize: 13,
    padding: 8,
    border: "1px solid #d4d4d4",
    borderRadius: 6,
    boxSizing: "border-box" as const,
  },
  row: { display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" as const, marginTop: 10 },
  button: {
    padding: "7px 14px",
    fontSize: 13,
    border: "1px solid #d4d4d4",
    borderRadius: 6,
    background: "#fff",
    cursor: "pointer",
  },
  primary: {
    padding: "7px 14px",
    fontSize: 13,
    border: "1px solid #146c43",
    borderRadius: 6,
    background: "#198754",
    color: "#fff",
    cursor: "pointer",
    fontWeight: 600,
  },
  message: { fontSize: 13, marginTop: 10, color: "#0b5ed7" },
  muted: { fontSize: 12, color: "#6b6b6b" },
  warn: { fontSize: 12, color: "#b02a37", fontWeight: 600 },
  mono: { fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12 },
  barOuter: {
    height: 8,
    background: "#eee",
    borderRadius: 999,
    overflow: "hidden",
    marginTop: 12,
  },
  barInner: { height: "100%", background: "#198754", transition: "width .3s ease" },
  table: { width: "100%", borderCollapse: "collapse" as const, fontSize: 13, minWidth: 900 },
  th: {
    textAlign: "left" as const,
    padding: "8px 10px",
    borderBottom: "2px solid #e2e2e2",
    fontSize: 11,
    textTransform: "uppercase" as const,
    letterSpacing: ".04em",
    color: "#6b6b6b",
    whiteSpace: "nowrap" as const,
  },
  td: { padding: "7px 10px", borderBottom: "1px solid #f0f0f0", whiteSpace: "nowrap" as const },
};
