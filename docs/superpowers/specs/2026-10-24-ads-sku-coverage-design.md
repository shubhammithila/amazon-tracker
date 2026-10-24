# Ads → SKU coverage: every SKU of an advertised product is in its ad group

Asked for as *"check that all the sku's of a parent has been added to that particular ad group where
it should — whether it is active or inactive is a different thing but it should be added. in SP. SB
works in a different way… maybe we do the list in fba and flex but forgot to add it in the ad group
where the easy ship sku was added. the parent and its sku's dependency will be the same which we
follow in the portfolio tab… in some ad groups we have added all the flavours of chana in one ad group
and somewhere each flavour has a different ad group."*

## Measured first (read-only, 24 Oct 2026)

- 6,404 SP product ads; 110 enabled ad groups in enabled campaigns we manage (119 paused groups,
  1,821 in campaigns not enabled, 185 in M19/Amazon campaigns skipped).
- **8 ad groups miss another SKU of an ASIN they already advertise: 77 SKUs** (Easy Ship 33, Flex 30,
  FBA 14), e.g. `HF_SP_Keywords › panchphoran` lacks `HF_PanPho_0.2kg_flex` and `HF_PanPho_0.5kg_flex`.
- 1 ad group misses sizes or flavours of its products — Bestsellers, a deliberate mixed group.
- `GET_MERCHANT_LISTINGS_ALL_DATA`: 688 SKUs in 32 s (Active 235, Inactive 380, Incomplete 73).
- `POST /sp/productAds` with an invalid ad group answers **207** with SP's
  `{"productAds": {"success": [], "error": [{index, errors}]}}` shape and creates nothing.

## Decisions (the owner's)

| Question | Answer |
|---|---|
| On a gap | show it, plus a one-click Add of the ticked rows |
| What counts | every SKU of every pack size of each product the group advertises |
| Sheet-inactive sizes (MRP Active = N) | never expected |
| Amazon listing status | Active and Inactive both expected; Incomplete skipped and counted |
| Deliberately mixed groups | "ignore" one product in a group, or the whole group, saved |

## The rule

- **SP only**; enabled campaigns we manage (`logic.manager_of == us`); enabled ad groups.
- **Product = the Portfolio Profit row**: `flavour_keys` (flavour id) else `parent_of_children`
  (Amazon's parent) else the ASIN itself. A flavour-split group expects only its flavour's sizes; a
  group holding every flavour expects every flavour's sizes.
- **Expected** = for each product the group advertises, every listing SKU of every child ASIN, minus
  ASINs the sheet marks explicitly inactive and Incomplete listings.
- **Present** = any SKU with a product ad in the group, ENABLED or PAUSED. A paused SKU is the
  owner's decision; an absent one is the gap.
- **Gap kind**: `sku` when the group already holds that ASIN, `size` when it does not.

## Data

- `listing_sku` (new): seller_sku (unique), asin, status, fulfillment_channel, item_name, fetched_at.
  Replaced whole on a good read; an empty or failed read writes nothing. The last attempt (when,
  rows, error) is stored in `portfolio_settings` under `listings_refresh`.
- `ads_coverage_ignore` (new): ad_group_id, product_key (`*` = the whole group), created_at.
  UNIQUE (ad_group_id, product_key).
- `ads_entity.asin` (new column) for product ads, filled by the refresh. Until then the listings
  map SKU → ASIN.
- One migration, with a detector branch in `deploy/update-ec2.sh`.

**The listings are read inside the 08:00 IST Ads refresh**, as their own isolated phase, and on
demand from the panel ("Re-read listings", ~1 minute).

## Screen

A **SKU coverage** button beside Run history opens a panel: campaign → ad group → product → missing
SKU rows (SKU · ASIN · size · channel · listing status · kind), with tick boxes (per group and per
row), **Ignore** per product and per group, "N ignored · show", **Add selected**, and **⬇ Excel**. A
line says when the listings were read; a failed read is a banner and the previous reading stands.

## Adding (the only write)

`POST /ads/coverage/add {rows: [{ad_group_id, sku}]}`:

1. Each row must be a CURRENT gap by the server's own computation — anything else is refused and
   named. The client is not a trust boundary.
2. `max_rows` guardrail.
3. Re-read the live product ads of those ad groups (`fetch_product_ads(ad_group_ids, states=())`);
   a SKU now present in any state but ARCHIVED is dropped and reported (`already_there`).
4. Ledger first: `action="create"`, writer `product_ad`, `new_state=ENABLED`, a provisional
   `entity_id` (`new-` + 20 hex of sha1(ad group + SKU)) replaced by Amazon's `adId` on success.
5. `POST /sp/productAds` `{campaignId, adGroupId, sku, state: ENABLED}`, 500 a batch; per-row
   outcome from the SP 207 shape; an unmentioned row is failed.
6. The new ads are written into the cached list so the panel updates at once.

**Undo pauses them** (`build_undo` turns an applied `create` into ENABLED → PAUSED). Amazon has no
delete. Nothing is scheduled; adding only happens on the button.

## Tests

`tests/test_ads_coverage.py`: the pure check over hand-built groups (flavour-split vs all-flavour,
sheet-inactive size, Incomplete listing, paused SKU present, ignored product and group, SB and
managed and paused groups skipped), the add route posting exactly what the screen sends (stale row
refused, live duplicate dropped, ledger before wire, adId recorded, cache updated), undo pausing,
the create outcome parser, the listings parse. `scripts/mutate_ads_coverage.py`.
