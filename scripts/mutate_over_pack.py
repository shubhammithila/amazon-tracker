"""Mutation harness for the over-pack approval.

Run: ``venv/Scripts/python scripts/mutate_over_pack.py``

Every mutation MUST be caught. The stakes: this feature SILENCES an error about a quantity that
reaches a GST invoice and an Amazon shipment, so the dangerous failures are all quiet ones — an
approval that mutes a row for ever, an approval that compounds with a raised plan, or an approval
that leaks into a declared quantity so Amazon expects a different count from what arrives at the FC.

Four mutations target the "nothing else changes" half, because that is the half no screen would
show: if the approval reached `verified_units_by_asin`, the app would look correct and the FC
receipt would not match.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LOGIC = "app/shipment/logic.py"
REPO = "app/shipment/repository.py"
ROUTER = "app/routers/shipment.py"
OPS = "templates/ops.html"
SHIP = "templates/shipment.html"
ORDERS = "templates/orders.html"
DEPLOY = "deploy/update-ec2.sh"

#: (file, find, replace, why this would be a real bug)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ── The arithmetic: the two properties the whole design rests on ──
    (
        LOGIC,
        "    return over_packed(max(_as_count(planned), _as_count(approved)), packed)",
        "    return over_packed(_as_count(planned) + _as_count(approved), packed)",
        "the approval ADDS to the plan, so raising the plan 60->100 moves the threshold to 102 "
        "and authorises an overage nobody looked at "
        "— test_a_RAISED_PLAN_supersedes_the_approval_rather_than_adding_to_it",
    ),
    (
        LOGIC,
        "    return over_packed(max(_as_count(planned), _as_count(approved)), packed)",
        "    return 0 if _as_count(approved) else over_packed(planned, packed)",
        "the approval is a MUTE rather than a ceiling, so a later 200-unit miscount reaches a GST "
        "invoice with both screens silent — test_an_approval_is_a_CEILING_and_not_a_mute",
    ),
    (
        LOGIC,
        "    return over_packed(max(_as_count(planned), _as_count(approved)), packed)",
        "    return over_packed(_as_count(planned), packed)",
        "the approval is ignored entirely, so the banner never clears and the feature does nothing "
        "— test_an_approved_over_pack_is_no_longer_reported",
    ),

    # ── Storing the wrong thing ──
    (
        REPO,
        "            item.over_pack_approved_units = int(units)",
        "            item.over_pack_approved_units = max(0, int(units) - int(item.shipment_plan or 0))",
        "the EXCESS is stored instead of the packed total, which is the compounding bug one layer "
        "down — test_a_RAISED_PLAN_supersedes_the_approval_rather_than_adding_to_it",
    ),
    (
        REPO,
        "        if item.excluded_at is not None:\n            continue",
        "        if False:\n            continue",
        "an excluded row can be approved, storing a decision that reappears if the row is "
        "restored — test_an_excluded_row_cannot_be_approved",
    ),
    (
        REPO,
        "            item.over_pack_approved_units = None\n            item.over_pack_approved_at = None",
        "            item.over_pack_approved_units = 0\n            item.over_pack_approved_at = None",
        "revoking stores 0 rather than clearing, so 'no decision' and 'approved zero' become "
        "indistinguishable — test_revoking_brings_the_whole_excess_back",
    ),

    # ── The drift guard ──
    (
        ROUTER,
        "            if seen != current:",
        "            if False:",
        "a stale packed figure is approved anyway, so the owner signs off a quantity he never saw "
        "— test_a_stale_packed_figure_is_REFUSED_and_names_both_numbers",
    ),
    (
        ROUTER,
        "    if not approved:\n        approvals = {asin: None for asin in asins}",
        "    if not approved:\n        approvals = {}",
        "revoke becomes a no-op, so an approval can never be undone "
        "— test_revoking_brings_the_whole_excess_back",
    ),
    (
        ROUTER,
        "        drifted = []",
        "        drifted = []  # noqa\n        seen_raw = {a: int(live.get(a, 0)) for a in asins}",
        "the server overwrites what the screen sent with the live figure, which IS the drift guard "
        "removed — test_a_stale_packed_figure_is_REFUSED_and_names_both_numbers",
    ),

    # ── The payloads ──
    (
        ROUTER,
        '        "over_packed": logic.unapproved_over_pack(\n'
        "            planned, packed, item.over_pack_approved_units\n        ),",
        '        "over_packed": logic.over_packed(planned, packed),',
        "the owner's banner ignores his own approval, so it never clears "
        "— test_approving_clears_the_owners_banner_and_records_the_figure",
    ),
    (
        ROUTER,
        '                "over_pack_approved": (\n'
        "                    int(item.over_pack_approved_units)\n"
        "                    if item.over_pack_approved_units is not None\n"
        "                    else 0\n                ),",
        '                "over_pack_approved": 0,',
        "the warehouse screen never learns about the approval, so the red banner stays there "
        "— test_the_packing_payload_carries_the_approved_THRESHOLD",
    ),
    (
        ROUTER,
        '                "over_packed": logic.unapproved_over_pack(\n'
        "                    planned,\n"
        "                    prior + int(mine.get(\"units\") or 0),\n"
        "                    item.over_pack_approved_units,\n                ),",
        '                "over_packed": logic.over_packed(\n'
        "                    planned, prior + int(mine.get(\"units\") or 0)\n                ),",
        "the packing payload reports the raw excess while the owner's reports the netted one — two "
        "numbers for one fact — test_the_packing_payload_carries_the_approved_THRESHOLD",
    ),

    # ── The approval must reach NO quantity ──
    (
        "app/shipment/documents.py",
        '            quantity = int(verified.get(asin, 0))',
        '            quantity = min(int(verified.get(asin, 0)), int(item.get("shipment_plan") or 0) or 10**9)',
        "the approval caps what Amazon is told, so the FC expects fewer units than arrive "
        "— test_the_amazon_upload_still_declares_what_was_PACKED",
    ),

    # ── The screens ──
    (
        OPS,
        "  const threshold = Math.max(\n"
        "    Number(row.planned || 0), Number(row.over_pack_approved || 0)\n  );",
        "  const threshold = Number(row.planned || 0);",
        "the live check ignores the approval, so the packer still sees red after the owner "
        "approved — test_both_screens_net_the_approval_through_ONE_helper",
    ),
    (
        OPS,
        "    const over = unapprovedOver(i);",
        "    const over = Math.max(0, Number(i.packed_before || 0) + units\n"
        "                             - Number(i.planned || 0));",
        "**the FIRST RENDER keeps its own copy**, so every row shows '+2 over' after the owner "
        "approved while the banner above is silent — the bug found by opening the page "
        "— test_both_screens_net_the_approval_through_ONE_helper",
    ),
    (
        OPS,
        "  const over = unapprovedOver(row);",
        "  const over = Math.max(0, Number(row.packed_before || 0) + rowUnits(row)\n"
        "                           - Number(row.planned || 0));",
        "the row's +N tag keeps its own copy of the rule, so it disagrees with the banner above it "
        "— test_both_screens_net_the_approval_through_ONE_helper",
    ),
    (
        SHIP,
        "       .forEach(i => { packed[i.asin] = Number(i.packed || 0); });",
        "       .forEach(i => { packed[i.asin] = 0; });",
        "the screen sends a packed figure it never displayed, so the drift guard fires on every "
        "approval — test_the_dashboard_offers_the_approval_and_a_way_to_revoke_it",
    ),
    (
        ORDERS,
        '  try{ return sessionStorage.getItem("ord.overDismissed") === String(data.pack_date); }',
        '  try{ return sessionStorage.getItem("ord.overDismissed") === "1"; }',
        "the Orders dismissal is not keyed on the day, so it also hides tomorrow's genuine "
        "over-pack — test_the_orders_warning_is_dismissed_for_the_DAY_and_stores_nothing",
    ),

    # ── The deploy detector ──
    (
        DEPLOY,
        'elif "over_pack_approved_units" in cols("shipment_plan_items"):\n    print("c5e2a91f47b3")',
        'elif False:\n    print("c5e2a91f47b3")',
        "the detector cannot see the new revision, so it stamps production BACKWARDS and the "
        "deploy fails on a duplicate column — test_the_deploy_detector_reports_the_head",
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
            "tests/test_over_pack_approval.py",
            "tests/test_shipment_over_packing.py",
            "tests/test_ops_ui.py",
            "tests/test_schema_migrations.py",
            "tests/test_shipment_documents.py",
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
