"""Mutation harness for the preview-grouping change. Throwaway; not imported by the app.

Each entry breaks ONE decision the change makes and names the test that must catch it. A mutation
that survives means the test asserts a conclusion rather than the reason for it — which has already
happened three times in the Ads feature, so this is run rather than reasoned about.

    venv/Scripts/python scripts/mutate_grouping.py
"""
import pathlib
import subprocess
import sys

LOGIC = pathlib.Path("app/ads/logic.py")
TEMPLATE = pathlib.Path("templates/ads.html")

MUTATIONS = [
    (
        "blocked counts every changing row again, not the appliable ones",
        LOGIC,
        "if len(approved_ids) > limits[\"max_rows\"]:",
        "if len(changes) > limits[\"max_rows\"]:",
        "test_the_row_limit_counts_only_rows_that_can_actually_be_sent",
    ),
    (
        "blocked is never set at all",
        LOGIC,
        "if len(approved_ids) > limits[\"max_rows\"]:",
        "if False and len(approved_ids) > limits[\"max_rows\"]:",
        "test_the_row_limit_still_blocks_a_genuinely_broad_rule",
    ),
    (
        "campaigns sort ASCENDING by spend, so the biggest spender lands last",
        LOGIC,
        "out.sort(key=lambda c: -c[\"spend\"])",
        "out.sort(key=lambda c: c[\"spend\"])",
        "test_changes_group_by_campaign_then_ad_group",
    ),
    (
        "ad groups sort ASCENDING inside a campaign",
        LOGIC,
        "key=lambda g: -g[\"spend\"])",
        "key=lambda g: g[\"spend\"])",
        "test_changes_group_by_campaign_then_ad_group",
    ),
    (
        "campaign ROW COUNT recomputed run-wide instead of rolled up from its ad groups",
        LOGIC,
        "\"rows\": sum(g[\"rows\"] for g in groups),",
        "\"rows\": len(changes),",
        "test_a_group_total_is_exactly_the_sum_of_its_own_rows",
    ),
    (
        "campaign SPEND recomputed run-wide",
        LOGIC,
        "\"spend\": round(sum(g[\"spend\"] for g in groups), 2),",
        "\"spend\": round(sum(_as_float(c.get(\"spend\")) for c in changes), 2),",
        "test_a_group_total_is_exactly_the_sum_of_its_own_rows",
    ),
    (
        "campaign MOVEMENT recomputed run-wide",
        LOGIC,
        "\"movement\": round(sum(g[\"movement\"] for g in groups), 2),",
        "\"movement\": round(sum(_as_float(c.get(\"new_bid\")) - _as_float(c.get(\"old_bid\"))\n"
        "                                for c in changes), 2),",
        "test_a_group_total_is_exactly_the_sum_of_its_own_rows",
    ),
    (
        "a row with no ad group is DROPPED instead of grouped",
        LOGIC,
        "        group_id = str(change.get(\"ad_group_id\") or \"\")",
        "        group_id = str(change.get(\"ad_group_id\") or \"\")\n"
        "        if not group_id:\n"
        "            continue",
        "test_a_row_with_no_ad_group_is_grouped_rather_than_dropped",
    ),
    (
        "bulk ticking a campaign re-enables the rows the once-per-day guard unticked",
        TEMPLATE,
        "if(change && !change.changed_today) approved.add(entityId);",
        "approved.add(entityId);",
        "test_bulk_ticking_cannot_re_enable_a_row_changed_today",
    ),
    (
        "Select all re-enables them too",
        TEMPLATE,
        "const appliable = ((plan && plan.changes) || []).filter(c => !c.changed_today);\n"
        "    if(approved.size >= appliable.length) approved.clear();\n"
        "    else approved = new Set(appliable.map(c => c.entity_id));",
        "const appliable = (plan && plan.changes) || [];\n"
        "    if(approved.size >= appliable.length) approved.clear();\n"
        "    else approved = new Set(appliable.map(c => c.entity_id));",
        "test_select_all_cannot_re_enable_a_row_changed_today_either",
    ),
    (
        "a new preview keeps whatever was expanded before it",
        TEMPLATE,
        "    openPreviewCampaigns = new Set();\n    openPreviewGroups = new Set();",
        "    /* mutated: expansion kept */",
        "test_a_new_preview_starts_collapsed",
    ),
]


def run(test_name):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-k", test_name, "tests"],
        capture_output=True, text=True,
    )
    return result.returncode, result.stdout.strip().splitlines()[-1] if result.stdout else ""


def main():
    survivors = []
    for label, path, old, new, test_name in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"SKIP  {label}\n      target text not found in {path}")
            survivors.append((label, "target text not found"))
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            code, summary = run(test_name)
        finally:
            path.write_text(original, encoding="utf-8")
        if code == 0:
            print(f"SURVIVED  {label}\n          {test_name} still passes -> {summary}")
            survivors.append((label, test_name))
        else:
            print(f"caught    {label}\n          {test_name} -> {summary}")

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
