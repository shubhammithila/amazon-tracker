# Ads per-rule guard and saved-rules panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One rule may change a keyword once per IST day while other rules may still change it, and
saved rules can be used, edited, copied and deleted from a proper panel.

**Architecture:** A rule's identity is the sentence `routers.ads._rule_summary` writes, already stored
on every `ads_mutation` row. `repository.changes_today` returns today's applied rows per entity;
`logic.plan_run(rule=…, today_changes=…)` guards on the same sentence and notes other rules. The
route `POST /ads/rules` gains `id` (update/rename) and `replace`. `templates/ads.html` replaces the
chip bar and `window.prompt` with a list panel and an inline save area.

**Tech Stack:** FastAPI, SQLAlchemy async, vanilla JS, pytest.

## Global Constraints
- Spec: `docs/superpowers/specs/2026-10-22-ads-rules-per-rule-guard-design.md`. No migration.
- Only `applied` ledger rows count; `reverted` rows (undone) never block.
- The day is `logic.ist_day` (IST), never the UTC date.
- `plan_run` without `today_changes` keeps its old any-change-blocks behaviour.
- Apply re-checks the guard server-side; the client is not a trust boundary.

---

### Task 1: The per-rule guard
**Files:** `app/ads/repository.py` (new `changes_today`), `app/ads/logic.py` (`plan_run` gains
`rule`, `today_changes`; new `SKIP` note text), `app/routers/ads.py` (preview + apply pass them; apply
stamps `AdsRule.last_run_at` for saved rules whose sentence matches), `tests/test_ads_rule_guard.py`.

**Interfaces:**
- `repository.changes_today(db, entity_ids, day) -> {entity_id: [{"rule", "at", "bid_from", "bid_to", "state"}]}`
  (applied rows whose `ist_day(created_at) == day`, oldest first).
- `logic.plan_run(..., rule: str | None = None, today_changes: Mapping | None = None)`: with
  `today_changes`, `changed_today` is True only when an entry's `rule == rule`; every other entry is
  listed in the change's `other_today` as `{"rule", "at", "what"}`.
- `logic.describe_change(entry) -> str`: `"bid ₹10.00 → ₹11.00"` or `"turned OFF"` / `"turned ON"`.

Tests (write first, see them fail, then implement):
- same rule changed K1 today → `changed_today`, not in `approved_ids`;
- a different rule changed K1 today → approved, `other_today[0]["rule"]` is that rule;
- a `reverted` same-rule row does not block;
- `changes_today` ignores yesterday (IST) and non-applied rows;
- apply (route) with a same-rule repeat returns it in `repeated`; with a different rule it proceeds
  (asserted with the Amazon client faked, as the existing apply tests do);
- old call without `today_changes` still blocks on any change today (existing tests stay green).

### Task 2: Saved rules — server
**Files:** `app/ads/repository.py` (`save_rule(db, name, rule)` honours `rule["id"]` and
`rule["replace"]`, raising `RuleNameTaken` (a `ValueError` subclass) for a clash), `app/routers/ads.py`
(`POST /ads/rules` returns 409 on `RuleNameTaken`), tests in `tests/test_ads_rule_guard.py`.

Tests: rename by id keeps one row; a new rule with a taken name → 409; with `replace: true` → 200
and one row; an id that does not exist → 404.

### Task 3: Saved rules — page
**Files:** `templates/ads.html`, tests in `tests/test_ads_rule_guard.py`.
- `#saved-bar` becomes a list (`renderSaved`): per rule a row with name, `ruleWords(r)`, last run, and
  buttons `data-use`, `data-edit`, `data-copy`, `data-delete`; a `#rule-search` box when > 8 rules.
- `#save-area` under the builder: `#rule-name` input; new mode → `Save`; edit mode → "Editing: name",
  `Save changes`, `Save as new`, `Cancel`. `editing = {id, name}` state; Cancel restores the builder
  snapshot taken when Edit was pressed.
- Delete uses `confirm()` naming the rule. No `window.prompt` anywhere.
- Preview rows render `other_today` as a note ("bid changed today at 10:42 by …").
Tests: no `window.prompt(` in the template; the four `data-` buttons and `#rule-name` exist;
`other_today` is rendered.

### Task 4: Verify and ship
Browser check (save, edit+rename, copy, delete, two rules previewed back to back); full suite;
`scripts/mutate_ads_state.py` plus new mutations; CLAUDE.md; commit; deploy; production check.
