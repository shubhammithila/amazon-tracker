# Ads product-ad (SKU) rules — implementation plan

Spec: `docs/superpowers/specs/2026-10-23-ads-product-ad-rules-design.md`. Executed inline.

1. **Migration `c5e9a2f71d36`** (revises `b8e4d1f27c60`): create `ads_product_daily` (unique
   `(day, ad_id)`, index `(day, ad_group_id)`); `ads_rule.level` (`String(10)`, default `keyword`);
   `ads_refresh.product_rows`, `ads_refresh.product_error`. Model classes. Detector branch
   `"ads_product_daily" in tables` newest first; required-tables list.
2. **logic**: `WRITER_PRODUCT_AD`, `LEVEL_KEYWORD/LEVEL_PRODUCT`, `sku_channel`, product FIELDS
   (`channel` choice, `sku_asin` text) and their operators; `condition_error(level=)`; `matches` for
   text/choice; `product_metrics`; `plan_run(level=, enabled_ads=)` with `SKIP_LAST_ACTIVE` and
   `guard_last_active` (pure, shared with apply); `split_by_writer` and totals know product ads.
3. **reports**: `build_product_report_request`; `fetch_targeting(report="product")`.
4. **spapi_ads**: `PRODUCT_ADS` endpoint, `fetch_product_ads(client, ad_group_ids=None)`;
   `fetch_current_bids` reads product ads by ad group (state + `ad_group_id`); `apply_changes` writer
   `product_ad`.
5. **repository**: `save_product_daily`, `product_days_held`, `product_range_completeness`,
   `sum_product_daily`, `replace_product_ads`, `enabled_product_ads`; purge; `open_run` entity type;
   `save_rule`/`load_rules` level; `record_refresh`/`last_refresh` product fields.
6. **refresh**: product-ad list in the entity phase; product report phase, isolated.
7. **router**: `_rule_summary(level=)`; preview level branch; apply product checks (mixed refusal,
   live last-active guard → `last_active`); saved-rule stamping uses level; GET payload carries
   `product_fields`, `levels`.
8. **page**: level switch, product fields, action limited, preview columns, saved rules' level, note.
9. **tests** `tests/test_ads_product_ads.py`; mutation entries; full suite; CLAUDE.md; commit, deploy,
   verify on production with a preview (never Apply).
