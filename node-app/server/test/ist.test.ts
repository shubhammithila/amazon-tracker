/**
 * `ist.ts` — the module that exists because this defect was found SEVEN times.
 *
 * **Every assertion is on the IST value, never on the UTC arithmetic.** Asserting `hour === 2`
 * would pin the conversion rather than the intent, which is exactly how the Python app ended up
 * with a comment reading "03:20 IST-ish (the box is UTC...)" — the misreading written down as a
 * fact, saying IST and meaning UTC in one sentence, while the job actually fired at 09:20 IST.
 *
 * The pretend dates are DERIVED from the real one where the real one is involved, because a
 * fixture whose pretend value equals today's real value proves nothing — two mutations survived in
 * the Python suite on exactly that mistake.
 */
import { describe, expect, it } from "vitest";

import {
  addDays,
  compare,
  dayOf,
  isoDate,
  parseIsoDate,
  scheduleLabel,
  today,
  todayIso,
  utcHhMm,
  utcInstant,
  utcInstantString,
  yesterday,
  IST_OFFSET_MINUTES,
} from "../src/ist.js";

describe("the offset", () => {
  it("is UTC+05:30 and nothing else", () => {
    expect(IST_OFFSET_MINUTES).toBe(330);
  });
});

describe("utcInstantString — the FBA ready-to-ship window", () => {
  it("turns a ship date into midnight IST, which is 18:30Z the PREVIOUS day", () => {
    // Pinned against a value Amazon itself returned for a shipment that really went out
    // (FBA15MG3ZWVW, ready 19 Sep). A sign error produces a plausible-looking timestamp 11 hours
    // out, so the measured value is the only trustworthy assertion here.
    expect(utcInstantString({ year: 2026, month: 9, day: 19 })).toBe("2026-09-18T18:30Z");
  });

  it("moves the calendar day BACK, which is the half a sign error gets wrong", () => {
    expect(utcInstantString({ year: 2026, month: 9, day: 19 })).toMatch(/^2026-09-18/);
  });

  it("emits minute precision and a literal Z, not toISOString's shape", () => {
    const out = utcInstantString({ year: 2026, month: 1, day: 5 });
    expect(out.endsWith("Z")).toBe(true);
    expect(out).not.toContain("+00:00");
    // Exactly one colon: seconds and milliseconds would be a different string for the same
    // instant, and Amazon has not been observed to accept that shape here.
    expect(out.split(":").length - 1).toBe(1);
  });

  it("handles a non-midnight IST time on the same day", () => {
    // 08:00 IST is 02:30 UTC — the same day, unlike midnight.
    expect(utcInstantString({ year: 2026, month: 9, day: 19 }, 8, 0)).toBe(
      "2026-09-19T02:30Z",
    );
  });

  it("crosses a month boundary correctly", () => {
    expect(utcInstantString({ year: 2026, month: 10, day: 1 })).toBe("2026-09-30T18:30Z");
  });

  it("crosses a YEAR boundary correctly", () => {
    expect(utcInstantString({ year: 2027, month: 1, day: 1 })).toBe("2026-12-31T18:30Z");
  });
});

describe("utcHhMm — an IST schedule as the UTC hour cron needs", () => {
  it("converts 08:00 IST, the ads refresh time", () => {
    expect(utcHhMm(8, 0)).toEqual([2, 30]);
  });

  it("wraps midnight, the case hand-conversion gets wrong", () => {
    // 02:00 IST is 20:30 UTC the PREVIOUS day. Cron has no opinion about the day, so only the
    // time travels — but getting the wrap wrong shifts a job by a full day.
    expect(utcHhMm(2, 0)).toEqual([20, 30]);
  });

  it("handles exactly midnight IST", () => {
    expect(utcHhMm(0, 0)).toEqual([18, 30]);
  });

  it("refuses a value that is not a wall-clock time", () => {
    expect(() => utcHhMm(24, 0)).toThrow(RangeError);
    expect(() => utcHhMm(-1, 0)).toThrow(RangeError);
    expect(() => utcHhMm(8, 60)).toThrow(RangeError);
    expect(() => utcHhMm(8.5, 0)).toThrow(RangeError);
  });
});

describe("scheduleLabel — states BOTH times so a log cannot mislead", () => {
  it("names the IST time first and the UTC one in brackets", () => {
    // The log is stamped in UTC, so a line carrying only one of the two leaves the next reader to
    // redo the arithmetic that went wrong in the first place.
    expect(scheduleLabel(8, 0)).toBe("08:00 IST (02:30 UTC)");
    expect(scheduleLabel(7, 30)).toBe("07:30 IST (02:00 UTC)");
  });
});

describe("dayOf — a stored UTC timestamp as its IST day", () => {
  it("counts 22:30 UTC as the NEXT IST day", () => {
    // The ads once-per-day bid guard. A change applied at 04:00 IST is 22:30 UTC the previous
    // day, so a UTC-day comparison calls it yesterday and allows a second run that morning —
    // which on a -10% rule compounds to -19% on live bids.
    expect(dayOf(new Date(Date.UTC(2026, 7, 25, 22, 30)))).toBe("2026-08-26");
  });

  it("counts 18:29 UTC as the SAME IST day, one minute before the boundary", () => {
    expect(dayOf(new Date(Date.UTC(2026, 7, 25, 18, 29)))).toBe("2026-08-25");
  });

  it("counts 18:30 UTC as the next day, exactly ON the boundary", () => {
    expect(dayOf(new Date(Date.UTC(2026, 7, 25, 18, 30)))).toBe("2026-08-26");
  });
});

describe("today / yesterday", () => {
  it("agrees with dayOf for the current instant", () => {
    // Derived rather than hardcoded: a fixture whose pretend date happens to equal the real one
    // proves nothing, and this dev box is in IST so a naive check passes for the wrong reason.
    expect(todayIso()).toBe(dayOf(new Date()));
  });

  it("puts yesterday exactly one day before today", () => {
    expect(compare(yesterday(), today())).toBeLessThan(0);
    expect(isoDate(addDays(yesterday(), 1))).toBe(todayIso());
  });
});

describe("parseIsoDate — never through new Date(string)", () => {
  it("reads a valid date", () => {
    expect(parseIsoDate("2026-09-19")).toEqual({ year: 2026, month: 9, day: 19 });
  });

  it("does NOT shift the day, which new Date('2026-08-25') would", () => {
    // The Orders-tab bug: `new Date("2026-08-25")` is UTC midnight by spec, so IST renders it as
    // 05:30 the following morning — half a day into the wrong day, in the column the warehouse
    // plans against.
    const parsed = parseIsoDate("2026-08-25");
    expect(parsed).not.toBeNull();
    expect(isoDate(parsed!)).toBe("2026-08-25");
  });

  it("refuses an impossible date instead of rolling it over", () => {
    // `new Date(2026, 1, 31)` silently becomes 3 March. A silently-corrected date is worse than a
    // refused one when it decides what ships.
    expect(parseIsoDate("2026-02-31")).toBeNull();
    expect(parseIsoDate("2026-13-01")).toBeNull();
    expect(parseIsoDate("2026-00-10")).toBeNull();
  });

  it("refuses shapes that are not YYYY-MM-DD", () => {
    expect(parseIsoDate("19/09/2026")).toBeNull();
    expect(parseIsoDate("2026-9-19")).toBeNull();
    expect(parseIsoDate("")).toBeNull();
    expect(parseIsoDate("2026-09-19T00:00Z")).toBeNull();
  });
});

describe("addDays", () => {
  it("crosses a month boundary", () => {
    expect(isoDate(addDays({ year: 2026, month: 8, day: 31 }, 1))).toBe("2026-09-01");
  });
  it("crosses a year boundary backwards", () => {
    expect(isoDate(addDays({ year: 2026, month: 1, day: 1 }, -1))).toBe("2025-12-31");
  });
  it("handles a leap day", () => {
    expect(isoDate(addDays({ year: 2028, month: 2, day: 28 }, 1))).toBe("2028-02-29");
  });
});

describe("utcInstant returns a real instant", () => {
  it("is exactly 18:30 before the IST midnight it represents", () => {
    const at = utcInstant({ year: 2026, month: 9, day: 19 });
    expect(at.getUTCHours()).toBe(18);
    expect(at.getUTCMinutes()).toBe(30);
    expect(at.getUTCDate()).toBe(18);
  });
});
