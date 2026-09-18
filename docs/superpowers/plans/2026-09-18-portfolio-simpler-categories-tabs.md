# Portfolio: simpler, category sales, three tabs, live ratings

Five requests. **Split across two deploys** at your direction — the first four ship together and are
usable tomorrow; daily economics is its own change because it touches every margin on the tab and a
silent error there is the most expensive kind.

## What was asked

1. Make it simpler
2. Category sales — Sattu, Chana, Flours, Staples, Seeds, others (from the Shipment priority)
3. Three tabs only: Scale (top), Maintain (mid), Kill or monitor (least)
4. Live ratings from the scraper daily, feeding the bucketing
5. Sales/ad data updated daily for all of 7d/30d/60d/90d, with any sub-range instant — the Ads tab pattern

## Decisions taken (yours)

- **All four simplifications**: fewer KPI tiles, banners collapsed, filter builder hidden, fewer columns
- **SURGICAL and AD DEPENDENT become row FLAGS, not tabs** — the information survives, the tab count drops
- **The scrape gets its own flag** and runs nightly, off-peak
- **P4 stays "Rice"**, consistent with the Shipment tab
- **Unclassified is its own bucket and is NAMED**, not folded into Rest
- **This deploy excludes daily economics**; that follows separately

---

## 1. Three tabs, with the two odd verdicts as flags

The seven verdicts collapse to three **groups**. `verdict_for` is NOT rewritten — it keeps returning
its seven answers, and a new `logic.verdict_group` maps them:

| Group | From | Why |
|---|---|---|
| **Scale** | BEST BET, SCALE | profitable and cheap to advertise |
| **Maintain** | MONITOR, SURGICAL | works overall; SURGICAL needs surgery, not a kill |
| **Kill or monitor** | KILL, DEAD, AD DEPENDENT | losing money, no signal, or ads that lose money |

**Why map rather than rewrite.** `verdict_for` holds seven rules whose ORDER is the rule, each with a
reason string the owner can check against Seller Central, and CLAUDE.md records what each one cost to
learn — rule 1 must stay first because a product that sold 2 units reported **+505.6% net**; rule 3 is
AND not OR; rule 4 is why the tab expands to sizes at all. Rewriting them into three would throw that
away and make the reasons unverifiable. Mapping keeps every rule and every reason, and the row still
says *why*.

**The two flags, on the row:**

- `SURGICAL` → **⚠ 1 size losing money**, naming the size. Measured live: Cheese & Cream Chana earns
  +27.1% overall while one 250 g pack burns 103% TACOS at −52.7% net. In "Maintain" without the flag
  that product reads as fine.
- `AD DEPENDENT` → **⚠ ads at 104% ACOS**. Profitable, but ₹1 of advertising returns less than ₹1.
  In "Kill or monitor" the flag is what stops a profitable product being killed when the fix is to
  cut spend.

`VERDICT_HELP` already explains each rule in words with live numbers substituted; the ⓘ keeps working
because the underlying verdict is unchanged.

## 2. Category sales

`logic.category_for` and `CATEGORY_LABELS` are **imported from `app.shipment.logic`, not copied**. One
rule, one vocabulary — and the keyword ORDER is the rule (`"Bangla Chana Sattu"` is a sattu, not a
chana; `"Rice Atta"` is a flour).

The join is ASIN → catalogue product name → `product_categories.product_key` (casefolded). Measured on
production: **38 of ~90 parents are classified**, so:

- A parent with a stored category uses it.
- A parent with none is **`Unclassified`** — its own bucket, counted and NAMED (capped at 8, the way
  the Shipment catalogue notes and the Projections `needs_review` list already do). Not folded into
  Rest, which would make Rest the largest bucket and meaningless, and not silently keyword-guessed,
  which would hide a wrong guess.

Rendered as a strip above the table: category · sales · ad spend · net · margin. **Every percentage is
recomputed from the summed numerator and denominator, never averaged** — the rule `_sum_sizes` already
follows, because the mean of 20 products' TACOS weights a 1-unit product equally with a 400-unit one.

The strip is **clickable to filter the table**, so "why is Seeds' margin low" is one click.

## 3. Simpler screen

| Now | After |
|---|---|
| 7 KPI tiles | **4**: Sales · Ad spend · Net (pre-COGS) · Net margin. TACOS and ACOS move into the table, where they are per-product; Units joins the row count line |
| 2 full-width banners | **one line** with an ⓘ that expands. Both facts survive — pre-COGS and the ratings date — because a margin read as profit and a stale star rating silently shaping a verdict are exactly what they are there to prevent |
| filter builder + search + toggle in a row | search and the Products/SKUs toggle stay; the **filter builder hides behind "Add filter"** |
| 11 table columns | **7 by default** — Product · Sales · Ad spend · TACOS · ACOS · Net · Margin — with Units, Returns, Rating and Verdict behind a "More columns" toggle |

Nothing is deleted: every hidden figure is one click away, and the export keeps all of them.

## 4. Live ratings, daily

**The finding, measured: the scrape has never run on production.** `SCHEDULER_ENABLED=false`, so the
06:00 product scrape is asleep — the 48 scrape days in `rating_history` are all manual, and the tab's
banner says ratings are from 2026-09-12.

That flag is off for a real reason: a 271-ASIN scrape once reached 419 MB RSS on a 951 MB box **with
no swap** and wedged the app. There is now **2 GB of swap (72 MB in use)**, and the same scrape
re-measured at 263 MB peak with the app answering in 0.1 s throughout.

So: **`SCRAPE_ENABLED`**, its own flag beside `ORDER_REFRESH_ENABLED`, OR'd into the guard the same
way — so the product scrape can run without waking the 07:30 keyword track or the 09:15 purge.

- **05:00 IST**, through `ist.utc_hhmm`. Deliberately before portfolio (07:30) and ads (08:00): the
  ratings must be fresh *when the portfolio job reads them*, and two multi-minute jobs must not
  overlap on this box.
- The existing ratings-staleness banner stays. It is what made this visible, and it must keep working
  for the night the scrape fails.

> `settings.scheduler_enabled` still registers everything, so an installation that never learns about
> this flag is unaffected. A test asserts the registered job **ids**, not the flag — the lesson from
> `setup_scheduler`'s guard, where moving one `if` to the top silently stopped the orders refresh.

## 5. Daily economics — NOT in this deploy

Recorded here so the reasoning is not lost, and specified in its own plan next.

The growth problem is already visible on production: economics is cached **per window**, so six
near-identical 30-day windows are stored separately —

```
2026-08-14 → 2026-09-12   721 rows
2026-08-15 → 2026-09-13   721 rows
...
2026-08-19 → 2026-09-17   802 rows      20,896 rows total
```

— each overlapping 29/30 days with the last, while any range *not* exactly cached costs a fresh 1–2
minute Amazon call. Daily rows invert both: 267 ASINs × 90 days ≈ **24,000 rows, bounded**, and every
sub-range becomes a `GROUP BY` (the Ads equivalent measured **327 ms** for a 3-day slice).

Three things that change will have to get right, each a documented trap:

- `DAY` granularity is supported — the economics module says so.
- **Completeness per DAY, not per span.** The Ads tab lost ₹1,26,328 to a window whose endpoints
  existed and whose middle did not, summing short. `range_completeness` is the precedent.
- **Ratios are recomputed, never summed.** Sales, spend, units and fees add; TACOS, ACOS and margin
  must come from the summed numerator over the summed denominator.

And one cost to weigh: a 90-day daily fetch is large and asynchronous, so the nightly job gets
materially longer on a 951 MB box. That argues for fetching **incrementally** — yesterday only, each
night — rather than re-fetching 90 days.

## Files

Changed: `app/portfolio/logic.py` · `app/portfolio/repository.py` · `app/routers/portfolio.py` ·
`app/scheduler.py` · `app/config.py` · `templates/portfolio.html` · `CLAUDE.md`

New: `tests/test_portfolio_groups.py` · `tests/test_portfolio_categories.py` ·
`scripts/mutate_portfolio_groups.py`

No migration.

## Verification

**Automated** (2,231 green now; each must fail against current code)

- Every one of the seven verdicts maps to exactly one of the three groups, and the mapping is
  **total** — asserted by iterating `VERDICT_ORDER`, so a new verdict cannot be added without a home.
- SURGICAL lands in Maintain and AD DEPENDENT in Kill-or-monitor, each **carrying its flag**. A
  profitable AD DEPENDENT product must not appear without the "cut the spend" reason.
- `verdict_for` is unchanged: its seven answers and their reason strings are asserted, so the grouping
  is provably a view rather than a rewrite.
- Category totals **sum to the account total**, and an unclassified parent is counted under
  `Unclassified` and NAMED — not silently in Rest.
- Category percentages are recomputed from sums: a category with one 1-unit product and one 400-unit
  product reports the weighted figure, not the mean.
- `category_for` is IMPORTED from `app.shipment.logic`, asserted at source, so the keyword order
  cannot fork into two copies.
- The scrape job registers under its own flag with `SCHEDULER_ENABLED=false`, asserted on job **ids**,
  and it is scheduled BEFORE the portfolio job.
- The four KPI tiles and the hidden columns are asserted in the template, and the export still carries
  every column.
- **Mutations**: a verdict mapped to two groups; SURGICAL folded in without its flag; category
  percentages averaged instead of recomputed; unclassified folded into Rest; `category_for` copied
  rather than imported; the scrape job registered only under the master flag.

**Manual, on production after deploy**

`/portfolio-page` → four tiles, one banner line, three tabs → the category strip totals match the
Sales KPI → a SURGICAL product shows ⚠ with its losing size named → 52 products appear under
Unclassified → tomorrow morning the ratings banner reads today's date.

## Out of scope

Daily economics (next plan). Any change to `verdict_for`'s rules or thresholds. The Excel export's
column set. Classifying the 52 unclassified products — they are named so you can do it on the
Shipment tab, where that control already exists.
