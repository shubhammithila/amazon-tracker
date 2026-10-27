"""Mutation harness for Ads → Keyword growth. Throwaway; not imported by the app.

Each entry breaks ONE decision from `docs/superpowers/specs/2026-10-26-ads-keyword-growth-design.md`
and names the test that must catch it. A SKIP (target text not found) counts as a survivor.

    venv/Scripts/python scripts/mutate_ads_growth.py
"""
import pathlib
import subprocess
import sys

CORE = pathlib.Path("app/ads/growth.py")
JUDGE = pathlib.Path("app/ads/growth_judge.py")
PAGES = pathlib.Path("app/ads/pages.py")
ROUTE = pathlib.Path("app/routers/ads_growth.py")
HTML = pathlib.Path("templates/ads_growth.html")

MUTATIONS = [
    ("a keyword held in some state is offered again", CORE,
     "missing = [m for m in MATCHES if (term, m) not in held_keywords]", "missing = list(MATCHES)",
     "test_anything_held_in_any_state"),
    ("a negated keyword is offered", CORE, "        if term in negatives:\n            dropped", "        if False:\n            dropped",
     "test_anything_held_in_any_state"),
    ("a targeted ASIN is offered again", CORE, "if term.upper() in held_asins:", "if False:",
     "test_anything_held_in_any_state"),
    ("an ASIN becomes a keyword", CORE, "if (kind == KIND_PRODUCT) != is_asin(term):", "if False:",
     "test_candidates_merge"),
    ("auto ad groups get a card", CORE, "        if kind == KIND_AUTO:\n            continue\n", "",
     "test_cards_are_enabled"),
    ("M19/Amazon campaigns get a card", CORE, "if logic.manager_of(campaign.get(\"name\")) != logic.MANAGER_US:",
     "if False:", "test_cards_are_enabled"),
    ("keywords go as Exact only", CORE, '(s.get("missing_matches") or list(MATCHES))', '["EXACT"]',
     "test_every_keyword_goes_as_exact_and_phrase"),
    ("the max-bid guardrail is skipped", CORE, "if max_bid and bid > max_bid:", "if False:",
     "test_every_keyword_goes_as_exact_and_phrase"),
    ("no suggestion no longer falls back to custom", CORE, "if mode == BID_LOW and low:", "if mode == BID_LOW:",
     "test_the_bid_is_amazons_low_end"),
    ("a term Claude skips is kept", JUDGE, 'said.get(c["term"], (False, "not judged"))',
     'said.get(c["term"], (True, "not judged"))', "test_the_judge_keeps_what_claude_keeps"),
    ("cached verdicts are asked again", JUDGE, 'todo = [c for c in candidates if c["term"] not in cached]',
     "todo = list(candidates)", "test_the_judge_keeps_what_claude_keeps"),
    ("a Bedrock failure keeps everything", JUDGE,
     'raise JudgeError(f"The relevance check failed: {exc}") from exc',
     'return {"verdicts": {c["term"]: (True, "") for c in candidates}, "ideas": [], "judged": 0, "cached": 0}',
     "failed"),
    ("ideas are asked for on every Find", JUDGE, "and not ideas and not asked else None", "and not ideas else None",
     "test_ideas_are_asked_for_once"),
    ("the daily cap is not checked", JUDGE, "if await used_today(db) >= cap:", "if False:",
     "test_a_failed_judge_raises_and_the_daily_cap_refuses"),
    ("a fresh page is read again", PAGES, "now - row.fetched_at < timedelta(days=max_age_days)", "False",
     "test_a_page_is_reread_only_when_stale"),
    ("a rejected row can be added", ROUTE, "unknown = [k for k in keys if k not in by_key or not by_key[k].keep]",
     "unknown = [k for k in keys if k not in by_key]", "test_add_sends_exact_and_phrase"),
    ("Add skips the live re-read", ROUTE,
     'or (r["kind"] == growth.KIND_KEYWORD and (r["text"], r["match_type"]) in held["keywords"]))]',
     "or False)]", "test_add_drops_a_match_amazon_already_holds"),
    ("an added keyword stays on the list", ROUTE, "        if added_keys:\n", "        if False:\n",
     "test_add_sends_exact_and_phrase"),
    ("the screen sends no bid choice", HTML,
     "JSON.stringify({ad_group_id: groupId, keys, bid_mode: mode, custom_bid: custom, progress_id: bar.id})",
     "JSON.stringify({ad_group_id: groupId, keys, progress_id: bar.id})", "test_the_screen_posts_keys"),
    ("a left-out tick adds instead of moving", HTML, "left.map(s => rowHtml(card, s, leftSet))",
     "left.map(s => rowHtml(card, s, set))", "test_left_out_rows_are_tickable_only_to_MOVE"),
    ("one bid choice for the whole page", HTML, "const {mode, custom} = bidMode[groupId];",
     "const {mode, custom} = bidMode[Object.keys(bidMode)[0]];", "test_the_screen_posts_keys"),
    ("Move to relevant is not stored", ROUTE, "        row.keep, row.reason = keep, reason\n", "",
     "test_a_left_out_row_moves_to_relevant"),
    ("a moved row is re-judged by the next Generate", ROUTE,
     "            verdict.keep, verdict.reason, verdict.judged_at = keep, reason, now",
     "            verdict.keep, verdict.reason, verdict.judged_at = not keep, reason, now",
     "test_a_left_out_row_moves_to_relevant"),
    ("an unknown key is moved anyway", ROUTE, "if not keys or len(rows) != len(keys):", "if not keys:",
     "test_a_left_out_row_moves_to_relevant"),
    ("the campaign list calls Amazon", ROUTE, "cards = await _cards(db, with_sb_asins=False)",
     "cards = await _cards(db)", "test_the_campaign_list_reads_only_the_database"),
    ("the judge sees only the group's own sizes", ROUTE, 'keys, catalogue = await _product_keys(db, card["asins"], family=True)',
     'keys, catalogue = await _product_keys(db, card["asins"])', "test_find_tells_the_judge_every_size"),
    ("the pack sizes never reach the prompt", ROUTE, 'page_text = ((sizes + "\\n\\n") if sizes else "") + ',
     'page_text = "" + ', "test_find_tells_the_judge_every_size"),
    ("an inactive size is offered", ROUTE, 'if key not in wanted or entry.get("active") is False:',
     'if key not in wanted:', "test_pack_sizes_name_every_active_size"),
    ("old-rules verdicts are reused", JUDGE,
     'return row.judged_at >= RULES_SINCE or str(row.reason or "").startswith("Moved to")',
     'return True', "test_verdicts_from_the_old_rules_are_judged_again"),
    ("a hand move is re-judged", JUDGE,
     'return row.judged_at >= RULES_SINCE or str(row.reason or "").startswith("Moved to")',
     'return row.judged_at >= RULES_SINCE', "test_verdicts_from_the_old_rules_are_judged_again"),
    ("competitors lose their tag", JUDGE, 'if keep and v.get("competitor") and not growth.is_competitor(reason):',
     'if False:', "test_a_kept_competitor_is_tagged"),
    ("the size rule is back", JUDGE, "PACK SIZES NEVER DECIDE A VERDICT.",
     "Reject a pack size or weight the ad group does not sell.", "test_the_rules_never_reject_for_a_pack_size"),
    ("custom only fills rows with no suggestion", CORE, "    if mode == BID_LOW and low:",
     "    if low:", "test_a_custom_bid_goes_on_EVERY_row"),
    ("the bid note ignores the choice", HTML, '  if(bm.mode === "custom") return "Every row',
     '  if(false) return "Every row', "test_the_bid_note_follows_the_choice"),
    ("the kinds cache ignores a new refresh", ROUTE,
     'if _kinds_cache["map"] is not None and _kinds_cache["version"] == version:',
     'if _kinds_cache["map"] is not None:', "test_group_kinds_are_cached"),
    ("a campaign shows every ad group", ROUTE, '             if c["campaign_id"] == campaign_id]',
     '             if True]', "test_a_campaign_shows_its_own_ad_groups"),
]


def run(expression):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-k", expression, "tests/test_ads_growth.py"],
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
