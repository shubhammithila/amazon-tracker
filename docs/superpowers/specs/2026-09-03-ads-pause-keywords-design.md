# Pausing keywords and targets from a rule

## The request

> "Now i want to add negative targeting as well in the ads tab. like say we make a rule to check
> if any keyword has spend>1000 and orders 0 or roas<1, then we remove or turn off that keyword."

Two distinct things are named there — "remove" and "turn off" — and on Amazon they are different
objects with different reversibility. This spec builds **turn off**, and deliberately does not build
**remove**. See "Why negatives are out of scope".

## This reverses a documented decision, knowingly

`CLAUDE.md` currently states, under the Ads tab:

> **Bids only.** No pause, no archive, no budget edits, no keyword creation. Pausing stays a manual
> job in Seller Central.

That sentence was right when written: every guard in `/ads/apply` was built around bid arithmetic,
and adding a second kind of write before that path was proven would have widened the blast radius of
the only feature in this app that spends money. The path is now proven — 105 real changes applied on
31 Aug, a working undo chain, a ledger that survives a crash.

What has NOT changed is the reasoning behind `no archive`. That half of the sentence stands, and this
spec strengthens the record of why (below). The line becomes "bids and pause/enable only".

## The rule was measured before it was designed

Against the live account, 30-day window `2026-08-04..2026-09-02`, 29,394 entity rows:

| Rule | Rows | Spend |
|---|---|---|
| `spend>1000 AND orders=0` | 15 | ₹20,648 |
| `spend>1000 AND roas<1` | **88** | **₹1,40,751** |

Three findings that shaped the design:

- **`orders=0` is a strict subset of `roas<1`** (set difference 0), so the owner's "or" is already
  expressed by `roas<1` alone. Both remain available; the spec does not need an OR combinator, and
  the existing AND-only condition builder is sufficient.
- **Every one of the 88 rows has more than 10 clicks. Zero rows have zero clicks.** This is the
  finding that makes the feature safe to build, and it is the direct counterpart to the zero-ROAS
  warning `CLAUDE.md` records as REJECTED. There, 1,107 of 1,425 rows had zero clicks — ROAS was an
  undefined ratio, not a measurement, and raising the bid was the correct action. Here the `spend>1000`
  floor does the discriminating: a keyword that has spent over ₹1,000 has been tested. **The
  distinction is the click count, not the ROAS**, and a future reader who sees "ROAS < 1" and recalls
  the rejected warning should read this paragraph before concluding the two contradict each other.
- **`roas<1` is 6.8% of total spend, spread over 46 ad groups, and no ad group would be fully
  silenced** (0 ad groups where every active row matches). So the rule cannot accidentally take a
  whole ad group off air.

Row composition, which is why all four writers are in scope:

| | SP | SB |
|---|---|---|
| keywords | 31 | 17 |
| targets | 23 | 17 |

Match types present: `PHRASE` 31, `TARGETING_EXPRESSION` 34, `EXACT` 15, `BROAD` 2,
`TARGETING_EXPRESSION_PREDEFINED` 6.

## Decisions taken (the owner's)

- **Pause and enable. No negative keywords**, now or in this change.
- **`state` is a rule action with two values**, so undo of a pause IS a forward enable and needs no
  reverse-specific code path.
- **Approach A: extend the existing write path.** One copy of the batching, request-ordering and
  three 207-response parsers, not a second writer.
- **Guardrails: reuse `max_rows`, add a range-checked `min_pause_spend`.**
- **Bid-less rows keep the existing skip.** Proposed as a change and correctly rejected by the owner
  ("there are no targets without a bid") — verified across 37,943 rows over 60 days: 8 have a null
  bid and exactly one of those has spend, of ₹2. Designing a branch for that is a distinction with no
  data behind it.

## The rules this must not break

1. **Nothing is written to Amazon without a per-row tick in a preview.** Unchanged.
2. **The ledger is written BEFORE the wire.** Unchanged — a crash mid-run must leave a knowable,
   reversible state.
3. **A row Amazon does not mention in a 207 is FAILED, never assumed applied.** Unchanged.
4. **No scheduled job can reach a write path.** Unchanged, and the existing test asserting the
   nightly job cannot call `apply_bids` must be extended to the renamed function.
5. **`ARCHIVED` is not writable from this app.**
6. `logic.py` stays pure (no DB, no network); `repository.py` is the only SQL.

---

## 1. The action

`app/ads/logic.py`:

```python
ACTION_SET_STATE = "set_state"
STATE_PAUSED = "PAUSED"
STATE_ENABLED = "ENABLED"

#: The ONLY states this app will write. `ARCHIVED` is deliberately absent — see the note below.
WRITABLE_STATES = (STATE_PAUSED, STATE_ENABLED)
```

`ACTIONS` gains `ACTION_SET_STATE`. For a state action the rule's `amount` carries the state STRING
rather than a number.

### `ARCHIVED` is absent, and the omission is the safety mechanism

Amazon's documentation is explicit that archiving is terminal — *"permanent and can't be undone"* —
and on Sponsored Brands an archived negative keyword can never be recreated for that campaign. The
entire safety model of `ads_mutation` is an `old_* → new_*` pair with a reversible undo chain. An
irreversible action has no undo, so it cannot honestly be offered through a bulk rule that can move
hundreds of rows in one click. Archiving stays a manual job in Seller Central. The constant records
this so a future reader does not "complete" the enum.

### Changes inside `plan_run`

`plan_run` already refuses automated campaigns, applies the once-per-day guard, and computes
`approved_ids`. Three points of contact:

- **`SKIP_NO_CHANGE` gets NO state twin in `plan_run`, and that is deliberate.** A row already in the
  target state must not be sent — it is a no-op write that would appear in the ledger as a change —
  but `plan_run` **cannot** detect it: the report carries no state column (measured, none of
  `spTargeting`'s 15 columns has one), so at preview time the live state is genuinely unknown. The
  precondition is therefore enforced at apply, where the state is read anyway, and reported as
  `unchanged` (§2).

  Stated explicitly because the alternative is worse than either option: a `SKIP_ALREADY_IN_STATE`
  constant added to `plan_run` for symmetry would be **dead code that can never fire**, and a reader
  would later "fix" it by inventing a state source that does not exist. `SKIP_NO_CHANGE` itself has
  already had to MOVE once, when the true-bid substitution landed, so where a check sits relative to
  the data it compares is load-bearing in this function.
- **The once-per-day guard needs its OWN basis for state, and cannot reuse the bid one.** Checked
  against the code rather than assumed: `last_applied_bids` filters `new_bid IS NOT NULL`, so it is
  structurally blind to a state row and can never report "this was paused today".

  A sibling `last_applied_states` is therefore required, filtering `new_state IS NOT NULL`. Without it
  the guard silently does nothing for the new action — the run would *look* guarded because the
  guarded-row machinery is on screen, while every row arrives ticked. The failure is invisible, which
  is why this is called out rather than left to the implementer.

  Its job differs from the bid guard's. A repeated bid change COMPOUNDS (`15.25 × 1.10 = 16.78`, so
  −10% twice is −19%); a repeated pause is idempotent. What the state guard prevents is a
  **pause/enable flip-flop within one day** — a keyword turned off by one rule and back on by the
  next, ending wherever the last run happened to land.
- **M19 / Amazon-managed campaigns stay excluded.** Measured: 0 of the 88 rows are in them, so this
  costs nothing today — and the reason is stronger for a pause than for a bid, since their optimiser
  would simply re-enable it and neither result is what anyone chose.

### Guardrails

`min_pause_spend` (default `100`) joins `DEFAULT_GUARDRAILS` and `GUARDRAIL_RANGES`, **range-checked
on READ as well as write** — the `good_rating: 99` lesson, where an unvalidated stored threshold
silently broke every verdict on the account with the rule panel dutifully explaining the impossible
value.

`max_rows` applies unchanged, measured on `approved_ids` (the rows that can actually be sent), which
is where it already lives.

`max_bid`, `min_bid` and `max_change_pct` are reported as **not applicable** to a state action rather
than silently skipped, so the preview can state which guards are live. A guard that is quietly absent
reads identically to a guard that passed.

**Why a spend floor is the right analogue.** For a bid rule the dangerous typo is `10%` written as
`100%`, which `max_change_pct` catches. For a pause there is no percentage — the dangerous typo is
`spend>10` where `spend>1000` was meant, which would pause a wide swathe of cheap but healthy
keywords, and `max_rows` alone does not catch it because 900 cheap rows fit under a 1,000-row ceiling.

## 2. The live re-read at apply, which is state-aware

`POST /ads/apply` runs four checks against Amazon's live response. Three of them encode bid
assumptions and must not apply to a state action.

| Current check | For a **pause** | For an **enable** |
|---|---|---|
| row absent entirely → `moved` | unchanged | unchanged |
| `live_state != ENABLED` → `inactive` | this IS the "already paused" case | **must be inverted** — an enable targets rows that are not enabled |
| `live_bid is None` → `moved` | must not apply | must not apply |
| `live_bid != old_bid` → `moved` | must not apply | must not apply |

The last row is the trap worth naming. The bid-drift check exists because applying a stale
PERCENTAGE to a bid someone has since changed produces a number nobody chose. A pause has no
arithmetic and therefore no staleness — refusing to pause a money-losing keyword because a colleague
nudged its bid would leave it running for the least relevant possible reason.

So for `ACTION_SET_STATE` the live STATE is the precondition and the live bid is not consulted:

```python
if action == ACTION_SET_STATE:
    if live_state == target_state:
        unchanged.append({**change, "live_state": live_state,
                          "reason": f"it is already {live_state.lower()} at Amazon."})
    else:
        to_send.append(change)
else:
    ...the existing four checks, untouched...
```

`unchanged` is a **third reported bucket** beside `moved` and `inactive`, following this codebase's
standing rule — excluded and named, never silently absent. "12 were already paused" is information;
a count that is quietly smaller than the table reads as the rule not working.

`fetch_current_bids` already returns state beside bid and is already grouped BY WRITER (it had to be
regrouped once, because SB rows were being looked up on the wrong API and a missing row is treated as
"moved"). No new requests: the state is exactly as fresh as the bid beside it.

## 3. The write

`app/ads/spapi_ads.py`: `apply_bids` becomes `apply_changes(client, rows, *, writer, field="bid")`,
where `field` selects the payload key. Everything that is hard to get right is untouched and shared:

- the four endpoints and their media types
- SB's extra `adGroupId` (and `campaignId` for a keyword state write — SB requires it where SP does
  not, and where a bid write did not)
- **SB states are lowercase** (`"paused"`, not `"PAUSED"`) while SP takes upper case. One
  normalisation point per writer, since sending the wrong case is a per-row rejection inside a 207
  that otherwise reads as success.
- the 500-row batching, and SB's cap of 100
- request-array order as the only link back to a row
- the three different 207 shapes, one parser each
- a row Amazon does not mention → FAILED

**No alias is kept.** Enumerated: `apply_bids` has exactly two production callers, both in
`app/routers/ads.py` — the apply path (line ~791) **and the undo path** (line ~850). Both must be
updated, and the undo one is the easier to miss; leaving it on a stale alias would mean undo and apply
diverging, which is the one place divergence corrupts the ledger. Also update `__all__` and the module
docstring, which name `apply_bids` explicitly.

> **The rename would silently retire a safety test.** `tests/test_retention_and_scheduler.py:337`
> asserts the nightly job cannot reach a write path by grepping source for the LITERAL strings
> `("apply_bids", "plan_run", "open_run", "/apply")`. After the rename that string no longer appears
> anywhere, so the assertion passes **vacuously** — a green test proving nothing, on the guard that
> stops a scheduled job moving live bids.
>
> This is the same defect `CLAUDE.md` already records for the deploy detector, where grepping for a
> revision id passed with the branch deleted because the id also appeared in a comment. The literal
> must be updated to `apply_changes` **and** the test must assert the searched name actually exists in
> the module, so a future rename fails loudly instead of quietly.

## 4. The ledger and undo

`AdsMutation` gains three columns (migration, `down_revision = "d3479a8ed8ad"`):

| column | why |
|---|---|
| `action` | `"bid"` or `"state"`. Without it a row is ambiguous: undo cannot tell what to reverse and the bid log cannot label it. |
| `old_state` · `new_state` | the pair, mirroring `old_bid`/`new_bid` |

### `build_undo` must branch on `action`

It currently skips any row where `old_bid is None`, with a comment saying this "should be impossible".
That was true when written. On a state row a null bid is NORMAL, so left as a blanket check, **undoing
a pause run would reverse nothing while reporting success.**

This is the same shape of defect as `delete_draft_plans`, whose docstring asserted an invariant that a
later feature invalidated and which destroyed 400 units of packed stock on production. A comment
stating an invariant is not the same as enforcing one, and the invariant here is being invalidated by
this very change.

The three undo rules hold unchanged: only `applied` rows reverse; `pending` are excluded and reported;
a partly-failed undo leaves the remainder `applied` so a second attempt can reach it.

### Two consequences elsewhere

- **`last_applied_bids` already ignores state rows — verified, not assumed.** It filters
  `new_bid IS NOT NULL`, so a state row (null `new_bid`) is excluded with no code change. **This needs
  a test and not an edit**, because the protection is incidental: it holds because of a filter written
  for a different reason, and a future refactor that widened it would silently make a null read as the
  current bid, compounding the next bid change. The test states the requirement so the filter cannot
  be removed as redundant.
- **`last_applied_states` is its own function** (§1), for the mirror-image reason: the bid filter that
  protects the above is exactly what makes `last_applied_bids` unusable as a state guard.
- **The bid log renders `PAUSED → ENABLED`** where a bid row shows `13.86 → 15.25`. It already uses
  `build_portfolio_xlsx` rather than `build_simple_xlsx` precisely because these columns hold words
  rather than summable numbers, so no export change is needed beyond the two new columns.

Retention stays 12 months. This is the audit trail for the only feature that spends money, it is not
refetchable from Amazon, and a pause is now part of that story.

## 5. The screen

`templates/ads.html`. The action dropdown gains **Pause** and **Enable**.

- **`amount: Number($("amount").value)` must not run for a state action.** `Number("PAUSED")` is
  `NaN`, which `JSON.stringify` serialises to `null` — so the server would receive no state at all.
  The numeric input is replaced by a state selector when the action is `set_state`, and the payload
  sends the string. The existing `change` handler that swaps the `%`/`₹` unit label is the same seam,
  and both call sites (`$("action").value` appears at the save-rule and preview paths) must be
  covered.
- **The bid columns become a state column** for a state run: `Bid → New bid` is meaningless, the row
  shows `ENABLED → PAUSED`. The **Suggested bid column is hidden**, because Amazon's suggestion is bid
  context and leaving it visible implies a bid is being changed.
- **The apply bar states what pausing does**: *"Pause 88 keywords and targets — they will stop
  serving"*, with a note that a pause is reversible from the run history. A bid nudge and stopping
  delivery are not the same promise and must not share one confirmation sentence.

Everything else is reused deliberately: campaign → ad-group grouping with rolled-up totals, tri-state
group checkboxes, per-row ticks, the guardrail panel, the once-per-day untick — **including the
corrected `Select all`**, which once selected `plan.changes` wholesale and undid the once-per-day
guard for every row. The pause path inherits the corrected `approved_ids` basis rather than becoming a
fourth copy of that logic.

## 6. Why negatives are out of scope

Researched against Amazon's current OpenAPI specs, not assumed, because the owner asked for "negative
targeting" by name and this spec declines to build it. Recorded so the decision is reviewable:

- A **pause** stops one row. A **negative keyword** blocks a search TERM across an ad group or whole
  campaign. They are complements, not alternatives: pausing exact-match `sattu` does not stop the same
  query arriving through a broad match or an auto campaign, so the spend can MOVE rather than stop.
  With 34 product targets and 6 auto targets among the 88 rows, that leak is real on this account.
- **40 of the 88 rows are targets with no search term to negate.** A negative keyword cannot express
  them at all, so it would cover under half of the very rule that motivated it.
- **SP allows `NEGATIVE_BROAD`; SB allows only `negativeExact`/`negativePhrase`, lower-cased. SB has no
  campaign-level negatives at all.** A shared constant would be wrong for one product.
- **A negative silently overrides an ENABLED positive keyword.** Nothing fails and nothing warns: the
  keyword keeps a live bid on screen and simply stops delivering. That is a worse screen-versus-reality
  gap than the ones this codebase has already had to fix.
- **SB negative targets return `200`, not `207`**, with errors inside a success code — a fourth and
  fifth response shape beyond the three already parsed.
- **Creating a duplicate negative errors rather than dedupes**, keyed on
  `(campaign, ad group, text, matchType)`, so `DUPLICATE_VALUE` is a benign no-op that must not be
  recorded as a fault.

Pause is reversible and ships now. Negatives are a larger, less reversible feature that is better
designed after the owner has used pause and can see where the spend actually moved to.

## 7. Files

Changed: `app/ads/logic.py` · `app/ads/spapi_ads.py` · `app/ads/repository.py` ·
`app/routers/ads.py` · `app/models.py` · `templates/ads.html` · `CLAUDE.md` ·
`deploy/update-ec2.sh` (baseline detector branch) · `tests/test_ads_*.py`

New: `alembic/versions/<rev>_ads_mutation_state.py` · `tests/test_ads_pause.py` ·
`scripts/mutate_ads_state.py`

**The deploy detector needs a new branch, newest-first, keyed on `ads_mutation.old_state`.** A stale
detector has stamped production BACKWARDS once and cost two failed deploys, and
`tests/test_schema_migrations.py` runs the detector against a freshly-migrated database and asserts it
answers with the real head.

## 8. Verification

**The bar is mutation testing, not a green suite.** `CLAUDE.md` records five separate cases of a bug
shipping past a fully green suite in this feature area alone.

### Runtime tests
- A pause plans and applies through all four writers, with the correct payload shape and case per
  writer.
- An already-`PAUSED` row is reported `unchanged` and NOT sent.
- An enable inverts the state precondition and acts on non-enabled rows.
- Undo of a pause is an enable, and reverses every applied row.
- A 207 partial failure attributes the error to the right row by request index.
- `min_pause_spend` blocks a rule below the floor; the message names the floor.
- A state value other than `PAUSED`/`ENABLED` — specifically `ARCHIVED` — is refused.
- `/ads/apply` re-checks guardrails against a hand-built request, before any Amazon call.
- `last_applied_bids` ignores state rows (pinning an incidental protection — §4).
- `last_applied_states` reports a row paused today, and the guard unticks it.
- The scheduled job cannot reach `apply_changes`.

### Source-level assertions
For properties no runtime test can observe:
- `WRITABLE_STATES` contains no `ARCHIVED`.
- The payload builder is not duplicated per writer.

### Mutations (`scripts/mutate_ads_state.py`) — one per decision
| Mutation | Caught by |
|---|---|
| `ARCHIVED` added to `WRITABLE_STATES` | the refusal test |
| the bid-drift check still applies to a state action | pause-with-moved-bid test |
| the `live_state != ENABLED` filter not inverted for an enable | the enable test |
| `build_undo`'s null-`old_bid` skip left as a blanket check | undo-of-a-pause test |
| `last_applied_bids`' `new_bid IS NOT NULL` filter removed | true-current-bid test |
| `last_applied_states` filters `new_bid` instead of `new_state` (so it always returns nothing) | flip-flop test |
| the state guard reuses `last_applied_bids` | flip-flop test |
| `Number()` still applied to the state value | payload test |
| SB state sent upper-case | SB writer payload test |
| the scheduler test's grep literal left as `apply_bids` after the rename | a test asserting the searched name exists in the module |

A `SKIP` (target text not found) is reported as a SURVIVOR, so "all caught" can never print while a
mutation silently never ran.

### Against production data
The rule must find the same **88 rows / ₹1,40,751** as the measurement above, and `POST /ads/preview`
must send nothing to Amazon.

### Manual, on production after deploy
Preview `spend>1000, roas<1` on 30d → 88 rows grouped by campaign → tick a **single** row and apply →
confirm one row `applied`, that keyword reads `PAUSED` in Seller Central → undo the run → it reads
`ENABLED` again. One row first, because the first live exercise of a new write path should move one
thing, not 88.

## 9. Out of scope

Negative keywords and negative product targets (§6). Archiving anything. Budget edits, keyword
creation, campaign or ad-group state changes. Any scheduled or automatic pausing — nothing in this app
writes to Amazon unattended, and a rule that could pause 88 keywords on a bad data night is exactly
what that rule prevents.
