"""Mutation harness for the pause/enable action. Throwaway; not imported by the app.

Each entry breaks ONE decision from
`docs/superpowers/specs/2026-09-03-ads-pause-keywords-design.md` and names the test that must catch
it.

Two of these were added AFTER the feature shipped broken: `/ads/apply` keyed the run on the client's
`action` field, which the screen does not send, so every pause returned "no usable bid". 20 passing
tests verified the server contract and none checked that the client honours it.

**The bar for this feature is mutation testing, not a green suite.** CLAUDE.md records five separate
cases in this feature area alone of a bug shipping past a fully green suite — including the original
Sponsored Brands 401, which ran for a week.

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
        "ARCHIVED becomes writable, so a rule could permanently archive keywords",
        LOGIC,
        "WRITABLE_STATES = (STATE_PAUSED, STATE_ENABLED)",
        'WRITABLE_STATES = (STATE_PAUSED, STATE_ENABLED, "ARCHIVED")',
        "test_archived_is_not_a_writable_state",
    ),
    (
        "the pause spend floor is ignored, so `spend>10` pauses hundreds of healthy keywords",
        LOGIC,
        '            if _as_float(m.get("spend")) < limits["min_pause_spend"]:',
        "            if False:",
        "test_a_row_below_the_pause_spend_floor_is_skipped_and_named",
    ),
    (
        "an SB state write is sent UPPER case, refused per-row inside a 207 that says success",
        LOGIC,
        "    return wanted.lower() if ad_product == AD_PRODUCT_SB else wanted",
        "    return wanted",
        "test_sb_states_are_lower_case_and_sp_states_are_upper",
    ),
    (
        "a state run keeps the bid-drift refusal, so a nudged bid blocks a pause",
        ROUTER,
        "                if target_state is not None:",
        "                if False:",
        "test_a_pause_is_not_refused_because_someone_moved_the_bid",
    ),
    (
        "an already-paused row is sent to Amazon anyway, as a pointless write in the ledger",
        ROUTER,
        "                    if live_state == target_state:",
        "                    if False:",
        "test_an_already_paused_row_is_reported_unchanged_and_not_sent",
    ),
    (
        "build_undo keeps its blanket null-old_bid skip, so undoing a pause reverses NOTHING",
        REPO,
        '        if r.action == "state":',
        "        if False:",
        "test_undo_of_a_pause_is_an_enable",
    ),
    (
        "last_applied_states filters new_bid, so the day guard is silently inert",
        REPO,
        "                AdsMutation.new_state.is_not(None),",
        "                AdsMutation.new_bid.is_not(None),",
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
        "an SP state write ALSO carries a bid, applying a change nobody previewed",
        WRITER,
        '        row["state"] = normalise_state(change["new_state"], AD_PRODUCT_SP)\n    else:\n        row["bid"] = round(float(change["new_bid"]), 2)',
        '        row["state"] = normalise_state(change["new_state"], AD_PRODUCT_SP)\n    if True:\n        row["bid"] = round(float(change.get("new_bid") or 0), 2)',
        "test_an_sp_pause_sends_state_and_no_bid",
    ),
    (
        "open_run stops recording the ad product, mislabelling every SB row as sp",
        REPO,
        "            ad_product=change.get(\"ad_product\") or logic.AD_PRODUCT_SP,",
        "            ad_product=logic.AD_PRODUCT_SP,",
        "test_open_run_records_the_ad_product_it_was_given",
    ),
    (
        "the ledger labels a pause as a bid change, so undo cannot tell them apart",
        REPO,
        '                    "state" if change.get("new_state") else "bid"),',
        '                    "bid"),',
        "test_open_run_records_a_state_change_and_its_action",
    ),
    (
        "the screen coerces the state to a number again (Number('PAUSED') is NaN -> null)",
        HTML,
        '  return isStateRun() ? $("state-amount").value : Number($("amount").value);',
        '  return Number($("amount").value);',
        "test_the_state_value_is_never_coerced_to_a_number",
    ),
    (
        "the scheduler guard goes back to the retired literal, passing vacuously",
        SCHED_TEST,
        'for forbidden in ("apply_changes", "plan_run", "open_run", "/apply"):',
        'for forbidden in ("apply_bids", "plan_run", "open_run", "/apply"):',
        "test_apply_bids_is_gone_and_the_scheduler_guard_names_the_new_function",
    ),
    (
        "apply keys the run on the client's `action` again, so the screen's payload 400s "
        "(the reported 'no usable bid' bug)",
        ROUTER,
        "    if logic.is_state_action(action) or states:",
        "    if logic.is_state_action(action):",
        "test_apply_infers_a_state_run_from_the_rows_when_the_action_is_absent",
    ),
    (
        "a mixed bid/state payload is half-applied instead of refused",
        ROUTER,
        "    if states and bids:",
        "    if False:",
        "test_a_mixed_payload_is_refused_rather_than_half_applied",
    ),
    (
        "save_rule puts the state back in the numeric amount column (the 500 on save)",
        REPO,
        "        amount, target_state = None, str(amount).strip().upper()",
        "        target_state = str(amount).strip().upper()",
        "test_the_two_kinds_are_stored_in_different_columns",
    ),
    (
        "load_rules coerces a saved state through _f(), so a pause rule loads with no state",
        REPO,
        '"amount": r.target_state if r.target_state else _f(r.amount),',
        '"amount": _f(r.amount),',
        "test_a_pause_rule_can_be_saved_and_read_back",
    ),
    (
        "the per-rule guard holds a keyword back for ANY rule's change today",
        LOGIC,
        '            if rule is not None and entry.get("rule") == rule:',
        '            if True:',
        'test_a_different_rule_may_change_it_and_says_what_happened_earlier',
    ),
    (
        'the per-rule guard never holds back the same rule',
        LOGIC,
        '        if same:\n            return True,',
        '        if False:\n            return True,',
        'test_the_same_rule_is_held_back_on_a_keyword_it_changed_today',
    ),
    (
        'apply lets the same rule through twice in a day',
        ROUTER,
        'if e["rule"] == rule_summary]',
        'if e["rule"] == rule_summary and False]',
        'test_apply_refuses_the_same_rule_twice',
    ),
    (
        'an undone change still blocks the rule',
        REPO,
        '                AdsMutation.status == "applied",\n                AdsMutation.created_at >= start,',
        '                AdsMutation.status.in_(("applied", "reverted")),\n                AdsMutation.created_at >= start,',
        'test_a_change_that_was_undone_does_not_block_the_rule or test_changes_today_is_the_ist_day',
    ),
    (
        'the rule sentence keeps 10.0 apart from 10, so a saved rule escapes its own limit',
        ROUTER,
        '        return f"{float(value):g}"',
        '        return str(value)',
        'test_the_same_rule_always_writes_the_same_sentence',
    ),
    (
        'a new rule silently overwrites one with the same name',
        REPO,
        '        if existing is not None and not rule.get("replace"):',
        '        if False:',
        'test_a_taken_name_is_refused_unless_replacing',
    ),
    (
        'the last-active guard is off, so a rule can silence an ad group',
        LOGIC,
        '        if not pausing or enabled - pausing:',
        '        if True:',
        'test_the_last_active_sku_keeps_the_best_seller_on or test_the_plan_lists_the_last_active',
    ),
    (
        'an ad group with unknown siblings is assumed to have others, so its last SKU goes',
        LOGIC,
        '        enabled = {str(e) for e in enabled} if enabled is not None else set(ids)',
        "        enabled = {str(e) for e in enabled} if enabled is not None else set(ids) | {'other'}",
        'test_an_ad_group_with_unknown_siblings_keeps_one_on',
    ),
    (
        "apply trusts the morning's list and skips the live last-active check",
        ROUTER,
        '                refused = logic.last_active_refusals(to_send, enabled_by_group)',
        '                refused = set()',
        'test_apply_checks_the_last_active_sku_again_on_live_states',
    ),
    (
        'the SKU channel becomes a substring test, so `fbagel 1kg` reads as FBA',
        LOGIC,
        '    if last == FBA_SKU_SUFFIX:',
        "    if FBA_SKU_SUFFIX in str(sku or '').upper():",
        'test_the_sku_channel_is_read_off_its_last_token',
    ),
    (
        'Flex SKUs read as Easy Ship',
        LOGIC,
        '    if last == "FLEX":',
        '    if last == "FLEXX":',
        'test_the_sku_channel_is_read_off_its_last_token or test_two_skus_of_one_asin',
    ),
    (
        "a product rule writes the keyword rule's sentence, so the two share a once-a-day limit",
        ROUTER,
        '    prefix = "Products: " if level == logic.LEVEL_PRODUCT else ""',
        '    prefix = ""',
        'test_the_same_numbers_at_the_two_levels_are_two_different_rules',
    ),
    (
        'a pause applied since the morning is ignored by the preview guard',
        REPO,
        '            state = entry["state"]',
        '            pass',
        'test_the_cached_list_is_overridden_by_a_pause_we_applied_since',
    ),
    (
        'an empty product-ad fetch wipes the cached list',
        REPO,
        '    rows = [r for r in rows or () if r.get("entity_id")]\n    if not rows:\n        return 0',
        '    rows = [r for r in rows or () if r.get("entity_id")]',
        'test_the_cached_list_is_overridden_by_a_pause_we_applied_since',
    ),
    (
        'a product rule may change a bid',
        LOGIC,
        '    if level == LEVEL_PRODUCT and not is_state_action(action):',
        '    if False:',
        'test_a_product_rule_cannot_change_a_bid_and_a_keyword_rule_has_no_sku',
    ),
    (
        'SKUs and keywords may share one run',
        ROUTER,
        '    if products and len(products) != len(approved):',
        '    if False:',
        'test_apply_refuses_skus_mixed_with_keywords_or_given_a_bid',
    ),
    (
        'product rows are never purged',
        REPO,
        '    products = await db.execute(delete(AdsProductDaily).where(AdsProductDaily.day < cutoff))',
        '    products = result',
        'test_the_purge_keeps_product_rows_for_the_same_60_days',
    ),
    (
        'a day split in the report keeps only one half',
        REPO,
        '        row["spend"] = round(row["spend"] + logic._as_float(raw.get("cost")), 2)',
        '        row["spend"] = round(logic._as_float(raw.get("cost")), 2)',
        'test_a_day_split_in_the_report_is_summed',
    ),
    (
        'the page never sends the level, so every SKU rule previews as a keyword rule',
        HTML,
        '        level: level,\n',
        '',
        'test_the_page_offers_the_level_and_posts_it',
    ),
    (
        'the product-ad write goes to the keyword endpoint',
        WRITER,
        '        path, head_type, id_field = PRODUCT_ADS[0], PRODUCT_ADS[1], "adId"',
        '        path, head_type, id_field = KEYWORDS[0], KEYWORDS[1], "adId"',
        'test_the_product_ad_write_is_the_sp_shape_keyed_by_adId',
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
            # A hang is a SURVIVOR, not an inconclusive result: an unbounded loop is exactly what a
            # missing bound looks like.
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
