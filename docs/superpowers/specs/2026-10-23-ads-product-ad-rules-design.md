# Ads: turn a SKU off (or on) inside an ad group — design

Asked for as *"just like I was able to turn off or on a keyword I should be able to turn off on a
product in an ad group. product means sku. since one asin can have multiple sku's for flex, easy ship
and fba. e.g. in SP keywords, chana sattu ad group, the 0.5kg easy ship sku has spend > 500 but 0
orders in the last 60 days — turn it off."*

## Measured first (23 Oct 2026, production, read-only)

| | |
|---|---|
| SP product ads | **6,404** (6,092 enabled, 312 paused) via `POST /sp/productAds/list`, `application/vnd.spProductAd.v3+json` |
| ad groups with ONE enabled product ad | **952** — pausing it silences every keyword in the group |
| ad groups holding one ASIN under more than one SKU | **1,194** |
| last SKU token | `fba` 2,508 · `flex` 950 · everything else is Easy Ship |

**Sponsored Brands has no per-SKU ad**: an SB ad holds an ASIN list, so there is nothing per SKU to
switch. Product rules are Sponsored Products only.

## Decisions (the owner's)

- **Last active SKU in an ad group: REFUSED.** A pause that would leave an ad group with no enabled
  product ad is not sent; the row is listed as skipped with the reason. Silencing a whole ad group is a
  Seller Central job.
- **Picking the SKU**: a *SKU channel is FBA / Flex / Easy Ship* condition (the Shipment tab's token
  rule, widened to three answers) and a *SKU or ASIN contains …* condition. The existing campaign and
  ad-group scope works as before.

## Design

**1. Data — a separate table, never mixed into the keyword rows.** `ads_product_daily`, one row per
`(day, ad_id)`: campaign, ad group, ASIN, SKU, impressions, clicks, spend, orders, sales. Filled by an
`spAdvertisedProduct` DAILY report (`groupBy advertiser`, columns incl. `adId`, `adGroupId`) in the
Ads refresh, after SP and SB, isolated in its own `try` (`product_error`). Same planning as SP: only
missing days plus the settling tail, 60 days kept, purged with the rest. **A product ad's spend is the
same money as its ad group's keyword spend** — in `ads_performance_daily` every campaign total would
double. The product-ad list (state per ad, per ad group) is cached in `ads_entity` as
`entity_type="product_ad"` (name = SKU), replaced wholesale on each refresh.

**2. Rules.** A rule has a `level`: `keyword` (today's rules, unchanged) or `product`. A product rule
may only `set_state` (a product ad has no bid). Its sentence starts `Products: …`, so a product rule
and a keyword rule with the same numbers are different rules for the once-a-day limit. `ads_rule`
gains `level`. Text/choice conditions (`channel`, `sku_asin`) are product-level only.

**3. Preview** (`POST /ads/preview` with `level: "product"`): completeness of the product table for
every day in the window, then the summed rows through the same `plan_run` (M19/Amazon campaigns
excluded, ₹100 pause floor, per-rule once-a-day limit, notes for other rules). For a pause,
`plan_run(enabled_ads=…)` applies the **last-active guard**: per ad group, if the run would pause every
enabled ad, the best seller among the matched rows is skipped (`SKIP_LAST_ACTIVE`). Enabled ads come
from the cache, overridden by any newer applied state in our ledger. An ad group the cache does not
know keeps one row — the safe default.

**4. Apply.** A fifth writer, `product_ad` → `PUT /sp/productAds {"productAds": [{adId, state}]}`, the
SP 207 shape keyed by `adId`. The live re-read lists the ads of every affected ad group
(`adGroupIdFilter`), which gives both the row's state and its siblings, and the **same guard runs
again on live states** — refused rows are reported as `last_active`. Ledger, undo and the per-rule
limit are unchanged; a payload mixing product and keyword rows is refused.

**5. Screen.** An *Apply to: Keywords & targets / Products (SKUs)* switch above the conditions. In
product mode the action is only *turn the SKU OFF/ON*, conditions offer the product fields, and the
preview columns read SKU · Channel · ASIN · Spend · ROAS · ACOS · State, grouped campaign → ad group as
today. A note says Sponsored Brands is not included and why. Saved rules remember their level.

## Tests
Last-active guard (preview and apply, live siblings), channel classification (FBA/Flex/Easy Ship incl.
underscore SKUs), product rows never reaching `sum_daily`, payload shape and 207 parsing for
`/sp/productAds`, the page posting `level`, rule sentence identity per level, migration detector.
Mutation entries in `scripts/mutate_ads_state.py`.
