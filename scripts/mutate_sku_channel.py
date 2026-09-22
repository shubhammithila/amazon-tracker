"""Mutation harness for the FBA-SKU channel rule, the flex fallback and the stock columns.

Run: ``venv/Scripts/python scripts/mutate_sku_channel.py``

Every mutation MUST be caught. The stakes are that all three faults were SILENT: a misfiled SKU
produced a plausible plan asking for stock already held, a Flex SKU looked exactly like a correct
one until Amazon refused the upload, and a wrong column set summed to a total nobody could tell was
wrong. Nothing on screen said anything in any of the three cases.

The reverse direction matters as much as the forward one, which is why several mutations here
RESTORE the old behaviour rather than breaking something new: the old behaviour passed 2,287 tests.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LOGIC = "app/portfolio/logic.py"
ROUTER = "app/routers/shipment.py"
CAT = "app/shipment/catalogue.py"

#: (file, find, replace, why this would be a real bug)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ── The channel rule ──
    (
        LOGIC,
        '_SKU_SEPARATORS = re.compile(r"[\\s_\\-]+")',
        '_SKU_SEPARATORS = re.compile(r"[\\s]+")',
        "**the original bug restored**: underscore SKUs read as merchant, so 798 units of real FBA "
        "stock are discarded and the plan asks for stock already held "
        "— test_the_fba_token_is_also_separated_by_UNDERSCORES_and_hyphens",
    ),
    (
        LOGIC,
        "    parts = _SKU_SEPARATORS.split(str(seller_sku or \"\").upper().strip())\n"
        "    return CHANNEL_FBA if parts and parts[-1] == FBA_SKU_SUFFIX else CHANNEL_MERCHANT",
        "    return CHANNEL_FBA if FBA_SKU_SUFFIX in str(seller_sku or \"\").upper() "
        "else CHANNEL_MERCHANT",
        "a SUBSTRING test rather than a token test, so `fbagel 1kg` is filed as FBA — the property "
        "the whitespace split existed to protect "
        "— test_widening_the_separators_did_NOT_widen_it_to_a_substring_test",
    ),
    (
        LOGIC,
        "    return CHANNEL_FBA if parts and parts[-1] == FBA_SKU_SUFFIX else CHANNEL_MERCHANT",
        "    return CHANNEL_FBA if parts and parts[-1].startswith(FBA_SKU_SUFFIX) "
        "else CHANNEL_MERCHANT",
        "a PREFIX match on the final token, so `1kg_FBAX` and `FBAGEL` are filed as FBA — the same "
        "class of over-match as a substring test, one step subtler "
        "— test_widening_the_separators_did_NOT_widen_it_to_a_substring_test",
    ),
    (
        LOGIC,
        '    parts = _SKU_SEPARATORS.split(str(seller_sku or "").upper().strip())',
        '    parts = _SKU_SEPARATORS.split(str(seller_sku or "").strip())',
        "the SKU is no longer upper-cased, so a lower-case `2kg kc fba` reads as merchant — Amazon's "
        "casing is not guaranteed — test_the_fba_suffix_is_what_marks_the_channel",
    ),

    # ── The flex fallback ──
    (
        ROUTER,
        "        if _channel_of(sku) == CHANNEL_FBA:\n            out.setdefault(asin, sku)\n    return out",
        "        if _channel_of(sku) == CHANNEL_FBA:\n            out.setdefault(asin, sku)\n"
        "        else:\n            out.setdefault(asin, sku)\n    return out",
        "**the reported bug restored**: a Flex SKU is written onto an FBA shipment line and Amazon "
        "rejects it — test_an_asin_with_no_fba_sku_gets_NO_sku_rather_than_the_flex_one",
    ),
    (
        ROUTER,
        "        if _channel_of(sku) == CHANNEL_FBA:\n            out.setdefault(asin, sku)",
        "        if True:\n            out.setdefault(asin, sku)",
        "the channel is not consulted at all, so whichever row comes first wins — the ordering "
        "accident that made the 07 Sep file report zero stock "
        "— test_an_asin_with_no_fba_sku_gets_NO_sku_rather_than_the_flex_one",
    ),

    # ── The stock columns ──
    (
        ROUTER,
        '        "afn-fc-transfer-quantity",',
        "",
        "column V is dropped, so stock in transit between FCs is not counted and the deficit is "
        "over-stated — test_the_owners_real_column_layout_parses",
    ),
    (
        ROUTER,
        '        "afn-fc-transfer-quantity",',
        '        "afn-fc-transfer-quantity", "afn-onhand-buyable-quantity",',
        "W is summed, and it DUPLICATES fulfillable — measured equal on 237 of 246 rows, so stock "
        "nearly doubles and every shipment goes out short "
        "— test_the_three_double_counting_columns_are_never_summed",
    ),
    (
        ROUTER,
        '        "afn-fc-transfer-quantity",',
        '        "afn-fc-transfer-quantity", "afn-total-quantity",',
        "Amazon's own total is summed on top of its parts, double-counting everything "
        "— test_the_three_double_counting_columns_are_never_summed",
    ),
    (
        ROUTER,
        '        "afn-fc-transfer-quantity",',
        '        "afn-fc-transfer-quantity", "afn-unsellable-quantity",',
        "damaged stock is counted as held, so the owner makes less than he needs "
        "— test_the_three_double_counting_columns_are_never_summed",
    ),

    # ── The sheet fallback and the moved columns ──
    (
        ROUTER,
        '            "fba_sku": sku_map.get(asin) or (sheet_row.get("fba_sku") or ""),',
        '            "fba_sku": (sheet_row.get("fba_sku") or "") or sku_map.get(asin, ""),',
        "the SHEET overrides Amazon's own export, so a stale hand-typed SKU beats the live one "
        "— test_the_sheets_column_K_fills_a_blank_but_never_overrides_the_csv",
    ),
    (
        ROUTER,
        '            "fba_sku": sku_map.get(asin) or (sheet_row.get("fba_sku") or ""),',
        '            "fba_sku": sku_map.get(asin, ""),',
        "the sheet is never consulted, so filling column K fixes nothing "
        "— test_the_sheets_column_K_fills_a_blank_but_never_overrides_the_csv",
    ),
    (
        CAT,
        "ACTIVE_COLUMN_FALLBACK = 21   # V  (was T, before Blinkit UPC Code was inserted)",
        "ACTIVE_COLUMN_FALLBACK = 19   # T",
        "**the landmine restored**: a renamed Active header falls back to a blank column and marks "
        "ALL products inactive, producing an empty plan "
        "— test_a_RENAMED_active_header_still_finds_the_flag",
    ),
    (
        CAT,
        "BRAND_COLUMN_FALLBACK = 20    # U  (was S)",
        "BRAND_COLUMN_FALLBACK = 18    # S",
        "the brand fallback is stale, so a renamed header sends every row to the wrong brand and "
        "the packer's sort order is wrong "
        "— test_the_positional_fallbacks_match_where_the_columns_ACTUALLY_are",
    ),
    (
        CAT,
        '    "amazon fba sku": ("fba_sku", FBA_SKU_COLUMN_FALLBACK),',
        "",
        "the sheet's SKU column is not read at all "
        "— test_the_sheets_fba_sku_column_is_read",
    ),
    (
        CAT,
        '                "fba_sku": _cell(row, columns["fba_sku"]),',
        '                "fba_sku": "",',
        "the column is located but never stored, which looks identical to an empty sheet "
        "— test_the_sheets_fba_sku_column_is_read",
    ),

    # ── Inactive products that still sold ──
    (
        ROUTER,
        '        "inactive_sales_units": sum(u for _, u in inactive_with_sales),',
        '        "inactive_sales_units": 0,',
        "the excluded demand is reported as zero, so the 3,337-vs-3,259 gap is unexplainable "
        "again — test_an_inactive_product_that_still_SELLS_is_named_with_its_units",
    ),
    (
        ROUTER,
        "        sold_7d = int(sales.get(asin, 0))",
        "        sold_7d = 0",
        "no inactive product is ever reported as selling, which is the original silent gap "
        "— test_an_inactive_product_that_still_SELLS_is_named_with_its_units",
    ),
    (
        ROUTER,
        "            for a, u in sorted(inactive_with_sales, key=lambda x: (-x[1], x[0]))",
        "            for a, u in sorted(inactive_with_sales, key=lambda x: (x[1], x[0]))",
        "the list is smallest-first, so a 1-unit run-down leads and the 36-unit mistake is "
        "pushed past the 8-name cap "
        "— test_an_inactive_product_that_still_SELLS_is_named_with_its_units",
    ),
    (
        ROUTER,
        "            if sold_7d > 0:\n                inactive_with_sales.append((asin, sold_7d))\n            continue\n        if not sheet_row and not catalogue.is_active(",
        "            continue\n        if not sheet_row and not catalogue.is_active(",
        "the FIRST skip branch stops reporting, and that is the branch the real Active=N rows take "
        "— test_an_inactive_product_that_still_SELLS_is_named_with_its_units",
    ),
    (
        "templates/shipment.html",
        "    const stale = c.inactive_with_sales || [];",
        "    const stale = [];",
        "the screen never renders the warning, so the server knows and the owner does not — the "
        "defect shape this codebase has shipped five times "
        "— test_the_banner_names_inactive_products_that_still_sold",
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
            "tests/test_shipment_stock_csv.py",
            "tests/test_shipment_catalogue.py",
            "tests/test_portfolio_logic.py",
            "tests/test_shipment_plan_db.py",
            "tests/test_shipment_admin_ui.py",
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
