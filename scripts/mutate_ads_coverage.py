"""Mutation harness for Ads → SKU coverage. Throwaway; not imported by the app.

Each entry breaks ONE decision from `docs/superpowers/specs/2026-10-24-ads-sku-coverage-design.md`
and names the test that must catch it. A SKIP (target text not found) counts as a survivor.

    venv/Scripts/python scripts/mutate_ads_coverage.py
"""
import pathlib
import subprocess
import sys

COV = pathlib.Path("app/ads/coverage.py")
LIST = pathlib.Path("app/ads/listings.py")
REPO = pathlib.Path("app/ads/repository.py")
WRITER = pathlib.Path("app/ads/spapi_ads.py")
ROUTE = pathlib.Path("app/routers/ads_coverage.py")
HTML = pathlib.Path("templates/ads_expansion.html")

MUTATIONS = [
    ("an ARCHIVED product ad counts as present", COV,
     'live = [a for a in ads if str(a.get("state") or "").upper() != ARCHIVED]',
     "live = list(ads)", "test_a_paused_sku_counts_as_present"),
    ("a size retired in the MRP sheet is expected", COV,
     "                if _sheet_inactive(catalogue.get(asin)):\n                    continue\n", "",
     "test_a_size_retired_in_the_MRP_sheet"),
    ("a missing Active flag retires a size", COV,
     'return isinstance(entry, Mapping) and "active" in entry and not entry.get("active")',
     'return isinstance(entry, Mapping) and not entry.get("active")',
     "test_a_missing_active_flag_is_not_a_retirement"),
    ("an Incomplete listing is expected", COV,
     'if str(listing.get("status") or "").lower() == INCOMPLETE:', "if False:",
     "test_an_inactive_listing_is_still_expected"),
    ("M19/Amazon-managed campaigns are checked", COV,
     'if logic.manager_of(campaign.get("name")) != logic.MANAGER_US:', "if False:",
     "test_paused_groups_paused_campaigns"),
    ("a paused ad group is checked", COV,
     'if group and str(group.get("state") or "").upper() != logic.STATE_ENABLED:', "if False:",
     "test_paused_groups_paused_campaigns"),
    ("Sponsored Brands ads are checked", COV,
     'if (ad.get("ad_product") or "sp") != "sp":', "if False:", "test_paused_groups_paused_campaigns"),
    ("a paused campaign is checked", COV,
     'if not campaign or str(campaign.get("state") or "").upper() != logic.STATE_ENABLED:',
     "if not campaign:", "test_paused_groups_paused_campaigns"),
    ("ignoring a whole group ignores nothing", COV,
     "ignored = group_ignored or (group_id, key) in ignores", "ignored = (group_id, key) in ignores",
     "test_an_ignored_product_or_group"),
    ("an ignored gap can still be added", COV,
     '                if product["ignored"]:\n                    continue\n', "",
     "test_an_ignored_product_cannot_be_added or test_an_ignored_product_or_group"),
    ("the flavour split is lost (Amazon's parent groups every flavour)", COV,
     "lambda a: flav.get(a) or parent.get(a) or a", "lambda a: parent.get(a) or a",
     "test_the_panel_splits_flavours"),
    ("a SIZE gap is labelled as a SKU gap", COV,
     '"kind": KIND_SKU if asin in have_asins else KIND_SIZE,', '"kind": KIND_SKU,',
     "test_a_size_the_group_does_not_hold"),
    ("the ASIN is not looked up in the listings when the cache lacks it", COV,
     'or sku_asin.get(str(a.get("name") or ""), "")', 'or ""',
     "test_the_asin_comes_from_the_listings"),
    ("the live re-read is skipped, so a SKU Amazon holds is created twice", ROUTE,
     'already = [w for w in wanted if (w["ad_group_id"], w["sku"]) in present]', "already = []",
     "test_add_sends_only_current_gaps"),
    ("the server trusts the client's rows", ROUTE,
     "wanted = [gaps[k] for k in asked if k in gaps]",
     'wanted = [gaps.get(k) or {"ad_group_id": k[0], "sku": k[1], "campaign_id": "c1", "asin": ""} for k in asked]',
     "test_add_sends_only_current_gaps or test_add_refuses"),
    ("the row limit is not checked", ROUTE,
     'if len(wanted) > guardrails["max_rows"]:', "if False:", "test_add_refuses"),
    ("the made ads are not put into the cache", ROUTE,
     "        await repository.add_product_ads_to_cache(db, [\n            {\"entity_id\": made",
     "        [\n            {\"entity_id\": made", "test_add_sends_only_current_gaps"),
    ("the ledger is written AFTER Amazon is asked", ROUTE,
     '''            run_id = await repository.open_run(
                db, changes, rule_summary=f"SKU coverage: add {len(changes)} SKU(s)")
            results = await spapi_ads.create_product_ads(client, changes)''',
     '''            results = await spapi_ads.create_product_ads(client, changes)
            run_id = await repository.open_run(
                db, changes, rule_summary=f"SKU coverage: add {len(changes)} SKU(s)")''',
     "test_the_ledger_is_written_before"),
    ("a created row keeps its provisional id", REPO,
     '            row.entity_id = str(result["ad_id"])', "            pass",
     "test_a_created_ad_takes_its_real_id"),
    ("undoing an add re-ENABLES instead of pausing", REPO,
     'undo.append({**common, "old_state": logic.STATE_ENABLED,\n                         "new_state": logic.STATE_PAUSED})',
     'undo.append({**common, "old_state": logic.STATE_ENABLED,\n                         "new_state": logic.STATE_ENABLED})',
     "test_a_created_ad_takes_its_real_id or test_undoing_an_add"),
    ("a create run is recorded as a state run", REPO,
     '            action=("create" if change.get("create") else', '            action=("state" if change.get("create") else',
     "test_a_created_ad_takes_its_real_id"),
    ("a success without an adId is ok", WRITER,
     '"ok": bool(ad_id), "ad_id": ad_id or None,', '"ok": True, "ad_id": ad_id or None,',
     "test_a_created_ad_carries_amazons_ad_id"),
    ("an unmentioned row is not reported", WRITER,
     '    results.extend({**r, "ad_id": None} for r in _unmentioned(order, seen))\n    return results',
     "    return results", "test_a_created_ad_carries_amazons_ad_id"),
    ("the create body names the ASIN, not the SKU", WRITER,
     '"sku": str(r["sku"]), "state": "ENABLED"} for r in batch]}',
     '"asin": str(r["sku"]), "state": "ENABLED"} for r in batch]}',
     "test_the_create_request_is_amazons_seller_shape"),
    ("an empty listings read wipes the stored list", LIST,
     "    rows = [r for r in rows or () if r.get(\"seller_sku\")]\n    if not rows:\n        return 0\n",
     "    rows = [r for r in rows or () if r.get(\"seller_sku\")]\n",
     "test_an_empty_read_never_wipes"),
    ("the byte-order mark is not stripped", LIST,
     '.replace("\\ufeff", "")', "", "test_the_listings_report_is_parsed"),
    ("the listings are re-read on every refresh", LIST,
     "< STALE_AFTER_HOURS * 3600:", "< 0:", "test_the_refresh_reads_the_listings_only_when_stale"),
    ("a failed read loses the last good time", LIST,
     '    if not error:\n        status["ok_at"] = now\n', '    status["ok_at"] = now\n',
     "test_a_failed_read_is_recorded"),
    ("the screen ticks an ignored row", HTML,
     'product.ignored ? "" : `<input type="checkbox" data-cov-pick', 'false ? "" : `<input type="checkbox" data-cov-pick',
     "test_the_screen_never_ticks_an_ignored_row"),
    ("a card's select-all ticks ignored products", HTML,
     "return g.products.filter(p => !p.ignored)", "return g.products",
     "test_the_screen_never_ticks_an_ignored_row"),
    ("the Expansion tab link is dropped", pathlib.Path("templates/_ads_tabs.html"),
     'href="/ads-page/expansion">Expansion</a>', 'href="/ads-page/expansion"></a>',
     "test_expansion_is_its_own or test_optimisation_carries"),
    ("a card per product instead of per ad group", HTML,
     "    out.push({c, g, products});", "    for(const p of products) out.push({c, g, products: [p]});",
     "test_a_card_is_one_ad_group"),
]


def run(expression):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-k", expression,
         "tests/test_ads_coverage.py"],
        capture_output=True, text=True, timeout=600,
    )
    last = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return result.returncode, last


def main():
    survivors = []
    for label, path, old, new, test_name in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"SKIP      {label}\n          target text not found in {path}")
            survivors.append((label, f"target text not found in {path}"))
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
