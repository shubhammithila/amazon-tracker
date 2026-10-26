# Ads → Keyword growth: relevant keywords and competitor targets, added at a bid you choose

Date: 26 Oct 2026. Asked for as *"Keyword/target addition new basis suggested keywords at a bid I
choose… suggested bid lower end or a custom bid… other keywords from the internet… the product detail
page… title bullets description… Product targets as well addition in the category ads where we add
competitor targets… leave out the keywords which are in negative or archived or paused… exact and
phrase… very relevant and close to the product detail page… sattu should not have just atta… both for
sb and sp"*.

## Decisions (the owner's)

| Question | Decision |
|---|---|
| Relevance | **Claude reads the product page** and keeps only close matches, each with a reason |
| Keyword sources | our search terms that sold · Amazon's suggested keywords (SP + SB) · amazon.in autocomplete · Claude's own ideas from the page |
| Competitor sources | competitor ASINs from our search terms that sold · Amazon's suggested product targets (SP) |
| Bid | per add: **Amazon's low end** or a **custom ₹**; rows with no Amazon suggestion (all SB, autocomplete, page ideas) take the custom ₹ |
| Match type | every keyword added as **Exact AND Phrase** |
| Already in another ad group | still suggested, no flag |
| When | **on demand per ad group**, cached |
| Tabs | **Optimisation · SKU coverage · Keyword growth** (Expansion renamed SKU coverage) |

## Measured first (26 Oct 2026, read-only, nothing created)

| Source | Result |
|---|---|
| Catalog API `GET /catalog/2022-04-01/items/{asin}` (`attributes,summaries`) | title, 6 bullets, description of our ASIN |
| `POST /sp/targets/keywords/recommendations` v5 (`KEYWORDS_FOR_ASINS`) | 200: keywords ranked, impression share, a `bid` per match type in an unknown unit |
| `POST /sb/recommendations/keyword` (`vnd.sbkeywordrecommendation.v3+json`) | 200: keywords, impression share, **no bid** |
| `POST /sp/targets/products/recommendations` (`vnd.spproductrecommendation.v3+json`) | 200: competitor ASINs with themes ("Top converting targets", "frequently viewed together") |
| `GET https://www.amazon.in/suggestions?...&mid=A21TJRUUN4KGV&alias=aps` | works from the EC2 box (gzip); includes competitor brands ("chana sattu rajdhani"). `completion.amazon.in` is a 502 |
| Our search terms with orders (30 d) not yet exact/phrase in their ad group | **2,127 terms, 4,081 orders**, 588 of them ASINs |

**The v5 bids are not rupees.** For "rice" EXACT v5 said 1029 / 1372 / 1715 while
`/sp/targets/bid/recommendations` (the Ads tab's verified rupee endpoint) said ₹5.75 / 7.66 / 9.57 — a
ratio of ~179, not 100. So **keyword lists come from v5, every bid from `/sp/targets/bid/recommendations`**
(which answered for a keyword the group did not yet hold). SB has no suggested bid (all candidates 404,
measured 2026-09).

## Shape

**`/ads-page/growth`, `templates/ads_growth.html`**, one card per enabled ad group in an enabled campaign
we manage (`logic.manager_of`), SP and SB. A card's KIND comes from what it already targets:

| Kind | Holds | Gets |
|---|---|---|
| keyword | exact/phrase/broad keywords | keyword suggestions |
| product | product or category targets | competitor ASIN suggestions |
| auto | auto targeting only | nothing (Amazon allows no manual targets there) — not shown |

A card lists what the group advertises (SP product ads' ASINs; SB ads' declared `creative.asins`) and a
**Find keywords** / **Find competitors** button.

### Find (~30-60 s, progress bar as on Apply)
1. **Page text** of each advertised product (the Profit row: flavour key, else Amazon's parent; one
   representative ASIN each) from the Catalog API; cached in `product_page` for 7 days.
2. **Candidates**: keywords — sold search terms of this group (orders > 0, window = the screen's), v5
   suggestions for its ASINs (SP) / SB suggestions (SB), autocomplete for the product's core phrase(s)
   (the leading noun phrase Claude extracts from the title, max 3 prefixes), and Claude's own ideas (≤ 15).
   Competitors — ASIN search terms with orders in this group, and SP product-target suggestions for its
   ASINs, each with its Catalog title.
3. **Already there, in ANY state**: the group's live keywords/targets listed from Amazon with every state
   (SP `/sp/keywords/list` and `/sp/targets/list` by `adGroupIdFilter`; SB lists filtered locally), plus
   cached negatives. Dropped and counted, never shown as addable.
4. **Claude judges** the rest against the page (one call per product, ≤ ~150 candidates, verdicts
   cached in `growth_verdict` for 30 days). Keep only phrases a shopper would type for THIS product;
   reject single generic words ("atta", "flour"), other products ("besan" for sattu), competitor brand
   names, pack sizes the group does not sell. A competitor ASIN is kept only when its title is the same
   kind of product.
5. **Bids** for kept SP keywords from `/sp/targets/bid/recommendations` (low / suggested / high), batched
   per ad group.

**Claude fails or times out → nothing is offered**, and the card says the relevance check could not run.
An unfiltered list is exactly what the owner asked not to see.

### What a row shows
term · source tags (sold here · Amazon · autocomplete · from the page) · our orders/spend/ROAS when it
was a search term · Amazon low/suggested bid (SP) · Claude's reason · tick. Rejected rows sit in a folded
list with their reasons; already-there rows as a count by state.

### Add (a WRITE to Amazon)
`POST /ads/growth/add` with `{ad_group_id, keys, bid_mode: "low"|"custom", custom_bid}`. The server
rebuilds the group's stored suggestion list, keeps only KEPT rows whose key was sent (a rejected or
stale key → 409), re-reads the live keywords/targets and drops `already_there`, applies the bid
(low end, else custom; no bid and no custom → not sent), checks `max_bid` and `max_rows`, writes the
ledger first (`action="create"`, provisional ids), sends, takes Amazon's ids. **Each keyword goes as
two rows: EXACT and PHRASE** (SB `exact`/`phrase`). Progress bar as on Apply. **Undo pauses** what was
added (Amazon has no delete for keywords/targets).

Create endpoints, each probed with an invalid ad group before use (nothing created):
`POST /sp/keywords`, `POST /sp/targets` (`ASIN_SAME_AS`), `POST /sb/keywords`, `POST /sb/targets`
(`asinSameAs`). Their 207/200 shapes get their own parsers, as for negatives.

## Storage (one migration)

| Table | Key | Holds |
|---|---|---|
| `product_page` | asin | title, bullets (JSON), description, fetched_at |
| `growth_verdict` | (product_key, kind, term) | keep (bool), reason, judged_at |
| `growth_suggestion` | (ad_group_id, key) | ad_product, kind, term, match/asin, sources (JSON), figures (JSON), verdict, reason, bids (JSON), found_at |

`growth_suggestion` is replaced whole per ad group on each Find. Retention: verdicts 30 days, pages 7.

## Cost and limits
One Claude call per product per Find, ~8-15k input / 2-4k output tokens, a few rupees; cached verdicts
make a repeat Find nearly free. `GROWTH_DAILY_LIMIT` (default 60 Finds per IST day). Logged in
`assistant_log` with kind `growth`.

## Tabs
`templates/_ads_tabs.html`: Optimisation (`/ads-page`) · SKU coverage (`/ads-page/expansion`, renamed) ·
Keyword growth (`/ads-page/growth`). The URL of SKU coverage stays, so bookmarks work.

## Testing
- Unit: candidate merge and source tags; the any-state exclusion; Claude verdict parsing (missing term =
  rejected); bid choice; Exact+Phrase doubling; the four create payloads and parsers.
- Routes: exactly what the screen sends; a rejected or forged key refused; a term Amazon already holds
  dropped; max-bid enforced; Claude faked everywhere (conftest), Amazon faked.
- `scripts/mutate_ads_growth.py`: any-state exclusion, rejected rows addable, forged keys, max_bid,
  SB custom-bid fallback, Exact+Phrase doubling, Claude failure shows nothing.
- Production read-only: a real Find on one keyword and one category ad group; the owner reviews kept and
  rejected lists before any add.

## Out of scope
Broad match; negatives (Optimisation has them); budgets; keyword research beyond the four sources; auto
ad groups.
