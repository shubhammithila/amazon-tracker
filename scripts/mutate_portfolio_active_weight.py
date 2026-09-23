"""Mutation harness for the Active flag, weight sold, and ACOS no longer deciding anything.

Run: ``venv/Scripts/python scripts/mutate_portfolio_active_weight.py``

Every mutation MUST be caught. Each breaks ONE decision and names the test that has to notice; a
survivor means the test asserts a conclusion rather than the reason for it, which this codebase has
now documented six times (the deploy detector's revision id, the SB ``daily=True`` fetch, the
scheduler guard's substring check, the ads pause shipping broken thrice, and twice while writing
today's tests — "weighted by reviews" matching a check for "weight", and a comment quoting
``channelHtml(s)``).

**The stakes are that every dangerous failure here produces a PLAUSIBLE number.** A parent row
claiming more than the rows beneath it add up to, a weight total silently short by the packs the
sheet has no weight for, Rs 45,042 of real sales vanishing from a dashboard whose purpose is
deciding what to stop selling — none of these look like errors on screen.

Several mutations RESTORE the old behaviour, because the old behaviour passed 2,338 tests.
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
TEMPLATE = "templates/portfolio.html"
DOCS = "app/shipment/documents.py"

#: (file, find, replace, why this would be a real bug)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ── The Active flag: what is hidden ──
    (
        LOGIC,
        '        "active": bool(entry.get("active", True)),',
        '        "active": bool(entry.get("active")),',
        "**an ASIN the sheet has never heard of is treated as INACTIVE** — all three decisions "
        "stored on production are on such ASINs, so every one becomes unreachable at once, and a "
        "Google outage (which returns an EMPTY catalogue) empties the tab from 90 products to 0. "
        "Zero live rows would show it — test_only_an_EXPLICIT_falsy_flag_hides_a_row",
    ),
    (
        LOGIC,
        '        sizes = [s for s in all_sizes if s["active"] or keep_all]',
        "        sizes = list(all_sizes)",
        "the filter does nothing while every banner still renders — a working calculation nothing "
        "consumes, which is the defect this codebase has shipped five times "
        "— test_a_wholly_inactive_parent_LEAVES_the_table_and_is_NAMED_with_its_figures",
    ),
    (
        LOGIC,
        "        agg = _sum_sizes(sizes if sizes else all_sizes)",
        "        agg = _sum_sizes(all_sizes)",
        "**the parent row claims MORE than the rows beneath it add up to** — the most dangerous "
        "mutation here, because both numbers look entirely plausible and the six mixed parents on "
        "the live account have zero sales on their inactive sizes, so real data would not show it "
        "— test_a_parents_money_equals_the_sum_of_the_sizes_SHOWN",
    ),
    (
        LOGIC,
        "        verdict, reason = verdict_for(agg, rating=rating, sizes=sizes, thresholds=limits)",
        "        verdict, reason = verdict_for(agg, rating=rating, sizes=all_sizes, "
        "thresholds=limits)",
        "a SURGICAL reason NAMES a pack size that is not on screen, so the owner is told to kill "
        "something he cannot see — a verdict he cannot check is the one thing these reasons exist "
        "to prevent — test_a_SURGICAL_reason_never_names_a_hidden_size",
    ),
    (
        LOGIC,
        "        naming = sizes or all_sizes",
        "        naming = all_sizes",
        "the row is named after flavours that are not rendered beneath it — the naming version of "
        "the SURGICAL defect — test_the_family_NAME_is_derived_from_the_sizes_shown",
    ),
    (
        LOGIC,
        "        keep_all = include_inactive or parent_asin in decisions",
        "        keep_all = include_inactive",
        "**a product carrying a stored DECISION is hidden**, so `ProductDecision` can no longer "
        "answer the one question it exists for. Measured on production: Bengali Moori and Moori "
        "both vanish — test_a_product_with_a_stored_DECISION_is_never_hidden",
    ),

    # ── The Active flag: what is REPORTED ──
    (
        LOGIC,
        "        if not keep_all:\n            hidden_sizes.extend(hidden)",
        "        if not keep_all:\n            pass",
        "53 parents and Rs 45,042 of real sales vanish with NOTHING naming them — the "
        "3,337-vs-3,259 failure repeated on a money column "
        "— test_EVERY_RUPEE_is_either_on_screen_or_named_as_excluded",
    ),
    (
        LOGIC,
        '        "inactive_sales": round(sum(_num(s.get("sales")) for s in hidden_sizes), 2),',
        '        "inactive_sales": round(sum(_num(p.get("sales")) for p in hidden_parents), 2),',
        "**the ORIGINAL bug, caught by the reconciliation test rather than by reading the code**: "
        "summing the vanished PARENTS misses a mixed parent's hidden size, so those rupees are "
        "neither on screen nor named "
        "— test_EVERY_RUPEE_is_either_on_screen_or_named_as_excluded",
    ),
    (
        LOGIC,
        "        ][:INACTIVE_SHOWN],",
        "        ],",
        "53 product names are printed as a column rather than a sentence "
        "— test_the_named_list_is_CAPPED_while_the_count_and_the_money_stay_exact",
    ),
    (
        LOGIC,
        '        "inactive_with_sales_count": sum(\n'
        '            1 for p in hidden_parents if int(p.get("units_ordered") or 0) > 0\n'
        "        ),",
        '        "inactive_with_sales_count": len([\n'
        '            p for p in hidden_parents if int(p.get("units_ordered") or 0) > 0\n'
        "        ][:INACTIVE_SHOWN]),",
        "the COUNT is capped like the list, so '8 products still sold' is stated while 9 did "
        "— test_the_named_list_is_CAPPED_while_the_count_and_the_money_stay_exact",
    ),
    (
        LOGIC,
        '            if int(p.get("units_ordered") or 0) > 0',
        "            if True",
        "47 products that sold NOTHING crowd out the ones that did, so the banner reads as a list "
        "of dead stock rather than a question about six live products "
        "— test_a_wholly_inactive_parent_that_sold_NOTHING_is_counted_but_NOT_named",
    ),
    (
        LOGIC,
        '            for p in sorted(hidden_parents, key=lambda p: -_num(p.get("sales")))',
        '            for p in sorted(hidden_parents, key=lambda p: -int(p.get("units_ordered") or 0))',
        "ordered by UNITS on a tab about money, so Bengali Moori (121u, Rs 21,633) ranks above "
        "Moringa Powder (70u, Rs 31,845) — test_the_named_list_is_ordered_by_SALES_and_not_by_units",
    ),

    # ── The route ──
    (
        ROUTER,
        "        include_inactive=include_inactive,\n    )",
        "    )",
        "the toggle is INERT: the screen asks and the server ignores it, so pressing it changes "
        "nothing and the button then disagrees with the grid "
        "— test_the_route_hides_inactive_products_and_include_inactive_restores_them",
    ),
    (
        ROUTER,
        "        data = await _dashboard(db, include_inactive=True)",
        "        data = await _dashboard(db)",
        "**a decision on a hidden product records NO figures** — `snapshot` stays None, silently "
        "defeating the only thing `ProductDecision.snapshot_json` exists for, on exactly the "
        "products most likely to be marked KILL "
        "— test_a_DECISION_on_a_hidden_product_still_records_its_FIGURES",
    ),
    (
        ROUTER,
        "    data = await _dashboard(db, window, include_inactive=include_inactive)\n"
        "    window = data.get(\"window\")",
        "    data = await _dashboard(db, window)\n"
        "    window = data.get(\"window\")",
        "the WORKBOOK ignores the toggle, so the file holds different products from the grid it was "
        "downloaded from — test_the_WORKBOOK_follows_the_toggle_and_names_what_it_excluded",
    ),

    # ── Weight sold ──
    (
        LOGIC,
        '        "weight_kg": line_weight(units, pack_weight) if pack_weight > 0 else None,',
        '        "weight_kg": line_weight(units, pack_weight),',
        "an unknown pack size becomes **0.0 kg rather than a dash**, so a 130 kg total reports 90 "
        "while looking complete — `shipment_weight`'s own documented failure "
        "— test_a_parent_with_NO_usable_weight_reports_a_DASH_rather_than_zero",
    ),
    (
        LOGIC,
        '        "weight_unknown": 1 if (units > 0 and pack_weight <= 0) else 0,',
        '        "weight_unknown": 0,',
        "the shortfall is real and UNCOUNTED, so a parent summing 8 of its 9 sizes reports a total "
        "that looks complete — test_a_size_with_no_pack_weight_is_EXCLUDED_and_COUNTED",
    ),
    (
        LOGIC,
        "    from app.shipment.logic import line_weight\n",
        "    def line_weight(u, w):\n        return u * w\n",
        "the multiplication is reimplemented, losing the 3-decimal rounding: 0.15 kg x 200 becomes "
        "30.000000000000004 in a spreadsheet cell, and the rule now has two homes "
        "— test_the_multiplication_is_the_shipment_tabs_and_not_a_second_copy",
    ),
    (
        LOGIC,
        "    weight_kg = round(sum(_num(w) for w in known_weights), 3) if known_weights else None",
        "    weight_kg = max((_num(w) for w in known_weights), default=None)",
        "the parent reports its LARGEST size's weight rather than the sum, so a parent is lighter "
        "than the rows beneath it — test_weight_sold_is_units_times_the_pack_size_at_ALL_THREE_grains",
    ),

    # ── The screen: the column layout ──
    (
        TEMPLATE,
        '  {key: "units",        label: "Units",    num: true},',
        '  {key: "units",        label: "Units",    num: true, extra: true},',
        "Units goes back behind the '+ More columns' toggle, so the figure the owner judges a row "
        "by is invisible by default — and the HEADER now renders one fewer column than the body "
        "— test_the_hidden_columns_are_gated_in_ALL_THREE_places",
    ),
    (
        TEMPLATE,
        "    <td class=\"num\">${n(r.units).toLocaleString(\"en-IN\")}</td>\n"
        "    <td class=\"num\">${kg(r.weight_kg)}</td>\n"
        "    ${showExtra ? `",
        "    ${showExtra ? `\n"
        "    <td class=\"num\">${n(r.units).toLocaleString(\"en-IN\")}</td>\n"
        "    <td class=\"num\">${kg(r.weight_kg)}</td>",
        "**Units and Weight are gated in ONE of the three render functions only**, so the header "
        "renders 12 columns over 10 body cells and every figure after Net % sits under the wrong "
        "heading — the 4th-instance trap this codebase records "
        "— test_units_and_weight_are_OUTSIDE_the_showExtra_gate_in_ALL_THREE_functions",
    ),
    (
        TEMPLATE,
        "  const weighed = list.filter(r => r.weight_kg !== null && r.weight_kg !== undefined);\n"
        "  const weight = weighed.reduce((a, r) => a + r.weight_kg, 0);",
        "  const weighed = list;\n"
        "  const weight = sum(\"weight_kg\");",
        "the totals row coerces an unknown weight to 0 kg via `n()`, which is the silent shortfall "
        "— test_the_weight_total_does_not_COERCE_an_unknown_weight_to_zero",
    ),
    (
        TEMPLATE,
        "  if(value === null || value === undefined) return '<span class=\"dim\">—</span>';\n"
        "  return value.toLocaleString(\"en-IN\", {maximumFractionDigits: 1}) + \" kg\";",
        "  return n(value).toLocaleString(\"en-IN\", {maximumFractionDigits: 1}) + \" kg\";",
        "`kg()` renders 0.0 kg where the sheet has no weight, so an absence reads as a measurement "
        "— test_the_kg_formatter_shows_a_DASH_and_never_zero_kg",
    ),

    # ── The screen: the size rows and the toggle ──
    (
        TEMPLATE,
        '    <td>${esc(sizeName(s))} <span class="asin">${esc(s.asin)}</span></td>',
        '    <td>${esc(sizeName(s))} <span class="asin">${esc(s.asin)}</span>${channelHtml(s)}</td>',
        "the ~150-character merchant/FBA sentence is back in the size row's first cell — the "
        "reported clutter, and what widened the Product column 353px to 780px "
        "— test_a_SIZE_row_carries_no_channel_note_while_the_SKU_row_STILL_DOES",
    ),
    (
        TEMPLATE,
        "          ${channelHtml(s)}\n",
        "",
        "'finishing the job' deletes the SURVIVING caller too, so the merchant/FBA split leaves "
        "the app entirely — test_a_SIZE_row_carries_no_channel_note_while_the_SKU_row_STILL_DOES",
    ),
    (
        TEMPLATE,
        '  if(includeInactive) parts.push("include_inactive=1");',
        "",
        "the toggle never reaches the server, so it is a button that does nothing "
        "— test_the_inactive_toggle_is_a_QUERY_PARAMETER_and_not_a_client_side_filter",
    ),
    (
        TEMPLATE,
        "  remember(\"includeInactive\", includeInactive);\n  await load();",
        "  remember(\"includeInactive\", includeInactive);\n  render();",
        "the toggle re-renders instead of re-fetching, leaving the OLD products on screen beneath a "
        "button claiming the opposite — test_the_inactive_toggle_RELOADS_rather_than_re_rendering",
    ),
    (
        TEMPLATE,
        "  const on = !!(data && data.include_inactive);",
        "  const on = includeInactive;",
        "the button reads the local flag, which has already flipped even if the request failed, so "
        "it can disagree with the grid beneath it "
        "— test_the_inactive_BUTTON_renders_from_the_servers_echoed_flag",
    ),
    (
        TEMPLATE,
        "let includeInactive = remembered(\"includeInactive\", false);",
        "let includeInactive = false;",
        "the choice is not remembered, so every render silently reverts to hiding — the toggle "
        "appears to do nothing on the next window change "
        "— test_the_inactive_toggle_RELOADS_rather_than_re_rendering",
    ),
    (
        TEMPLATE,
        'if(!data.include_inactive && n(data.inactive_hidden_skus)){',
        "if(false){",
        "the banner never renders, so the Sales KPI drops Rs 45,042 with nothing on screen "
        "explaining it — test_the_banner_states_the_excluded_money_AND_units_and_names_the_products",
    ),
    (
        TEMPLATE,
        "        <strong>${n(data.inactive_sales_units)} units, ${money(data.inactive_sales)}</strong>",
        "        <strong>${n(data.inactive_sales_units)} units</strong>",
        "the excluded RUPEES are not stated, so a 1.5% gap against the Business Report has nothing "
        "to reconcile against — the exact shape of the 3,337-vs-3,259 report "
        "— test_the_banner_states_the_excluded_money_AND_units_and_names_the_products",
    ),
    (
        TEMPLATE,
        '          ${p.inactive ? `<span class="fcount" title="Active = N in the MRP sheet">inactive</span>` : ""}',
        "",
        "an inactive row renders identically to a live one, so the flag silently stops mattering "
        "for the rows it kept — test_a_shown_but_inactive_row_SAYS_it_is_inactive",
    ),

    # ── The thresholds panel ──
    (
        LOGIC,
        '    "dead_units": GROUP_KILL,\n}',
        "}",
        "a threshold is filed under no decision, so its input falls through to whichever heading "
        "renders last — test_every_threshold_is_filed_under_exactly_one_DECISION",
    ),
    (
        TEMPLATE,
        "    ${groupOrder.map(section).join(\"\")}",
        "    ${[\"Scale\", \"Maintain\", \"Kill or monitor\"].map(section).join(\"\")}",
        "the panel holds a SECOND copy of the grouping, which falls out of step the first time a "
        "group is renamed — test_the_rules_panel_reads_its_GROUPING_from_the_server",
    ),

    # ── ACOS decides nothing ──
    (
        LOGIC,
        "    if net_pct >= limits[\"good_net\"] and tacos is not None "
        "and tacos <= limits[\"good_tacos\"]:",
        "    if net_pct > 0 and row.get(\"acos_infinite\"):\n"
        "        return VERDICT_MONITOR, \"the ads produced no attributed sales\"\n"
        "    if net_pct >= limits[\"good_net\"] and tacos is not None "
        "and tacos <= limits[\"good_tacos\"]:",
        "an ACOS branch is reintroduced under a DIFFERENT NAME, which 'AD DEPENDENT is absent' "
        "could never catch — test_no_verdict_rule_reads_acos",
    ),
    (
        LOGIC,
        '            + (f", ACOS {acos * 100:.0f}%" if acos is not None else "")',
        "",
        "ACOS leaves the reason string, so the number the owner was promised as context disappears "
        "— test_an_efficiently_advertised_product_is_still_a_best_bet",
    ),

    # ── The export ──
    (
        ROUTER,
        '        totals["net"], totals["units"], _kg(totals.get("weight_kg")),',
        '        totals["net"], totals["units"], _kg(sum(\n'
        '            (p.get("weight_kg") or 0) for p in data["parents"]) or None),',
        "the TOTAL row re-sums the rows, and each parent already contains its sizes — the "
        "double-count `build_portfolio_xlsx` has no `_totals_row` in order to avoid "
        "— test_the_workbooks_TOTAL_uses_the_aggregate_not_a_resum_of_the_rows",
    ),
    (
        DOCS,
        '            elif heading in ("Sales", "Ad spend", "Ad sales", "Net", "Units", '
        '"Weight (kg)",\n                             "Net %", "TACOS", "ACOS"):',
        '            elif heading in ("Sales", "Ad spend", "Net", "Units", "Net %", "TACOS"):',
        "the new numeric columns render left-aligned, so digits do not line up down the column — "
        "the pre-existing miss this change fixed for ACOS and Ad sales "
        "— test_the_workbook_carries_the_weight_on_every_row_type",
    ),
]

TESTS = [
    "tests/test_portfolio_active.py",
    "tests/test_portfolio_weight.py",
    "tests/test_portfolio_logic.py",
    "tests/test_portfolio_groups.py",
    "tests/test_portfolio_screen.py",
    "tests/test_portfolio_api.py",
]


def run_tests() -> bool:
    proc = subprocess.run(
        [str(ROOT / "venv/Scripts/python"), "-m", "pytest", "-q", "-x", "-p", "no:randomly", *TESTS],
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
