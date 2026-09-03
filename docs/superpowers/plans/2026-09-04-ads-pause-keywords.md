# Pausing keywords and targets from a rule — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `set_state` as a fifth Ads rule action so a rule like `spend>1000, roas<1` can PAUSE (or re-ENABLE) the matching keywords and targets, reversibly, through the existing preview → tick → apply → ledger → undo path.

**Architecture:** Approach A from the spec — extend the one write path rather than build a second. `apply_bids` becomes `apply_changes(..., field="bid"|"state")`, so the four endpoints, the 500-row batching, the request-order-to-row mapping and the three different 207 parsers each keep exactly one copy. `plan_run` gains a state branch; `/ads/apply`'s live re-read becomes state-aware; `ads_mutation` gains `action`, `old_state`, `new_state`.

**Tech Stack:** FastAPI · async SQLAlchemy · SQLite + Alembic · httpx · vanilla JS · pytest

**Spec:** `docs/superpowers/specs/2026-09-03-ads-pause-keywords-design.md` (commit `aee24b0`)

## Global Constraints

- **`ARCHIVED` is never writable.** `WRITABLE_STATES = ("PAUSED", "ENABLED")` only. Archiving is terminal at Amazon and has no undo, so it cannot go through a bulk rule.
- **Sponsored Brands states are lower-case** (`"paused"`), Sponsored Products upper-case (`"PAUSED"`). Wrong case is a per-row rejection inside a 207 that otherwise reads as success.
- **SB payloads need `adGroupId`; an SB *keyword state* write also needs `campaignId`.** SP needs neither.
- **The ledger is written BEFORE the wire.** Never reorder `open_run` after the request.
- **A row Amazon does not mention in a 207 is `failed`, never assumed applied.**
- **`logic.py` stays pure** — no DB, no network. `repository.py` is the only SQL.
- **Nothing scheduled may reach a write path.**
- Run tests with `venv/Scripts/python -m pytest`. Add `-p no:randomly` when asserting on a single test.
- Guardrail values are range-checked **on read as well as write**.
- Every new migration adds a branch to `deploy/update-ec2.sh`'s baseline detector, **newest first**.

## Measured facts this plan relies on

| Fact | Value |
|---|---|
| `spend>1000 AND roas<1` on 30d | **88 rows, ₹1,40,751** (6.8% of spend) |
| of those, rows with 0 clicks | **0** — all have >10 clicks |
| composition | SP kw 31 · SP target 23 · SB kw 17 · SB target 17 |
| ad groups fully silenced by the rule | **0** |
| null-bid rows in 37,943 rows over 60d | 8, of which 1 has spend (₹2) |
| `apply_bids` production callers | **2** — `routers/ads.py` apply *and* undo |
| SB rows currently mislabelled `ad_product="sp"` in the ledger | **304** (Task 3 fixes) |

---

### Task 1: The action constants and `min_pause_spend`

Pure additions to `logic.py`. No behaviour changes yet — this task only makes the vocabulary exist so later tasks can reference it.

**Files:**
- Modify: `app/ads/logic.py` (near `ACTION_*` ~line 483; `DEFAULT_GUARDRAILS` ~line 239; `GUARDRAIL_RANGES` ~line 268)
- Test: `tests/test_ads_pause.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `ACTION_SET_STATE = "set_state"`, `STATE_PAUSED = "PAUSED"`, `STATE_ENABLED = "ENABLED"`, `WRITABLE_STATES = ("PAUSED", "ENABLED")`, `is_state_action(action) -> bool`, `state_error(value) -> str | None`; guardrail key `"min_pause_spend"` default `100.0`, range `(1.0, 100000.0)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ads_pause.py`:

```python
"""Pausing and re-enabling keywords and targets from a rule.

The action that turns a keyword OFF. Every test here guards a decision recorded in
docs/superpowers/specs/2026-09-03-ads-pause-keywords-design.md.
"""
import pytest

from app.ads import logic


def test_archived_is_not_a_writable_state():
    """**The whole safety argument for this feature.**

    Amazon documents archiving as terminal — "permanent and can't be undone" — and on Sponsored
    Brands an archived negative can never be recreated. The ledger's safety model is an
    `old -> new` pair with a reversible undo chain, so an irreversible action has no undo and
    cannot honestly be offered through a rule that moves hundreds of rows in one click.

    Asserted on the CONSTANT rather than only on `state_error`, so a future reader who
    "completes" the enum for symmetry fails here with the reason in front of them.
    """
    assert logic.WRITABLE_STATES == (logic.STATE_PAUSED, logic.STATE_ENABLED)
    assert "ARCHIVED" not in logic.WRITABLE_STATES
    assert logic.state_error("ARCHIVED") is not None
    assert "permanent" in logic.state_error("ARCHIVED").lower()


def test_the_state_action_is_recognised_and_distinguishable_from_a_bid_action():
    """`is_state_action` is the branch every later task keys on, so it is pinned here."""
    assert logic.ACTION_SET_STATE in logic.ACTIONS
    assert logic.is_state_action(logic.ACTION_SET_STATE) is True
    for bid_action in (logic.ACTION_INCREASE_PCT, logic.ACTION_DECREASE_PCT,
                       logic.ACTION_INCREASE_ABS, logic.ACTION_DECREASE_ABS, logic.ACTION_SET):
        assert logic.is_state_action(bid_action) is False


@pytest.mark.parametrize("value", ["PAUSED", "ENABLED", "paused", " enabled "])
def test_a_legal_state_is_accepted_in_any_case_and_with_whitespace(value):
    """The screen sends a string from a <select>; a stray space must not read as an unknown state."""
    assert logic.state_error(value) is None


@pytest.mark.parametrize("value", ["", None, "PAUSE", "off", 0, "ARCHIVED", "ENABLING"])
def test_an_illegal_state_is_refused_with_a_reason(value):
    """Refusals carry prose, like `guardrail_error`, so the screen can say what is allowed."""
    problem = logic.state_error(value)
    assert problem, f"{value!r} should be refused"
    assert isinstance(problem, str)


def test_min_pause_spend_is_a_range_checked_guardrail():
    """The pause equivalent of `max_change_pct`.

    For a bid rule the dangerous typo is 10% written as 100%. For a pause there is no percentage —
    the dangerous typo is `spend>10` where `spend>1000` was meant, which `max_rows` cannot catch
    because 900 cheap rows fit under a 1,000-row ceiling.

    Range-checked on READ as well as write is the `good_rating: 99` lesson: a stored value nothing
    could ever reach silently zeroed a whole verdict on the Portfolio tab.
    """
    assert logic.DEFAULT_GUARDRAILS["min_pause_spend"] == 100.0
    assert "min_pause_spend" in logic.GUARDRAIL_RANGES
    assert logic.guardrail_error("min_pause_spend", 100) is None
    assert logic.guardrail_error("min_pause_spend", 0) is not None
    assert logic.guardrail_error("min_pause_spend", -5) is not None
    # Read path: an absurd stored value falls back to the default rather than being honoured.
    assert logic.guardrails_or_default(
        {"min_pause_spend": 0}
    )["min_pause_spend"] == 100.0
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `venv/Scripts/python -m pytest tests/test_ads_pause.py -q -p no:randomly`
Expected: FAIL — `AttributeError: module 'app.ads.logic' has no attribute 'WRITABLE_STATES'`

- [ ] **Step 3: Add the guardrail**

In `app/ads/logic.py`, inside `DEFAULT_GUARDRAILS`, after the `"max_rows": 1000,` entry:

```python
    #: A pause rule may not act on a row that spent less than this in the window.
    #:
    #: **The pause equivalent of `max_change_pct`, and it guards a different mistake.** A bid rule's
    #: dangerous typo is "10" written as "100", which the percentage ceiling catches. A pause has no
    #: percentage: its dangerous typo is `spend > 10` where `spend > 1000` was meant, which would
    #: turn off a wide swathe of cheap but perfectly healthy keywords. `max_rows` cannot catch that —
    #: 900 cheap rows fit comfortably under a 1,000-row ceiling.
    #:
    #: Rs 100 rather than higher because the owner's own rule uses `spend > 1000`; this is a floor
    #: under a mistyped rule, not a second opinion about the rule itself.
    "min_pause_spend": 100.0,
```

In `GUARDRAIL_RANGES`, after `"max_rows": (1, 20000),`:

```python
    # Upper bound is generous: the point is to refuse 0 (which disables the guard) and negatives,
    # not to have an opinion about how selective a pause rule should be.
    "min_pause_spend": (1.0, 100000.0),
```

- [ ] **Step 4: Add the action constants**

In `app/ads/logic.py`, replace the block at ~line 483:

```python
ACTION_INCREASE_PCT = "increase_pct"
ACTION_DECREASE_PCT = "decrease_pct"
ACTION_INCREASE_ABS = "increase_abs"
ACTION_DECREASE_ABS = "decrease_abs"
ACTION_SET = "set"

ACTIONS = (ACTION_INCREASE_PCT, ACTION_DECREASE_PCT,
           ACTION_INCREASE_ABS, ACTION_DECREASE_ABS, ACTION_SET)
```

with:

```python
ACTION_INCREASE_PCT = "increase_pct"
ACTION_DECREASE_PCT = "decrease_pct"
ACTION_INCREASE_ABS = "increase_abs"
ACTION_DECREASE_ABS = "decrease_abs"
ACTION_SET = "set"
#: Turn a keyword or target OFF (or back on). `amount` carries a STATE STRING, not a number.
ACTION_SET_STATE = "set_state"

BID_ACTIONS = (ACTION_INCREASE_PCT, ACTION_DECREASE_PCT,
               ACTION_INCREASE_ABS, ACTION_DECREASE_ABS, ACTION_SET)

ACTIONS = BID_ACTIONS + (ACTION_SET_STATE,)

STATE_PAUSED = "PAUSED"
STATE_ENABLED = "ENABLED"

#: **The ONLY states this app will ever write, and the omission of `ARCHIVED` is the safety
#: mechanism rather than an oversight.**
#:
#: Amazon documents archiving as terminal — "permanent and can't be undone" — and on Sponsored
#: Brands an archived negative keyword can never be recreated for that campaign. The entire safety
#: model of `ads_mutation` is an `old_* -> new_*` pair with a reversible undo chain, so an
#: irreversible action has no undo and must not be reachable from a rule that can move several
#: hundred rows in one click. Archiving stays a manual job in Seller Central.
#:
#: `ENABLING`, `PROPOSED` and the rest of Amazon's read-side enum are absent for a duller reason:
#: they are states Amazon reports, not states a caller sets.
WRITABLE_STATES = (STATE_PAUSED, STATE_ENABLED)


def is_state_action(action: str) -> bool:
    """Does this action change an entity's state rather than its bid?

    One named predicate rather than `action == ACTION_SET_STATE` repeated at each branch: the
    check appears in `plan_run`, in `/ads/apply`'s live re-read, in the writer and in the ledger,
    and a missed site is a silent wrong-field write rather than an error.
    """
    return action == ACTION_SET_STATE


def state_error(value) -> str | None:
    """The REASON a state value is refused, or None if it is acceptable.

    Prose rather than False, matching `guardrail_error`, so a refusal can name what is allowed.
    Accepts any case and surrounding whitespace because the value arrives from a `<select>`.
    """
    if not isinstance(value, str) or not value.strip():
        return (f"A pause rule needs a state to set. Valid states: "
                f"{', '.join(WRITABLE_STATES)}.")
    wanted = value.strip().upper()
    if wanted == "ARCHIVED":
        return ("Archiving is permanent at Amazon and cannot be undone, so this app will not "
                "archive from a rule. Pause the row instead, or archive it in Seller Central.")
    if wanted not in WRITABLE_STATES:
        return f"{value!r} is not a state this app can set. Valid states: {', '.join(WRITABLE_STATES)}."
    return None


def normalise_state(value, ad_product: str = AD_PRODUCT_SP) -> str:
    """The state string as the given ad product's API expects it.

    **Sponsored Brands takes lower case and Sponsored Products upper case.** Sending the wrong case
    is refused per-row inside a 207 whose HTTP status says success — the same class of silent
    failure as SB's missing `adGroupId`. One function, so the rule is stated once.
    """
    wanted = str(value).strip().upper()
    return wanted.lower() if ad_product == AD_PRODUCT_SB else wanted
```

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `venv/Scripts/python -m pytest tests/test_ads_pause.py -q -p no:randomly`
Expected: PASS (6 tests — the two parametrised ones expand)

- [ ] **Step 6: Check nothing else broke**

Run: `venv/Scripts/python -m pytest tests/test_ads_logic.py tests/test_ads_writes.py tests/test_ads_sb.py -q`
Expected: PASS. `ACTIONS` grew, so any test asserting its exact contents will fail here — if one does, update it to assert that the five bid actions are present rather than that the tuple has five members, and note in the test why.

- [ ] **Step 7: Commit**

```bash
git add app/ads/logic.py tests/test_ads_pause.py
git commit -m "feat(ads): the set_state action vocabulary and a min_pause_spend guardrail

ARCHIVED is deliberately absent from WRITABLE_STATES: it is terminal at Amazon
and has no undo, so it cannot be reachable from a bulk rule. normalise_state
holds the SB-lower/SP-upper rule in one place."
```

---

### Task 2: `plan_run` plans a state change

**Files:**
- Modify: `app/ads/logic.py` — `SKIP_*` block (~line 532), `plan_run` (~line 554)
- Test: `tests/test_ads_pause.py`

**Interfaces:**
- Consumes: `ACTION_SET_STATE`, `is_state_action`, `state_error`, `WRITABLE_STATES`, `DEFAULT_GUARDRAILS["min_pause_spend"]` (Task 1).
- Produces: `plan_run(..., action=ACTION_SET_STATE, amount="PAUSED")` returns changes carrying `new_state` and `old_state=None`, plus `SKIP_BELOW_PAUSE_SPEND`. `totals` gains `"pausing"`. `changes` rows for a state run carry **no** `new_bid` key.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ads_pause.py`:

```python
def _row(entity_id="1", *, spend=2000.0, roas=0.5, bid=12.0,
         writer=logic.WRITER_KEYWORD, ad_product="sp", campaign="MF_SP_keywords"):
    """One report row, already in `metrics_for` shape so `plan_run` uses it as given."""
    return {
        "entity_id": entity_id, "writer": writer, "ad_product": ad_product,
        "match_type": "PHRASE", "text": f"kw {entity_id}",
        # `manager` explicitly, not left to `manager_of(campaign_name)` to derive. The fallback
        # would classify this fixture as `us` anyway, but only because of the campaign name chosen
        # here — a test that passes by luck stops passing when someone renames the fixture.
        "manager": logic.MANAGER_US,
        "campaign_id": "c1", "campaign_name": campaign,
        "ad_group_id": "g1", "ad_group_name": "ag",
        "bid": bid, "spend": spend, "sales": spend * roas, "roas": roas,
        "clicks": 100, "impressions": 5000, "orders": 0, "acos": 2.0,
    }


def _pause(rows, *, amount="PAUSED", conditions=None, guardrails=None, applied_today=None):
    return logic.plan_run(
        rows,
        conditions=conditions if conditions is not None else [
            {"field": "spend", "op": ">", "value": 1000},
        ],
        action=logic.ACTION_SET_STATE,
        amount=amount,
        guardrails=guardrails,
        applied_today=applied_today,
    )


def test_a_pause_plan_carries_a_state_and_no_new_bid():
    """A state row must not carry `new_bid`.

    Five sites downstream read the bid off a change. A state row that carried one — even a copy of
    the current bid — would let a pause be recorded in the ledger as a bid change, and then
    `last_applied_bids` would serve it as the true current bid.
    """
    plan = _pause([_row("1")])
    assert plan["blocked"] is None
    assert len(plan["changes"]) == 1
    change = plan["changes"][0]
    assert change["new_state"] == "PAUSED"
    # The live state is unknowable from the report — see the docstring on the skip below.
    assert change["old_state"] is None
    assert "new_bid" not in change
    assert plan["totals"]["pausing"] == 1


def test_the_report_cannot_supply_the_live_state_so_no_row_is_skipped_for_being_paused():
    """**There is deliberately no `SKIP_ALREADY_PAUSED` here, and this test is why.**

    `spTargeting` has no state column — none of its 15 columns carries one — so at preview time
    `plan_run` genuinely cannot know whether a row is already paused. The precondition is enforced
    at apply, where the live state is read anyway, and reported as `unchanged`.

    A skip constant added here for symmetry would be dead code that can never fire, and a later
    reader would "fix" it by inventing a state source that does not exist.
    """
    assert not any(name.startswith("SKIP_ALREADY") for name in dir(logic))
    plan = _pause([_row("1")])
    assert len(plan["changes"]) == 1


def test_a_row_below_the_pause_spend_floor_is_skipped_and_named():
    """The guard against `spend>10` typed for `spend>1000`.

    Skipped and NAMED, never silently absent — a row missing from a preview is indistinguishable
    from a bug.
    """
    plan = _pause([_row("1", spend=50.0)], conditions=[{"field": "spend", "op": ">", "value": 10}])
    assert plan["changes"] == []
    assert len(plan["skipped"]) == 1
    assert plan["skipped"][0]["reason"] == logic.SKIP_BELOW_PAUSE_SPEND
    assert plan["totals"]["below_pause_spend"] == 1


def test_a_pause_run_ignores_the_bid_guardrails_because_they_cannot_apply():
    """`max_bid`, `min_bid` and `max_change_pct` are about arithmetic a pause does not do.

    A row whose bid sits above the ceiling is still perfectly pausable — indeed it is likelier to
    need it. Applying the bid ceiling here would refuse to turn off the most expensive keywords in
    the account.
    """
    plan = _pause([_row("1", bid=900.0)], guardrails={"max_bid": 60.0, "min_bid": 1.0})
    assert plan["blocked"] is None
    assert len(plan["changes"]) == 1


def test_an_illegal_state_blocks_the_whole_run_rather_than_skipping_rows():
    """The rule itself is wrong, so this is a refusal and not a per-row exclusion.

    Same distinction the function already draws for a guardrail breach: `blocked` means the rule is
    wrong, `skipped` means a row is unsuitable. A preview of 88 rows the owner might approve must
    not be produced from an unusable rule.
    """
    for bad in ("ARCHIVED", "", "off"):
        plan = _pause([_row("1")], amount=bad)
        assert plan["changes"] == []
        assert plan["blocked"], f"{bad!r} should block the run"


def test_the_row_ceiling_still_applies_to_a_pause():
    """`max_rows` is the one bid guardrail that DOES transfer: it bounds surprise, not arithmetic."""
    rows = [_row(str(i)) for i in range(12)]
    plan = _pause(rows, guardrails={"max_rows": 5})
    assert plan["blocked"] is not None
    assert "12" in plan["blocked"]


def test_a_row_paused_today_arrives_unticked_rather_than_dropped():
    """The once-per-day guard, on its own basis.

    Its job differs from the bid guard's: a repeated bid change COMPOUNDS, a repeated pause is
    idempotent. What this prevents is a pause/enable flip-flop inside one day, ending wherever the
    last run happened to land.
    """
    today = logic.ist_day(__import__("datetime").datetime.utcnow())
    plan = _pause(
        [_row("1"), _row("2")],
        applied_today={"1": {"state": "PAUSED", "day": today, "at": "now", "rule": "earlier rule"}},
    )
    assert len(plan["changes"]) == 2, "still visible with its reason"
    assert plan["approved_ids"] == ["2"], "but not ticked"
    assert plan["totals"]["changed_today"] == 1
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `venv/Scripts/python -m pytest tests/test_ads_pause.py -q -p no:randomly -k "pause_plan or live_state or pause_spend or guardrails_because or illegal_state or row_ceiling or paused_today"`
Expected: FAIL — `AttributeError: ... has no attribute 'SKIP_BELOW_PAUSE_SPEND'`

- [ ] **Step 3: Add the skip reason**

In `app/ads/logic.py`, after `SKIP_CHANGED_TODAY` (~line 551):

```python
#: A pause rule matched a row that barely spent anything.
#:
#: **Not a bid guard — a typo guard.** `spend > 10` where `spend > 1000` was meant matches hundreds
#: of cheap, healthy keywords, and `max_rows` cannot see the difference because they all fit under
#: the ceiling. Named on the preview like every other skip, because a row silently missing from a
#: 1,005-row table is indistinguishable from a bug.
SKIP_BELOW_PAUSE_SPEND = (
    "it spent less than the pause floor, so a rule this broad is probably a typo"
)
```

- [ ] **Step 4: Add the state branch to `plan_run`**

4a. In `plan_run`, immediately after the `if action not in ACTIONS:` block, insert:

```python
    # **A state action's `amount` is a STATE STRING, validated before any row is considered.**
    # Refused here rather than at the writer so the owner sees "this rule is not allowed" instead of
    # a preview they might approve. `ARCHIVED` is refused by name with its reason — see
    # `WRITABLE_STATES`.
    target_state = None
    if is_state_action(action):
        problem = state_error(amount)
        if problem:
            return {"changes": [], "skipped": [], "blocked": problem, "totals": {}}
        target_state = str(amount).strip().upper()
```

4b. Change the percentage-guardrail check so it cannot fire for a state action. Replace:

```python
    if action in (ACTION_INCREASE_PCT, ACTION_DECREASE_PCT):
```

with:

```python
    # A state action has no percentage, so this ceiling has nothing to measure. Scoped explicitly
    # rather than left to `action in (...)` happening to exclude it, because the equivalent bid
    # guards below are scoped by the same reasoning and a reader needs to see it stated.
    if not is_state_action(action) and action in (ACTION_INCREASE_PCT, ACTION_DECREASE_PCT):
```

4c. Inside the row loop, replace the four-line block:

```python
        if not m.get("writer"):
            skipped.append({**m, "reason": SKIP_UNKNOWN_WRITER})
            continue
        if not m.get("bid"):
            skipped.append({**m, "reason": SKIP_NO_BID})
            continue
```

with:

```python
        if not m.get("writer"):
            skipped.append({**m, "reason": SKIP_UNKNOWN_WRITER})
            continue

        # ── A state change: no arithmetic, so none of the bid guards below apply ──
        #
        # Deliberately BEFORE the `SKIP_NO_BID` check. That check exists because bid MATH needs a
        # bid; a pause does not. Measured across 37,943 rows over 60 days, only 8 have a null bid
        # and one of those has spend (of Rs 2) — so this ordering changes almost nothing today and
        # is simply the honest reason.
        #
        # **`old_state` is None on purpose.** The report carries no state column, so the live state
        # is genuinely unknown here; `/ads/apply` reads it and fills it in before the ledger is
        # written. Writing a guess would put a fiction in the audit trail and give undo a value
        # Amazon never held.
        if is_state_action(action):
            if _as_float(m.get("spend")) < limits["min_pause_spend"]:
                skipped.append({**m, "reason": SKIP_BELOW_PAUSE_SPEND})
                continue
            ledger = (applied_today or {}).get(str(m.get("entity_id"))) or {}
            changes.append({
                **m,
                "old_state": None,
                "new_state": target_state,
                "changed_today": bool(ledger) and ledger.get("day") == this_day,
                "changed_at": ledger.get("at") or "",
                "changed_rule": ledger.get("rule") or "",
            })
            continue

        if not m.get("bid"):
            skipped.append({**m, "reason": SKIP_NO_BID})
            continue
```

4d. In the returned `totals` dict, after the `"changed_today"` entry:

```python
            # How many rows this run would turn off (or on). Separate from `changing` so a mixed
            # reading of the preview is impossible: a pause run's `spend` total is money being
            # stopped, not money being re-priced.
            "pausing": sum(1 for c in changes if c.get("new_state")),
            "below_pause_spend": sum(
                1 for s in skipped if s.get("reason") == SKIP_BELOW_PAUSE_SPEND
            ),
```

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `venv/Scripts/python -m pytest tests/test_ads_pause.py -q -p no:randomly`
Expected: PASS

- [ ] **Step 6: Check the bid path is untouched**

Run: `venv/Scripts/python -m pytest tests/test_ads_logic.py tests/test_ads_rules.py -q`
Expected: PASS — the bid branch was not modified, only guarded.

- [ ] **Step 7: Commit**

```bash
git add app/ads/logic.py tests/test_ads_pause.py
git commit -m "feat(ads): plan_run plans a pause, with its own spend floor

State rows carry new_state and no new_bid, so a pause can never be recorded as
a bid change. old_state stays None because the report has no state column — the
apply step reads it. No SKIP_ALREADY_PAUSED: it could never fire here."
```

---

### Task 3: The ledger columns, and the `ad_product` bug beside them

**Files:**
- Modify: `app/models.py` — `AdsMutation` (~line 1245)
- Create: `alembic/versions/<rev>_ads_mutation_state.py`
- Modify: `app/ads/repository.py` — `open_run` (~line 799)
- Modify: `deploy/update-ec2.sh` — detector (~line 369)
- Test: `tests/test_ads_pause.py`, `tests/test_schema_migrations.py`

**Interfaces:**
- Consumes: Task 1 constants.
- Produces: `AdsMutation.action` (`String(8)`, default `"bid"`), `.old_state`, `.new_state` (`String(12)`); `open_run` writes all three plus a correct `ad_product`.

> **A pre-existing bug is fixed here, deliberately in this task.** Measured on production:
> `SELECT ad_product, writer, COUNT(*) FROM ads_mutation GROUP BY 1,2` returns **304 Sponsored
> Brands rows recorded as `ad_product="sp"`** — `open_run` never passes the field, so it silently
> takes its `default="sp"`. It is currently harmless because `writer` carries the routing and
> `split_by_writer` reads that, so undo works. But the column's own docstring says it exists so
> "an undo must go back to the same endpoint with the same payload shape", which is a claim the
> data does not support, and this task adds two more columns immediately beside it. Left alone it
> becomes a wrong audit trail that a future reader trusts.
>
> **Existing rows are not back-filled.** They could be derived from `writer`, but the ledger is an
> audit trail and rewriting historical rows to what they *should* have said is a worse precedent
> than leaving 304 rows provably wrong with a note. New rows are correct from here.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ads_pause.py`:

```python
from sqlalchemy import select

from app.ads import repository as ads_repo
from app.models import AdsMutation


async def test_open_run_records_a_state_change_and_its_action(db):
    """The ledger must be able to express a pause, or undo cannot reverse one."""
    run_id = await ads_repo.open_run(db, [{
        "entity_id": "111", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
        "text": "kw", "campaign_id": "c1", "ad_group_id": "g1",
        "old_state": "ENABLED", "new_state": "PAUSED",
    }], rule_summary="spend>1000, roas<1 -> PAUSED")

    row = (await db.execute(
        select(AdsMutation).where(AdsMutation.run_id == run_id)
    )).scalars().one()
    assert row.action == "state"
    assert row.old_state == "ENABLED"
    assert row.new_state == "PAUSED"
    assert row.old_bid is None and row.new_bid is None
    assert row.status == "pending", "written BEFORE the wire"


async def test_open_run_records_a_bid_change_as_action_bid(db):
    """The default must stay `bid`, so a year of existing rows keep their meaning."""
    run_id = await ads_repo.open_run(db, [{
        "entity_id": "222", "writer": logic.WRITER_TARGET, "ad_product": "sp",
        "old_bid": 12.0, "new_bid": 13.2,
    }], rule_summary="+10%")
    row = (await db.execute(
        select(AdsMutation).where(AdsMutation.run_id == run_id)
    )).scalars().one()
    assert row.action == "bid"
    assert row.new_state is None


async def test_open_run_records_the_ad_product_it_was_given(db):
    """**A pre-existing bug, fixed with this change rather than around it.**

    Measured on production: 304 Sponsored Brands rows were stored as `ad_product="sp"` because
    `open_run` never passed the field and the column default won. Harmless so far — `writer` carries
    the routing — but the column exists so the audit trail can name the API that was written to, and
    it was naming the wrong one.
    """
    run_id = await ads_repo.open_run(db, [{
        "entity_id": "333", "writer": logic.WRITER_SB_KEYWORD, "ad_product": "sb",
        "old_bid": 20.0, "new_bid": 18.0,
    }], rule_summary="-10%")
    row = (await db.execute(
        select(AdsMutation).where(AdsMutation.run_id == run_id)
    )).scalars().one()
    assert row.ad_product == "sb", "an SB row must not be recorded as Sponsored Products"
    assert row.entity_type == "keyword", "sb_keyword is a keyword, not a target"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `venv/Scripts/python -m pytest tests/test_ads_pause.py -q -p no:randomly -k "open_run"`
Expected: FAIL — three failures: no `action` attribute, and `ad_product == "sp"` for the SB row.

- [ ] **Step 3: Add the model columns**

In `app/models.py`, in `AdsMutation`, replace:

```python
    old_bid = Column(Numeric(12, 2))
    new_bid = Column(Numeric(12, 2))
```

with:

```python
    #: `"bid"` or `"state"` — WHICH KIND of change this row is.
    #:
    #: **Without it a row is ambiguous and undo cannot reverse it.** `build_undo` has to know whether
    #: to restore a bid or a state, and a null `new_bid` is not a safe signal: it is also what a row
    #: from a crashed run looks like. Defaults to `"bid"` so the ~2,900 existing rows keep their
    #: meaning with no back-fill.
    action = Column(String(8), nullable=False, default="bid", server_default="bid")
    old_bid = Column(Numeric(12, 2))
    new_bid = Column(Numeric(12, 2))
    #: The state pair, mirroring the bid pair. `old_state` is read LIVE from Amazon at apply time,
    #: never taken from the report — the `spTargeting` report has no state column at all, so a value
    #: here is a measurement rather than a guess, which is what makes undo trustworthy.
    old_state = Column(String(12))
    new_state = Column(String(12))
```

- [ ] **Step 4: Generate and edit the migration**

```bash
venv/Scripts/python -m alembic revision -m "ads_mutation state"
```

Note the generated revision id, then replace the file body (substituting it for `<generated>`):

```python
"""ads_mutation gains action, old_state and new_state.

A pause is a different kind of change from a bid edit, and `build_undo` must be able to tell them
apart — a null `new_bid` cannot serve as the signal, because that is also what a crashed run leaves.

`action` defaults to "bid" with a server_default so existing rows keep their meaning and no
back-fill is needed.
"""
from alembic import op
import sqlalchemy as sa

revision = "<generated>"
down_revision = "d3479a8ed8ad"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ads_mutation") as batch:
        batch.add_column(sa.Column("action", sa.String(length=8), nullable=False,
                                   server_default="bid"))
        batch.add_column(sa.Column("old_state", sa.String(length=12), nullable=True))
        batch.add_column(sa.Column("new_state", sa.String(length=12), nullable=True))


def downgrade():
    with op.batch_alter_table("ads_mutation") as batch:
        batch.drop_column("new_state")
        batch.drop_column("old_state")
        batch.drop_column("action")
```

- [ ] **Step 5: Apply it and confirm the head moved**

```bash
venv/Scripts/python -m alembic upgrade head
venv/Scripts/python -m alembic heads
```
Expected: the new revision id, singular (no branch).

- [ ] **Step 6: Teach `open_run` the new fields**

In `app/ads/repository.py`, in `open_run`, replace the whole `db.add(AdsMutation(...))` call with:

```python
        # **`ad_product` is passed explicitly, and it was not before.** Measured on production: 304
        # Sponsored Brands rows sat in this table labelled `sp`, because the column default won when
        # the field was omitted. Harmless in effect — `writer` is what `split_by_writer` routes on —
        # but the column exists so the audit trail can name the API that was written to.
        db.add(AdsMutation(
            run_id=run_id,
            entity_id=str(change["entity_id"]),
            entity_type=("keyword" if change.get("writer") in (
                logic.WRITER_KEYWORD, logic.WRITER_SB_KEYWORD) else "target"),
            ad_product=change.get("ad_product") or logic.AD_PRODUCT_SP,
            writer=change.get("writer") or logic.WRITER_KEYWORD,
            text=(change.get("text") or "")[:500],
            campaign_id=change.get("campaign_id") or None,
            ad_group_id=change.get("ad_group_id") or None,
            # **Which KIND of change, derived from what the plan actually carries.** Keyed on
            # `new_state` rather than on a passed-in action so a caller cannot label a state change
            # as a bid change; the two column pairs are then mutually exclusive by construction.
            action="state" if change.get("new_state") else "bid",
            # The value BEFORE. Without this the run is not reversible.
            old_bid=change.get("old_bid"),
            new_bid=change.get("new_bid"),
            old_state=change.get("old_state"),
            new_state=change.get("new_state"),
            status="pending",
            rule_summary=(rule_summary or "")[:300],
            reverts_run_id=reverts_run_id,
            created_at=now,
        ))
```

> `entity_type` also gained `WRITER_SB_KEYWORD`. It previously read `"keyword"` only for
> `WRITER_KEYWORD`, so every SB keyword was recorded as a `target` — the same class of mislabel as
> `ad_product`, in the same expression.

- [ ] **Step 7: Add the deploy detector branch**

In `deploy/update-ec2.sh`, replace these two lines (~line 369):

```
elif "user_login_events" in tables:
    print("d3479a8ed8ad")                           # head: login-event log
```

with:

```
elif "new_state" in cols("ads_mutation"):
    print("<generated>")                            # head: pause/enable from a rule
elif "user_login_events" in tables:
    print("d3479a8ed8ad")                           # login-event log
```

> **Newest first, keyed on a column this revision adds.** A stale detector has stamped production
> BACKWARDS once and cost two failed deploys, and `tests/test_schema_migrations.py` *runs* this
> heredoc against a freshly-migrated database — grepping for the id is not enough, because the id
> also appears in comments.

- [ ] **Step 8: Run the tests**

```bash
venv/Scripts/python -m pytest tests/test_ads_pause.py tests/test_schema_migrations.py -q -p no:randomly
```
Expected: PASS. If the schema test reports the detector answering an older revision, the branch is in
the wrong position — it must sit ABOVE `user_login_events`.

- [ ] **Step 9: Commit**

```bash
git add app/models.py app/ads/repository.py alembic/versions deploy/update-ec2.sh tests/test_ads_pause.py
git commit -m "feat(ads): ledger records action, old_state and new_state"
```

---

### Task 4: `apply_changes` — one writer, two payload fields

**Files:**
- Modify: `app/ads/spapi_ads.py` — `apply_bids` (~line 786), `_sb_payload_row` (~line 895), `_sb_target_payload_row` (~line 912), `__all__` (~line 1129), module docstring (line 4)
- Modify: `app/routers/ads.py` — two call sites (~line 791 apply, ~line 850 undo)
- Modify: `tests/test_retention_and_scheduler.py:337`
- Test: `tests/test_ads_pause.py`

**Interfaces:**
- Consumes: `logic.normalise_state`, `logic.AD_PRODUCT_SP` (Task 1).
- Produces: `apply_changes(client, changes, *, writer, sleep=asyncio.sleep) -> list[dict]` — same return shape as `apply_bids` (`[{"entity_id", "ok", "error"}]`). The payload field is chosen per row from `new_state`/`new_bid`. **`apply_bids` no longer exists.**

- [ ] **Step 1: Write the failing test**

First add these helpers to `tests/test_ads_pause.py`, directly after the imports:

```python
async def _fake_token(client):
    """`_access_token` stubbed out — no test may authenticate against LWA."""
    return "token"


@pytest.fixture
def ads_endpoint(monkeypatch):
    """A fake endpoint, so the writer builds a URL without reaching Amazon."""
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "ads_endpoint", "https://ads.test", raising=False)
    return settings


class _Recorder:
    """A fake httpx client that records what was PUT. Mirrors the fakes in test_ads_writes.py."""

    def __init__(self, body=None, status=207):
        self.sent = []
        self._body = body if body is not None else {"keywords": {"success": [], "error": []}}
        self._status = status

    async def put(self, url, json=None, headers=None):
        self.sent.append({"url": url, "json": json, "headers": headers or {}})
        body, status = self._body, self._status

        class _Response:
            status_code = status
            text = ""

            def json(self):
                return body

        return _Response()
```

Then append the tests:

```python
async def test_an_sp_pause_sends_state_and_no_bid(monkeypatch, ads_endpoint):
    """The SP payload must carry `state` INSTEAD of `bid`, not alongside it.

    Amazon's update schemas require only the id and treat everything else as a partial update, so
    sending both would apply a bid change nobody previewed.
    """
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = _Recorder({"keywords": {"success": [{"index": 0, "keywordId": "111"}], "error": []}})

    await spapi_ads.apply_changes(client, [{
        "entity_id": "111", "ad_group_id": "g1", "campaign_id": "c1",
        "ad_product": "sp", "new_state": "PAUSED",
    }], writer=logic.WRITER_KEYWORD)

    row = client.sent[0]["json"]["keywords"][0]
    assert row == {"keywordId": "111", "state": "PAUSED"}
    assert "bid" not in row


async def test_an_sb_pause_is_lower_case_and_carries_its_parent_ids(monkeypatch, ads_endpoint):
    """**Three SB requirements, each of which fails inside a 207 whose status says success.**

    Lower-case state, `adGroupId`, and — for a keyword STATE write specifically — `campaignId`,
    which a bid write does not need.
    """
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = _Recorder([{"code": "SUCCESS", "keywordId": 111}])

    await spapi_ads.apply_changes(client, [{
        "entity_id": "111", "ad_group_id": "222", "campaign_id": "333",
        "ad_product": "sb", "new_state": "PAUSED",
    }], writer=logic.WRITER_SB_KEYWORD)

    row = client.sent[0]["json"][0]
    assert row["state"] == "paused", "SB rejects upper case"
    assert row["adGroupId"] == 222
    assert row["campaignId"] == 333, "required for an SB keyword STATE write"
    assert "bid" not in row


async def test_an_sb_target_pause_is_lower_case_under_its_targets_key(monkeypatch, ads_endpoint):
    """SB targets use a dict under `targets`, not SB keywords' bare list."""
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = _Recorder({"updateTargetSuccessResults": [
        {"targetRequestIndex": 0, "targetId": 444}], "updateTargetErrorResults": []})

    await spapi_ads.apply_changes(client, [{
        "entity_id": "444", "ad_group_id": "222", "campaign_id": "333",
        "ad_product": "sb", "new_state": "PAUSED",
    }], writer=logic.WRITER_SB_TARGET)

    row = client.sent[0]["json"]["targets"][0]
    assert row["state"] == "paused"
    assert row["targetId"] == 444
    assert "bid" not in row


async def test_a_bid_write_is_unchanged_by_the_rename(monkeypatch, ads_endpoint):
    """The regression guard. A bid payload must not gain a state key."""
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = _Recorder()
    await spapi_ads.apply_changes(client, [{
        "entity_id": "111", "ad_product": "sp", "old_bid": 12.0, "new_bid": 13.2,
    }], writer=logic.WRITER_KEYWORD)
    assert client.sent[0]["json"]["keywords"][0] == {"keywordId": "111", "bid": 13.2}


def test_apply_bids_is_gone_and_the_scheduler_guard_names_the_new_function():
    """**The rename would otherwise silently retire a safety assertion.**

    `tests/test_retention_and_scheduler.py` proves the nightly job cannot write to Amazon by
    grepping source for literal names. After a rename the old literal appears nowhere, so that loop
    passes VACUOUSLY — a green test on the guard that stops a scheduled job moving live bids. Same
    trap CLAUDE.md records for the deploy detector, where grepping for a revision id passed with the
    branch deleted because the id also appeared in a comment.
    """
    import pathlib

    assert not hasattr(spapi_ads, "apply_bids"), "no alias: both callers must be updated"
    assert hasattr(spapi_ads, "apply_changes")
    text = pathlib.Path("tests/test_retention_and_scheduler.py").read_text(encoding="utf-8")
    assert "apply_changes" in text, "the scheduler guard must search for the CURRENT name"
```

Add `from app.ads import spapi_ads` to the imports at the top of the file.

- [ ] **Step 2: Run it to make sure it fails**

Run: `venv/Scripts/python -m pytest tests/test_ads_pause.py -q -p no:randomly -k "sp_pause or sb_pause or sb_target_pause or unchanged_by_the_rename or apply_bids_is_gone"`
Expected: FAIL — `module 'app.ads.spapi_ads' has no attribute 'apply_changes'`

- [ ] **Step 3: Rename the function and document why it takes both kinds**

Replace the signature line `async def apply_bids(` with `async def apply_changes(`, and append this
to its docstring immediately before the closing `"""`:

```
    **Two kinds of change go through this one function, deliberately.** A row carrying `new_state` is
    a pause or an enable; a row carrying `new_bid` is a bid edit. Everything that is hard to get
    right — the four endpoints, the 500-row batching, request-array order as the only link back to a
    row, and three mutually-unreadable 207 body shapes — is identical for both, so a second writer
    would mean two copies of the code whose own comment says that getting the order wrong makes the
    ledger blame the wrong keyword. `fetch_current_bids` already had to be regrouped by writer after
    exactly that kind of divergence.

    **A state row never also carries a bid.** Each payload builder picks one field, so a pause cannot
    apply an unpreviewed bid change as a side effect.
```

- [ ] **Step 4: Add `_sp_payload_row` and use it**

Replace the SP payload branch inside the batch loop:

```python
        else:
            payload = {body_key: [
                {id_field: str(c["entity_id"]), "bid": round(float(c["new_bid"]), 2)}
                for c in batch
            ]}
```

with:

```python
        else:
            payload = {body_key: [_sp_payload_row(c, id_field) for c in batch]}
```

Add this function immediately above `_sb_payload_row`:

```python
def _sp_payload_row(change: Mapping, id_field: str) -> dict:
    """One row of a Sponsored Products write — a bid change OR a state change, never both.

    Amazon's update schemas require only the id and treat every other attribute as a partial update,
    so sending `state` alone leaves the bid untouched and vice versa. Exactly one is sent, because a
    pause that also carried a bid would apply a change nobody previewed.

    Its own function for the same reason `_sb_payload_row` is: the requirement is then stated once
    and pinned by a test, rather than living as an easily-dropped key inside a comprehension.
    """
    from app.ads.logic import AD_PRODUCT_SP, normalise_state

    row = {id_field: str(change["entity_id"])}
    if change.get("new_state"):
        row["state"] = normalise_state(change["new_state"], AD_PRODUCT_SP)
    else:
        row["bid"] = round(float(change["new_bid"]), 2)
    return row
```

- [ ] **Step 5: Teach both SB builders the state field**

In `_sb_payload_row`, replace its `return {...}` with:

```python
    from app.ads.logic import AD_PRODUCT_SB, normalise_state

    def _as_id(value):
        text = str(value or "")
        return int(text) if text.isdigit() else value

    row = {
        "keywordId": _as_id(change["entity_id"]),
        "adGroupId": _as_id(change.get("ad_group_id")),
    }
    if change.get("new_state"):
        # **`campaignId` is required for an SB keyword STATE write and is NOT required for a bid
        # write.** Amazon's update schema lists all three ids as required on the state path; omitting
        # it is a per-row refusal inside a 207 whose HTTP status says success — the same silent shape
        # as the missing `adGroupId` this function was written for.
        row["campaignId"] = _as_id(change.get("campaign_id"))
        # **Lower case.** SB's state enum is `enabled|paused|archived|draft` where SP's is upper.
        row["state"] = normalise_state(change["new_state"], AD_PRODUCT_SB)
    else:
        row["bid"] = round(float(change["new_bid"]), 2)
    return row
```

In `_sb_target_payload_row`, replace its `return {...}` with:

```python
    from app.ads.logic import AD_PRODUCT_SB, normalise_state

    identifier = str(change["entity_id"])
    ad_group = str(change.get("ad_group_id") or "")
    row = {
        "targetId": int(identifier) if identifier.isdigit() else identifier,
        "adGroupId": int(ad_group) if ad_group.isdigit() else ad_group,
    }
    if change.get("new_state"):
        row["state"] = normalise_state(change["new_state"], AD_PRODUCT_SB)
    else:
        row["bid"] = round(float(change["new_bid"]), 2)
    return row
```

- [ ] **Step 6: Update the name everywhere else in the module**

- The batch-failure log line: `"ads: bid write batch failed: %s"` becomes `"ads: write batch failed: %s"`.
- `__all__` (~line 1129): `"apply_bids"` becomes `"apply_changes"`.
- Module docstring (line 4): `` `apply_bids` changes live bids `` becomes `` `apply_changes` changes live bids and states ``.

- [ ] **Step 7: Update both callers**

In `app/routers/ads.py`, at **both** ~line 791 (apply) and ~line 850 (undo), replace:

```python
                    results.extend(await spapi_ads.apply_bids(client, rows, writer=writer))
```

with:

```python
                    results.extend(await spapi_ads.apply_changes(client, rows, writer=writer))
```

> Both, not one. The undo site is the easier to miss and the more damaging to leave behind: apply and
> undo diverging is the one place divergence corrupts the ledger.

- [ ] **Step 8: Fix the scheduler guard so the rename cannot retire it**

In `tests/test_retention_and_scheduler.py:337`, replace:

```python
    for forbidden in ("apply_bids", "plan_run", "open_run", "/apply"):
```

with:

```python
    # **These are LITERAL source searches, so a rename silently retires the assertion.** When
    # `apply_bids` became `apply_changes` the old literal appeared nowhere and this loop passed
    # vacuously — a green test on the guard that stops a scheduled job moving live bids. The searched
    # names are therefore asserted to EXIST first.
    from app.ads import logic as _logic
    from app.ads import spapi_ads as _spapi

    assert hasattr(_spapi, "apply_changes") and hasattr(_logic, "plan_run"), (
        "a searched name no longer exists, so this test would pass without proving anything"
    )
    for forbidden in ("apply_changes", "plan_run", "open_run", "/apply"):
```

- [ ] **Step 9: Rename the remaining call sites in the tests**

```bash
venv/Scripts/python -m pytest tests/test_ads_writes.py tests/test_ads_sb.py -q -p no:randomly
```
Expected: ~11 failures in `tests/test_ads_writes.py` on `spapi_ads.apply_bids`, plus
`tests/test_ads_sb.py:557`'s `monkeypatch.setattr(sp, "apply_bids", fake_apply)`. Rename each to
`apply_changes`. **Mechanical — change no assertion.** If a rename appears to require an assertion
change, stop: that is a real behaviour difference and the payload switch is wrong.

- [ ] **Step 10: Run everything**

```bash
venv/Scripts/python -m pytest -q
```
Expected: 2084+ passed, 17 skipped, 0 failed.

- [ ] **Step 11: Commit**

```bash
git add app/ads/spapi_ads.py app/routers/ads.py tests/
git commit -m "feat(ads): apply_bids becomes apply_changes, writing a bid OR a state

One payload switch per writer; batching, request ordering and all three 207
parsers keep exactly one copy. SB gets lower-case states, and campaignId on a
keyword state write. The scheduler guard now asserts its searched names exist,
so the next rename cannot retire it silently."
```

---

### Task 5: `last_applied_states`, and pinning the bid guard's accidental protection

**Files:**
- Modify: `app/ads/repository.py` — after `last_applied_bids` (~line 916)
- Test: `tests/test_ads_pause.py`

**Interfaces:**
- Consumes: Task 3's `new_state` column.
- Produces: `last_applied_states(db, entity_ids) -> {entity_id: {"state", "at", "rule", "day"}}`.

> **Why this cannot reuse `last_applied_bids`.** Checked against the code rather than assumed: that
> function filters `AdsMutation.new_bid.is_not(None)`, so it is structurally blind to a state row and
> can never report "this was paused today". Reusing it would leave the once-per-day guard silently
> doing nothing for the new action — and the failure is invisible, because the guarded-row machinery
> is still on screen while every row arrives ticked.
>
> The same filter is what makes `last_applied_bids` correct for its own job once state rows exist, so
> Step 1 pins it. That protection is **incidental** — it holds because of a filter written for a
> different reason — and a future refactor that widened it would make a null read as the true current
> bid and compound the next bid change.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ads_pause.py`:

```python
async def test_last_applied_bids_ignores_state_rows(db):
    """**Pins an INCIDENTAL protection, which is why it needs a test rather than an edit.**

    `last_applied_bids` filters `new_bid IS NOT NULL`, so a state row is already excluded with no
    code change. But it holds by accident of a filter written for another purpose: widen it and a
    paused row's null bid becomes the "true current bid", and the next percentage rule computes from
    a null. Stated as a requirement so the filter cannot be removed as redundant.
    """
    run_id = await ads_repo.open_run(db, [{
        "entity_id": "555", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
        "old_state": "ENABLED", "new_state": "PAUSED",
    }], rule_summary="pause")
    await ads_repo.record_results(db, run_id, [{"entity_id": "555", "ok": True}])

    assert await ads_repo.last_applied_bids(db, ["555"]) == {}, (
        "a pause is not a bid change and must never be served as the current bid"
    )


async def test_last_applied_states_reports_a_row_paused_today(db):
    """The day guard's own basis. Only `applied` rows count, like the bid version."""
    run_id = await ads_repo.open_run(db, [{
        "entity_id": "666", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
        "old_state": "ENABLED", "new_state": "PAUSED",
    }], rule_summary="spend>1000 -> PAUSED")
    await ads_repo.record_results(db, run_id, [{"entity_id": "666", "ok": True}])

    found = await ads_repo.last_applied_states(db, ["666"])
    assert found["666"]["state"] == "PAUSED"
    assert found["666"]["day"] == logic.ist_day(_dt.datetime.utcnow())
    assert found["666"]["rule"] == "spend>1000 -> PAUSED"


async def test_last_applied_states_excludes_a_failed_row(db):
    """A failed row never changed anything at Amazon, so it must not gate a later run.

    Same rule `build_undo` follows for the same reason: treating a refusal as a real change is how a
    guard starts blocking work that was never done.
    """
    run_id = await ads_repo.open_run(db, [{
        "entity_id": "777", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
        "old_state": "ENABLED", "new_state": "PAUSED",
    }], rule_summary="pause")
    await ads_repo.record_results(
        db, run_id, [{"entity_id": "777", "ok": False, "error": "refused"}])

    assert await ads_repo.last_applied_states(db, ["777"]) == {}


async def test_last_applied_states_ignores_bid_rows(db):
    """The mirror image of the first test: a bid change is not a state change."""
    run_id = await ads_repo.open_run(db, [{
        "entity_id": "888", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
        "old_bid": 10.0, "new_bid": 11.0,
    }], rule_summary="+10%")
    await ads_repo.record_results(db, run_id, [{"entity_id": "888", "ok": True}])

    assert await ads_repo.last_applied_states(db, ["888"]) == {}
```

Add `import datetime as _dt` to the imports at the top of the file.

- [ ] **Step 2: Run it to make sure it fails**

Run: `venv/Scripts/python -m pytest tests/test_ads_pause.py -q -p no:randomly -k "last_applied"`
Expected: FAIL — `module 'app.ads.repository' has no attribute 'last_applied_states'`. The
`last_applied_bids` test should already PASS: it pins behaviour that is already correct.

- [ ] **Step 3: Add the function**

In `app/ads/repository.py`, immediately after `last_applied_bids`:

```python
async def last_applied_states(db: AsyncSession, entity_ids: Sequence[str]) -> dict[str, dict]:
    """`{entity_id: {"state", "at", "rule", "day"}}` — the newest APPLIED state change per entity.

    **A sibling of `last_applied_bids` rather than a parameter on it, because that one CANNOT do this
    job.** It filters `new_bid IS NOT NULL`, which is what correctly keeps a paused row from being
    served as the true current bid — and is exactly what makes it blind to a state row. Reusing it as
    the state guard's basis would leave the guard silently doing nothing: the screen would still show
    the guarded-row machinery while every row arrived ticked.

    **What the guard prevents here differs from the bid version.** A repeated bid change COMPOUNDS
    (15.25 x 1.10 = 16.78, so -10% twice is -19%). A repeated pause is idempotent. What this stops is
    a pause/enable FLIP-FLOP inside one day — a keyword turned off by one rule and back on by the
    next, ending wherever the last run happened to land.

    **Only `applied` rows**, like the bid version: a `failed` row never changed anything at Amazon and
    a `pending` one is unknown, so neither may gate a later run.

    Chunked for the same reason — a real rule matched 1,005 rows and SQLite caps an `IN (...)` list.
    """
    wanted = [str(e) for e in entity_ids if e]
    if not wanted:
        return {}

    out: dict[str, dict] = {}
    CHUNK = 500
    for start in range(0, len(wanted), CHUNK):
        rows = (await db.execute(
            select(AdsMutation)
            .where(
                AdsMutation.entity_id.in_(wanted[start:start + CHUNK]),
                AdsMutation.status == "applied",
                AdsMutation.new_state.is_not(None),
            )
            # ASCENDING, so the last write per entity simply overwrites — one fewer thing to get
            # backwards than a descending scan with `setdefault`.
            .order_by(AdsMutation.created_at, AdsMutation.id)
        )).scalars().all()
        for row in rows:
            out[row.entity_id] = {
                "state": row.new_state,
                "at": row.created_at.isoformat() if row.created_at else "",
                "rule": row.rule_summary or "",
                "day": logic.ist_day(row.created_at),
            }
    return out
```

- [ ] **Step 4: Run the tests**

Run: `venv/Scripts/python -m pytest tests/test_ads_pause.py -q -p no:randomly`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/ads/repository.py tests/test_ads_pause.py
git commit -m "feat(ads): last_applied_states, the day guard's own basis

last_applied_bids filters new_bid IS NOT NULL, so it is structurally blind to a
state row and cannot serve as the state guard — reusing it would leave the guard
silently inert. That same filter is now pinned by a test, because the protection
it gives the bid path is incidental."
```

---

### Task 6: `/ads/apply` re-reads the live STATE, and `/ads/preview` accepts the action

**Files:**
- Modify: `app/routers/ads.py` — `preview` (~line 640) and `apply` (~line 715-810), `undo` (~line 818)
- Modify: `app/ads/repository.py` — `build_undo` (~line 980)
- Test: `tests/test_ads_pause.py`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: `/ads/preview` and `/ads/apply` accept `action="set_state"` with `amount="PAUSED"|"ENABLED"`; the apply response gains an `unchanged` list; `build_undo` returns state rows with the pair swapped.

> **Three of the four live-re-read checks encode bid assumptions.** The `live_bid is None` and
> `live_bid != old_bid` checks must not apply to a state action: a pause has no arithmetic and
> therefore no staleness, and refusing to pause a money-losing keyword because a colleague nudged its
> bid would leave it running for the least relevant possible reason. The `live_state != ENABLED`
> check is the "already paused" case for a pause and must be **inverted** for an enable.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ads_pause.py`:

```python
async def test_a_pause_is_not_refused_because_someone_moved_the_bid(monkeypatch, auth_client):
    """**The trap in reusing the bid path's live re-read.**

    The bid-drift check exists because applying a stale PERCENTAGE produces a number nobody chose.
    A pause has no arithmetic and no staleness, so a bid that moved since the window was fetched is
    not a reason to keep a money-losing keyword running.
    """
    async def fake_live(_client, changes):
        return {"111": {"bid": 99.0, "state": "ENABLED"}}   # bid moved 12.0 -> 99.0

    sent = []

    async def fake_apply(_client, rows, *, writer):
        sent.extend(rows)
        return [{"entity_id": r["entity_id"], "ok": True} for r in rows]

    monkeypatch.setattr(spapi_ads, "fetch_current_bids", fake_live)
    monkeypatch.setattr(spapi_ads, "apply_changes", fake_apply)

    response = await auth_client.post("/ads/apply", json={
        "action": logic.ACTION_SET_STATE, "amount": "PAUSED", "rule": "spend>1000 -> PAUSED",
        "changes": [{
            "entity_id": "111", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
            "ad_group_id": "g1", "campaign_id": "c1", "old_bid": 12.0, "new_state": "PAUSED",
        }],
    })
    assert response.status_code == 200
    body = response.json()
    assert body["applied"] == 1, body
    assert body["moved"] == [], "a moved bid must not block a pause"
    assert len(sent) == 1


async def test_an_already_paused_row_is_reported_unchanged_and_not_sent(monkeypatch, auth_client):
    """The precondition the report could not supply, enforced where the state is actually known.

    Reported in its own bucket rather than dropped: "12 were already paused" is information, and a
    count quietly smaller than the table reads as the rule not working.
    """
    async def fake_live(_client, changes):
        return {"111": {"bid": 12.0, "state": "PAUSED"}}

    sent = []

    async def fake_apply(_client, rows, *, writer):
        sent.extend(rows)
        return [{"entity_id": r["entity_id"], "ok": True} for r in rows]

    monkeypatch.setattr(spapi_ads, "fetch_current_bids", fake_live)
    monkeypatch.setattr(spapi_ads, "apply_changes", fake_apply)

    response = await auth_client.post("/ads/apply", json={
        "action": logic.ACTION_SET_STATE, "amount": "PAUSED", "rule": "pause",
        "changes": [{
            "entity_id": "111", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
            "ad_group_id": "g1", "campaign_id": "c1", "new_state": "PAUSED",
        }],
    })
    body = response.json()
    assert sent == [], "nothing may be sent for a row already in the target state"
    assert len(body["unchanged"]) == 1
    assert "already paused" in body["unchanged"][0]["reason"].lower()


async def test_an_enable_acts_on_a_paused_row(monkeypatch, auth_client):
    """**The inverted precondition.**

    The existing filter drops anything not ENABLED, because a bid change to a paused row does
    nothing. An enable targets precisely those rows — unchanged, it would drop every row it was
    meant to act on and report a completely successful run of zero.
    """
    async def fake_live(_client, changes):
        return {"111": {"bid": 12.0, "state": "PAUSED"}}

    sent = []

    async def fake_apply(_client, rows, *, writer):
        sent.extend(rows)
        return [{"entity_id": r["entity_id"], "ok": True} for r in rows]

    monkeypatch.setattr(spapi_ads, "fetch_current_bids", fake_live)
    monkeypatch.setattr(spapi_ads, "apply_changes", fake_apply)

    response = await auth_client.post("/ads/apply", json={
        "action": logic.ACTION_SET_STATE, "amount": "ENABLED", "rule": "re-enable",
        "changes": [{
            "entity_id": "111", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
            "ad_group_id": "g1", "campaign_id": "c1", "new_state": "ENABLED",
        }],
    })
    body = response.json()
    assert body["applied"] == 1, body
    assert len(sent) == 1
    assert body["inactive"] == [], "a paused row is the TARGET of an enable, not an exclusion"


async def test_the_ledger_records_the_live_state_as_old_state(monkeypatch, auth_client, db):
    """`old_state` must be a MEASUREMENT, not a guess — it is what undo writes back."""
    async def fake_live(_client, changes):
        return {"111": {"bid": 12.0, "state": "ENABLED"}}

    async def fake_apply(_client, rows, *, writer):
        return [{"entity_id": r["entity_id"], "ok": True} for r in rows]

    monkeypatch.setattr(spapi_ads, "fetch_current_bids", fake_live)
    monkeypatch.setattr(spapi_ads, "apply_changes", fake_apply)

    response = await auth_client.post("/ads/apply", json={
        "action": logic.ACTION_SET_STATE, "amount": "PAUSED", "rule": "pause",
        "changes": [{
            "entity_id": "111", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
            "ad_group_id": "g1", "campaign_id": "c1", "new_state": "PAUSED",
        }],
    })
    run_id = response.json()["run_id"]
    row = (await db.execute(
        select(AdsMutation).where(AdsMutation.run_id == run_id)
    )).scalars().one()
    assert row.old_state == "ENABLED"
    assert row.new_state == "PAUSED"


async def test_undo_of_a_pause_is_an_enable(db):
    """**`build_undo`'s null-`old_bid` skip would otherwise reverse NOTHING and report success.**

    That skip was correct when written and its comment said the case "should be impossible". On a
    state row a null bid is normal, so left as a blanket check an undo of an 88-row pause run would
    silently do nothing. Same shape as `delete_draft_plans`, whose docstring asserted an invariant a
    later feature invalidated — and which destroyed 400 units of packed stock on production.
    """
    run_id = await ads_repo.open_run(db, [{
        "entity_id": "999", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
        "ad_group_id": "g1", "campaign_id": "c1",
        "old_state": "ENABLED", "new_state": "PAUSED",
    }], rule_summary="pause")
    await ads_repo.record_results(db, run_id, [{"entity_id": "999", "ok": True}])

    undo = await ads_repo.build_undo(db, run_id)
    assert len(undo) == 1, "a paused row must be reversible"
    assert undo[0]["new_state"] == "ENABLED", "the undo of a pause is an enable"
    assert undo[0]["old_state"] == "PAUSED"
    assert "new_bid" not in undo[0]
    # The writer needs these to build an SB payload; losing them here is a per-row refusal later.
    assert undo[0]["ad_group_id"] == "g1"
    assert undo[0]["campaign_id"] == "c1"


async def test_preview_accepts_a_pause_rule_and_contacts_nobody(monkeypatch, auth_client):
    """A preview must never reach Amazon — the same rule the suggested-bid column follows."""
    def explode(*_a, **_k):
        raise AssertionError("preview must not call Amazon")

    monkeypatch.setattr(spapi_ads, "fetch_current_bids", explode)
    monkeypatch.setattr(spapi_ads, "apply_changes", explode)

    response = await auth_client.post("/ads/preview", json={
        "days": 30, "action": logic.ACTION_SET_STATE, "amount": "PAUSED",
        "conditions": [{"field": "spend", "op": ">", "value": 1000}],
    })
    assert response.status_code == 200


async def test_apply_refuses_an_archived_state_from_a_hand_built_request(monkeypatch, auth_client):
    """The client is not a trust boundary. Refused before any Amazon call."""
    def explode(*_a, **_k):
        raise AssertionError("nothing may be sent for an illegal state")

    monkeypatch.setattr(spapi_ads, "fetch_current_bids", explode)
    monkeypatch.setattr(spapi_ads, "apply_changes", explode)

    response = await auth_client.post("/ads/apply", json={
        "action": logic.ACTION_SET_STATE, "amount": "ARCHIVED", "rule": "hand-built",
        "changes": [{
            "entity_id": "111", "writer": logic.WRITER_KEYWORD, "ad_product": "sp",
            "new_state": "ARCHIVED",
        }],
    })
    assert response.status_code == 400
    assert "permanent" in response.json()["error"].lower()
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `venv/Scripts/python -m pytest tests/test_ads_pause.py -q -p no:randomly -k "pause_is_not_refused or already_paused or enable_acts or live_state_as_old or undo_of_a_pause or preview_accepts or archived_state_from"`
Expected: FAIL — `KeyError: 'unchanged'`, and the undo test returns `[]`.

- [ ] **Step 3: Make `build_undo` branch on `action`**

In `app/ads/repository.py`, in `build_undo`, replace the loop body:

```python
    undo = []
    for r in rows:
        if r.old_bid is None:
            # Cannot restore what was never recorded. Should be impossible — `open_run` always
            # writes it — but a row from a future code path with a null old_bid must be skipped
            # rather than written as bid 0.
            continue
        undo.append({
            "entity_id": r.entity_id,
            "writer": r.writer,
            "text": r.text or "",
            "campaign_id": r.campaign_id or "",
            "ad_group_id": r.ad_group_id or "",
            "old_bid": _f(r.new_bid),      # what it is now
            "new_bid": _f(r.old_bid),      # what it was before the run
        })
    return undo
```

with:

```python
    undo = []
    for r in rows:
        common = {
            "entity_id": r.entity_id,
            "writer": r.writer,
            # Carried so an SB payload can be rebuilt: `adGroupId` is required on every SB write and
            # `campaignId` on an SB keyword state write. Dropping them here is a per-row refusal
            # inside a 207 whose status says success.
            "ad_product": r.ad_product or "sp",
            "text": r.text or "",
            "campaign_id": r.campaign_id or "",
            "ad_group_id": r.ad_group_id or "",
        }

        # **Branch on `action`, because the null-check below is per-KIND.**
        #
        # This used to skip any row with a null `old_bid`, under a comment saying that was
        # impossible. It was, when written. On a state row a null bid is NORMAL — so left as a
        # blanket check, undoing an 88-row pause run would reverse nothing and report success.
        # Exactly the shape of `delete_draft_plans`, whose docstring asserted an invariant that a
        # later feature invalidated, and which destroyed 400 units of packed stock on production.
        if r.action == "state":
            if not r.old_state:
                # Nothing measured to restore. `/ads/apply` always records the live state, so this
                # means a row from a path that did not — skipped rather than guessed, because
                # guessing writes a state Amazon may never have held.
                continue
            undo.append({**common,
                         "old_state": r.new_state,     # what it is now
                         "new_state": r.old_state})    # what it was before the run
            continue

        if r.old_bid is None:
            continue
        undo.append({**common,
                     "old_bid": _f(r.new_bid),
                     "new_bid": _f(r.old_bid)})
    return undo
```

Also extend the docstring, after the `pending` paragraph:

```
    **A state row is reversed by swapping the state pair, so the undo of a pause IS an enable** — a
    forward `set_state` through the same writer, with no reverse-specific code. `old_state` is the
    value read live from Amazon at apply time, never a value taken from the report.
```

- [ ] **Step 4: Make the apply route state-aware**

4a. In `app/routers/ads.py`, in the apply handler, immediately after the `rule_summary = ...` line:

```python
    # **A state action is validated here too, not only in `plan_run`.** The client is not a trust
    # boundary and this is the only route in the app that spends money — a hand-built request naming
    # `ARCHIVED` must be refused before any Amazon call, not merely absent from the screen.
    action = body.get("action") or ""
    target_state = None
    if logic.is_state_action(action):
        problem = logic.state_error(body.get("amount"))
        if problem:
            return JSONResponse({"error": problem}, status_code=400)
        target_state = str(body.get("amount")).strip().upper()
```

4b. Replace the per-row loop body — from `identifier = str(change["entity_id"])` down to
`to_send.append(change)` — with:

```python
            for change in approved:
                identifier = str(change["entity_id"])
                current = live.get(identifier) or {}
                live_bid = current.get("bid")
                live_state = (current.get("state") or "").upper()
                expected = change.get("old_bid")

                if not current:
                    moved.append({**change, "live_bid": None,
                                  "reason": "Amazon no longer reports this row at all."})
                    continue

                # ── A state change: the live STATE is the precondition, and the live BID is not
                #    consulted at all ──
                #
                # Three of the four bid checks below encode arithmetic assumptions:
                #
                # * `live_state != ENABLED` is the "already paused" case for a pause and is exactly
                #   INVERTED for an enable, which targets rows that are not enabled.
                # * `live_bid is None` cannot apply: a state write needs no bid.
                # * the bid-drift check cannot apply either. It exists because applying a stale
                #   PERCENTAGE produces a number nobody chose; a pause has no arithmetic and so no
                #   staleness. Refusing to turn off a money-losing keyword because a colleague nudged
                #   its bid would leave it running for the least relevant possible reason.
                if target_state is not None:
                    if live_state == target_state:
                        unchanged.append({
                            **change, "live_state": live_state,
                            "reason": f"it is already {live_state.lower()} at Amazon.",
                        })
                        continue
                    # `old_state` is a MEASUREMENT taken here, because the report has no state
                    # column. It is what undo writes back, so a guess would give undo a value Amazon
                    # never held.
                    to_send.append({**change, "old_state": live_state or None,
                                    "new_state": target_state})
                    continue

                # **Only ENABLED rows are written.** The `spTargeting` report has NO state column —
                # measured, none of its 15 columns carries one — so a plan built from it cannot tell
                # an enabled target from a paused one. Amazon reports whatever had activity in the
                # window, and 168 of 12,205 rows (1.4%) turn out to be PAUSED or ARCHIVED. Editing
                # the bid of something that is not serving does nothing useful and makes the run's
                # own count a lie.
                #
                # Checked here rather than at preview because this response carries the state
                # anyway: no extra request, and the state is exactly as fresh as the bid beside it.
                if live_state and live_state != "ENABLED":
                    inactive.append({**change, "live_state": live_state,
                                     "reason": f"it is {live_state.lower()} at Amazon, so it is not "
                                               f"serving and a bid change would do nothing."})
                    continue

                if live_bid is None:
                    moved.append({**change, "live_bid": None,
                                  "reason": "Amazon no longer reports a bid for this row."})
                    continue
                if expected is not None and round(float(expected), 2) != round(live_bid, 2):
                    moved.append({**change, "live_bid": live_bid,
                                  "reason": f"The bid is now {live_bid}, not {expected} — someone "
                                            f"changed it since this window was fetched."})
                    continue
                to_send.append(change)
```

4c. Change the bucket declaration from:

```python
            to_send, moved, inactive = [], [], []
```

to:

```python
            # `unchanged` is a THIRD reported bucket, not a silent drop. The standing rule in this
            # feature is excluded and NAMED: "12 were already paused" is information, where a count
            # quietly smaller than the table on screen reads as the rule not working.
            to_send, moved, inactive, unchanged = [], [], [], []
```

4d. Add `unchanged` to **both** return paths — the early `if not to_send:` dict and the final one:

```python
                    "moved": moved, "inactive": inactive, "unchanged": unchanged,
                    "repeated": repeated,
```

and in the final return, beside the existing `"moved"`/`"inactive"` keys:

```python
        "unchanged": unchanged,
```

- [ ] **Step 5: Pass the action through `/ads/preview`**

In the preview handler, where `plan_run` is called, ensure the action and amount come from the body
unchanged (they already do), and pass the state ledger alongside the bid one:

```python
    # Both ledgers: the bid one supplies the true current bid, the state one gates a flip-flop. They
    # are separate functions because `last_applied_bids` filters `new_bid IS NOT NULL` and so cannot
    # see a state row — see `repository.last_applied_states`.
    if logic.is_state_action(body.get("action") or ""):
        applied_today = await repository.last_applied_states(db, entity_ids)
    else:
        applied_today = await repository.last_applied_bids(db, entity_ids)
```

> `plan_run` reads `applied_today[...]["day"]` for the guard and `["bid"]` only in the bid branch, so
> one parameter carries both shapes without a signature change.

- [ ] **Step 6: Run the tests**

```bash
venv/Scripts/python -m pytest tests/test_ads_pause.py tests/test_ads_api.py -q -p no:randomly
```
Expected: PASS. If `test_ads_api.py` fails on a missing `unchanged` key, add it to that test's
expected shape — the response gained a field.

- [ ] **Step 7: Run everything**

```bash
venv/Scripts/python -m pytest -q
```
Expected: 0 failures.

- [ ] **Step 8: Commit**

```bash
git add app/routers/ads.py app/ads/repository.py tests/
git commit -m "feat(ads): apply re-reads the live state; undo of a pause is an enable

The bid-drift and null-bid checks are scoped to bid actions — a pause has no
arithmetic and so no staleness. The not-ENABLED filter is inverted for an enable.
build_undo branches on action, because its null-old_bid skip would otherwise
reverse a pause run silently and report success."
```

---

### Task 7: The screen

**Files:**
- Modify: `templates/ads.html` — action select (~line 231), payload builders (~line 741, ~line 944), action `change` handler (~line 1683), rule loader (~line 720), preview table + apply bar
- Test: `tests/test_ads_pause.py`, `tests/test_ads_ui_pause.py` (create)

**Interfaces:**
- Consumes: `/ads/preview` and `/ads/apply` accepting `action="set_state"` with a string `amount` (Task 6).
- Produces: no new server interface. Source-level assertions only, since no runtime test can watch a template read the wrong field.

> **`amount: Number($("amount").value)` is a live bug for this feature.** `Number("PAUSED")` is `NaN`,
> and `JSON.stringify` serialises `NaN` to `null` — so the server would receive no state at all and
> the run would either 400 or, worse, fall through to a default. The numeric input is replaced by a
> state `<select>` when the action is `set_state`, and there are **two** call sites that read it
> (save-rule ~line 741 and preview ~line 944).

- [ ] **Step 1: Write the failing test**

Create `tests/test_ads_ui_pause.py`:

```python
"""Source-level guards for the pause action's screen.

**Source-level because no runtime test can watch a template read the wrong field**, which is the same
honesty `tests/test_ads_one_source.py` already uses for `daily=True` and
`tests/test_ads_sb.py` for the poll loops' header dicts.
"""
import pathlib
import re

import pytest

ADS_HTML = pathlib.Path("templates/ads.html")


@pytest.fixture(scope="module")
def markup():
    return ADS_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script(markup):
    """Just the <script> bodies, so prose in the page cannot satisfy a code assertion."""
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", markup, re.S))


def test_the_action_dropdown_offers_pause_and_enable(markup):
    assert 'value="set_state"' in markup
    assert "Pause" in markup and "Enable" in markup


def test_archived_is_not_offered_anywhere_on_the_screen(markup):
    """The screen must not offer what the server refuses.

    Amazon's archive is terminal and has no undo, so it is absent from `WRITABLE_STATES`. A control
    that produced a 400 every time would read as a broken app.
    """
    assert "ARCHIVED" not in markup.upper().replace("ARCHIVED IS", "")


def test_the_state_value_is_never_passed_through_Number(script):
    """**`Number("PAUSED")` is NaN, which JSON.stringify writes as null.**

    So the server would receive no state at all. Asserted on SOURCE because the failure is a silently
    absent field rather than an exception: the request succeeds and does the wrong thing.

    Both call sites matter — the save-rule path and the preview path each read the amount box.
    """
    # Every `amount:` payload key must be conditional on the action rather than a bare Number().
    bare = re.findall(r"amount:\s*Number\(", script)
    assert not bare, (
        f"{len(bare)} site(s) still coerce the amount to a number unconditionally; a state action "
        f"sends a string and Number('PAUSED') is NaN -> null"
    )
    assert "ruleAmount(" in script, "one helper decides the amount for both call sites"


def test_the_amount_helper_is_used_by_both_the_preview_and_the_save_paths(script):
    """Two copies of this decision is how one path starts sending null."""
    assert len(re.findall(r"amount:\s*ruleAmount\(\)", script)) >= 2


def test_a_state_run_does_not_render_bid_columns(script):
    """`Bid -> New bid` is meaningless for a pause, and the suggested bid implies a bid is moving.

    Amazon offers no suggested bid for Sponsored Brands at all, so leaving that column visible on a
    state run would show a dash whose reason is a different one from the usual.
    """
    assert "isStateRun" in script, "the renderer must know which kind of run it is drawing"
    assert "new_state" in script, "a state run renders the state pair"


def test_the_apply_bar_says_pausing_stops_delivery(markup):
    """A bid nudge and stopping delivery cannot share one confirmation sentence."""
    lowered = markup.lower()
    assert "stop serving" in lowered or "stop delivering" in lowered
    assert "reversible" in lowered or "undo" in lowered
```

Append to `tests/test_ads_pause.py`:

```python
def test_the_template_still_has_no_toISOString_or_bare_date_parse():
    """The pause work touches date-free code, but this file is where a regression would land.

    `tests/test_local_dates.py` owns this rule across every template — asserted here too because a
    new `<select>` and a new payload builder are exactly the kind of edit that reintroduces it.
    """
    import pathlib

    script = pathlib.Path("templates/ads.html").read_text(encoding="utf-8")
    assert "toISOString" not in script
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `venv/Scripts/python -m pytest tests/test_ads_ui_pause.py -q -p no:randomly`
Expected: FAIL — `'value="set_state"' not in markup`

- [ ] **Step 3: Add the controls**

In `templates/ads.html`, replace lines ~231-239:

```html
    <select id="action">
      <option value="decrease_pct">decrease bid by</option>
      <option value="increase_pct">increase bid by</option>
      <option value="decrease_abs">decrease bid by (₹)</option>
      <option value="increase_abs">increase bid by (₹)</option>
      <option value="set">set bid to (₹)</option>
    </select>
    <input type="number" id="amount" value="10" step="0.5" style="width:90px"/>
    <span class="dim" id="amount-unit">%</span>
```

with:

```html
    <select id="action">
      <option value="decrease_pct">decrease bid by</option>
      <option value="increase_pct">increase bid by</option>
      <option value="decrease_abs">decrease bid by (₹)</option>
      <option value="increase_abs">increase bid by (₹)</option>
      <option value="set">set bid to (₹)</option>
      <option value="set_state">turn the keyword</option>
    </select>
    <input type="number" id="amount" value="10" step="0.5" style="width:90px"/>
    <!-- Shown INSTEAD of the number box for a state action. A state is not a quantity, and
         `Number("PAUSED")` is NaN, which JSON.stringify sends as null. -->
    <select id="state-amount" style="display:none">
      <option value="PAUSED">OFF (pause it)</option>
      <option value="ENABLED">ON (enable it)</option>
    </select>
    <span class="dim" id="amount-unit">%</span>
```

- [ ] **Step 4: Add one amount helper and use it at both call sites**

Add near the other small helpers in the `<script>`:

```javascript
// **The rule's amount, which is a NUMBER for a bid action and a STRING for a state action.**
//
// One helper for both call sites (save-rule and preview). The bug this prevents is quiet:
// `Number("PAUSED")` is NaN and `JSON.stringify` writes NaN as `null`, so the server would receive
// a rule with no state and either refuse it or fall through to a default. Nothing would throw.
function isStateRun(action){ return (action || $("action").value) === "set_state"; }

function ruleAmount(){
  return isStateRun() ? $("state-amount").value : Number($("amount").value);
}
```

At ~line 741 (save rule) and ~line 944 (preview), replace `amount: Number($("amount").value),` with:

```javascript
      amount: ruleAmount(),
```

- [ ] **Step 5: Swap the control when the action changes**

Extend the existing handler at ~line 1683:

```javascript
$("action").addEventListener("change", () => {
  const value = $("action").value;
  const state = value === "set_state";
  // A state action has no unit and no number: the box is REPLACED, not relabelled, so a stale
  // numeric value cannot travel with a rule that has no use for it.
  $("amount").style.display = state ? "none" : "";
  $("state-amount").style.display = state ? "" : "none";
  $("amount-unit").textContent = state ? "" : (value.endsWith("_pct") ? "%" : "₹");
});
```

And in the rule loader at ~line 720-722, replace:

```javascript
  if(rule.action) $("action").value = rule.action;
  if(rule.amount !== null && rule.amount !== undefined) $("amount").value = rule.amount;
  $("amount-unit").textContent = String(rule.action || "").endsWith("_pct") ? "%" : "₹";
```

with:

```javascript
  if(rule.action) $("action").value = rule.action;
  if(rule.amount !== null && rule.amount !== undefined){
    // A saved state rule holds "PAUSED"/"ENABLED"; a saved bid rule holds a number. Loading one
    // into the other's control is how a saved rule silently changes meaning.
    if(isStateRun(rule.action)) $("state-amount").value = rule.amount;
    else $("amount").value = rule.amount;
  }
  // Reuse the one handler so the loaded rule's controls match its action.
  $("action").dispatchEvent(new Event("change"));
```

- [ ] **Step 6: Render the state pair instead of the bid pair**

In the preview row renderer, wrap the bid cells so a state run shows its own pair. Find the cells
rendering `old_bid`/`new_bid` and the suggested-bid cell, and make each conditional:

```javascript
    const stateRun = !!change.new_state;
    const bidCells = stateRun
      // `ENABLED -> PAUSED`. The live state is only known at apply time, so before that the left
      // side reads "serving" rather than inventing a value the report cannot supply.
      ? `<td class="num">${change.old_state || "serving"}</td>
         <td class="num"><b>${change.new_state}</b></td>
         <td class="num dim" title="Amazon offers no suggested bid for a state change">—</td>`
      : `<td class="num">${money(change.old_bid)}</td>
         <td class="num"><b>${money(change.new_bid)}</b></td>
         ${suggestedCell(change)}`;
```

And the header row:

```javascript
    const bidHeaders = isStateRun()
      ? `<th>State</th><th>New state</th><th>Suggested</th>`
      : `<th>Bid</th><th>New bid</th><th>Suggested</th>`;
```

- [ ] **Step 7: Change the apply bar's promise**

In `applyBarHtml`, make the button and note state-aware:

```javascript
  // **A bid nudge and stopping delivery are not the same promise.** The existing sentence describes
  // re-pricing; a pause takes a keyword off air, so it says so and says that it is reversible.
  const verb = isStateRun()
    ? ($("state-amount").value === "PAUSED"
        ? `Pause ${n} keyword(s) and target(s) — they will stop serving`
        : `Enable ${n} keyword(s) and target(s) — they will start serving`)
    : `Apply ${n} bid change(s)`;
  const note = isStateRun()
    ? `Reversible: undo this run from the history below and every row goes back to its previous state.`
    : `Amazon has no undo of its own; this app records the previous bid so a run can be reversed.`;
```

- [ ] **Step 8: Render the `unchanged` bucket**

Wherever `moved` and `inactive` are reported after an apply, add:

```javascript
  if((result.unchanged || []).length){
    // Named, not silently absent — "12 were already paused" is information, and a count quietly
    // smaller than the table reads as the rule not working.
    parts.push(`${result.unchanged.length} row(s) were already in that state, so nothing was sent.`);
  }
```

- [ ] **Step 9: Run the tests**

```bash
venv/Scripts/python -m pytest tests/test_ads_ui_pause.py tests/test_ads_pause.py tests/test_local_dates.py tests/test_template_render_targets.py tests/test_theme.py -q -p no:randomly
```
Expected: PASS. `test_template_render_targets.py` will fail if `state-amount` is written to but not
declared — it checks every `getElementById` that is assigned against the ids each template declares.

- [ ] **Step 10: Verify in a browser**

```bash
# Start the app, sign in, open /ads-page
```
Check, in order:
1. Choosing **turn the keyword** replaces the number box with ON/OFF and clears the `%`.
2. Preview `spend>1000`, `roas<1` on 30d → rows appear with a **State / New state** column, no bid figures.
3. The apply bar reads *"Pause N keywords and targets — they will stop serving"*.
4. Switch back to **decrease bid by** → the number box returns with `%`, and a preview shows bid columns again.
5. Save the state rule, reload the page, load the saved rule → the ON/OFF control comes back set to OFF, not the number box.

> Step 5 is the one a test cannot cover well, and it is where the loader bug would show: a saved
> state rule loading `"PAUSED"` into the numeric input renders an empty box.

- [ ] **Step 11: Commit**

```bash
git add templates/ads.html tests/test_ads_ui_pause.py tests/test_ads_pause.py
git commit -m "feat(ads): the screen can build and preview a pause rule

One ruleAmount() helper for both payload sites, because Number('PAUSED') is NaN
and JSON.stringify sends NaN as null — the server would have received no state at
all with nothing throwing. Bid columns become the state pair; the apply bar says
delivery stops and that it is reversible."
```

---

### Task 8: The mutation harness, and the docs

**Files:**
- Create: `scripts/mutate_ads_state.py`
- Modify: `CLAUDE.md`
- Test: the harness itself is the test

**Interfaces:**
- Consumes: every earlier task.
- Produces: nothing importable. `venv/Scripts/python scripts/mutate_ads_state.py` exits 0 only when every mutation is caught.

> **The bar is mutation testing, not a green suite.** CLAUDE.md records five separate cases in this
> feature area of a bug shipping past a fully green suite.

- [ ] **Step 1: Write the harness**

Create `scripts/mutate_ads_state.py`:

```python
"""Mutation harness for the pause/enable action. Throwaway; not imported by the app.

Each entry breaks ONE decision from the spec and names the test that must catch it.

    venv/Scripts/python scripts/mutate_ads_state.py
"""
import pathlib
import subprocess
import sys

LOGIC = pathlib.Path("app/ads/logic.py")
REPO = pathlib.Path("app/ads/repository.py")
ROUTER = pathlib.Path("app/routers/ads.py")
WRITER = pathlib.Path("app/ads/spapi_ads.py")
HTML = pathlib.Path("templates/ads.html")
SCHED_TEST = pathlib.Path("tests/test_retention_and_scheduler.py")

MUTATIONS = [
    (
        "ARCHIVED becomes writable",
        LOGIC,
        "WRITABLE_STATES = (STATE_PAUSED, STATE_ENABLED)",
        'WRITABLE_STATES = (STATE_PAUSED, STATE_ENABLED, "ARCHIVED")',
        "test_archived_is_not_a_writable_state",
    ),
    (
        "the pause spend floor is ignored",
        LOGIC,
        '            if _as_float(m.get("spend")) < limits["min_pause_spend"]:',
        "            if False:",
        "test_a_row_below_the_pause_spend_floor_is_skipped_and_named",
    ),
    (
        "a state run keeps the bid-drift refusal, so a nudged bid blocks a pause",
        ROUTER,
        "                if target_state is not None:",
        "                if False:",
        "test_a_pause_is_not_refused_because_someone_moved_the_bid",
    ),
    (
        "an already-paused row is sent anyway",
        ROUTER,
        "                    if live_state == target_state:",
        "                    if False:",
        "test_an_already_paused_row_is_reported_unchanged_and_not_sent",
    ),
    (
        "build_undo keeps its blanket null-old_bid skip, so a pause reverses nothing",
        REPO,
        '        if r.action == "state":',
        "        if False:",
        "test_undo_of_a_pause_is_an_enable",
    ),
    (
        "last_applied_states filters new_bid, so the day guard is silently inert",
        REPO,
        "                AdsMutation.new_state.is_not(None),\n            )\n            # ASCENDING, so the last write per entity simply overwrites",
        "                AdsMutation.new_bid.is_not(None),\n            )\n            # ASCENDING, so the last write per entity simply overwrites",
        "test_last_applied_states_reports_a_row_paused_today",
    ),
    (
        "last_applied_bids stops filtering nulls, so a pause becomes the true current bid",
        REPO,
        "                AdsMutation.new_bid.is_not(None),\n            )\n            # ASCENDING, so the last write per entity wins",
        "            )\n            # ASCENDING, so the last write per entity wins",
        "test_last_applied_bids_ignores_state_rows",
    ),
    (
        "an SB state write is sent upper-case",
        LOGIC,
        "    return wanted.lower() if ad_product == AD_PRODUCT_SB else wanted",
        "    return wanted",
        "test_an_sb_pause_is_lower_case_and_carries_its_parent_ids",
    ),
    (
        "an SP state write also carries a bid",
        WRITER,
        '        row["state"] = normalise_state(change["new_state"], AD_PRODUCT_SP)\n    else:',
        '        row["state"] = normalise_state(change["new_state"], AD_PRODUCT_SP)\n    if True:',
        "test_an_sp_pause_sends_state_and_no_bid",
    ),
    (
        "the screen coerces the state to a number again",
        HTML,
        "  return isStateRun() ? $(\"state-amount\").value : Number($(\"amount\").value);",
        "  return Number($(\"amount\").value);",
        "test_the_state_value_is_never_passed_through_Number",
    ),
    (
        "the scheduler guard goes back to the retired literal",
        SCHED_TEST,
        'for forbidden in ("apply_changes", "plan_run", "open_run", "/apply"):',
        'for forbidden in ("apply_bids", "plan_run", "open_run", "/apply"):',
        "test_apply_bids_is_gone_and_the_scheduler_guard_names_the_new_function",
    ),
]


def run(expression):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-k", expression, "tests"],
        capture_output=True, text=True, timeout=600,
    )
    last = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return result.returncode, last


def main():
    survivors = []
    for label, path, old, new, test_name in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        if old not in original:
            # A SKIP is a HARNESS bug, not a pass. Reported as a survivor so "all caught" can never
            # print while a mutation silently never ran.
            print(f"SKIP      {label}\n          target text not found in {path}")
            survivors.append((label, f"target text not found in {path}"))
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            code, summary = run(test_name)
        except subprocess.TimeoutExpired:
            path.write_text(original, encoding="utf-8")
            print(f"SURVIVED  {label}\n          {test_name} HUNG -> timeout")
            survivors.append((label, f"{test_name} hung"))
            continue
        finally:
            path.write_text(original, encoding="utf-8")
        if code == 0:
            print(f"SURVIVED  {label}\n          {test_name} still passes -> {summary}")
            survivors.append((label, test_name))
        else:
            print(f"caught    {label}\n          {summary}")

    print()
    if survivors:
        print(f"{len(survivors)} SURVIVOR(S):")
        for label, detail in survivors:
            print(f"  - {label} ({detail})")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it**

```bash
venv/Scripts/python scripts/mutate_ads_state.py
```
Expected: `all 11 mutations caught`.

**A `SKIP` is a failure of this harness, not a pass** — it means the target text does not match the
source, usually because an earlier task's code was written slightly differently. Fix the harness
string to match the real source, then re-run. **A `SURVIVED` is a missing test**: write the test that
catches it before continuing, do not weaken the mutation.

- [ ] **Step 3: Update CLAUDE.md**

In `CLAUDE.md`, under the Ads tab, find the line:

```
**Bids only.** No pause, no archive, no budget edits, no keyword creation. Pausing stays a manual
job in Seller Central.
```

Replace it with:

```
**Bids and pause/enable only.** No archive, no negative keywords, no budget edits, no keyword
creation.

> **"Bids only" was true until 04 Sep 2026, and the reason it changed is worth keeping.** That
> sentence was right when written: every guard in `/ads/apply` was built around bid arithmetic, and
> adding a second kind of write before that path was proven would have widened the blast radius of the
> only feature in this app that spends money. The path is now proven — 105 real changes applied on 31
> Aug, a working undo chain, a ledger that survives a crash. **The `no archive` half stands**, and for
> a reason that does not expire: Amazon documents archiving as terminal, "permanent and can't be
> undone", and on Sponsored Brands an archived negative can never be recreated. The whole safety model
> here is an `old -> new` pair with a reversible undo, so an irreversible action has no undo and
> cannot honestly go through a rule that moves hundreds of rows in one click. `WRITABLE_STATES`
> records this so nobody "completes" the enum.

### Pausing a keyword is a rule action, and three of the apply guards had to be scoped
Asked for as *"if any keyword has spend>1000 and orders 0 or roas<1, then we remove or turn off that
keyword"*. Measured before building: `spend>1000 AND roas<1` matches **88 rows carrying ₹1,40,751**
(6.8% of spend) across 46 ad groups, and `orders=0` is a strict SUBSET of it, so one condition
expresses both.

**Every one of those 88 rows has more than 10 clicks. Zero have none** — and that is what makes this
safe where the zero-ROAS *bid* warning was rejected. There, 1,107 of 1,425 rows had zero clicks, so
ROAS was an undefined ratio rather than a measurement and raising the bid was correct. Here the
`spend>1000` floor does the discriminating: a keyword that has spent ₹1,000 has been tested. **The
distinction is the CLICK COUNT, not the ROAS** — a future reader who sees "ROAS < 1" and recalls the
rejected warning should read this paragraph before concluding the two contradict each other.

`set_state` is a fifth action through the SAME write path (`apply_bids` became `apply_changes` with a
per-row payload switch), because everything hard about that function — four endpoints, 500-row
batching, request-array order as the only link back to a row, three mutually-unreadable 207 shapes —
is identical for a state write. `fetch_current_bids` already had to be regrouped by writer once after
exactly that kind of divergence.

**Three of the four live re-read checks encode bid assumptions, and reusing them would have broken
this two ways:**

| Check | For a pause | For an enable |
|---|---|---|
| `live_state != ENABLED` | this IS the "already paused" case | **inverted** — an enable targets rows that are not enabled, so unchanged it drops every row it was meant to act on |
| `live_bid is None` | must not apply — a state write needs no bid | must not apply |
| `live_bid != old_bid` | must not apply | must not apply |

That last one is the trap. The bid-drift check exists because applying a stale PERCENTAGE produces a
number nobody chose; a pause has no arithmetic and therefore no staleness. Refusing to turn off a
money-losing keyword because a colleague nudged its bid would leave it running for the least relevant
possible reason.

- **`old_state` is a MEASUREMENT taken at apply time, never from the report.** `spTargeting` has no
  state column at all, so there is deliberately no `SKIP_ALREADY_PAUSED` in `plan_run` — it could
  never fire, and a constant that cannot fire invites a later reader to invent a state source that
  does not exist. The precondition lives where the state is actually known, and an already-paused row
  is reported in its own `unchanged` bucket rather than dropped.
- **`build_undo` had to branch on `action`, and this is the second time that class of bug has bitten.**
  It skipped any row with a null `old_bid` under a comment saying that was impossible — true when
  written, and normal for a state row. Left alone, undoing an 88-row pause run would have reversed
  **nothing and reported success**. Exactly the shape of `delete_draft_plans`, whose docstring
  asserted an invariant a later feature invalidated, and which destroyed 400 units of packed stock.
- **`last_applied_states` is its own function because `last_applied_bids` CANNOT do the job.** That
  one filters `new_bid IS NOT NULL` — which correctly stops a paused row being served as the true
  current bid, and is exactly what makes it blind to a state row. Reusing it as the day guard's basis
  would have left the guard silently inert while the screen still showed the guarded-row machinery.
  The bid filter's protection is *incidental*, so it is now pinned by its own test.
- **`min_pause_spend` (₹100) is the pause equivalent of `max_change_pct`**, guarding a different
  mistake: a pause has no percentage, so its dangerous typo is `spend>10` where `spend>1000` was
  meant. `max_rows` cannot catch that — 900 cheap rows fit under a 1,000-row ceiling.
- **`Number("PAUSED")` is `NaN`, which `JSON.stringify` sends as `null`.** Both payload call sites now
  go through one `ruleAmount()` helper; a bare `Number()` would have sent a rule with no state at all,
  with nothing throwing.
- **SB needs lower-case states AND `campaignId` on a keyword state write** — the latter is not needed
  for a bid write. Both fail per-row inside a 207 whose HTTP status says success.

> **Two pre-existing ledger bugs were fixed alongside, both found by reading the code this change
> touches.** `open_run` never passed `ad_product`, so **304 Sponsored Brands rows on production are
> recorded as `sp`** — harmless in effect, since `writer` carries the routing, but the column exists
> so the audit trail can name the API that was written to. In the same expression, every SB keyword
> was recorded as `entity_type="target"`. Historical rows are **not** back-filled: rewriting an audit
> trail to what it should have said is a worse precedent than 304 provably-wrong rows with a note.

**Negative keywords were asked for and deliberately NOT built.** Researched against Amazon's current
OpenAPI specs rather than assumed, and recorded in the spec so the decision is reviewable: **40 of the
88 matching rows are targets with no search term to negate**; a negative silently overrides an ENABLED
positive keyword, so the screen would show a live bid on something that cannot serve; SB negatives
cannot be recreated once archived; SB negative targets return **200, not 207**, with errors inside a
success code; and a duplicate negative errors rather than dedupes. A pause is reversible and covers
all 88 rows. Negatives are better designed after the owner has used pause and can see where the spend
actually moved to — pausing an exact-match keyword does not stop the same query arriving through a
broad match or an auto campaign.
```

- [ ] **Step 4: Run the full suite and both harnesses**

```bash
venv/Scripts/python -m pytest -q
venv/Scripts/python scripts/mutate_ads_state.py
venv/Scripts/python scripts/mutate_ads_token.py
venv/Scripts/python scripts/mutate_projections.py
```
Expected: 0 failures; `all 11 mutations caught`; `all 4 mutations caught`; `all 14 mutations caught`.
The last two prove this change did no collateral damage.

- [ ] **Step 5: Commit**

```bash
git add scripts/mutate_ads_state.py CLAUDE.md
git commit -m "test(ads): mutation harness for the pause action, and record the decision

Documents why 'bids only' changed, why 'no archive' did not, and why the click
count — not the ROAS — is what makes this rule safe where the zero-ROAS bid
warning was rejected."
```

---

## Final verification

- [ ] **Full suite**: `venv/Scripts/python -m pytest -q` → 0 failures
- [ ] **All three harnesses** green (11 / 4 / 14)
- [ ] **Against production data**, before deploying — the rule must find what the spec measured:

```bash
ssh -i "<key>" ubuntu@13.233.144.148 "cd /opt/amazon-tracker && venv/bin/python -c \"
import asyncio
from app.database import async_session
from app.ads import repository, refresh, logic

async def main():
    async with async_session() as db:
        s, e = refresh.default_window(30)
        rows = await repository.sum_daily(db, s, e)
        plan = logic.plan_run(
            rows,
            conditions=[{'field':'spend','op':'>','value':1000},
                        {'field':'roas','op':'<','value':1}],
            action=logic.ACTION_SET_STATE, amount='PAUSED')
        print(len(plan['changes']), 'rows', round(sum(c['spend'] for c in plan['changes'])))
asyncio.run(main())
\""
```
Expected: `88 rows 140751` (±small drift as the window rolls forward).

- [ ] **Deploy**

```bash
ssh -i "<key>" ubuntu@13.233.144.148
cd /opt/amazon-tracker && bash deploy/update-ec2.sh   # answer y to the hsn_master.json stash
```

> A migration ships in this change, so if the deploy fails on the baseline detector, check the script
> out FIRST and redeploy — the rollback restores the old script and the failure is self-perpetuating:
> `git fetch origin claude/stoic-allen-bb3a55 && git checkout origin/claude/stoic-allen-bb3a55 -- deploy/update-ec2.sh`

- [ ] **Manual, on production — ONE row first**

1. `/ads-page` → rule `spend>1000`, `roas<1`, action **turn the keyword → OFF**, window 30d → Preview.
2. Confirm **88 rows**, grouped by campaign, with a **State / New state** column and no bid figures.
3. Untick everything, tick **ONE** row, note its keyword text and ad group.
4. Apply → the response says `1 applied`.
5. In Seller Central, that keyword reads **Paused**.
6. Undo the run from the history panel → the same keyword reads **Enabled** again.

> One row, not 88, because the first live exercise of a new write path should move one thing. The
> undo step is the real test: it is the guard that the null-`old_bid` skip would have broken silently.
