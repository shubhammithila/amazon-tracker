# Ads: True Current Bid, Once-Per-Day Guard, Match Type and the Bid Log — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the true current bid after a rule has run (from the ledger, no fetch), refuse to move the same bid twice in an IST day, show the keyword's match type as its own column, and add a searchable log of every bid change with a 12-month retention.

**Architecture:** All three read data that already exists. `match_type` is already on every preview row and merely collapsed by `writerTag`. The true current bid and the repeat guard both come from `ads_mutation` — the existing ledger — via one new repository query. The log is a new view over the same table. No new Amazon calls, no new table.

**Tech Stack:** FastAPI async, SQLAlchemy 2.0 async, aiosqlite, Alembic, pytest, vanilla JS in Jinja2.

## Global Constraints

- **Tests:** `venv/Scripts/python -m pytest -q` from the repo root. 1871 pass now; every task must leave the suite green.
- **`app/ads/logic.py` is PURE** — no DB, no network. The guard's decision belongs there; the query that feeds it belongs in `repository.py`.
- **The guard lives in `plan_run`, not the UI.** A screen-level untick leaves `POST /ads/apply` re-appliable from a hand-built request, and that is the only route in this app that spends money. `/ads/apply` re-checks too, because a preview can sit open while another run happens.
- **`ads_mutation` is the audit trail and the undo chain**, and CLAUDE.md calls it *"the only table here that must never be treated as disposable"*. It is now also the source of the true current bid. **Retention is 12 months, not 1** — unlike the report rows it is NOT refetchable: Amazon will not tell us what we set a bid to in July.
- **Timestamps in the ledger are `datetime.utcnow()`.** "The same day" is an IST question. CLAUDE.md records **four** separate bugs in this codebase from exactly this boundary, so the conversion is one named function with its own test.
- **Every new migration MUST add a newest-first branch to the detector** in `deploy/update-ec2.sh` (~line 358), keyed on something the revision changes. **And check that script out first when deploying** — it replaced itself mid-deploy last time and failed on its own stale table list.
- **No inline event handlers in templates.** Keyword text comes from Amazon; use delegated listeners.
- **Current Alembic head:** `a1c7e93f24b8`.

---

## File Structure

**Modify:**
- `app/ads/logic.py` — `ist_day()`, `MATCH_LABELS`, `SKIP_CHANGED_TODAY`; `plan_run` takes `applied_today` and computes from the ledger bid.
- `app/ads/repository.py` — `last_applied_bids()`, `load_bid_log()`, `purge_mutations()`.
- `app/routers/ads.py` — feed `applied_today` into preview and apply; `GET /ads/bid-log`, `GET /ads/bid-log.xlsx`.
- `app/scheduler.py` — purge the ledger in the nightly sweep.
- `templates/ads.html` — Match column, the "changed today" badge and untick, the log panel.
- `deploy/update-ec2.sh` — detector branch for the new index.
- `CLAUDE.md`, `tests/test_ads_api.py`, `tests/test_ads_ledger.py`.

**Create:**
- `alembic/versions/<rev>_ads_mutation_log_index.py`
- `tests/test_ads_repeat_guard.py`
- `tests/test_ads_bid_log.py`

**Already correct — do NOT rebuild:**
- **`match_type` is already on every preview row.** `plan_run` does `{**m, ...}` and `metrics_for` sets it. Task A is rendering only.
- **`ads_mutation` already stores everything the log needs** — `old_bid`, `new_bid`, `status`, `error`, `rule_summary`, `created_at`, `text`, `writer`, `ad_product`, `campaign_id`, `ad_group_id`.
- **A per-RUN history already exists** (`load_runs`, `GET /ads/runs`, the history panel, undo). The log is a per-CHANGE view beside it, not a replacement.
- **`build_undo` already reads the ledger** and only reverses `applied` rows.

---

## Task A: Match type as its own column

**Files:**
- Modify: `app/ads/logic.py` (add `MATCH_LABELS`, `match_label()`)
- Modify: `templates/ads.html` (`matchTag`, the preview header and row)
- Test: `tests/test_ads_api.py`, `tests/test_ads_logic.py`

**Interfaces:**
- Produces: `logic.match_label(match_type, ad_product="sp") -> str` returning `"exact" | "phrase" | "broad" | "auto" | "product" | "theme" | "?"`.

**Why both columns stay:** the Match column is how the keyword competes; the existing Type column is which endpoint the write goes to. `TARGETING_EXPRESSION` appears under **both** SP and SB and routes to different APIs, so collapsing them is how a routing bug becomes invisible.

Measured in the live data — all six labels occur:

```
sp  TARGETING_EXPRESSION            27708      sb  EXACT                 6679
sp  PHRASE                          15951      sb  TARGETING_EXPRESSION  4189
sp  EXACT                           14435      sb  PHRASE                3521
sp  TARGETING_EXPRESSION_PREDEFINED  2889      sb  THEME                   56
sp  BROAD                            1418
```

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ads_logic.py`:

```python
def test_every_match_type_in_the_live_data_has_a_label():
    """**All six labels occur on the real account**, so none of these branches is hypothetical.

    Measured row counts: SP TARGETING_EXPRESSION 27,708 / PHRASE 15,951 / EXACT 14,435 /
    TARGETING_EXPRESSION_PREDEFINED 2,889 / BROAD 1,418; SB EXACT 6,679 / TARGETING_EXPRESSION 4,189 /
    PHRASE 3,521 / THEME 56.

    `broad` matters particularly: `writerTag` collapsed EXACT, PHRASE and BROAD into one "keyword"
    tag, so 1,418 broad-match rows were indistinguishable from exact ones on screen — and they
    compete completely differently.
    """
    assert logic.match_label("EXACT") == "exact"
    assert logic.match_label("PHRASE") == "phrase"
    assert logic.match_label("BROAD") == "broad"
    assert logic.match_label("TARGETING_EXPRESSION_PREDEFINED") == "auto"
    assert logic.match_label("TARGETING_EXPRESSION") == "product"
    assert logic.match_label("THEME", logic.AD_PRODUCT_SB) == "theme"


def test_an_unknown_match_type_is_labelled_not_guessed():
    """The same rule `writer_for` follows: unrecognised is named, never inferred.

    A row `writer_for` excludes must still be describable on screen, or the skipped list cannot say
    what it skipped.
    """
    assert logic.match_label("SOMETHING_NEW") == "?"
    assert logic.match_label("") == "?"
    assert logic.match_label(None) == "?"


def test_the_match_label_is_not_the_writer():
    """**Two different questions, and TARGETING_EXPRESSION is why.**

    It exists under BOTH ad products and routes to different APIs — `/sp/targets` versus
    `/sb/targets` — so a screen showing only one of the two columns cannot reveal a misrouted write.
    """
    assert logic.match_label("TARGETING_EXPRESSION", logic.AD_PRODUCT_SP) == "product"
    assert logic.match_label("TARGETING_EXPRESSION", logic.AD_PRODUCT_SB) == "product"
    assert logic.writer_for("TARGETING_EXPRESSION", logic.AD_PRODUCT_SP) == logic.WRITER_TARGET
    assert logic.writer_for("TARGETING_EXPRESSION", logic.AD_PRODUCT_SB) == logic.WRITER_SB_TARGET
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/Scripts/python -m pytest tests/test_ads_logic.py -q -p no:randomly -k match_label`

Expected: FAIL with `AttributeError: module 'app.ads.logic' has no attribute 'match_label'`.

- [ ] **Step 3: Add `match_label` to logic**

In `app/ads/logic.py`, after `SB_TARGET_MATCH_TYPES`:

```python
#: How a row COMPETES, for the screen — as distinct from `writer_for`, which is where its bid is
#: written. Both are shown, because `TARGETING_EXPRESSION` occurs under both ad products and routes
#: to different APIs, so one column cannot reveal a misrouted write.
#:
#: `writerTag` in the template used to collapse EXACT, PHRASE and BROAD into a single "keyword" tag,
#: which made 1,418 broad-match rows indistinguishable from 14,435 exact ones — and they compete
#: completely differently, so a bid decision on one is not a bid decision on the other.
MATCH_LABELS = {
    "EXACT": "exact",
    "PHRASE": "phrase",
    "BROAD": "broad",
    "TARGETING_EXPRESSION_PREDEFINED": "auto",
    "TARGETING_EXPRESSION": "product",
    "THEME": "theme",
}

#: Shown for a match type we have no name for. A question mark rather than a guess, matching
#: `writer_for` returning None: an unrecognised type is a new Amazon feature or our own typo, and a
#: plausible-looking label would hide it.
MATCH_UNKNOWN = "?"


def match_label(match_type, ad_product: str = AD_PRODUCT_SP) -> str:
    """A short human label for how a row competes: `exact`, `phrase`, `broad`, `auto`, `product`,
    `theme`, or `?`.

    `ad_product` is accepted but not currently needed — the labels happen to agree across products.
    It is in the signature because `writer_for` needs it for the same input and a caller holding one
    row should not have to remember which of the two functions cares.
    """
    return MATCH_LABELS.get(str(match_type or "").upper(), MATCH_UNKNOWN)
```

- [ ] **Step 4: Run to verify it passes**

Run: `venv/Scripts/python -m pytest tests/test_ads_logic.py -q -p no:randomly`

Expected: all pass.

- [ ] **Step 5: Render the column**

In `templates/ads.html`, beside `writerTag`:

```javascript
/* How the row COMPETES. A separate column from `writerTag`, which says which Amazon API the bid is
   written to — `TARGETING_EXPRESSION` exists under both ad products and routes differently, so one
   column cannot show a misrouted write.

   Asked for because EXACT, PHRASE and BROAD were all rendered as one "keyword" tag: measured, 1,418
   broad rows looked identical to 14,435 exact ones, and they compete completely differently. */
function matchTag(row){
  const label = MATCH_LABELS[String(row.match_type || "").toUpperCase()] || "?";
  return `<span class="tag m-${esc(label)}">${esc(label)}</span>`;
}
```

Add the vocabulary near the top of the script, sent from the server so the two cannot drift:

```javascript
/* Mirrors `logic.MATCH_LABELS`. Sent in the payload rather than hardcoded here, so adding a match
   type on the server does not silently render "?" on screen. */
const MATCH_LABELS = {{ match_labels | tojson }};
```

In `app/main.py`'s `ads_page`, pass it:

```python
    return templates.TemplateResponse(request, "ads.html", {
        "active": "ads", "grant": grant,
        # The match-type vocabulary, so the template cannot drift from `logic.MATCH_LABELS`.
        "match_labels": ads_logic.MATCH_LABELS,
    })
```

importing `from app.ads import logic as ads_logic` at the top of `app/main.py`.

In `renderPreview`, add the header between Type and Spend:

```javascript
        <th scope="col">Match</th>
```

and the cell after `writerTag(c)`:

```javascript
      <td>${matchTag(c)}</td>
```

Add the tag colours to the existing `.tag` block in the template's `<style>`:

```css
/* Match types. Keyword matches share a hue and the two target kinds another, so the shape of a
   preview is readable at a glance without reading every row. */
.m-exact{background:var(--blue-soft);color:var(--blue)}
.m-phrase{background:var(--blue-soft);color:var(--blue)}
.m-broad{background:var(--yellow-soft);color:var(--yellow)}
.m-auto{background:var(--orange-soft);color:var(--orange)}
.m-product{background:var(--green-soft);color:var(--green)}
.m-theme{background:var(--green-soft);color:var(--green)}
```

- [ ] **Step 6: Pin it in the template tests**

Append to `tests/test_ads_api.py`:

```python
def test_the_preview_shows_the_match_type_as_well_as_the_writer():
    """**EXACT, PHRASE and BROAD were one "keyword" tag**, so 1,418 broad rows looked exact.

    Both columns stay: Match is how the row competes, Type is which Amazon API its bid is written to.
    `TARGETING_EXPRESSION` exists under both ad products and routes differently, so a screen showing
    only one of them cannot reveal a misrouted write.
    """
    source = _code_only(_template())
    assert "function matchTag(" in source, "there is no match-type column"
    assert "function writerTag(" in source, "the writer column was replaced rather than joined"
    body = _js_function(source, "renderPreview")
    assert "matchTag(c)" in body and "writerTag(c)" in body, (
        "the preview renders one of the two columns but not both"
    )
    assert ">Match<" in body, "the Match column has no header"


def test_the_match_vocabulary_comes_from_the_server():
    """Hardcoding the labels in the template would let it drift from `logic.MATCH_LABELS`.

    A new match type would then render "?" on screen while the server knew its name.
    """
    source = _template()
    assert "match_labels | tojson" in source, "the labels are hardcoded in the template"
    from app.ads import logic as ads_logic
    main = (Path(__file__).parent.parent / "app" / "main.py").read_text(encoding="utf-8")
    assert "match_labels" in main, "the page route does not pass the vocabulary"
    assert set(ads_logic.MATCH_LABELS) >= {"EXACT", "PHRASE", "BROAD", "TARGETING_EXPRESSION"}
```

- [ ] **Step 7: Verify the JS parses and the suite is green**

```bash
node -e "const fs=require('fs');const s=fs.readFileSync('templates/ads.html','utf8');const m=[...s.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(x=>x[1]).join('\n');fs.writeFileSync('ads_check.js',m);" && node --check ads_check.js && echo "JS OK" && rm -f ads_check.js
venv/Scripts/python -m pytest -q
```

Expected: `JS OK`, all tests pass. Note `{{ match_labels | tojson }}` is Jinja, so `node --check` sees
it as a syntax error unless the extraction strips it — if so, substitute `0` for `{{...}}` first, as
`tests/test_local_dates.py` already does.

- [ ] **Step 8: Commit**

```bash
git add app/ads/logic.py app/main.py templates/ads.html tests/test_ads_logic.py tests/test_ads_api.py
git commit -m "feat(ads): show the match type as its own column

EXACT, PHRASE and BROAD were rendered as one 'keyword' tag, so 1,418 broad-match
rows were indistinguishable from 14,435 exact ones — and they compete
completely differently. Both columns stay: Match is how the row competes, Type
is which API its bid is written to, and TARGETING_EXPRESSION exists under both
ad products routing to different endpoints."
```

---

## Task B: The true current bid, and no twice in one IST day

**Files:**
- Modify: `app/ads/logic.py` (`ist_day`, `SKIP_CHANGED_TODAY`, `plan_run`)
- Modify: `app/ads/repository.py` (`last_applied_bids`)
- Modify: `app/routers/ads.py` (preview and apply)
- Modify: `templates/ads.html` (badge, untick, note)
- Test: `tests/test_ads_repeat_guard.py` (create)

**Interfaces:**
- Consumes: `AdsMutation` rows with `status == "applied"`.
- Produces:
  - `logic.ist_day(when: datetime | None) -> str` — the IST calendar date as `YYYY-MM-DD`, from a naive UTC datetime.
  - `repository.last_applied_bids(db, entity_ids: Sequence[str]) -> dict[str, dict]`, each value
    `{"bid": float, "at": str, "rule": str, "day": str}` — the newest `applied` mutation per entity.
  - `plan_run(..., applied_today: Mapping | None = None)`. Rows in it get `old_bid` from the ledger,
    `changed_today=True`, `changed_at`, `changed_rule`, `report_bid` (what the stale report said),
    and are **excluded from the default approved set** by the template.
  - `totals["changed_today"]`.

**These two ship in ONE commit, and the order is the whole point.** Right now the preview computes
from the STALE report bid, so running the same rule twice today lands on the same number by accident:
`13.86 × 1.10 = 15.25` both times. Measured on production — the report holds 13.86 while Amazon holds
15.25. **Fixing the display creates the compounding**: `15.25 × 1.10 = 16.78`. A −10% rule run twice
would be −19%, on live bids. So the guard cannot be a follow-up.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ads_repeat_guard.py`:

```python
"""The bid shown after a rule has run, and refusing to move it twice in a day.

**Both halves are one change, and the order is why.** The preview computes a new bid from the report
figure, which does not update when we change a bid — measured on production, the report held 13.86
while Amazon held the 15.25 we had just set. That staleness accidentally made a repeat run idempotent
(13.86 x 1.10 = 15.25 twice). Showing the true bid CREATES the compounding: 15.25 x 1.10 = 16.78, so a
-10% rule applied twice is -19%. The guard therefore ships with the fix, not after it.

The true bid needs no Amazon call: `ads_mutation` already records what we set it to.
"""
from datetime import datetime

import pytest

from app.ads import logic, repository

pytestmark = pytest.mark.regression


def _row(entity_id, *, bid, spend=500.0, sales=1500.0, match_type="EXACT", campaign="MF_SP_kw"):
    return {
        "keywordId": str(entity_id), "matchType": match_type, "keyword": f"kw{entity_id}",
        "campaignName": campaign, "campaignId": "c1", "adGroupId": "g1",
        "cost": spend, "sales7d": sales, "keywordBid": bid,
    }


RULE = dict(conditions=[{"field": "spend", "op": "gt", "value": 100}],
            action=logic.ACTION_INCREASE_PCT, amount=10)


# ─── The IST day ─────────────────────────────────────────────────────────────


def test_the_ist_day_is_not_the_utc_day_for_five_and_a_half_hours():
    """**CLAUDE.md records FOUR separate bugs in this codebase from this exact boundary**, including
    one that back-dated a GST invoice.

    The ledger stores `datetime.utcnow()`, and "not twice on the same day" is a decision taken in IST.
    A change applied at 04:00 IST must count as TODAY: in UTC that is 22:30 the previous day, so a
    UTC-day guard would allow a second run that morning.
    """
    # 22:30 UTC on the 30th is 04:00 IST on the 31st.
    assert logic.ist_day(datetime(2026, 8, 30, 22, 30)) == "2026-08-31"
    # 18:29 UTC on the 31st is 23:59 IST the same day — the last minute of the IST day.
    assert logic.ist_day(datetime(2026, 8, 31, 18, 29)) == "2026-08-31"
    # 18:30 UTC is 00:00 IST the NEXT day.
    assert logic.ist_day(datetime(2026, 8, 31, 18, 30)) == "2026-09-01"
    assert logic.ist_day(None) == ""


# ─── The true current bid ────────────────────────────────────────────────────


def test_the_bid_comes_from_the_ledger_not_the_stale_report():
    """Measured on production: the report said 13.86 for a keyword Amazon held at 15.25.

    The ledger knows, so no fetch is needed. `report_bid` travels too, because "the report is stale"
    is a fact worth showing rather than silently correcting.
    """
    plan = logic.plan_run(
        [_row("K1", bid=13.86)], **RULE,
        applied_today={"K1": {"bid": 15.25, "at": "2026-08-31T14:28:02",
                              "rule": "ROAS >= 5 -> bid increase 10%", "day": "2026-08-31"}},
    )
    change = plan["changes"][0]
    assert change["old_bid"] == 15.25, "the preview still shows the stale report bid"
    assert change["report_bid"] == 13.86, "the stale figure is hidden rather than shown"
    assert change["changed_today"] is True
    assert change["changed_at"] == "2026-08-31T14:28:02"
    assert "increase" in change["changed_rule"]


def test_the_new_bid_is_computed_from_the_true_bid():
    """The consequence that matters: a percentage applied to the wrong base is the wrong bid."""
    plan = logic.plan_run(
        [_row("K1", bid=13.86)], **RULE,
        applied_today={"K1": {"bid": 15.25, "at": "x", "rule": "r", "day": "2026-08-31"}},
    )
    # 15.25 * 1.10, not 13.86 * 1.10 (which would be 15.25 — the accidental idempotence).
    assert plan["changes"][0]["new_bid"] == pytest.approx(16.78, abs=0.01)


def test_a_row_never_changed_keeps_the_report_bid():
    """The common case must be untouched: most rows have no ledger entry at all."""
    plan = logic.plan_run([_row("K1", bid=13.86)], **RULE, applied_today={})
    change = plan["changes"][0]
    assert change["old_bid"] == 13.86
    assert change["changed_today"] is False
    assert change["new_bid"] == pytest.approx(15.25, abs=0.01)


# ─── Once per day ────────────────────────────────────────────────────────────


def test_a_row_changed_today_is_flagged_and_counted_but_not_dropped():
    """**Unticked and visible, not hidden.**

    A row silently missing from a 1,005-row preview is indistinguishable from a bug — the rule this
    whole screen follows. It stays in the table with its reason so the owner can deliberately re-tick
    it; there are legitimate reasons to move a bid twice.
    """
    plan = logic.plan_run(
        [_row("K1", bid=15.25), _row("K2", bid=10.0)], **RULE,
        applied_today={"K1": {"bid": 15.25, "at": "2026-08-31T14:28", "rule": "r",
                              "day": "2026-08-31"}},
    )
    assert len(plan["changes"]) == 2, "the row was dropped instead of flagged"
    by_id = {c["entity_id"]: c for c in plan["changes"]}
    assert by_id["K1"]["changed_today"] is True
    assert by_id["K2"]["changed_today"] is False
    assert plan["totals"]["changed_today"] == 1


def test_the_default_selection_excludes_rows_changed_today():
    """`approved_ids` is what the screen ticks by default, so the guard is in the DATA.

    Computed here rather than in the template, because `POST /ads/apply` must be able to make the same
    judgement — a screen-level untick leaves the route re-appliable from a hand-built request, and
    this is the only route in the app that spends money.
    """
    plan = logic.plan_run(
        [_row("K1", bid=15.25), _row("K2", bid=10.0)], **RULE,
        applied_today={"K1": {"bid": 15.25, "at": "x", "rule": "r", "day": "2026-08-31"}},
    )
    assert plan["approved_ids"] == ["K2"], (
        f"a row already changed today is ticked by default: {plan['approved_ids']}"
    )


def test_compounding_is_what_the_guard_prevents():
    """The number that justifies the feature.

    A -10% rule applied twice to the same live bid is **-19%**, not -10%. Built as a decrease because
    that is the direction that quietly loses impressions — an over-increase shows up as spend.
    """
    first = logic.plan_run(
        [_row("K1", bid=18.75)],
        conditions=[{"field": "spend", "op": "gt", "value": 100}],
        action=logic.ACTION_DECREASE_PCT, amount=10, applied_today={},
    )
    assert first["changes"][0]["new_bid"] == pytest.approx(16.88, abs=0.01)

    # A second run the same day, now seeing the true bid.
    second = logic.plan_run(
        [_row("K1", bid=18.75)],
        conditions=[{"field": "spend", "op": "gt", "value": 100}],
        action=logic.ACTION_DECREASE_PCT, amount=10,
        applied_today={"K1": {"bid": 16.88, "at": "x", "rule": "r", "day": "2026-08-31"}},
    )
    proposed = second["changes"][0]["new_bid"]
    assert proposed == pytest.approx(15.19, abs=0.01), "not computed from the true bid"
    assert second["approved_ids"] == [], "the compounding change was ticked by default"
    assert round((15.19 / 18.75 - 1) * 100) == -19, "the arithmetic this guard exists to prevent"


# ─── The repository query ────────────────────────────────────────────────────


async def _apply(db, entity_id, *, old, new, when, status="applied", rule="r"):
    from app.models import AdsMutation

    db.add(AdsMutation(run_id=f"run-{entity_id}-{when.isoformat()}", entity_id=entity_id,
                       entity_type="keyword", writer="keyword", old_bid=old, new_bid=new,
                       status=status, rule_summary=rule, created_at=when))
    await db.commit()


async def test_only_applied_rows_count_as_a_change(db):
    """A `failed` row never changed anything at Amazon, and a `pending` one is unknown.

    Treating either as the current bid would compute the next change from a value Amazon never held —
    the same reasoning `build_undo` follows when it reverses only `applied` rows.
    """
    now = datetime(2026, 8, 31, 9, 0)
    await _apply(db, "K1", old=10.0, new=11.0, when=now, status="failed")
    await _apply(db, "K2", old=10.0, new=11.0, when=now, status="pending")
    await _apply(db, "K3", old=10.0, new=11.0, when=now, status="applied")

    found = await repository.last_applied_bids(db, ["K1", "K2", "K3"])
    assert set(found) == {"K3"}, f"a non-applied row was treated as the current bid: {found}"
    assert found["K3"]["bid"] == 11.0


async def test_the_newest_applied_row_wins(db):
    """Two changes in a day means the LAST one is the current bid."""
    await _apply(db, "K1", old=10.0, new=11.0, when=datetime(2026, 8, 31, 9, 0))
    await _apply(db, "K1", old=11.0, new=12.5, when=datetime(2026, 8, 31, 15, 0))

    found = await repository.last_applied_bids(db, ["K1"])
    assert found["K1"]["bid"] == 12.5


async def test_a_change_from_a_previous_day_gives_the_bid_but_not_the_guard(db):
    """**The two facts are separate, and this is the case that proves it.**

    Yesterday's change is still the true current bid — the report is stale for as long as nobody
    refetches. But it must NOT block a run today, or a rule could never touch the same keyword twice
    in its life.
    """
    await _apply(db, "K1", old=10.0, new=11.0, when=datetime(2026, 8, 29, 9, 0))
    found = await repository.last_applied_bids(db, ["K1"])
    assert found["K1"]["bid"] == 11.0
    assert found["K1"]["day"] == "2026-08-29"

    plan = logic.plan_run([_row("K1", bid=10.0)], **RULE, applied_today=found,
                          today="2026-08-31")
    change = plan["changes"][0]
    assert change["old_bid"] == 11.0, "yesterday's true bid was ignored"
    assert change["changed_today"] is False, "yesterday's change blocked a run today"
    assert plan["approved_ids"] == ["K1"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_ads_repeat_guard.py -q -p no:randomly`

Expected: every test FAILS — `logic.ist_day` and `repository.last_applied_bids` do not exist and
`plan_run` has no `applied_today`.

- [ ] **Step 3: Add `ist_day` to logic**

In `app/ads/logic.py`, near the top after the imports:

```python
#: India is UTC+5:30, and the ledger stores naive `datetime.utcnow()`.
#:
#: **This offset is written once, here.** CLAUDE.md records FOUR separate bugs in this codebase from
#: exactly this boundary — the Ads date picker, the Orders ship-by column, the Portfolio window, and a
#: GST invoice that came out dated a day early — so the conversion is a named function with its own
#: test rather than arithmetic inline at a call site.
IST_OFFSET = timedelta(hours=5, minutes=30)


def ist_day(when) -> str:
    """The IST calendar date of a naive UTC datetime, as `YYYY-MM-DD`. `""` for None.

    "Not twice on the same day" is a decision taken in IST, and the ledger records UTC. A change
    applied at 04:00 IST is 22:30 UTC the PREVIOUS day, so a UTC-day comparison would call it
    yesterday and allow a second run that morning — 5.5 hours out of every 24 in which the guard
    silently would not hold.
    """
    if when is None:
        return ""
    return (when + IST_OFFSET).date().isoformat()
```

Add `from datetime import timedelta` to the imports if absent.

- [ ] **Step 4: Add the skip reason and thread `applied_today` through `plan_run`**

Beside the other `SKIP_` constants:

```python
#: A row whose bid we already changed today. **Not a refusal — the row stays visible and unticked**,
#: because there are legitimate reasons to move a bid twice (a big spender mid-sale) and a row
#: silently missing from a 1,005-row preview is indistinguishable from a bug.
SKIP_CHANGED_TODAY = "the bid was already changed today, so applying again would compound it"
```

In `plan_run`'s signature add `applied_today: Mapping | None = None` and `today: str | None = None`,
and document them:

```python
    ``applied_today`` is `{entity_id: {"bid", "at", "rule", "day"}}` from
    `repository.last_applied_bids` — the newest APPLIED ledger row per entity. It does two jobs that
    must not be confused:

    * **The bid.** A report figure does not change when we change a bid, so after a run the screen
      showed a stale one — measured on production, the report said 13.86 for a keyword Amazon held at
      15.25. The ledger knows, so this needs no Amazon call.
    * **The guard.** Where that change happened TODAY (in IST), the row is flagged and left out of
      `approved_ids`, because applying the same percentage again compounds it: a -10% rule twice is
      -19%.

    The two are separate on purpose. Yesterday's change is still the true current bid but must not
    block a run today, or a rule could never touch the same keyword twice in its life.

    ``today`` is the IST day to compare against, defaulting to now. A parameter so a test can pin the
    boundary without freezing the clock.
```

Inside the row loop, the substitution must happen **before** `new_bid(...)` is called — and **every
guard downstream of it must read `old_bid`, not `m["bid"]`.** Verified against the real code at
`app/ads/logic.py:569-583`, there are five:

```python
        proposed = new_bid(m["bid"], action, amount)              # <- the computation
        ...
        if proposed < limits["min_bid"]:            SKIP_BELOW_FLOOR
        if proposed > limits["max_bid"]:            SKIP_ABOVE_CEILING
        if round(proposed, 2) == round(_as_float(m["bid"]), 2):   SKIP_NO_CHANGE   # <- reads the bid
        changes.append({..., "old_bid": round(_as_float(m["bid"]), 2), ...})       # <- reads the bid
```

`SKIP_NO_CHANGE` is the one that would bite quietly: computed from the true bid but compared against
the stale one, a row whose bid is already at the target would be reported as changing. The helper is
`new_bid(bid, action, amount)` — **not** `_proposed_bid`, which does not exist.

```python
        # ── The TRUE current bid, and the repeat guard ──
        #
        # The report figure is stale the moment a rule runs: Amazon's report does not re-issue because
        # we changed a bid. Measured on production, the report held 13.86 for a keyword we had just
        # set to 15.25 — so a percentage applied to the report figure is a percentage of the wrong
        # number.
        ledger = (applied_today or {}).get(str(m["entity_id"])) or {}
        report_bid = round(_as_float(m["bid"]), 2)
        old_bid = round(_as_float(ledger.get("bid")), 2) if ledger.get("bid") is not None \
            else report_bid
        changed_today = bool(ledger) and ledger.get("day") == this_day

        proposed = new_bid(old_bid, action, amount)
        ...
        changes.append({
            **m,
            "old_bid": old_bid,
            "new_bid": proposed,
            # What the REPORT said, kept so the screen can show that it was stale rather than
            # silently correcting it — the owner should be able to see why the number moved.
            "report_bid": report_bid,
            "changed_today": changed_today,
            "changed_at": ledger.get("at") or "",
            "changed_rule": ledger.get("rule") or "",
        })
```

with `this_day = today or ist_day(datetime.utcnow())` computed once before the loop, and
`approved_ids` built after it:

```python
    # **The default selection, computed HERE rather than in the template.** `POST /ads/apply` must be
    # able to make the same judgement: a screen-level untick would leave the route re-appliable from a
    # hand-built request, and it is the only route in this app that spends money.
    approved_ids = [c["entity_id"] for c in changes if not c["changed_today"]]
```

Return `"approved_ids": approved_ids` and add
`"changed_today": sum(1 for c in changes if c["changed_today"])` to `totals`.

Every one of those five sites moves to `old_bid`. Leave the guards' order and their skip reasons
exactly as they are — only what they read changes.

- [ ] **Step 5: Run the logic tests**

Run: `venv/Scripts/python -m pytest tests/test_ads_repeat_guard.py -q -p no:randomly -k "ist_day or ledger or true_bid or compounding or flagged or default_selection or report_bid"`

Expected: the pure-logic tests pass; the two `db` tests still fail on `last_applied_bids`.

- [ ] **Step 6: Add `last_applied_bids` to the repository**

In `app/ads/repository.py`, near `load_runs`:

```python
async def last_applied_bids(db: AsyncSession, entity_ids: Sequence[str]) -> dict[str, dict]:
    """`{entity_id: {"bid", "at", "rule", "day"}}` — the newest APPLIED bid change per entity.

    **This is where the true current bid comes from, and it needs no Amazon call.** A performance
    report does not re-issue because we changed a bid, so the figure the preview shows is stale the
    moment a rule runs — measured on production, the report held 13.86 for a keyword we had just set
    to 15.25. The ledger already records what we set it to.

    **Only `applied` rows.** A `failed` row never changed anything at Amazon and a `pending` one is
    unknown; treating either as the current bid would compute the next change from a value Amazon
    never held. Same rule `build_undo` follows when it reverses only applied rows.

    `day` is the IST calendar day (`logic.ist_day`), because "not twice on the same day" is an IST
    decision and this column stores UTC.

    Batched in chunks: SQLite caps an `IN (...)` list, and a real rule matched 1,005 rows.
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
                AdsMutation.new_bid.is_not(None),
            )
            .order_by(AdsMutation.created_at, AdsMutation.id)
        )).scalars().all()
        # Ascending, so the last write per entity wins — the newest applied change.
        for row in rows:
            out[row.entity_id] = {
                "bid": _f(row.new_bid),
                "at": row.created_at.isoformat() if row.created_at else "",
                "rule": row.rule_summary or "",
                "day": logic.ist_day(row.created_at),
            }
    return out
```

- [ ] **Step 7: Run the repository tests**

Run: `venv/Scripts/python -m pytest tests/test_ads_repeat_guard.py -q -p no:randomly`

Expected: all pass.

- [ ] **Step 8: Feed it into the routes**

In `app/routers/ads.py`'s `preview`, after `rows = await repository.attach_names(db, rows)`:

```python
    # The true current bid and the repeat guard, both from the ledger — no Amazon call. See
    # `logic.plan_run`'s docstring for why one query serves two purposes.
    applied_today = await repository.last_applied_bids(db, [r["entity_id"] for r in rows])
```

and pass `applied_today=applied_today` to `logic.plan_run`.

In `apply`, after the guardrail checks and **before** the live re-read, re-check the guard:

```python
    # **Re-checked here, not trusted from the client.** A preview can sit open while another run
    # happens, and the screen is not a trust boundary — this is the only route in the app that spends
    # money. Reported like `moved` and `inactive`: named, not silently dropped.
    ledger = await repository.last_applied_bids(db, [str(c.get("entity_id")) for c in approved])
    today = logic.ist_day(datetime.utcnow())
    repeated = [
        c for c in approved
        if (ledger.get(str(c.get("entity_id"))) or {}).get("day") == today
    ]
    if repeated:
        approved = [c for c in approved if c not in repeated]
```

and add `"repeated": [ ... ]` to the response with the same shape `moved` uses, plus the count in the
summary. Import `datetime` if absent.

- [ ] **Step 9: Render the badge and honour the default selection**

In `templates/ads.html`, `runPreview` currently does
`approved = new Set((body.changes || []).map(c => c.entity_id));`. Change it to trust the server:

```javascript
  /* **The server decides the default selection**, because `POST /ads/apply` re-checks the same rule.
     Rows whose bid we already changed today arrive UNTICKED — applying the same percentage again
     compounds it, and a -10% rule twice is -19%. */
  approved = new Set(body.approved_ids || (body.changes || []).map(c => c.entity_id));
```

In the row template, show the true bid and the badge:

```javascript
      <td class="num"><span class="bid-old">₹${c.old_bid.toFixed(2)}</span>
        → <span class="bid-new">₹${c.new_bid.toFixed(2)}</span>${
        c.changed_today
          ? `<br/><span class="tag warn-tag" title="Changed at ${esc(c.changed_at)} by: ${
              esc(c.changed_rule)}. The report still says ₹${(c.report_bid || 0).toFixed(2)}.">
              changed today</span>`
          : ""}</td>
```

Add to the `section-sub` summary, after the existing sentence:

```javascript
      ${(totals.changed_today || 0) > 0
        ? `<br/><strong>${totals.changed_today} row(s) were already changed today</strong> and are
           unticked — applying the same rule again would compound it. Their current bid is what we
           set it to, not what the report says. Re-tick deliberately if you mean to move them again.`
        : ""}
```

and a style beside the other tags:

```css
.warn-tag{background:var(--orange-soft);color:var(--orange)}
```

- [ ] **Step 10: Pin the screen behaviour**

Append to `tests/test_ads_api.py`:

```python
def test_the_preview_trusts_the_servers_default_selection():
    """**The guard is in the DATA, not the screen.**

    `plan_run` computes `approved_ids`, excluding rows already changed today, and `POST /ads/apply`
    re-checks the same rule. A template that ticked everything would put the compounding change one
    click away.
    """
    source = _code_only(_template())
    assert "body.approved_ids" in source, (
        "the screen ticks every row rather than honouring the server's selection"
    )


def test_a_row_changed_today_says_so_on_the_row():
    """The reason has to be ON the row: with 1,005 rows a summary line is not enough to explain why
    one is unticked."""
    body = _js_function(_code_only(_template()), "renderPreview")
    assert "changed_today" in body, "the badge is missing"
    assert "report_bid" in body, (
        "the stale report figure is hidden rather than shown, so the owner cannot see why the "
        "current bid differs from the last preview"
    )


async def test_apply_refuses_a_row_already_changed_today(auth_client, db, monkeypatch):
    """**Re-checked server-side, because a preview can sit open while another run happens.**

    The screen is not a trust boundary and this is the only route in the app that spends money.
    """
    from datetime import datetime

    from app.ads import logic as ads_logic
    from app.models import AdsMutation

    await _seed(db)
    db.add(AdsMutation(run_id="earlier", entity_id="111", entity_type="keyword", writer="keyword",
                       old_bid=18.75, new_bid=16.88, status="applied", rule_summary="earlier rule",
                       created_at=datetime.utcnow()))
    await db.commit()

    response = await auth_client.post("/ads/apply", json={
        "rule": "spend > 100 -> bid -10%",
        "changes": [{"entity_id": "111", "writer": "keyword", "text": "makhana",
                     "old_bid": 16.88, "new_bid": 15.19, "match_type": "PHRASE",
                     "campaign_id": "c1", "ad_group_id": "g1"}],
    })
    body = response.json()
    assert response.status_code == 200, body
    assert body.get("applied", 0) == 0, "a row already changed today was sent to Amazon"
    assert body.get("repeated"), "the refusal is not reported, so the row vanishes silently"
```

- [ ] **Step 11: Verify and commit**

```bash
node -e "const fs=require('fs');const s=fs.readFileSync('templates/ads.html','utf8');const m=[...s.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(x=>x[1]).join('\n');fs.writeFileSync('ads_check.js',m);" && node --check ads_check.js && echo "JS OK" && rm -f ads_check.js
venv/Scripts/python -m pytest -q
```

Expected: `JS OK`, all pass.

```bash
git add app/ads/logic.py app/ads/repository.py app/routers/ads.py templates/ads.html tests/
git commit -m "fix(ads): show the true current bid, and refuse to move it twice in one IST day

The report figure does not change when we change a bid, so after a rule ran the
preview showed a stale one — measured on production, the report held 13.86 for
a keyword Amazon held at 15.25.

Both halves ship together because the order is the point: the staleness made a
repeat run accidentally idempotent (13.86 x 1.10 = 15.25 twice), so fixing the
display CREATES the compounding — 15.25 x 1.10 = 16.78, and a -10% rule twice
is -19% on live bids.

The bid comes from ads_mutation, so no Amazon call. Rows changed today arrive
unticked with the reason on the row, and /ads/apply re-checks."
```

---

## Task C: The bid change log

**Files:**
- Modify: `app/ads/repository.py` (`load_bid_log`, `purge_mutations`)
- Modify: `app/routers/ads.py` (`GET /ads/bid-log`, `GET /ads/bid-log.xlsx`)
- Modify: `app/scheduler.py` (nightly purge)
- Modify: `templates/ads.html` (the log panel)
- Create: `alembic/versions/<rev>_ads_mutation_log_index.py`
- Modify: `deploy/update-ec2.sh`
- Test: `tests/test_ads_bid_log.py` (create)

**Interfaces:**
- Produces:
  - `repository.load_bid_log(db, *, search=None, start=None, end=None, status=None, limit=500, offset=0) -> dict` with `{"rows": [...], "total": int}`.
  - `repository.purge_mutations(db, *, keep_days=MUTATION_RETENTION_DAYS, today=None) -> int`.
  - `MUTATION_RETENTION_DAYS = 365`.

**Retention is 12 months, and that is a decision against the first instinct.** Measured: 105 rows /
0.035 MB today, ~0.34 KB per row — 37 MB/year at one 300-row run a day, 365 MB/year at three
1,000-row runs. Monthly deletion was the original ask and is wrong here: this table is the undo chain
and the audit trail, it is now the source of the true current bid, and **unlike the daily report rows
it is not refetchable** — Amazon will not say what we set a bid to in July.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ads_bid_log.py`:

```python
"""The bid change log: every individual change, searchable, with a 12-month retention.

A per-RUN history already existed (`load_runs`, the history panel, undo). This is the other question —
"what has happened to THIS keyword" — over the same `ads_mutation` rows, so there is no second source
of truth and nothing new to store.
"""
from datetime import datetime, timedelta

import pytest

from app.ads import repository
from app.models import AdsMutation

pytestmark = pytest.mark.regression


async def _change(db, entity_id, *, text, old, new, when, status="applied",
                  rule="spend > 100 -> bid -10%", run="r1"):
    db.add(AdsMutation(run_id=run, entity_id=entity_id, entity_type="keyword", writer="keyword",
                       text=text, old_bid=old, new_bid=new, status=status, rule_summary=rule,
                       campaign_id="c1", ad_group_id="g1", created_at=when))
    await db.commit()


async def test_the_log_returns_every_change_newest_first(db):
    now = datetime(2026, 8, 31, 12, 0)
    await _change(db, "K1", text="makhana", old=18.75, new=16.88, when=now - timedelta(hours=2))
    await _change(db, "K2", text="roasted chana", old=13.01, new=11.71, when=now)

    log = await repository.load_bid_log(db)
    assert log["total"] == 2
    assert [r["text"] for r in log["rows"]] == ["roasted chana", "makhana"]
    first = log["rows"][0]
    assert first["old_bid"] == 13.01 and first["new_bid"] == 11.71
    assert first["rule"] == "spend > 100 -> bid -10%"
    assert first["status"] == "applied"
    # The IST day travels, because that is the day the owner thinks in.
    assert first["day"] == "2026-08-31"


async def test_searching_by_keyword_text_finds_its_whole_history(db):
    """The "what happened to this keyword" question, which the per-run history cannot answer."""
    now = datetime(2026, 8, 31, 12, 0)
    await _change(db, "K1", text="makhana", old=18.75, new=16.88, when=now - timedelta(days=2))
    await _change(db, "K1", text="makhana", old=16.88, new=18.57, when=now)
    await _change(db, "K2", text="roasted chana", old=13.01, new=11.71, when=now)

    log = await repository.load_bid_log(db, search="makhana")
    assert log["total"] == 2
    assert all(r["text"] == "makhana" for r in log["rows"])
    # Case-insensitive and partial: the owner types what he remembers, not an exact string.
    assert (await repository.load_bid_log(db, search="MAKH"))["total"] == 2


async def test_filtering_by_date_range_and_status(db):
    now = datetime(2026, 8, 31, 12, 0)
    await _change(db, "K1", text="a", old=10.0, new=11.0, when=datetime(2026, 8, 20, 9, 0))
    await _change(db, "K2", text="b", old=10.0, new=11.0, when=now, status="failed")
    await _change(db, "K3", text="c", old=10.0, new=11.0, when=now)

    assert (await repository.load_bid_log(db, start="2026-08-31"))["total"] == 2
    assert (await repository.load_bid_log(db, end="2026-08-25"))["total"] == 1
    assert (await repository.load_bid_log(db, status="failed"))["total"] == 1
    assert (await repository.load_bid_log(db, status="applied"))["total"] == 2


async def test_the_log_shows_the_bid_path_for_one_keyword(db):
    """**A compounding mistake is a SHAPE, not a row.**

    13.86 -> 15.25 -> 16.78 in one day is the thing the repeat guard exists to prevent, and it is only
    visible when the changes are read in sequence. Ascending for this view, unlike the newest-first
    list, because a path reads forwards.
    """
    base = datetime(2026, 8, 31, 9, 0)
    for index, (old, new) in enumerate([(13.86, 15.25), (15.25, 16.78), (16.78, 15.10)]):
        await _change(db, "K1", text="makhana", old=old, new=new,
                      when=base + timedelta(hours=index))

    log = await repository.load_bid_log(db, search="makhana", ascending=True)
    path = [(r["old_bid"], r["new_bid"]) for r in log["rows"]]
    assert path == [(13.86, 15.25), (15.25, 16.78), (16.78, 15.10)]
    # Each change starts where the previous one ended — the property that makes the path meaningful.
    for earlier, later in zip(path, path[1:]):
        assert earlier[1] == later[0], f"the path is broken at {earlier} -> {later}"


async def test_the_log_is_paged_because_a_year_of_runs_is_large(db):
    """At three 1,000-row runs a day this table holds a million rows a year. Unbounded SELECTs are
    how a page that used to load becomes a page that times out."""
    now = datetime(2026, 8, 31, 12, 0)
    for index in range(12):
        await _change(db, f"K{index}", text=f"kw{index}", old=10.0, new=11.0,
                      when=now - timedelta(minutes=index))

    page = await repository.load_bid_log(db, limit=5)
    assert len(page["rows"]) == 5
    assert page["total"] == 12, "the total must count ALL matches, not the page"
    second = await repository.load_bid_log(db, limit=5, offset=5)
    assert {r["entity_id"] for r in page["rows"]} & {r["entity_id"] for r in second["rows"]} == set()


# ─── Retention ────────────────────────────────────────────────────────────────


async def test_the_ledger_keeps_twelve_months(db):
    """**12 months, not 1 — and that is a decision against the first instinct.**

    Measured: ~0.34 KB a row, so 37 MB/year at one 300-row run a day. This table is the undo chain and
    the audit trail, it is the source of the true current bid, and **unlike the daily report rows it is
    NOT refetchable** — Amazon will not say what we set a bid to in July. So it is kept long and
    bounded, rather than kept short.
    """
    assert repository.MUTATION_RETENTION_DAYS == 365

    now = datetime(2026, 8, 31, 12, 0)
    await _change(db, "OLD", text="old", old=10.0, new=11.0, when=now - timedelta(days=400))
    await _change(db, "NEW", text="new", old=10.0, new=11.0, when=now - timedelta(days=300))

    removed = await repository.purge_mutations(db, today=now.date())
    assert removed == 1, f"expected the 400-day-old row gone, removed {removed}"
    left = await repository.load_bid_log(db)
    assert [r["text"] for r in left["rows"]] == ["new"]


async def test_the_purge_runs_in_the_nightly_sweep():
    """A retention policy that is only called from a success path is a side effect, not a policy —
    the lesson `purge_daily` already records."""
    from pathlib import Path

    source = (Path(__file__).parent.parent / "app" / "scheduler.py").read_text(encoding="utf-8")
    assert "purge_mutations" in source, "the ledger is never purged, so it grows without bound"
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_ads_bid_log.py -q -p no:randomly`

Expected: FAIL — `load_bid_log`, `purge_mutations` and `MUTATION_RETENTION_DAYS` do not exist.

- [ ] **Step 3: Add `load_bid_log` and the retention**

In `app/ads/repository.py`:

```python
#: How long the bid-change ledger is kept. **365 days, and this is deliberately long.**
#:
#: Measured: ~0.34 KB per row, so 37 MB/year at one 300-row run a day and ~365 MB/year at three
#: 1,000-row runs. Monthly deletion was the first instinct and is wrong here on three counts: this
#: table is the UNDO CHAIN, it is the AUDIT TRAIL for the only feature that spends money, and it is
#: now the source of the TRUE CURRENT BID. Unlike `ads_performance_daily` it is also **not
#: refetchable** — Amazon will not tell us what we set a bid to in July.
MUTATION_RETENTION_DAYS = 365


async def load_bid_log(
    db: AsyncSession,
    *,
    search: str | None = None,
    start: str | None = None,
    end: str | None = None,
    status: str | None = None,
    ascending: bool = False,
    limit: int = 500,
    offset: int = 0,
) -> dict:
    """Individual bid changes for the log view: `{"rows": [...], "total": int}`.

    A sibling of `load_runs`, not a replacement. That one answers "what did that run do"; this one
    answers **"what has happened to this keyword"**, which needs the rows ungrouped.

    `ascending=True` reads the BID PATH forwards — 13.86 -> 15.25 -> 16.78 — because a compounding
    mistake or an oscillation is a shape rather than a row, and a shape only reads in order.

    Paged because at three 1,000-row runs a day this table holds a million rows a year, and `total`
    counts every match rather than the page so the screen can say what it is showing part of.
    """
    query = select(AdsMutation)
    count_query = select(func.count()).select_from(AdsMutation)

    filters = []
    if search:
        like = f"%{search.strip()}%"
        # Text OR entity id: the owner searches for a keyword he remembers, but a support question
        # arrives as an id.
        #
        # `ilike` rather than `like`, and it is portable — verified, SQLAlchemy compiles it to
        # `lower(col) LIKE lower(?)` on SQLite, so it is genuinely case-insensitive on both dialects
        # rather than relying on SQLite's ASCII-only LIKE. The owner types what he remembers, not the
        # catalogue's casing.
        filters.append(AdsMutation.text.ilike(like) | AdsMutation.entity_id.ilike(like))
    if status:
        filters.append(AdsMutation.status == status)
    if start:
        filters.append(AdsMutation.created_at >= datetime.fromisoformat(start))
    if end:
        # Inclusive of the whole end day, so `end=2026-08-31` includes that day's changes.
        filters.append(
            AdsMutation.created_at < datetime.fromisoformat(end) + timedelta(days=1)
        )
    for condition in filters:
        query = query.where(condition)
        count_query = count_query.where(condition)

    order = AdsMutation.created_at.asc() if ascending else AdsMutation.created_at.desc()
    rows = (await db.execute(
        query.order_by(order, AdsMutation.id.asc() if ascending else AdsMutation.id.desc())
        .limit(max(1, min(limit, 5000))).offset(max(0, offset))
    )).scalars().all()
    total = int((await db.execute(count_query)).scalar() or 0)

    return {
        "total": total,
        "rows": [
            {
                "entity_id": r.entity_id,
                "text": r.text or "",
                "match_type": "",          # not stored on the ledger; the log names the writer
                "writer": r.writer,
                "ad_product": r.ad_product or "sp",
                "campaign_id": r.campaign_id or "",
                "ad_group_id": r.ad_group_id or "",
                "old_bid": _f(r.old_bid),
                "new_bid": _f(r.new_bid),
                "status": r.status,
                "error": r.error or "",
                "rule": r.rule_summary or "",
                "run_id": r.run_id,
                "reverts_run_id": r.reverts_run_id or "",
                "at": r.created_at.isoformat() if r.created_at else "",
                # The IST day, because that is the day the owner thinks in and the column is UTC.
                "day": logic.ist_day(r.created_at),
            }
            for r in rows
        ],
    }


async def purge_mutations(db: AsyncSession, *, keep_days: int = MUTATION_RETENTION_DAYS,
                          today: date | None = None) -> int:
    """Delete ledger rows older than the retention window. Returns the number removed.

    **Bounded but long** — see `MUTATION_RETENTION_DAYS`. A year of a busy account is ~365 MB, which
    is why this exists at all on a box that has sat at 89% disk.
    """
    cutoff = (today or date.today()) - timedelta(days=keep_days)
    result = await db.execute(
        delete(AdsMutation).where(AdsMutation.created_at < datetime.combine(
            cutoff, datetime.min.time()))
    )
    await db.commit()
    removed = int(result.rowcount or 0)
    if removed:
        logger.info("ads: purged %d ledger row(s) older than %s", removed, cutoff.isoformat())
    return removed
```

- [ ] **Step 4: Purge nightly**

In `app/scheduler.py`, in the retention sweep beside the `purge_daily` block:

```python
    # The bid-change ledger. Kept 12 MONTHS, not 30 days: it is the undo chain, the audit trail for
    # the only feature that spends money, and the source of the true current bid — and unlike the
    # report rows it cannot be refetched from Amazon.
    async with async_session() as db:
        ledger_gone = await ads_repo.purge_mutations(db)
    if ledger_gone:
        deleted_total += ledger_gone
        logger.info(
            f"Retention: deleted {ledger_gone} rows from ads_mutation "
            f"(kept {ads_repo.MUTATION_RETENTION_DAYS} days)"
        )
```

- [ ] **Step 5: Add the routes**

In `app/routers/ads.py`:

```python
@router.get("/bid-log")
async def bid_log(
    request: Request,
    search: str | None = None,
    start: str | None = None,
    end: str | None = None,
    status: str | None = None,
    ascending: bool = False,
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    """Every individual bid change, searchable. The log beside the per-run history.

    Reads the ledger only — no Amazon call, and nothing is written.
    """
    try:
        log = await repository.load_bid_log(
            db, search=search, start=start, end=end, status=status,
            ascending=ascending, limit=limit, offset=offset,
        )
    except ValueError as exc:                      # a malformed date
        return JSONResponse({"error": f"Bad date: {exc}"}, status_code=400)
    return {**log, "retention_days": repository.MUTATION_RETENTION_DAYS}


@router.get("/bid-log.xlsx")
async def bid_log_xlsx(
    request: Request,
    search: str | None = None,
    start: str | None = None,
    end: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    grant=Depends(require_area(permissions.ADS)),
):
    """The same rows as a spreadsheet, for reconciling against Seller Central — and for keeping a
    copy beyond the 12-month retention.

    Built through the same `load_bid_log` the screen uses, so the file cannot disagree with it.
    """
    log = await repository.load_bid_log(
        db, search=search, start=start, end=end, status=status, limit=5000,
    )
    rows = [
        [r["day"], r["at"][11:19], r["text"], r["entity_id"], r["ad_product"], r["writer"],
         r["campaign_id"], r["old_bid"], r["new_bid"], r["status"], r["rule"], r["error"]]
        for r in log["rows"]
    ]
    stream = documents.build_portfolio_xlsx(
        "Ads bid changes",
        f"{log['total']} change(s)" + (f" matching {search!r}" if search else "")
        + f" · bids in Rs · kept {repository.MUTATION_RETENTION_DAYS} days",
        ["Day (IST)", "Time", "Keyword / target", "Id", "Product", "Endpoint", "Campaign",
         "Old bid", "New bid", "Status", "Rule", "Amazon's error"],
        rows,
        [12, 9, 30, 18, 9, 12, 14, 10, 10, 10, 40, 40],
    )
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="ads-bid-changes.xlsx"'},
    )
```

Import `documents` from `app.shipment` and `StreamingResponse` from `fastapi.responses` if absent.

> **`build_portfolio_xlsx`, NOT `build_simple_xlsx`** — verified in the source, and the difference is a
> crash rather than a preference. `build_simple_xlsx` appends `_totals_row(...)`, which sums every
> trailing column with `int(row[col] or 0)`. This log's trailing columns hold a status word, a rule
> sentence and Amazon's error text, so it would raise `ValueError`. CLAUDE.md already records exactly
> this: the Portfolio export is a sibling builder for the same reason, and widening `_totals_row` would
> silently change four working picking documents.
>
> Summing bids would be meaningless here anyway — a bid is a rate, and the log is a sequence of them.

- [ ] **Step 6: Add the log panel**

In `templates/ads.html`, beside the existing history panel: a `Bid log` toggle button, a
`<div id="bidlog-panel" style="display:none">` with a search box, two date inputs, a status select, a
Download button and a table. Follow the existing `renderHistory` pattern exactly — delegated
listeners, `localDate` for any date default, and `esc()` on every keyword.

The panel must show: day, time, keyword, old → new, status, rule. Clicking a keyword re-searches for
it with `ascending=true`, which is the bid-path view.

- [ ] **Step 7: The migration for the log's index**

The log filters on `created_at` and searches `text`. Create
`alembic/versions/<rev>_ads_mutation_log_index.py` (chain from `a1c7e93f24b8`):

```python
"""ads: index ads_mutation for the bid-change log

The log filters by date and searches text. `idx_ads_mutation_entity` covers "this keyword by id" but
nothing covers a date range, and this table is now expected to hold a year of runs — measured at three
1,000-row runs a day that is ~1,000,000 rows, where an unindexed range scan is what turns a page that
loads into a page that times out.

Revision ID: <rev>
Revises: a1c7e93f24b8
"""
from alembic import op

revision = "<rev>"
down_revision = "a1c7e93f24b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_ads_mutation_created", "ads_mutation", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_ads_mutation_created", table_name="ads_mutation")
```

Add the matching `Index("idx_ads_mutation_created", "created_at")` to `AdsMutation.__table_args__`,
and the detector branch in `deploy/update-ec2.sh` **above** the current head branch:

```sh
elif "ads_mutation" in tables and "idx_ads_mutation_created" in indexes("ads_mutation"):
    print("<rev>")                                  # head: bid-log index
```

adding an `indexes()` helper beside `cols()` in that heredoc:

```python
def indexes(table):
    return {r[1] for r in con.execute(f'PRAGMA index_list("{table}")')}
```

- [ ] **Step 8: Migrate, round-trip, and run everything**

```bash
venv/Scripts/python -m alembic upgrade head
venv/Scripts/python -m alembic downgrade a1c7e93f24b8
venv/Scripts/python -m alembic upgrade head
venv/Scripts/python -m pytest -q
```

Expected: the migration round-trips; all tests pass including `test_schema_migrations.py`.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat(ads): a searchable log of every bid change, kept 12 months

A per-RUN history already existed; this answers the other question — what has
happened to THIS keyword — over the same ads_mutation rows, so there is no
second source of truth and nothing new to store.

Search by text or id, filter by date and status, read the bid path forwards
(13.86 -> 15.25 -> 16.78, which is how a compounding mistake becomes visible),
and download as Excel.

Retention is 12 months rather than the 1 first suggested: ~0.34 KB a row means
37 MB/year at real usage, and this table is the undo chain, the audit trail for
the only feature that spends money, and now the source of the true current bid
— and unlike the report rows Amazon will not tell us what we set a bid to in
July."
```

---

## Task D: Mutations, docs, deploy, verification

- [ ] **Step 1: Run the mutations**

Each must be CAUGHT; restore the file after every one.

| Mutation | Must fail |
|---|---|
| `plan_run` ignores `applied_today` and uses `m["bid"]` | `test_the_bid_comes_from_the_ledger_not_the_stale_report` |
| `changed_today` always False | `test_the_default_selection_excludes_rows_changed_today` |
| `approved_ids` returns every change | same |
| `ist_day` returns the UTC day | `test_the_ist_day_is_not_the_utc_day_for_five_and_a_half_hours` |
| `last_applied_bids` accepts `pending` rows too | `test_only_applied_rows_count_as_a_change` |
| `last_applied_bids` orders descending | `test_the_newest_applied_row_wins` |
| the apply-side re-check deleted | `test_apply_refuses_a_row_already_changed_today` |
| template ticks every row | `test_the_preview_trusts_the_servers_default_selection` |
| `match_label` returns the writer | `test_the_match_label_is_not_the_writer` |
| `MUTATION_RETENTION_DAYS = 30` | `test_the_ledger_keeps_twelve_months` |
| `load_bid_log` ignores `search` | `test_searching_by_keyword_text_finds_its_whole_history` |
| `load_bid_log`'s `total` returns the page length | `test_the_log_is_paged_because_a_year_of_runs_is_large` |

- [ ] **Step 2: Update CLAUDE.md**

Under the Ads tab, record: the stale-bid measurement (report 13.86 vs Amazon 15.25 on 105 real
changes); that the staleness made a repeat accidentally idempotent so fixing the display created the
compounding, hence one commit; −10% twice = −19%; the IST-day boundary as the FIFTH instance of that
defect class in this codebase; the match-type column and why both columns stay; the log; and the
12-month retention with the reasoning for rejecting monthly. Update the test count on line 10.

- [ ] **Step 3: Deploy — script FIRST**

```bash
git push origin claude/stoic-allen-bb3a55
ssh -i "<key>" ubuntu@13.233.144.148 "cd /opt/amazon-tracker && git fetch -q origin claude/stoic-allen-bb3a55 && git checkout origin/claude/stoic-allen-bb3a55 -- deploy/update-ec2.sh"
ssh -i "<key>" ubuntu@13.233.144.148 "cd /opt/amazon-tracker && bash deploy/update-ec2.sh"
```

**The checkout is not optional.** The script replaces itself mid-deploy, so a stale table list or
detector fails the deploy and the rollback restores the stale copy — which is how the last deploy
failed and left the schema forward of the code.

- [ ] **Step 4: Verify on production**

- The 105 keywords changed today show the **new** bid as current (15.25, not 13.86) with a
  "changed today" badge, and arrive **unticked**.
- Previewing the same rule twice shows `changed_today` for every row of the second run.
- `POST /ads/apply` on one of those rows applies **0** and reports it under `repeated`.
- The Match column shows `exact` / `phrase` / `broad` / `auto` / `product` / `theme`.
- The bid log lists today's 105 changes; searching a keyword shows its path; the xlsx downloads.
- `df -h /` — free space unchanged (the index is small; nothing is deleted yet at 12 months).

## Out of scope

Auto-applying a suggested bid; changing the undo chain; a per-rule schedule; any Amazon call added to
the preview path.
