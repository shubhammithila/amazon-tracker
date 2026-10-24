# Plan: Ads SKU coverage (spec `docs/superpowers/specs/2026-10-24-ads-sku-coverage-design.md`)

Inline, one session. Each step ends with its tests green.

1. **Migration + models.** `listing_sku`, `ads_coverage_ignore`, `ads_entity.asin`; detector branch
   newest first in `deploy/update-ec2.sh` (keyed on `listing_sku`); required tables list.
2. **`app/ads/listings.py`.** `parse_rows(report_rows)` (BOM-tolerant headers, skip blank SKU),
   `fetch(client)` (create → poll → download via `app/repeat/fetch.py` helpers, quota waits),
   `refresh(db_factory, client)` storing rows + `listings_refresh` status, never raising.
3. **Repository.** `replace_listings`, `load_listings`, `listings_status`, `save_listings_status`,
   ignore add/remove/load, `replace_product_ads` stores `asin`, `add_product_ads_to_cache`,
   ledger: `open_run` honours `action="create"`, `record_create_results` (provisional id → adId),
   `build_undo` for create.
4. **`app/ads/coverage.py` (pure).** `compute(product_ads, campaigns, ad_groups, listings, row_of,
   catalogue, ignores)` → groups with missing rows and counts; `provisional_id`.
5. **Writer.** `spapi_ads.create_product_ads(client, rows)` + `_parse_create_outcome`.
6. **Routes.** `GET /ads/coverage`, `GET /ads/coverage.xlsx`, `POST /ads/coverage/listings`,
   `POST /ads/coverage/ignore`, `DELETE /ads/coverage/ignore`, `POST /ads/coverage/add`.
7. **Refresh.** Listings phase in `refresh.run` (isolated); product ads with asin.
8. **Screen.** Coverage button + panel in `templates/ads.html`; Run-history words for "added".
9. **Tests + mutations**, full suite, CLAUDE.md, commit, deploy (migration: check the script out
   first), verify on production read-only against the probe's 8 groups / 77 SKUs.
