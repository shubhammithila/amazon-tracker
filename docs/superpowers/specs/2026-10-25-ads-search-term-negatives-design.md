# Ads → Optimisation: negative targeting from search terms, and Grow | Cut rule cards

Asked for as *"negative targeting based on search terms not just targets added… in auto campaigns there
is close match, loose match etc… some of the keywords that it targets might be overspending… add that
keyword as a negative target… negative exact only not negative phrase… for the asins/other products
there is a separate negative… make it independent of the turn target off or on"*, plus *"segregate the
positive impact rules… and the negative ones… once we have run a rule its color should change… so we
know it has been run today"* and *"when we change the time period… the rules which were set grey shows
again normally since it has not been run on that time period"*.

## Measured first (25 Oct 2026, read-only)

- `spSearchTerm`, 30 days: **18,545 rows, ₹12.3 lakh**; auto 6,972 (loose 2,021 · close 1,810 ·
  substitutes 1,809 · complements 1,332), phrase 5,841, product/category 3,460, exact 1,539, broad 733;
  ASIN search terms 3,526 rows, ₹3.58 lakh.
- Terms with ₹200+ spend and 0 orders: **413, ₹1.46 lakh — but 301 (₹1.08 lakh) ARE the target**
  (an exact keyword's own text, an ASIN target's own ASIN). Negating those is a pause, which the
  turn-off rule already does. The real negatives: **112 terms, ₹37k**.
- Create calls probed with an invalid ad group (nothing created): SP negative keyword and negative
  product target answer 207 in SP's shape; SB negative keyword 207 as a bare list; **SB negative
  product 200 with the error inside** (`createTargetErrorResults`). SP delete (undo) is 207 too.

## Decisions (the owner's)

| | |
|---|---|
| Where the negative goes | the AD GROUP the term overspent in |
| Products | Sponsored Products **and** Sponsored Brands |
| ASIN terms | negative PRODUCT target; word terms negative EXACT keyword; one rule, auto-detected |
| Rule cards | two columns, Grow (green) and Cut (red); ran today = grey, still usable |
| Ran today | per rule AND per time period |

## The rule (level `search`, action `negate`)

- A third level in the rule builder, **Search terms**, beside Keywords & targets and Products (SKUs).
  Its only action is "add as negative". Never touches a keyword, target or SKU, and no other level adds
  a negative.
- **Grain: a search term within one ad group**, summed over the window across the targets that brought
  it in (`ads_search_term_daily`, per day, so any window inside the held days is a GROUP BY).
- **Conditions:** spend, sales, CPC, ROAS, ACOS, CTR, conversion, clicks, impressions, orders, plus
  **came in via** (close / loose / substitutes / complements / broad / phrase / exact / category /
  product / theme) and **term is** (a search phrase / a product ASIN), plus text contains.
- **Never negated, each named in the preview:**
  - the term is one of the ad group's OWN keywords or ASIN targets (term-is-the-target);
  - already negated in that ad group (Amazon errors on a duplicate);
  - campaigns managed by M19 or Amazon (`logic.manager_of`);
  - spend under `min_negative_spend` (new guardrail, ₹100), the pause floor's sibling;
  - more than `max_rows` → the run is blocked.
- **Word term → negative EXACT keyword; ASIN term (`b0` + 8) → negative product target (ASIN same as).**
- Sentence prefix `Search terms: `, verb `add as negative`; a different rule from any keyword rule.

## Applying

`POST /ads/negatives/apply` re-runs the same plan on the server and accepts only rows the plan approves
(the client sends keys only), re-reads the live negatives of those ad groups and drops duplicates,
writes the ledger first (`action="negate"`, provisional id → Amazon's id), then creates:

| | Endpoint | Body |
|---|---|---|
| SP keyword | `POST /sp/negativeKeywords` | `{negativeKeywords: [{campaignId, adGroupId, keywordText, matchType: NEGATIVE_EXACT, state: ENABLED}]}` |
| SP product | `POST /sp/negativeTargets` | `{negativeTargetingClauses: [{campaignId, adGroupId, expression: [{type: ASIN_SAME_AS, value}], state: ENABLED}]}` |
| SB keyword | `POST /sb/negativeKeywords` | `[{campaignId, adGroupId, keywordText, matchType: negativeExact}]` |
| SB product | `POST /sb/negativeTargets` | `{negativeTargets: [{campaignId, adGroupId, expressions: [{type: asinSameAs, value}]}]}` |

**Undo:** SP negatives are deleted (`/sp/negativeKeywords/delete`, `/sp/negativeTargets/delete`).
**SB negatives cannot be undone** — Amazon's archive is permanent — so they are tagged on screen, need
`confirm_sb`, and undo skips them, saying so.

## Data

- `ads_search_term_daily` (day, ad_product, campaign, ad group, keyword/target id and text, match type,
  targeting, search term, impressions, clicks, cost, orders, sales), 60-day retention like the keyword
  rows. Filled by a daily `spSearchTerm` report in the 08:00 refresh as its own isolated phase; the
  `sbSearchTerm` report only after that run's SB keyword report landed (SB reports are rationed).
- Existing negatives cached in `ads_entity` as `neg_keyword` / `neg_target`, replaced whole each refresh.
- `ads_mutation.rule_window` (`start..end`), so "ran today" is per rule and time period.
- `ads_refresh.search_rows`, `search_error`.

## Grow | Cut

`logic.rule_impact`: increase / set / turn ON → grow; decrease / turn OFF / negate → cut. The saved-rules
panel shows two columns; a card whose sentence has an APPLIED change today **for the window on screen**
is grey with "✓ ran today HH:MM" and stays usable (the once-a-day limit already holds back its rows).
