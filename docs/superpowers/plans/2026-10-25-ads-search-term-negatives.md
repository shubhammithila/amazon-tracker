# Plan: search-term negatives + Grow | Cut cards (spec `docs/superpowers/specs/2026-10-25-ads-search-term-negatives-design.md`)

Inline, one session.

1. Migration `e9b4c2d7a813`: `ads_search_term_daily`, `ads_mutation.rule_window`,
   `ads_refresh.search_rows/search_error`; detector branch; required tables.
2. Reports: `build_search_report_request(start, end, ad_product)`; `fetch_targeting(report="search")`.
3. Repository: `save_search_term_daily`, `search_days_held`, `sum_search_terms`, `replace_negatives`,
   `load_negatives`, `group_positive_texts`; ledger `action="negate"`, `rule_window`, `ran_today_by_rule`;
   purge.
4. `app/ads/negatives.py` (pure): via, is_asin, term_is_target, metrics, `plan`.
5. logic: `LEVEL_SEARCH`, `SEARCH_FIELDS`, `ACTION_NEGATE`, `min_negative_spend`, `rule_impact`.
6. spapi_ads: `fetch_negatives`, `create_negatives`, `delete_negatives` + outcome parsers.
7. Refresh: search-term phase (SP, then SB after SB keywords), negatives cache.
8. Router `app/routers/ads_negatives.py`: preview, apply; undo route handles `negate`; rules payload
   carries `impact` and `ran_today`.
9. Screen: Search terms level, negative preview + apply, Grow | Cut columns, grey ran-today per window.
10. Tests, mutation harness, full suite, CLAUDE.md, commit, deploy (migration), verify read-only.
