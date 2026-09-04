"""Mutation harness for the pause/enable action. Throwaway; not imported by the app.

Each entry breaks ONE decision from
`docs/superpowers/specs/2026-09-03-ads-pause-keywords-design.md` and names the test that must catch
it.

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
        '            action="state" if change.get("new_state") else "bid",',
        '            action="bid",',
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
