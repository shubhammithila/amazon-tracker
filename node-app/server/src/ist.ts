/**
 * India Standard Time — the ONE place the offset lives, and the one place "today" is decided.
 *
 * **Ported FIRST, before anything that could need it.** The Python `app/ist.py` exists because the
 * same defect was found SEVEN times, each instance fixed in the file that broke, which is exactly
 * why there was always another one: a GST invoice dated before the shipment it billed; the nightly
 * jobs silently running at 09:20 IST under a comment claiming 03:20; the Orders tab rendering a
 * ship-by date 5.5 hours into the wrong day; an FBA `readyToShipWindow` that would have declared
 * the previous Indian day.
 *
 * **JavaScript is worse at this than Python, so the guard has to be stricter here.** Three traps
 * that do not exist in Python:
 *
 * 1. `new Date("2026-08-25")` is parsed as UTC midnight **by specification**, so rendering it in
 *    IST gives 05:30 the following morning. A date-only string and a timestamp parse differently.
 * 2. `toISOString()` is a UTC formatter. Calling it on a correct local `Date` silently answers
 *    yesterday for the 5.5 hours after IST midnight — this shipped four times in the Python app's
 *    templates, including on the GST invoice date.
 * 3. `Date` has no timezone. It is an instant; every "which day is it" question needs an explicit
 *    offset applied, and there is nowhere to hang one.
 *
 * So the rule for this codebase: **no `toISOString()` anywhere, and no `new Date(dateOnlyString)`.**
 * `scripts/check-dates.mjs` enforces both over the whole tree, from day one rather than after the
 * fourth occurrence.
 *
 * A fixed offset, not a tzdata lookup: India has no DST and has not changed offset since 1945, so
 * `Intl` with a zone id would add a dependency on the runtime's ICU data for no behavioural
 * difference — and missing ICU fails at runtime, inside a scheduled job, at 2am.
 */

/** India Standard Time: UTC+05:30, no DST, ever. */
export const IST_OFFSET_MINUTES = 5 * 60 + 30;

const MS_PER_MINUTE = 60_000;

/** A calendar date with no time and no zone — the thing a `Date` cannot safely represent. */
export interface IstDate {
  readonly year: number;
  readonly month: number; // 1-12, NOT JavaScript's 0-11. See `monthIndex` below.
  readonly day: number;
}

/**
 * `month` is 1-12 here while `Date` uses 0-11, and that mismatch is itself a classic bug source.
 * The conversion is named rather than inlined so it appears once.
 */
function monthIndex(month: number): number {
  return month - 1;
}

/** The current instant, shifted into IST so its UTC getters read as IST wall clock. */
function nowShifted(): Date {
  const now = new Date();
  return new Date(now.getTime() + IST_OFFSET_MINUTES * MS_PER_MINUTE);
}

/**
 * **The IST calendar date — what "today" means to this business.**
 *
 * Never the host's local date: production runs UTC, where the two differ for the 5.5 hours after
 * IST midnight. That window matters more than its size suggests — it is when the day's last
 * invoice is raised and when the nightly jobs run.
 */
export function today(): IstDate {
  const shifted = nowShifted();
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
  };
}

/**
 * The last COMPLETE IST day.
 *
 * The ads and portfolio windows end here rather than today, because an ad charge lands hours after
 * the click it belongs to — a window including today reads a punishing ACOS every morning that
 * settles by evening, and a bid rule acting on that would cut bids on a measurement artefact.
 */
export function yesterday(): IstDate {
  return addDays(today(), -1);
}

/** `n` days from `date`, handling month and year boundaries via UTC arithmetic. */
export function addDays(date: IstDate, n: number): IstDate {
  const shifted = new Date(
    Date.UTC(date.year, monthIndex(date.month), date.day + n),
  );
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
  };
}

/**
 * `YYYY-MM-DD`. **The only way a date should reach a query string, a filename or the DOM.**
 *
 * The Python app's `localDate()` in four templates, for the same reason: `toISOString()` would
 * format the same value through UTC and answer the previous day for part of every night.
 */
export function isoDate(date: IstDate): string {
  const mm = String(date.month).padStart(2, "0");
  const dd = String(date.day).padStart(2, "0");
  return `${date.year}-${mm}-${dd}`;
}

/** Today as `YYYY-MM-DD` in IST — the common case, so it does not get hand-assembled. */
export function todayIso(): string {
  return isoDate(today());
}

/**
 * Parse `YYYY-MM-DD` into an `IstDate`, or return null.
 *
 * **Deliberately does not use `new Date(text)`.** That parses a date-only string as UTC midnight,
 * which is the Orders-tab bug: the value then renders as 05:30 the next morning in IST. Parsing
 * the digits directly means no instant is ever constructed, so there is no zone to get wrong.
 *
 * Rejects impossible dates (`2026-02-31`) rather than rolling them over the way `Date` does — a
 * silently-corrected date is worse than a refused one when it decides what ships.
 */
export function parseIsoDate(text: string): IstDate | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text.trim());
  if (!match) return null;
  const [, y, m, d] = match;
  const year = Number(y);
  const month = Number(m);
  const day = Number(d);
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  // Round-trip through UTC to reject 31 February, which Date would roll into March.
  const probe = new Date(Date.UTC(year, monthIndex(month), day));
  if (
    probe.getUTCFullYear() !== year ||
    probe.getUTCMonth() !== monthIndex(month) ||
    probe.getUTCDate() !== day
  ) {
    return null;
  }
  return { year, month, day };
}

/** Compare two IST dates. Negative when `a` is earlier. */
export function compare(a: IstDate, b: IstDate): number {
  return (
    a.year - b.year || a.month - b.month || a.day - b.day
  );
}

/**
 * A stored UTC timestamp as its IST calendar date, `YYYY-MM-DD`.
 *
 * This is what the ads once-per-day bid guard needs. "Not twice on the same day" is decided in IST
 * while the ledger records UTC, so a change applied at 04:00 IST is 22:30 UTC the PREVIOUS day — a
 * UTC-day comparison calls it yesterday and allows a second run that morning.
 *
 * A value with no zone information is treated as UTC, because that is what it is: the database
 * columns are naive UTC by design (see the schema generator's note on TIMESTAMP).
 */
export function dayOf(when: Date): string {
  const shifted = new Date(when.getTime() + IST_OFFSET_MINUTES * MS_PER_MINUTE);
  return isoDate({
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
  });
}

/**
 * An IST wall-clock time on `date` as the instant Amazon and cron want.
 *
 * The Python `ist.utc_instant`, and the reason it exists: Amazon holds
 * `readyToShipWindow: 2026-09-18T18:30Z` for a ship date of the **19th**, because 18:30Z is
 * midnight IST. The obvious `` `${day}T00:00Z` `` declares the previous Indian day — the truck
 * arrives on the 19th against a shipment declared ready on the 18th.
 */
export function utcInstant(date: IstDate, hour = 0, minute = 0): Date {
  return new Date(
    Date.UTC(date.year, monthIndex(date.month), date.day, hour, minute) -
      IST_OFFSET_MINUTES * MS_PER_MINUTE,
  );
}

/**
 * `YYYY-MM-DDTHH:mmZ` — the exact shape Amazon returns and accepts.
 *
 * Assembled from UTC getters rather than `toISOString()`, which would emit seconds and
 * milliseconds. Amazon's schema documents minute precision, and this codebase's rule is that
 * `toISOString` never appears — a single exemption is how a ban stops being one.
 */
export function utcInstantString(date: IstDate, hour = 0, minute = 0): string {
  const at = utcInstant(date, hour, minute);
  const y = at.getUTCFullYear();
  const mo = String(at.getUTCMonth() + 1).padStart(2, "0");
  const d = String(at.getUTCDate()).padStart(2, "0");
  const h = String(at.getUTCHours()).padStart(2, "0");
  const mi = String(at.getUTCMinutes()).padStart(2, "0");
  return `${y}-${mo}-${d}T${h}:${mi}Z`;
}

/**
 * An IST wall-clock time as the `[hour, minute]` a UTC-clocked scheduler needs.
 *
 * `utcHhMm(8, 0) === [2, 30]`. The Python version fixed nightly jobs that were firing at 09:20 IST
 * under a comment that said 03:20 — the misreading written down as a fact. Wraps midnight, which
 * is the case hand-conversion gets wrong: 02:00 IST is 20:30 UTC the *previous* day.
 */
export function utcHhMm(hour: number, minute = 0): [number, number] {
  if (!Number.isInteger(hour) || hour < 0 || hour > 23) {
    throw new RangeError(`${hour} is not an hour`);
  }
  if (!Number.isInteger(minute) || minute < 0 || minute > 59) {
    throw new RangeError(`${minute} is not a minute`);
  }
  const total = hour * 60 + minute - IST_OFFSET_MINUTES;
  const wrapped = ((total % 1440) + 1440) % 1440;
  return [Math.floor(wrapped / 60), wrapped % 60];
}

/**
 * `"08:00 IST (02:30 UTC)"` — for the startup log.
 *
 * Both times, deliberately: the IST one is what was intended and the UTC one is what the log
 * timestamps will show, so a log line proves which was meant instead of leaving the next reader to
 * redo the arithmetic that went wrong in the first place.
 */
export function scheduleLabel(hour: number, minute = 0): string {
  const [uh, um] = utcHhMm(hour, minute);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(hour)}:${pad(minute)} IST (${pad(uh)}:${pad(um)} UTC)`;
}
