"""Mutation harness for the three-group view, the category strip and the daily ratings scrape.

Run: ``venv/Scripts/python scripts/mutate_portfolio_groups.py``

Every mutation MUST be caught. Each one breaks ONE decision the change makes and names the test
that has to notice — a survivor means the test asserts a conclusion rather than the reason for it,
which this codebase has documented four times (the deploy detector's revision id, the SB
``daily=True`` fetch, the scheduler guard's substring check, the ads pause shipping broken thrice).

The stakes here are that a **wrong grouping recommends the wrong action on a real product**: a
SURGICAL parent filed under "Kill or monitor" with no flag invites killing something that earns
+27.1%, and an averaged category percentage reads as a measurement while belonging to no product.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LOGIC = "app/portfolio/logic.py"
ROUTER = "app/routers/portfolio.py"
SCHED = "app/scheduler.py"
TEMPLATE = "templates/portfolio.html"

#: (file, find, replace, why this would be a real bug)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ── The mapping: seven verdicts into three groups ──
    (
        LOGIC,
        "    VERDICT_SURGICAL: GROUP_MAINTAIN,",
        "    VERDICT_SURGICAL: GROUP_KILL,",
        "SURGICAL is filed under Kill or monitor, inviting a kill on a parent that earns +27.1% "
        "— test_surgical_is_maintain_not_kill",
    ),
    (
        LOGIC,
        "    VERDICT_AD_DEPENDENT: GROUP_KILL,",
        "    VERDICT_AD_DEPENDENT: GROUP_MAINTAIN,",
        "AD DEPENDENT reads as steady, so six profitable products keep burning 104-316% ACOS "
        "— test_ad_dependent_is_kill_or_monitor_but_carries_its_flag",
    ),
    (
        LOGIC,
        '    VERDICT_SURGICAL: "some sizes lose money",',
        "",
        "SURGICAL loses its flag, so in Maintain it reads as simply fine "
        "— test_exactly_the_two_odd_verdicts_carry_flags",
    ),
    (
        LOGIC,
        '    VERDICT_AD_DEPENDENT: "ads lose money on their own terms",',
        "",
        "AD DEPENDENT loses its flag, so in Kill or monitor it reads as 'kill it' when the fix is "
        "to cut the spend — test_ad_dependent_is_kill_or_monitor_but_carries_its_flag",
    ),
    (
        LOGIC,
        "    VERDICT_MONITOR: GROUP_MAINTAIN,",
        "",
        "MONITOR is unmapped, so the largest group falls through the default rather than by "
        "decision — test_every_verdict_has_exactly_one_group",
    ),
    (
        LOGIC,
        "    return VERDICT_GROUPS.get(verdict, GROUP_MAINTAIN)",
        "    return VERDICT_GROUPS[verdict]",
        "an unknown verdict RAISES instead of landing in the safe bucket, so a new verdict 500s "
        "the whole dashboard — test_an_unknown_verdict_lands_in_maintain_rather_than_vanishing",
    ),
    (
        LOGIC,
        "    counts = {group: 0 for group in GROUP_ORDER}",
        "    counts = {}",
        "a group matching nothing is absent, so its tab vanishes and the active filter has no "
        "control left to undo it — test_group_counts_covers_every_group_even_at_zero",
    ),
    (
        LOGIC,
        "GROUP_ORDER = (GROUP_SCALE, GROUP_MAINTAIN, GROUP_KILL)",
        "GROUP_ORDER = (GROUP_KILL, GROUP_MAINTAIN, GROUP_SCALE)",
        "the tabs read worst-first, which is the seven-chip WORKLIST order and the wrong question "
        "for a three-tab strip — test_there_are_exactly_three_groups",
    ),

    # ── Category sales ──
    (
        LOGIC,
        "    from app.shipment.logic import CATEGORY_LABELS",
        '    CATEGORY_LABELS = {1: "Sattu", 2: "Chana", 3: "Flours", 4: "Rice", 5: "Seeds", 6: "Rest"}',
        "the labels are a SECOND copy, so a category total can disagree with the packer's sort "
        "order — test_the_labels_come_from_the_shipment_tab_not_a_second_copy",
    ),
    (
        LOGIC,
        '        bucket["tacos"] = _ratio(bucket["ad_spend"], bucket["sales"])',
        '        bucket["tacos"] = _ratio(bucket["net"], bucket["sales"])',
        "TACOS and margin are the same figure — the copy-paste error between two adjacent lines "
        "— test_percentages_are_recomputed_from_the_sums_never_averaged",
    ),
    (
        LOGIC,
        '        bucket["sales"] += _num(parent.get("sales"))',
        '        bucket["sales"] = _num(parent.get("sales"))',
        "the LAST product in a category wins rather than the rows being summed — the shape of the "
        "FLEX SKU bug that read every warehouse as empty "
        "— test_category_totals_sum_to_the_account_total",
    ),
    (
        LOGIC,
        '        bucket["products"] += 1',
        "        pass",
        "every category reports 0 products while carrying real sales "
        "— test_category_totals_sum_to_the_account_total",
    ),
    (
        LOGIC,
        '        bucket["margin"] = _ratio(bucket["net"], bucket["sales"])',
        '        bucket["margin"] = bucket["net"] / bucket["sales"] if bucket["sales"] else 0.0',
        "a category with no sales reports 0% margin rather than a dash, ranking it as the most "
        "ad-efficient thing in the portfolio "
        "— test_a_category_with_no_sales_reports_no_tacos_rather_than_zero",
    ),
    (
        LOGIC,
        "            label = CATEGORY_UNCLASSIFIED",
        "            label = CATEGORY_LABELS[6]",
        "unclassified products are folded into Rest, which makes Rest the largest category and "
        "stops it meaning anything — test_an_unclassified_parent_is_its_own_bucket_and_is_NAMED",
    ),
    (
        LOGIC,
        "        candidates = [name] + [\n"
        "            str(size.get(\"product\") or \"\") for size in parent.get(\"sizes\") or []\n"
        "        ]",
        "        candidates = [name]",
        "only the PARENT name is matched, so 52 of 90 products go unclassified and Rs 2.33 lakh of "
        "Chana sales move into Unclassified "
        "— test_a_renamed_multi_flavour_parent_matches_on_its_SIZE_names",
    ),
    (
        LOGIC,
        "    ordered = sorted(\n        buckets.values(), key=lambda b: (-b[\"sales\"], b[\"category\"])\n    )",
        "    ordered = sorted(\n        buckets.values(), key=lambda b: (b[\"sales\"], b[\"category\"])\n    )",
        "the strip is ordered smallest-first, so 'where is the money' reads backwards "
        "— test_categories_are_ordered_biggest_first",
    ),
    (
        LOGIC,
        '        "unclassified_names": unclassified_names[:UNCLASSIFIED_SHOWN],',
        '        "unclassified_names": unclassified_names,',
        "52 names are printed as a column rather than a sentence "
        "— test_the_named_unclassified_list_is_capped_but_the_count_is_exact",
    ),
    (
        LOGIC,
        '        "unclassified_total": len(unclassified_names),',
        '        "unclassified_total": len(unclassified_names[:UNCLASSIFIED_SHOWN]),',
        "the COUNT is capped too, so '8 products have no category' is stated while 52 do "
        "— test_the_named_unclassified_list_is_capped_but_the_count_is_exact",
    ),
    (
        LOGIC,
        "        if key and key in categories:\n            return categories[key]",
        "        if key:\n            from app.shipment.logic import category_for\n"
        "            return category_for(key)",
        "a keyword GUESS is served as though it were the owner's decision, which is exactly what "
        "naming the unclassified products exists to avoid "
        "— test_the_stored_choice_is_used_not_a_keyword_guess",
    ),

    # ── The router: both views computed on the SERVER ──
    (
        ROUTER,
        '    result["group_counts"] = logic.group_counts(result["parents"])',
        '    result["group_counts"] = logic.group_counts(result["skus"])',
        "the product tabs count SKU rows, so a tab says 36 above a table showing 11 "
        "— test_the_payload_counts_each_grain_against_its_own_rows",
    ),
    (
        ROUTER,
        '    result["sku_group_counts"] = logic.group_counts(result["skus"])',
        '    result["sku_group_counts"] = logic.group_counts(result["parents"])',
        "the SKU tabs count parent rows — the same disagreement the other way "
        "— test_the_payload_counts_each_grain_against_its_own_rows",
    ),
    (
        ROUTER,
        '    result["verdict_groups"] = dict(logic.VERDICT_GROUPS)',
        "",
        "the mapping never reaches the screen, so every row falls back to Maintain and the tabs "
        "contradict the table — test_the_payload_carries_the_grouping_and_the_categories",
    ),
    (
        ROUTER,
        '    result["group_flags"] = dict(logic.GROUP_FLAGS)',
        "",
        "the two flags never reach the screen, so SURGICAL reads as healthy "
        "— test_the_payload_carries_the_grouping_and_the_categories",
    ),
    (
        ROUTER,
        "        {row.product_key: row.priority for row in category_rows},",
        "        {},",
        "no categories are passed, so every product is Unclassified and the strip is one card "
        "— test_the_category_strip_uses_the_shipment_tabs_own_classification",
    ),

    # ── The daily ratings scrape ──
    (
        SCHED,
        "        or settings.scrape_enabled",
        "",
        "the scrape flag cannot wake the scheduler at all, so the 05:00 job never registers on "
        "production — test_the_scrape_can_run_without_waking_the_keyword_track_or_the_purge",
    ),
    (
        SCHED,
        "    elif settings.scrape_enabled:",
        "    elif settings.scrape_enabled and settings.scheduler_enabled:",
        "the scrape needs the master flag after all, which is the whole bug — the ratings were six "
        "days old — test_the_scrape_can_run_without_waking_the_keyword_track_or_the_purge",
    ),
    (
        SCHED,
        "        scrape_hour, scrape_minute = ist.utc_hhmm(\n"
        "            settings.scrape_ist_hour, settings.scrape_ist_minute\n        )",
        "        scrape_hour, scrape_minute = (\n"
        "            settings.scrape_ist_hour, settings.scrape_ist_minute\n        )",
        "the IST hour reaches CronTrigger unconverted, so the scrape fires 10:30 IST — the sixth "
        "instance of that defect — test_the_scrape_runs_BEFORE_the_portfolio_job_reads_the_ratings",
    ),

    # ── The screen ──
    (
        TEMPLATE,
        'let showExtra = remembered("showExtra", false);',
        "",
        "showExtra is undeclared, so every column render throws "
        "— test_showExtra_is_declared_AFTER_the_helper_it_calls",
    ),
    (
        TEMPLATE,
        "  return COLUMNS.filter(c => !c.extra || showExtra);",
        "  return COLUMNS;",
        "the header renders 11 columns while the body renders 8, shifting every figure left "
        "— test_the_hidden_columns_are_gated_in_ALL_THREE_places",
    ),
    (
        TEMPLATE,
        "    if(filter && verdictGroup(r.verdict) !== filter) return false;",
        "    if(filter && r.verdict !== filter) return false;",
        "the tab filters on the VERDICT rather than the group, so every tab matches nothing "
        "— test_the_table_filters_on_the_group_not_the_verdict",
    ),
    (
        TEMPLATE,
        "  const map = data.verdict_groups || {};\n  return map[verdict] || \"Maintain\";",
        "  const map = {\"BEST BET\": \"Scale\", \"SCALE\": \"Scale\"};\n"
        "  return map[verdict] || \"Maintain\";",
        "the screen holds a SECOND copy of the mapping, which is how a tab count comes to "
        "disagree with the rows beneath it "
        "— test_the_screen_reads_the_grouping_from_the_server_not_a_second_copy",
    ),
]


def run_tests() -> bool:
    proc = subprocess.run(
        [
            str(ROOT / "venv/Scripts/python"),
            "-m",
            "pytest",
            "-q",
            "-x",
            "-p",
            "no:randomly",
            "tests/test_portfolio_groups.py",
            "tests/test_portfolio_api.py",
            "tests/test_portfolio_screen.py",
            "tests/test_retention_and_scheduler.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def main() -> int:
    survivors: list[str] = []
    for index, (rel, find, replace, why) in enumerate(MUTATIONS, 1):
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        label = f"[{index:2}/{len(MUTATIONS)}]"

        if find not in original:
            print(f"{label} SKIP  target text not found in {rel}")
            print(f"          looked for: {find[:78]!r}")
            survivors.append(f"{rel}: target not found — {why}")
            continue

        backup = tempfile.mktemp(suffix=".bak")
        shutil.copy2(path, backup)
        try:
            path.write_text(original.replace(find, replace, 1), encoding="utf-8")
            if run_tests():
                print(f"{label} SURVIVED  {why}")
                survivors.append(f"{rel}: {why}")
            else:
                print(f"{label} caught    {why}")
        finally:
            shutil.copy2(backup, path)
            Path(backup).unlink(missing_ok=True)

    print()
    if survivors:
        print(f"{len(survivors)} MUTATION(S) SURVIVED — a test is missing:")
        for item in survivors:
            print("  -", item)
        return 1
    print(f"All {len(MUTATIONS)} mutations caught.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
