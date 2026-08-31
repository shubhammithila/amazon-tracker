# Ads tab: one source of truth, and the ₹1,26,328 that disappeared

## The report

Two windows, the second a strict superset of the first:

| Window | Total spend |
|---|---|
| 22–28 Aug | **₹4,44,550** |
| 22–29 Aug | **₹3,34,300** |

Adding a day *reduced* the total. Reported alongside a request to make the tab faster by scraping a
fixed 60-day history nightly and deriving every view from it.

## The cause, measured

`refresh.run` stores one payload at **two grains**:

* `ads_performance` — one row set per window
* `ads_performance_daily` — one row per entity per day

and the read side picks between them:

```
exact window cached?  → load_performance()   → SP + SB   ✓
otherwise             → sum_daily()          → SP only   ✗
```

**Sponsored Brands is only ever written to the window table.** The code says so, with its reasoning:

> *"**Stored as SUMMARY only, not daily.** SB is 2,914 rows against SP's 12,205, so the sub-range
> machinery matters far less there, and a second daily report would double the slowest phase of the
> refresh for a fifth of the data."*

That reasoned from **row count**. The figure that matters is **spend share**: SB is 28% of the money.

Confirmed against the live account:

| Window | Provenance | Total | SP | SB |
|---|---|---|---|---|
| 22–28 | `derived=false` — stored window | ₹4,44,550 | ₹3,18,222 | **₹1,26,328** |
| 22–29 | `derived=true` — summed daily | ₹3,34,300 | ₹3,34,300 | **₹0** |

`ads_performance_daily` holds nine days of SP rows and **not one SB row**.

### The serious half: bid rules go SB-blind

`POST /ads/preview` uses the same fallback, so a rule previewed on a derived window cannot see any
Sponsored Brands row. Same rule (`spend > 100, ROAS < 2, −10%`), one day apart:

| Window | Changes | of which SB |
|---|---|---|
| 22–28 | **1,005** | **296** |
| 22–29 | **743** | **0** |

296 live SB bids were invisible to a rule meant to act on them — and this is the only feature in the
app that spends money. The `745 bid(s)` in the reported screenshot is the SB-blind number.

Every ROAS and ACOS figure on a derived window is wrong for the same reason, so the KPI strip reading
`1.49x` against `1.35x` is this defect, not a real change in performance.

## Decisions taken

* **Daily rows become the ONLY source.** SB is stored daily like SP; `ads_performance` is deleted;
  every window including 7/14/30 is a `GROUP BY` over daily rows.
* **60 days of history**, extended in a second commit.
* **Presets end yesterday**, default 7d; today only via a manual Refresh, labelled as settling.
* **`KEEP_BACKUPS` 5 → 3** rather than growing the EBS volume.
* **Apply/Cancel at the top of the preview as well as the bottom.**
* Preview gains a **select-all toggle**, the **ad group** per row, and **Amazon's suggested bid**.

## Costs, measured not estimated

One real 31-day DAILY `spTargeting` report on this account: **259,900 rows, 19.5 minutes.**

| | |
|---|---|
| Nightly 60d = 2 SP chunks + 2 SB chunks | **~50–60 min** at 03:50 |
| 60 days of daily rows, SP+SB | **~93 MB** |
| `ads_performance` deleted | **−17.1 MB** (currently the largest table in the database) |
| Net database change | ~+42 MB, 48 MB → ~90 MB |
| Free disk after, with `KEEP_BACKUPS=3` | **~835 MB** of 912 MB |

July runs 8,384 rows/day against August's 6,107, so a projection taken from August alone
under-counts by 1.4×. Both figures above use the July measurement.

## 1. Storage — one table, not two

Migration deletes `ads_performance` (105,755 rows, all reproducible from daily rows or a refetch).

**`update-ec2.sh` needs a newest-first detector branch keyed on a column this revision changes.**
CLAUDE.md records that a stale detector once **stamped production backwards** and failed two
deploys; `tests/test_schema_migrations.py` runs the detector against a freshly-migrated database.

Deleting the window table also removes `purge_windows`, `WINDOW_RETENTION_COUNT` and the
"6 most recently viewed windows" eviction policy — a retention rule that only existed because the
same numbers were being cached twice.

## 2. `app/ads/refresh.py` — SB daily, and chunk-by-chunk commits

Commit 1 keeps the 7-day nightly window and adds `daily=True` to the SB fetch. Commit 2 extends to
60 days, which needs three properties the current job lacks:

* **Each chunk commits as it lands.** A failure in chunk 3 of 4 leaves the stored days stored. The
  existing SP-before-SB ordering already works this way — verified on production, where SP stored
  12,213 rows in a run whose SB report was throttled — and this extends the rule to chunks.
* **SB throttling is expected.** `sbTargeting` has been measured returning 429 **after 15 minutes of
  complete idleness**, because reports had been created earlier the same day. A 4-report job will
  meet it. A throttled chunk is reported as "not now"; it must not read as a failed refresh.
* **`daily_range_complete` is the guard that makes a gap safe.** It already checks every day in a
  range rather than the endpoints, so a missing chunk makes that window decline to answer instead of
  quietly summing short.

## 3. `app/ads/repository.py` — and a trap in `save_daily`

`save_daily` **already accepts `ad_product`** and already writes it onto every row; `refresh.run`
simply never passes it for SB, because it never calls `save_daily` for SB at all. So the change at
the call site is one argument.

**But `save_daily` cannot be called twice for the same days as it stands, and that is the trap.** It
is delete-then-bulk-insert — the deliberate 62× deviation from the house upsert — and its delete is
scoped by **day alone**:

```python
delete(AdsPerformanceDaily).where(AdsPerformanceDaily.day.in_(sorted(days)))
```

Storing SP and then SB for the same week would therefore have the SB write **delete every SP row it
just stored**, leaving SB-only days: the current bug inverted, and worse, because SP is 72% of spend.
The delete must be scoped by `(day, ad_product)` so each product replaces only its own rows.

This is exactly the property the function's docstring already claims — *"scoped per DAY so refetching
a 7-day window cannot disturb the other 23 days"* — extended one dimension, because until now only
one product ever reached it.

* `sum_daily` becomes the only read path; `load_performance` is deleted with its table.
* `purge_daily` retention 30 → 60 days.

## 4. Suggested bids — the endpoint had to be found by probing

Four candidates called for real against the live account:

| Endpoint | Result |
|---|---|
| `/v2/sp/adGroups/{id}/bidRecommendations` | **404** "Method Not Found" |
| `/v2/sp/keywords/{id}/bidRecommendations` | **404** |
| `/sp/keywords/bid/recommendations` | **403**, twice, with a spurious SigV4 error |
| **`/sp/targets/bid/recommendations`** | **200 — real bids** |

The working endpoint takes `targetingExpressions`, handles keywords via `KEYWORD_EXACT_MATCH`, and is
**batched per ad group** — so a 1,005-row preview costs roughly one call per distinct ad group rather
than 1,005 calls. Media type `application/vnd.spthemebasedbidrecommendation.v3+json`.

**Amazon returns THREE bids, not one:** `[10.68, 14.24, 17.80]`. The middle value is the suggestion
and the outer two are its range, so the column shows `₹14.24` with `10.68–17.80` available. Recording
one number as "the suggested bid" would be a silent choice between three.

The response also carries `impactMetrics` (estimated clicks and orders per bid level). Out of scope.

**Fetched live at preview time**, so a large preview makes N ad-group calls before rendering. If that
measures slow, the fix is lazy per-row fetching — deliberately not built speculatively.

**A suggestion is never applied by a rule.** It sits beside the bid as context for a human.

### Sponsored Brands has NO suggested bid, and that was measured too

Assuming the SP endpoint covers SB rows would be the same mistake as the SB-blind bid rule this
change exists to fix — SB has its own paths for everything else, including its own 100-row page cap
and three distinct `207` body shapes. So it was probed separately. All three candidates **404**:

| Endpoint | Result |
|---|---|
| `/sb/recommendations/bids/keyword` | **404** "Could not find resource for full path" |
| `/sb/recommendations/bids/targets` | **404** |
| `/sb/targets/bid/recommendations` | **404** |

So the suggested-bid column is **empty for the ~296 SB rows in a typical preview**, and it must render
an em dash with a reason available — never a blank, and never an SP figure borrowed for an SB row.
A blank cell in a bid column reads as "no suggestion, bid low"; the honest answer is "Amazon does not
offer one here". This is the same three-state discipline the Portfolio tab's ACOS column already
follows (`—` / `no sales` / the number).

## 5. `templates/ads.html` — four preview changes

1. **Apply/Cancel at the top as well as the bottom.** One handler and one count rendered twice, so
   they cannot disagree. With 1,005 rows the top bar is the one that gets used; the bottom one serves
   someone who has read to the end.
2. **A select-all TOGGLE**, not a lone unselect button — the real gesture on an all-ticked list of
   1,005 is "clear everything, then pick the five I want". Its label derives from `approved.size`.
3. **The ad group per row.** `attach_names` already resolves `ad_group_name` in one query and the
   preview simply never rendered it. It matters because **the same keyword text exists in several ad
   groups at different bids**, so the campaign alone does not identify the row being changed.
4. **The suggested bid column**, beside the old → new bid.

## Verification

**The automated tests matter less than usual here, because every existing test passed while
₹1,26,328 was disappearing.** So the assertions are invariants, not descriptions:

* **A superset window can never report less than its subset.** Asserted over stored rows at both
  grains. This is the violated invariant, stated as itself rather than as "SB is stored daily" —
  a test phrased the second way would pass again the next time a product is added.
* **A rule preview returns identical rows for a window whether fetched exactly or derived.** Pins
  the 1,005-vs-743 gap.
* Every window with SB spend reports it: totals, per campaign, KPI strip, and rule preview.
* **Storing SB for a week does not delete that week's SP rows.** The `save_daily` trap above, pinned
  directly: store SP, store SB for the same days, assert both are present. Fails against a delete
  scoped by day alone.
* `daily_range_complete` refuses a range with an interior gap.
* An SB row's suggested bid renders as a dash with a reason, never blank and never an SP value.
* The deploy detector answers the new head.
* **Mutations:** drop SB from the daily write; make `sum_daily` SP-only; scope the `save_daily` delete
  by day alone; let `daily_range_complete` check only the endpoints; take the first of the three
  suggested bids instead of the middle.

**On production:** re-run the reported rule on 22–28 and 22–29 and confirm both report 1,005 changes
with 296 SB rows and the same total spend.

## Out of scope

`impactMetrics`; automatic application of a suggested bid; growing the EBS volume; any change to the
guardrails, the ledger, or the four writers.
