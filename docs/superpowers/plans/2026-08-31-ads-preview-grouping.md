# Ads Preview: Group 1,700 Rows by Campaign and Ad Group — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a 1,700-row bid preview reviewable by grouping it campaign → ad group → rows, collapsed by default, tickable at any level. Plus one correctness fix: the row limit currently counts rows the once-per-day guard has already unticked.

**Architecture:** Pure presentation over the plan the server already returns. `logic.plan_run` gains a grouping helper so the *counts and sums* per group are computed once, in the pure module, rather than in JavaScript — a group total that disagreed with its rows is the defect class this codebase keeps hitting. The template renders three levels using the parent/child pattern `renderCampaigns` already establishes.

**Tech Stack:** FastAPI async, vanilla JS in Jinja2, pytest.

**Measured on the live account** for the reported rule (`spend < 50, bid < 40 → +10%`):

| | |
|---|---|
| Changing rows | **1,700** |
| Campaigns spanned | **13** |
| Ad groups spanned | **118** |
| Largest campaign | MF_SP_keywords, **941 rows** |

So the flat list is 1,700 lines and the grouped view opens at 13.

## Global Constraints

- **Tests:** `venv/Scripts/python -m pytest -q`. 1915 pass now; every task must leave the suite green.
- **`app/ads/logic.py` is PURE** — no DB, no network. Group sums belong there; the DOM does not.
- **A group total is the SUM of its own rows, never a second calculation.** The Orders tab shipped "86 orders beside 87 lines" and the Portfolio tab's parent rows exist for the same reason. Here the numbers gate a live bid change.
- **No inline event handlers.** Keyword and campaign text come from Amazon; use delegated listeners, as this template already does.
- **`approved` stays the single source of what will be sent**, keyed by `entity_id`. Ticking a campaign is a bulk edit to that set, not a second piece of state — two selections that can disagree is how a row gets sent that nobody ticked.
- **A row the once-per-day guard unticked must stay unticked** when its campaign is ticked. Bulk selection must not silently re-enable the compounding the guard exists to prevent.
- **No new Amazon calls.** Everything here is already in the plan payload.

## What is already correct — do NOT rebuild

- **`renderCampaigns` already does parent → child** with `openCampaigns`, a caret, and `data-campaign`. Follow it; do not invent a second nesting idiom.
- **The rows already carry what grouping needs**: `campaign_id`, `campaign_name`, `ad_group_id`, `ad_group_name` (set by `repository.attach_names`), `spend`, `old_bid`, `new_bid`.
- **`applyBarHtml` renders twice from one builder** and the counts update via `[data-apply-count]`. Reuse it; the count must keep working when a whole campaign is ticked.
- **`suggestedCell`, `matchTag`, `writerTag` and the `changed today` badge** are per-row and unchanged.

## Out of scope — decided against, with the reason

**A zero-ROAS warning.** I proposed one and the data refuted it. Of the 1,632 zero-ROAS rows this rule
targets, **1,107 have had ZERO clicks and not one row has reached 10 clicks**:

```
0 clicks      1107 rows   Rs      0 spent
1-4 clicks     517 rows   Rs 14,439 spent
5-9 clicks       8 rows   Rs    324 spent
10-29 clicks     0 rows
30+ clicks       0 rows
```

"ROAS 0.00x" here does not mean *failed*, it means **never given enough impressions to find out** — so
raising the bid is the correct action and the rule is a deliberate discovery strategy. A warning would
have argued against the right move on 1,624 rows using a number that looks damning and is not.
Recorded because the same misreading would be just as wrong on the Portfolio tab.

---

## File Structure

**Modify:**
- `app/ads/logic.py` — `group_changes()`; the row-limit check counts appliable rows.
- `templates/ads.html` — grouped rendering, hierarchical ticking, expand/collapse.
- `tests/test_ads_logic.py`, `tests/test_ads_api.py`, `tests/test_ads_repeat_guard.py`.
- `CLAUDE.md`.

**Create:** nothing. No migration, no new route, no new table.

---

## Task 1: The row limit counts what will actually be sent

**Files:**
- Modify: `app/ads/logic.py:708-714` (the `blocked` check)
- Test: `tests/test_ads_repeat_guard.py`

**Interfaces:**
- Consumes: `approved_ids` (already computed).
- Produces: `blocked` is decided by `len(approved_ids)`; the message still names the full match count.

**The flaw, measured:** on the owner's `ROAS ≥ 5` rule this morning the plan held
`changing = 109, changed_today = 105, appliable = 4`. Scaled up, a rule matching 1,100 rows where
1,050 already moved today would be **blocked for exceeding a 1,000-row limit while only 50 rows could
be applied**. The limit exists to bound what reaches Amazon; counting unsendable rows measures the
wrong thing. Verified separately that raising `max_rows` to 10,000 in the Limits panel unblocks a
1,053-row rule with no code change — so this is about the limit being *right*, not about it being
adjustable.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ads_repeat_guard.py`:

```python
def test_the_row_limit_counts_only_rows_that_can_actually_be_sent():
    """**A rule was blocked for a size it would never send.**

    Measured on the owner's real rule: 109 changing, 105 already changed today, 4 appliable. Scaled up,
    1,100 matches with 1,050 already moved today would be refused for exceeding a 1,000-row limit while
    only 50 rows could go to Amazon.

    The limit exists to bound what reaches Amazon — `/ads/apply` enforces it again on what is actually
    approved — so counting rows the guard has already unticked measures the wrong thing.
    """
    ledger = {}
    rows = []
    for index in range(30):
        rows.append(_row(f"K{index}", bid=10.0))
        if index >= 5:                       # 25 of the 30 already moved today
            ledger[f"K{index}"] = {"bid": 10.0, "at": "x", "rule": "r", "day": "2026-08-31"}

    plan = logic.plan_run(
        rows, **RULE, applied_today=ledger, today="2026-08-31",
        guardrails={"max_rows": 10, "max_bid": 60.0, "min_bid": 1.0, "max_change_pct": 25.0},
    )
    assert plan["blocked"] is None, (
        f"blocked at a 10-row limit while only 5 rows are appliable: {plan['blocked']}"
    )
    assert plan["totals"]["changing"] == 30, "the full match count must still be reported"
    assert len(plan["approved_ids"]) == 5


def test_the_row_limit_still_blocks_a_genuinely_broad_rule():
    """The other half: the guard must still fire when the rows really would be sent."""
    rows = [_row(f"K{index}", bid=10.0) for index in range(30)]
    plan = logic.plan_run(
        rows, **RULE, applied_today={}, today="2026-08-31",
        guardrails={"max_rows": 10, "max_bid": 60.0, "min_bid": 1.0, "max_change_pct": 25.0},
    )
    assert plan["blocked"], "a 30-row run under a 10-row limit was allowed"
    assert "30" in plan["blocked"], "the message does not name the size that was refused"
```

- [ ] **Step 2: Run to verify the first fails**

Run: `venv/Scripts/python -m pytest tests/test_ads_repeat_guard.py -q -p no:randomly -k row_limit`

Expected: `test_the_row_limit_counts_only_rows_that_can_actually_be_sent` FAILS (blocked is set);
the second passes already.

- [ ] **Step 3: Move the check onto the appliable rows**

In `app/ads/logic.py`, the `approved_ids` computation must move ABOVE the `blocked` check, then:

```python
    # **The default selection, computed HERE rather than in the template.** (existing comment)
    approved_ids = [c["entity_id"] for c in changes if not c["changed_today"]]

    # The row limit bounds what will actually be SENT, so it counts appliable rows — not rows the
    # once-per-day guard has already unticked.
    #
    # Measured: a real rule held 109 changes of which 105 had already moved today, leaving 4
    # appliable. Counting all 109 would refuse a run that could only ever send 4, and the message
    # would name a number corresponding to nothing that would happen. `/ads/apply` enforces the same
    # limit again on what is actually approved, so nothing is loosened by this — the ceiling still
    # applies to every row that goes to Amazon.
    #
    # The MESSAGE still names the full match count, because that is what the owner sees on screen and
    # a refusal citing a smaller number than the table shows would read as a different bug.
    blocked = None
    if len(approved_ids) > limits["max_rows"]:
        blocked = (
            f"This rule matches {len(changes):,} rows and the limit is "
            f"{limits['max_rows']:,} per run. Narrow it with another condition, or scope it to "
            f"fewer campaigns."
        )
```

- [ ] **Step 4: Run to verify both pass, then the whole suite**

```bash
venv/Scripts/python -m pytest tests/test_ads_repeat_guard.py -q -p no:randomly
venv/Scripts/python -m pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/ads/logic.py tests/test_ads_repeat_guard.py
git commit -m "fix(ads): the row limit counts rows that can actually be sent

Measured on a real rule: 109 changing, 105 already changed today, 4 appliable.
A rule matching 1,100 rows with 1,050 already moved would have been refused for
exceeding a 1,000-row limit while only 50 rows could reach Amazon.

The limit bounds what is SENT — /ads/apply enforces it again on what is
approved — so counting rows the guard has unticked measures the wrong thing.
The message still names the full match count, because that is the number on
screen."
```

---

## Task 2: Group the preview campaign → ad group → rows

**Files:**
- Modify: `app/ads/logic.py` (add `group_changes`)
- Modify: `templates/ads.html` (`renderPreview` and its listeners)
- Test: `tests/test_ads_logic.py`, `tests/test_ads_api.py`

**Interfaces:**
- Produces: `logic.group_changes(changes) -> list[dict]`, each
  `{"campaign_id", "campaign_name", "rows", "spend", "movement", "changed_today",
    "ad_groups": [{"ad_group_id", "ad_group_name", "rows", "spend", "movement", "changed_today",
                   "entity_ids": [...]}]}`
  — `rows` is a COUNT, `entity_ids` is the list. Campaigns sorted by spend descending, ad groups
  likewise within their campaign.
- The plan payload gains `"groups": [...]`. `changes` is unchanged, so nothing downstream breaks.

**Why the sums are computed in Python:** a campaign header showing spend and total bid movement is a
claim about its own rows. Computed in JavaScript it can drift from what the table shows — and this
codebase has shipped exactly that twice (Orders' "86 orders beside 87 lines", and the Portfolio parent
rows that exist to prevent it). Here the number gates a live bid change.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ads_logic.py`:

```python
# ─── Grouping a large preview ────────────────────────────────────────────────


def _change(entity_id, *, campaign, campaign_name, ad_group, ad_group_name,
            spend, old_bid, new_bid, changed_today=False):
    return {
        "entity_id": entity_id, "campaign_id": campaign, "campaign_name": campaign_name,
        "ad_group_id": ad_group, "ad_group_name": ad_group_name,
        "spend": spend, "old_bid": old_bid, "new_bid": new_bid,
        "changed_today": changed_today, "writer": logic.WRITER_KEYWORD, "match_type": "EXACT",
    }


def test_changes_group_by_campaign_then_ad_group():
    """**1,700 rows across 13 campaigns and 118 ad groups is not reviewable flat.**

    Measured on the live account for `spend < 50, bid < 40 -> +10%`. The grouped view opens at 13
    lines instead of 1,700, and MF_SP_keywords alone holds 941 rows — which is why grouping by
    campaign ALONE would not be enough either.
    """
    changes = [
        _change("A", campaign="c1", campaign_name="MF_SP_keywords", ad_group="g1",
                ad_group_name="Sattu", spend=300.0, old_bid=10.0, new_bid=11.0),
        _change("B", campaign="c1", campaign_name="MF_SP_keywords", ad_group="g1",
                ad_group_name="Sattu", spend=200.0, old_bid=20.0, new_bid=22.0),
        _change("C", campaign="c1", campaign_name="MF_SP_keywords", ad_group="g2",
                ad_group_name="Chana", spend=100.0, old_bid=5.0, new_bid=5.5),
        _change("D", campaign="c2", campaign_name="HF_SP_Keywords", ad_group="g3",
                ad_group_name="Rice", spend=50.0, old_bid=8.0, new_bid=8.8),
    ]
    groups = logic.group_changes(changes)

    assert [g["campaign_name"] for g in groups] == ["MF_SP_keywords", "HF_SP_Keywords"], (
        "campaigns are not ordered by spend, so the biggest is not first"
    )
    first = groups[0]
    assert first["rows"] == 3
    assert [a["ad_group_name"] for a in first["ad_groups"]] == ["Sattu", "Chana"]
    assert first["ad_groups"][0]["entity_ids"] == ["A", "B"]


def test_a_group_total_is_exactly_the_sum_of_its_own_rows():
    """**The invariant that stops a header contradicting its table.**

    This codebase has shipped that defect twice — the Orders tab's "86 orders beside 87 lines", and the
    Portfolio parent rows that exist to prevent it. Here the number gates a live bid change, so the
    sums are computed in the pure module rather than in JavaScript.
    """
    changes = [
        _change("A", campaign="c1", campaign_name="C1", ad_group="g1", ad_group_name="G1",
                spend=300.0, old_bid=10.0, new_bid=11.0),
        _change("B", campaign="c1", campaign_name="C1", ad_group="g1", ad_group_name="G1",
                spend=200.5, old_bid=20.0, new_bid=18.0),
        _change("C", campaign="c1", campaign_name="C1", ad_group="g2", ad_group_name="G2",
                spend=100.25, old_bid=5.0, new_bid=5.5),
    ]
    group = logic.group_changes(changes)[0]

    assert group["spend"] == pytest.approx(600.75)
    assert group["spend"] == pytest.approx(sum(a["spend"] for a in group["ad_groups"]))
    assert group["rows"] == sum(a["rows"] for a in group["ad_groups"])
    # Movement is the NET change to the bids, which is what the header is claiming.
    assert group["movement"] == pytest.approx(1.0 - 2.0 + 0.5)
    assert group["movement"] == pytest.approx(sum(a["movement"] for a in group["ad_groups"]))


def test_a_group_counts_its_rows_changed_today():
    """So a campaign header can say why some of its rows arrive unticked, without expanding it."""
    changes = [
        _change("A", campaign="c1", campaign_name="C1", ad_group="g1", ad_group_name="G1",
                spend=10.0, old_bid=10.0, new_bid=11.0, changed_today=True),
        _change("B", campaign="c1", campaign_name="C1", ad_group="g1", ad_group_name="G1",
                spend=10.0, old_bid=10.0, new_bid=11.0),
    ]
    group = logic.group_changes(changes)[0]
    assert group["changed_today"] == 1
    assert group["ad_groups"][0]["changed_today"] == 1


def test_a_row_with_no_ad_group_is_grouped_rather_than_dropped():
    """A report row can arrive without an ad group id, and a dropped row is an unreviewable change.

    The same rule the whole preview follows: excluded and named, never silently absent.
    """
    changes = [
        _change("A", campaign="c1", campaign_name="C1", ad_group="", ad_group_name="",
                spend=10.0, old_bid=10.0, new_bid=11.0),
    ]
    groups = logic.group_changes(changes)
    assert groups[0]["rows"] == 1
    assert groups[0]["ad_groups"][0]["rows"] == 1
    assert groups[0]["ad_groups"][0]["ad_group_name"], "the group has no label at all"


def test_grouping_never_loses_or_duplicates_a_row():
    """The property that makes the grouped view trustworthy: it is a re-arrangement, not a filter."""
    changes = [
        _change(f"K{i}", campaign=f"c{i % 3}", campaign_name=f"C{i % 3}",
                ad_group=f"g{i % 7}", ad_group_name=f"G{i % 7}",
                spend=float(i), old_bid=10.0, new_bid=11.0)
        for i in range(50)
    ]
    groups = logic.group_changes(changes)
    seen = [e for g in groups for a in g["ad_groups"] for e in a["entity_ids"]]
    assert sorted(seen) == sorted(c["entity_id"] for c in changes)
    assert len(seen) == len(set(seen)), "a row appears in two groups"
    assert sum(g["rows"] for g in groups) == len(changes)
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_ads_logic.py -q -p no:randomly -k group`

Expected: FAIL with `AttributeError: module 'app.ads.logic' has no attribute 'group_changes'`.

- [ ] **Step 3: Implement `group_changes`**

Add to `app/ads/logic.py`, after `plan_run`:

```python
#: Shown for a row whose ad group Amazon did not name. Labelled rather than dropped: a change nobody
#: can see is a change nobody can review, which is the rule the whole preview follows.
UNGROUPED_LABEL = "(no ad group)"


def group_changes(changes: Sequence[Mapping]) -> list[dict]:
    """`changes` arranged campaign -> ad group -> rows, with each level's own totals.

    **A re-arrangement, never a filter.** Every row appears exactly once and
    `sum(group["rows"]) == len(changes)`, which is what makes the grouped view trustworthy — a
    collapsed header that hid rows would hide bid changes.

    **The totals are computed HERE, not in the template.** A campaign header showing spend and total
    bid movement is a claim about its own rows; computed in JavaScript it can drift from the table
    beneath it, and this codebase has shipped that defect twice (the Orders tab's "86 orders beside 87
    lines", and the Portfolio parent rows that exist to prevent it). Here the number gates a live bid
    change.

    Ordered by spend descending at both levels, because "where is the money" is the question a 1,700-
    row preview is being triaged for. Measured on the live account: one such rule spans 13 campaigns
    and 118 ad groups, and its largest campaign alone holds 941 rows — which is why the second level
    exists at all.

    `movement` is the NET rupee change to the bids in that group: what the header is actually claiming.
    """
    by_campaign: dict[str, dict] = {}

    for change in changes:
        campaign_id = str(change.get("campaign_id") or "")
        campaign = by_campaign.setdefault(campaign_id, {
            "campaign_id": campaign_id,
            "campaign_name": change.get("campaign_name") or campaign_id or UNGROUPED_LABEL,
            "_groups": {},
        })
        group_id = str(change.get("ad_group_id") or "")
        group = campaign["_groups"].setdefault(group_id, {
            "ad_group_id": group_id,
            "ad_group_name": change.get("ad_group_name") or group_id or UNGROUPED_LABEL,
            "entity_ids": [],
            "spend": 0.0,
            "movement": 0.0,
            "changed_today": 0,
        })
        group["entity_ids"].append(change["entity_id"])
        group["spend"] += _as_float(change.get("spend"))
        group["movement"] += (_as_float(change.get("new_bid"))
                              - _as_float(change.get("old_bid")))
        if change.get("changed_today"):
            group["changed_today"] += 1

    out = []
    for campaign in by_campaign.values():
        groups = sorted(campaign.pop("_groups").values(), key=lambda g: -g["spend"])
        for group in groups:
            group["rows"] = len(group["entity_ids"])
            group["spend"] = round(group["spend"], 2)
            group["movement"] = round(group["movement"], 2)
        out.append({
            **campaign,
            "ad_groups": groups,
            # Rolled up from the ad groups, which are rolled up from the rows — so the two levels
            # cannot disagree with each other or with the table.
            "rows": sum(g["rows"] for g in groups),
            "spend": round(sum(g["spend"] for g in groups), 2),
            "movement": round(sum(g["movement"] for g in groups), 2),
            "changed_today": sum(g["changed_today"] for g in groups),
        })
    out.sort(key=lambda c: -c["spend"])
    return out
```

Then include it in `plan_run`'s return, beside `changes`:

```python
        # The same rows arranged for review. `changes` is unchanged, so every existing consumer —
        # `/ads/apply`, the ledger, the tests — is untouched.
        "groups": group_changes(changes),
```

- [ ] **Step 4: Run to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_ads_logic.py -q -p no:randomly`

Expected: all pass.

- [ ] **Step 5: Render the three levels**

In `templates/ads.html`, add the open-set beside `openCampaigns`:

```javascript
/* Which preview groups are expanded. Two sets rather than one, because a campaign and an ad group can
   share neither id space nor meaning — and a collapsed campaign must remember which of its ad groups
   were open when it is re-opened. */
let openPreviewCampaigns = new Set();
let openPreviewGroups = new Set();
```

Replace the flat `changes.map(...)` in `renderPreview` with a grouped render. Campaign rows carry
`data-pcampaign`, ad group rows `data-pgroup`, and each has a tri-state checkbox
(`data-ptick-campaign` / `data-ptick-group`) plus the group's own totals. Keep the existing per-row
markup exactly as it is — only its position changes, nested under its ad group and rendered when that
group is open.

A campaign line shows: caret, name, SP/SB tag, `N rows`, spend, net movement, and `N changed today`
when that count is non-zero. An ad group line shows the same one level in.

**Collapsed by default:** `openPreviewCampaigns` starts empty on every new preview, so 1,700 rows open
as 13 lines. Add to `runPreview`, beside the `approved` assignment:

```javascript
    /* Every new preview starts collapsed: the point of grouping is that 1,700 rows open as 13 lines.
       Carrying the previous expansion over would re-open groups belonging to a rule that no longer
       exists. */
    openPreviewCampaigns = new Set();
    openPreviewGroups = new Set();
```

- [ ] **Step 6: Wire hierarchical ticking**

In the `preview-area` click listener, before the existing handlers:

```javascript
  /* Expand / collapse. Separate from ticking: clicking a row's checkbox must not also open it, and
     clicking the name must not change what will be sent to Amazon. */
  const pc = e.target.closest("[data-pcampaign]");
  if(pc && !e.target.closest("input")){
    const id = pc.dataset.pcampaign;
    if(openPreviewCampaigns.has(id)) openPreviewCampaigns.delete(id);
    else openPreviewCampaigns.add(id);
    renderPreview({fetchSuggestions: false});
    return;
  }
  const pg = e.target.closest("[data-pgroup]");
  if(pg && !e.target.closest("input")){
    const id = pg.dataset.pgroup;
    if(openPreviewGroups.has(id)) openPreviewGroups.delete(id);
    else openPreviewGroups.add(id);
    renderPreview({fetchSuggestions: false});
    return;
  }
```

And in the `change` listener, bulk ticking:

```javascript
  /* Ticking a campaign or ad group edits the SAME `approved` set the rows use. One source of truth:
     two selections that can disagree is how a row gets sent that nobody ticked.

     **A row the once-per-day guard unticked stays unticked.** Bulk selection must not silently
     re-enable the compounding the guard exists to prevent — the owner can still tick that row
     individually, which is a deliberate act. */
  const bulk = e.target.closest("[data-ptick-campaign], [data-ptick-group]");
  if(bulk){
    const isCampaign = bulk.hasAttribute("data-ptick-campaign");
    const id = isCampaign ? bulk.dataset.ptickCampaign : bulk.dataset.ptickGroup;
    const ids = previewEntityIds(id, isCampaign);
    ids.forEach(entityId => {
      if(bulk.checked){
        const change = (plan.changes || []).find(c => c.entity_id === entityId);
        if(change && !change.changed_today) approved.add(entityId);
      } else {
        approved.delete(entityId);
      }
    });
    renderPreview({fetchSuggestions: false});
    return;
  }
```

with the helper:

```javascript
/* The entity ids under one campaign or ad group, read from the server's own grouping so the screen and
   the plan cannot disagree about which rows a header covers. */
function previewEntityIds(id, isCampaign){
  const groups = (plan && plan.groups) || [];
  if(isCampaign){
    const campaign = groups.find(g => g.campaign_id === id);
    return campaign ? campaign.ad_groups.flatMap(a => a.entity_ids) : [];
  }
  for(const campaign of groups){
    const group = campaign.ad_groups.find(a => a.ad_group_id === id);
    if(group) return group.entity_ids;
  }
  return [];
}
```

Note the per-row `change` handler must keep working: it updates `[data-apply-count]` in place today to
avoid rebuilding 1,700 checkboxes. Leave that alone — only the bulk path re-renders, and it must,
because the parent checkboxes change state.

- [ ] **Step 7: Tri-state parent checkboxes**

A campaign whose rows are partly ticked must not look fully ticked. In the campaign/ad-group markup:

```javascript
    const ids = group.entity_ids || [];
    const tickedCount = ids.filter(i => approved.has(i)).length;
    const allTicked = ids.length > 0 && tickedCount === ids.length;
    const someTicked = tickedCount > 0 && !allTicked;
```

and render `checked` when `allTicked`, setting `indeterminate` after insertion — the attribute does not
exist in HTML, so it must be set on the element:

```javascript
/* `indeterminate` is a PROPERTY, not an attribute — there is no way to express it in markup, so it is
   applied after the table is inserted. Without it a campaign with 3 of 941 rows ticked looks
   completely unticked, which invites ticking the whole thing. */
function applyIndeterminate(){
  document.querySelectorAll("[data-partial]").forEach(box => {
    box.indeterminate = box.dataset.partial === "1";
  });
}
```

called at the end of `renderPreview`.

- [ ] **Step 8: Pin the screen behaviour**

Append to `tests/test_ads_api.py`:

```python
def test_the_preview_groups_by_campaign_and_ad_group():
    """**1,700 rows across 13 campaigns and 118 ad groups is not reviewable flat.**

    Measured on the live account. The grouped view opens at 13 lines, and grouping by campaign alone
    would not be enough — MF_SP_keywords alone holds 941 rows.
    """
    source = _code_only(_template())
    assert "plan.groups" in source, "the preview ignores the server's grouping"
    assert "data-pcampaign" in source and "data-pgroup" in source, "there are no group rows"
    assert "openPreviewCampaigns" in source and "openPreviewGroups" in source


def test_a_new_preview_starts_collapsed():
    """The whole point is that 1,700 rows open as 13 lines. Carrying expansion over from a previous
    rule would re-open groups belonging to a rule that no longer exists."""
    body = _js_function(_code_only(_template()), "runPreview")
    assert "openPreviewCampaigns = new Set()" in body, "the previous expansion is carried over"


def test_bulk_ticking_edits_the_same_approved_set():
    """ONE source of truth for what will be sent. Two selections that can disagree is how a row gets
    sent that nobody ticked."""
    source = _code_only(_template())
    assert "function previewEntityIds(" in source, "the ids come from somewhere other than the plan"
    assert "approved.add(entityId)" in source and "approved.delete(entityId)" in source


def test_bulk_ticking_cannot_re_enable_a_row_changed_today():
    """**The guard must survive a bulk gesture.**

    Ticking a campaign of 941 rows must not silently re-enable the compounding for the ones that
    already moved today. The owner can still tick such a row individually — that is deliberate.
    """
    source = _code_only(_template())
    assert "!change.changed_today" in source, (
        "ticking a campaign re-enables rows the once-per-day guard unticked"
    )


def test_a_partly_ticked_group_shows_as_indeterminate():
    """A campaign with 3 of 941 rows ticked must not look unticked — that invites ticking all 941.

    `indeterminate` is a property with no markup form, so it has to be applied after insertion.
    """
    source = _code_only(_template())
    assert "function applyIndeterminate(" in source
    assert ".indeterminate = " in source, "the tri-state is never actually set"
    assert "data-partial" in source
```

- [ ] **Step 9: Verify the JS parses, then the suite**

```bash
node -e "const fs=require('fs');let s=fs.readFileSync('templates/ads.html','utf8');const m=[...s.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(x=>x[1]).join('\n').replace(/\{\{[\s\S]*?\}\}/g,'0').replace(/\{%[\s\S]*?%\}/g,'');fs.writeFileSync('ads_check.js',m);" && node --check ads_check.js && echo "JS OK" && rm -f ads_check.js
venv/Scripts/python -m pytest -q
```

- [ ] **Step 10: Verify in a browser against the real 1,700-row rule**

Drive `spend < 50, bid < 40 → +10%` over `2026-08-24..2026-08-30` and confirm:

- it opens as **13 campaign lines**, not 1,700 rows;
- MF_SP_keywords reads **941 rows** and its spend equals the sum of its ad groups;
- expanding it lists ad groups; expanding one lists keywords with the Match, Suggested and bid columns
  intact;
- ticking a campaign selects its rows and the Apply count matches `approved.size`;
- unticking one row turns its campaign box indeterminate;
- the totals in a campaign header equal the sum of the rows beneath it.

- [ ] **Step 11: Commit**

```bash
git add app/ads/logic.py templates/ads.html tests/
git commit -m "feat(ads): group the preview by campaign and ad group

A real rule matched 1,700 rows across 13 campaigns and 118 ad groups, which is
not reviewable as a flat list — and grouping by campaign alone would not help,
since MF_SP_keywords holds 941 of them.

The totals are computed in logic.group_changes rather than in JavaScript: a
campaign header showing spend and net bid movement is a claim about its own
rows, and this codebase has shipped a header disagreeing with its table twice.
Grouping is a re-arrangement, never a filter — a test asserts no row is lost or
duplicated.

Ticking a campaign edits the same `approved` set the rows use, and cannot
re-enable a row the once-per-day guard unticked."
```

---

## Task 3: Docs

- [x] **Step 1: Run the mutations** — 11 written, all caught. One survivor first time round (a campaign total recomputed run-wide) because the fixture had a single campaign; fixed by adding a second whose movement has the opposite sign. Harness: `scripts/mutate_grouping.py`.

| Mutation | Must fail |
|---|---|
| `blocked` counts `changes` again | `test_the_row_limit_counts_only_rows_that_can_actually_be_sent` |
| `blocked` never set | `test_the_row_limit_still_blocks_a_genuinely_broad_rule` |
| `group_changes` sorts ascending by spend | `test_changes_group_by_campaign_then_ad_group` |
| campaign totals summed from `changes` not `ad_groups` | `test_a_group_total_is_exactly_the_sum_of_its_own_rows` |
| rows with no ad group skipped | `test_a_row_with_no_ad_group_is_grouped_rather_than_dropped` |
| bulk tick drops the `changed_today` check | `test_bulk_ticking_cannot_re_enable_a_row_changed_today` |
| `runPreview` keeps the previous expansion | `test_a_new_preview_starts_collapsed` |

- [x] **Step 2: CLAUDE.md**

Record: the 1,700 / 13 / 118 measurement and why two levels rather than one; that group sums are
computed in the pure module and why; that bulk ticking cannot override the once-per-day guard; the
row-limit basis change with its measured example; and — importantly — **the zero-ROAS warning that was
proposed and rejected**, with the click distribution that refuted it (1,107 rows at zero clicks, none
above 10), because that misreading would be just as wrong on the Portfolio tab.

- [ ] **Step 3: Deploy**

```bash
git push origin claude/stoic-allen-bb3a55
ssh -i "<key>" ubuntu@13.233.144.148 "cd /opt/amazon-tracker && git fetch -q origin claude/stoic-allen-bb3a55 && git checkout origin/claude/stoic-allen-bb3a55 -- deploy/update-ec2.sh"
ssh -i "<key>" ubuntu@13.233.144.148 "cd /opt/amazon-tracker && bash deploy/update-ec2.sh"
```

No migration in this change, but the script is still checked out first — it has replaced itself
mid-deploy and failed on its own stale contents once already.

## Out of scope

The zero-ROAS warning (rejected above, with the measurement). Sorting groups by anything other than
spend. Persisting expansion across previews. Any change to `/ads/apply`'s own limit enforcement.
