"""Mutation harness for Ads → Search-term negatives. Throwaway; not imported by the app.

Each entry breaks ONE decision from `docs/superpowers/specs/2026-10-25-ads-search-term-negatives-design.md`
and names the test that must catch it. A SKIP (target text not found) counts as a survivor.

    venv/Scripts/python scripts/mutate_ads_negatives.py
"""
import pathlib
import subprocess
import sys

NEG = pathlib.Path("app/ads/negatives.py")
STORE = pathlib.Path("app/ads/search_store.py")
REPO = pathlib.Path("app/ads/repository.py")
WRITER = pathlib.Path("app/ads/spapi_ads.py")
ROUTE = pathlib.Path("app/routers/ads_negatives.py")
HTML = pathlib.Path("templates/ads.html")

MUTATIONS = [
    ("a term that IS the group's own target is negated", NEG,
     "elif term_is_target(term, positives.get(group, set())):", "elif False:",
     "test_a_term_that_IS_the_ad_groups_own"),
    ("an existing negative is offered again", NEG,
     'elif (group, "neg_target" if kind == KIND_PRODUCT else "neg_keyword", term) in negatives:', "elif False:",
     "test_a_term_already_negated"),
    ("the negative's TYPE is ignored when checking existing ones", NEG,
     '(group, "neg_target" if kind == KIND_PRODUCT else "neg_keyword", term) in negatives',
     '(group, "neg_keyword", term) in negatives', "test_a_term_already_negated"),
    ("M19/Amazon campaigns get negatives", NEG,
     "elif logic.manager_of(cname) != logic.MANAGER_US:", "elif False:", "test_a_campaign_m19_or_amazon"),
    ("the spend floor is dropped", NEG, 'elif m["spend"] < floor:', "elif False:", "test_the_spend_floor"),
    ("the row limit is dropped", NEG, "if limit and len(changes) > limit:", "if False:", "test_the_spend_floor"),
    ("an ASIN is sent as a word", NEG, '_ASIN = re.compile(r"^b0[0-9a-z]{8}$", re.I)', '_ASIN = re.compile(r"^$")',
     "test_a_word_term_becomes"),
    ("SP negative is PHRASE", WRITER, '"matchType": "NEGATIVE_EXACT"', '"matchType": "NEGATIVE_PHRASE"',
     "test_sp_negative_keyword_is_NEGATIVE_EXACT"),
    ("SB negative is PHRASE", WRITER, '"matchType": "negativeExact"', '"matchType": "negativePhrase"',
     "test_sb_negative_keyword_is_a_BARE_LIST"),
    ("the SP ASIN is sent lower-case", WRITER,
     '"expression": [{"type": "ASIN_SAME_AS", "value": term.upper()}]',
     '"expression": [{"type": "ASIN_SAME_AS", "value": term}]', "test_sp_negative_product_is_ASIN_SAME_AS"),
    ("SB target refusals inside a 200 are ignored", WRITER,
     'for item in body.get("createTargetErrorResults") or []:', "for item in []:",
     "test_sb_negative_product_is_a_200"),
    ("undo tries to remove an SB negative", REPO,
     'if r.entity_id.startswith("neg-") or (r.writer or "").startswith("neg_sb"):',
     'if r.entity_id.startswith("neg-"):', "test_a_negate_run_is_ledgered"),
    ("the delete accepts an SB negative", WRITER,
     "if writer not in (W_SP_KEYWORD, W_SP_PRODUCT):", "if False:", "test_undo_removes_an_SP_negative"),
    ("ran-today ignores the window", REPO, 'key = window or ""', 'key = ""',
     "test_ran_today_is_per_rule_AND_per_window"),
    ("ran-today ignores the IST day", REPO,
     "if not summary or logic.ist_day(created) != day:", "if not summary:",
     "test_ran_today_uses_the_IST_day"),
    ("the ledger drops the window", REPO, 'rule_window=(rule_window or "")[:23] or None,', "rule_window=None,",
     "test_ran_today_is_per_rule_AND_per_window"),
    ("a negative Amazon already holds is sent again", ROUTE,
     "to_send = [c for c in wanted if c not in already]", "to_send = list(wanted)",
     "test_apply_sends_only_ticked_keys"),
    ("apply takes every planned row, not the ticked keys", ROUTE,
     'wanted = [c for c in plan["changes"] if c["key"] in keys]', 'wanted = list(plan["changes"])',
     "test_apply_refuses_a_forged_key"),
    ("SB is sent without confirmation", ROUTE,
     'if any(c["no_undo"] for c in wanted) and not body.get("confirm_sb"):', "if False:",
     "test_apply_refuses_a_forged_key"),
    ("a partly-held SB window is summed short", ROUTE,
     'rows = [r for r in rows if r["ad_product"] != "sb"]', "rows = rows",
     "test_sponsored_brands_missing_days"),
    ("a keyword rule may add negatives", REPO,
     "if (level == logic.LEVEL_SEARCH) != (action == logic.ACTION_NEGATE):", "if False:",
     "test_a_negate_action_belongs_only"),
    ("a re-stored day doubles", STORE,
     "AdsSearchTermDaily.day.in_(days), AdsSearchTermDaily.ad_product == product))",
     "AdsSearchTermDaily.day.in_([]), AdsSearchTermDaily.ad_product == product))",
     "test_search_terms_sum_per_ad_group"),
    ("an empty negatives fetch wipes the cache", STORE,
     "    if not rows:\n        return 0\n    now = datetime.utcnow()\n    await db.execute(delete(AdsEntity)",
     "    now = datetime.utcnow()\n    await db.execute(delete(AdsEntity)", "test_an_empty_negatives_fetch"),
    ("the negate action is offered on other levels", HTML,
     'o.disabled = search ? o.value !== "negate" : (o.value === "negate" || (product && o.value !== "set_state"));',
     'o.disabled = search ? o.value !== "negate" : (product && o.value !== "set_state");',
     "test_the_screen_has_a_search_terms_level"),
    ("a rule greys on ANY window", HTML,
     '(r.ran_today || {})[(data.window || []).join("..")]', "Object.values(r.ran_today || {})[0]",
     "test_saved_rules_split_into_grow_and_cut"),
    ("keyword apply stops sending the window", HTML,
     "rule: plan.rule, start: data.window[0], end: data.window[1]", "rule: plan.rule",
     "test_keyword_apply_sends_the_window"),
]


def run(expression):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-k", expression,
         "tests/test_ads_negatives.py"],
        capture_output=True, text=True, timeout=600,
    )
    last = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return result.returncode, last


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def main():
    survivors = []
    for label, path, old, new, test_name in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"SKIP      {label}\n          target text not found in {path}")
            survivors.append((label, f"target text not found in {path}"))
            continue
        _write(path, original.replace(old, new, 1))
        try:
            code, summary = run(test_name)
        finally:
            _write(path, original)
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
