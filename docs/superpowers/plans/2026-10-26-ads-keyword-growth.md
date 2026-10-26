# Ads Keyword Growth Implementation Plan

> **For agentic workers:** executed INLINE (superpowers:executing-plans) per the owner's standing rule —
> no subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A third Ads sub-tab, Keyword growth, that finds keywords (SP + SB) and competitor ASIN targets
(SP + SB) relevant to each ad group's products — filtered by Claude against the product page — and adds
the ticked ones at Amazon's low-end bid or a custom bid, every keyword as Exact AND Phrase.

**Architecture:** Pure core (`app/ads/growth.py`) decides cards, candidates, exclusions, bids and the
rows to send. Amazon reads and creates live in `app/ads/spapi_ads.py` beside the other writers; the
Catalog read in `app/ads/pages.py`; the Claude judge in `app/ads/growth_judge.py` (Bedrock via
`app.assistant.bedrock.converse`). Route `app/routers/ads_growth.py`; screen `templates/ads_growth.html`.
The write path copies `/ads/coverage/add`: server-side rebuild, keys only, live re-read, ledger first,
progress bar, undo = pause (`build_undo` already pauses `create` rows through the row's own writer).

**Tech Stack:** FastAPI, SQLAlchemy async, SQLite, Alembic, httpx, vanilla JS, Bedrock Converse.

## Global Constraints
- Spec: `docs/superpowers/specs/2026-10-26-ads-keyword-growth-design.md`.
- Tabs: **Optimisation · SKU coverage · Keyword growth**; SKU coverage keeps `/ads-page/expansion`.
- Every keyword is sent as TWO rows, EXACT and PHRASE (SB `exact` / `phrase`). Never BROAD.
- Bids: `low` = Amazon's low end from `/sp/targets/bid/recommendations`; `custom` = the typed ₹; a row with
  no suggestion (all SB, autocomplete, page ideas) takes the custom ₹; no bid and no custom → not sent.
  Every bid ≤ `max_bid` guardrail; rows ≤ `max_rows`.
- Exclude anything the ad group holds in ANY state (enabled, paused, archived) and any cached negative.
- Claude failure → nothing offered for that product, the card says why.
- No test may call Amazon or Bedrock; conftest already refuses Bedrock.
- Colour only via `static/theme.css` variables; new fragments listed in `tests/test_theme.py` FRAGMENTS.
- Never press Add/Apply during testing; probes only with an invalid ad group.
- Every new migration adds a branch to `deploy/update-ec2.sh`'s detector, newest first.
- Commits end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Storage, tabs, empty page
**Files:** Create `alembic/versions/a1c5e8f20b74_ads_keyword_growth.py`; modify `app/models.py`,
`deploy/update-ec2.sh`, `templates/_ads_tabs.html`, `app/main.py`; create `templates/ads_growth.html`;
test `tests/test_ads_growth.py`.
**Produces:** models `ProductPage(asin PK, title, bullets_json, description, fetched_at)`,
`GrowthVerdict(id, product_key, kind, term, keep, reason, judged_at; UNIQUE(product_key, kind, term))`,
`GrowthSuggestion(id, ad_group_id, key, ad_product, kind, term, sources_json, figures_json, keep, reason,
bids_json, found_at; UNIQUE(ad_group_id, key))`; route `/ads-page/growth` (area ads).
- [ ] Test: tabs read "Optimisation", "SKU coverage", "Keyword growth" in that order on all three pages;
      `/ads-page/growth` 200 signed in; detector answers `a1c5e8f20b74` on a migrated DB.
- [ ] Migration (revises `e9b4c2d7a813`), models, detector branch `elif "growth_suggestion" in tables`.
- [ ] Tabs partial + route + empty page skeleton. Run tests; commit.

### Task 2: Product page text from the Catalog API
**Files:** Create `app/ads/pages.py`; test in `tests/test_ads_growth.py`.
**Produces:** `parse_catalog(payload) -> {"title", "bullets": [str], "description"}` (pure; strips HTML
tags from the description); `async load_pages(db, asins, *, fetch=None, max_age_days=7) -> {asin: page}`
(stale or missing → `fetch(asin)` = `spapi._get("/catalog/2022-04-01/items/{asin}", marketplaceIds,
includedData=attributes,summaries)`; a failed fetch keeps an older row, or omits the ASIN).
- [ ] Tests: parse a captured-shape payload; cached row under 7 days not refetched; stale refetched;
      failed fetch keeps the old row.
- [ ] Implement; run; commit.

### Task 3: Amazon reads for candidates and for "already there"
**Files:** modify `app/ads/spapi_ads.py`; tests in `tests/test_ads_growth.py`; conftest stubs.
**Produces:**
- `async fetch_sp_keyword_suggestions(client, asins) -> [{"term", "rank", "share"}]`
  (`/sp/targets/keywords/recommendations`, `vnd.spkeywordsrecommendation.v5+json`, `KEYWORDS_FOR_ASINS`,
  max 200; the v5 `bid` is ignored — not rupees, measured).
- `async fetch_sb_keyword_suggestions(client, asins) -> [{"term", "rank", "share"}]`
  (`/sb/recommendations/keyword`, `vnd.sbkeywordrecommendation.v3+json`, `type == "addKeyword"`).
- `async fetch_product_target_suggestions(client, asins) -> [{"asin", "themes"}]`
  (`/sp/targets/products/recommendations`, `vnd.spproductrecommendation.v3+json`, count 50).
- `async fetch_autocomplete(client, prefix) -> [str]` (`https://www.amazon.in/suggestions`,
  `mid=A21TJRUUN4KGV&alias=aps&limit=11&suggestion-type=KEYWORD`, browser UA; failure → `[]`).
- `async fetch_group_targeting(client, ad_group_id, ad_product) -> {"keywords": {(text, MATCH)},
  "asins": {asin}, "states": Counter}` — every state: SP `/sp/keywords/list` + `/sp/targets/list` with
  `adGroupIdFilter`; SB `fetch_sb_keywords(states=())` + `fetch_sb_targets(states=())` filtered locally.
- conftest: autouse stubs for the five so no test reaches Amazon.
- [ ] Tests with fake clients for each parser (shape captured on 26 Oct); autocomplete failure → [].
- [ ] Implement; run; commit.

### Task 4: The pure core
**Files:** Create `app/ads/growth.py`; tests.
**Produces:**
- `KIND_KEYWORD = "keyword"`, `KIND_PRODUCT = "product"`, `KIND_AUTO = "auto"`;
  `SRC_SOLD, SRC_AMAZON, SRC_AUTOCOMPLETE, SRC_PAGE` with labels.
- `group_kind(match_types: set[str]) -> str` (EXACT/PHRASE/BROAD → keyword; TARGETING_EXPRESSION/THEME →
  product; only PREDEFINED → auto; empty → keyword).
- `cards(groups, campaigns, kinds, asins_by_group, names) -> [card]` (enabled groups in enabled campaigns
  we manage, auto excluded, SP+SB).
- `merge_candidates(kind, sources: {src: [ {term|asin, ...figures} ]}) -> [cand]` — lower-cased,
  whitespace-normalised, one row per term with `sources` list and summed figures; ASIN terms only for
  product kind, word terms only for keyword kind.
- `exclude_existing(cands, held, negatives) -> (kept, dropped_counts)` — a keyword is dropped when the
  group holds it in EXACT or PHRASE in any state; an ASIN when targeted in any state; either when a
  cached negative of that text exists.
- `key_of(ad_product, ad_group_id, kind, term) -> str`; `provisional_id(key, match) -> "new-…"`.
- `choose_bid(row, mode, custom) -> float | None` (`low` → row low else custom; `custom` → custom).
- `rows_to_send(selected, mode, custom, guardrails) -> (rows, refused)` — keyword → EXACT + PHRASE rows;
  ASIN → one row; refused when no bid or bid > max_bid; writer per (ad_product, kind).
- [ ] Tests for each (unequal figures; ASIN vs word routing; any-state exclusion; doubling; max_bid).
- [ ] Implement; run; commit.

### Task 5: The relevance judge
**Files:** Create `app/ads/growth_judge.py`; tests.
**Produces:** `async judge(db, product_key, page, kind, candidates, *, converse=None) -> {term: (keep,
reason)}` — cached verdicts (≤ 30 days) reused; the rest in ONE Converse call with a `verdicts` tool
(`[{term, keep, reason}]`) and, for keywords, `ideas` (≤ 15 extra phrases, each judged keep). Missing term
in the answer = rejected ("not judged"). Raises `JudgeError` on Bedrock failure. Stores new verdicts.
`SYSTEM_PROMPT` states the rules: keep only phrases a shopper would type for THIS product; reject single
generic words, other products, competitor brand names, pack sizes not sold; competitor ASINs kept only when
their title is the same kind of product. Daily cap `GROWTH_DAILY_LIMIT` (60) counted in `assistant_log`.
- [ ] Tests with a fake converse: cached verdicts not re-sent; missing term rejected; ideas added with
      source page; failure raises; cap refuses.
- [ ] Implement; run; commit.

### Task 6: The four create writers
**Files:** modify `app/ads/spapi_ads.py`; tests. First PROBE each endpoint on production with an INVALID
ad group (nothing created) to capture the shapes.
**Produces:** `async create_targeting(client, rows, *, writer) -> [{entity_id, ok, new_id, error}]` for
writers `logic.WRITER_KEYWORD` (`POST /sp/keywords {keywords:[{campaignId, adGroupId, keywordText,
matchType, bid, state:ENABLED}]}`), `WRITER_TARGET` (`POST /sp/targets {targetingClauses:[{…, expression:
[{type: ASIN_SAME_AS, value}], expressionType: MANUAL, bid}]}`), `WRITER_SB_KEYWORD` (`POST /sb/keywords`
bare list, `matchType` lower), `WRITER_SB_TARGET` (`POST /sb/targets {targets:[{…, expressions:[{type:
asinSameAs, value}]}]}`). Batches of 500 (SB 100), ticks progress, unmentioned = failed.
- [ ] Tests per writer: payload and parsed outcome.
- [ ] Implement; run; commit.

### Task 7: Routes
**Files:** Create `app/routers/ads_growth.py`; modify `app/main.py`; tests.
**Produces:** `GET /ads/growth` (cards + last stored suggestions per group);
`POST /ads/growth/find {ad_group_id, start, end}` → gathers sources, live held, judge, bids (SP keywords
via `fetch_bid_recommendations`), replaces `growth_suggestion` rows, returns the card's suggestions;
`POST /ads/growth/add {ad_group_id, keys, bid_mode, custom_bid, progress_id}` → stored kept rows by key
(unknown/rejected key → 409), live re-read drops `already_there`, `rows_to_send`, max_rows, ledger
(`action=create`, `new-` ids), `create_targeting` per writer, `record_create_results`, progress bar.
- [ ] Tests posting exactly what the screen sends; rejected key refused; already-there dropped; doubling
      reaches the writer; judge failure → card error, nothing kept; undo pauses created rows.
- [ ] Implement; run; commit.

### Task 8: The screen
**Files:** `templates/ads_growth.html`; tests (source-level).
Cards per ad group (kind badge, ASINs, Find button, last found time); suggestions table (term, sources,
orders/spend, Amazon low/suggested, reason, tick); folded rejected list with reasons; already-there counts;
sticky bar with bid mode (Amazon low end / custom ₹), Add selected, progress bar (`startApplyProgress`
copy), result + undo. Search box over cards.
- [ ] Source tests: posts `{ad_group_id, keys, bid_mode, custom_bid, progress_id}`; escapes server strings.
- [ ] Browser check locally with seeded suggestions (never Add); commit.

### Task 9: Harness, docs, deploy, production read-only check
- [ ] `scripts/mutate_ads_growth.py` (any-state exclusion, rejected addable, forged key, max_bid, SB custom
      fallback, doubling, judge failure shows nothing); all caught.
- [ ] Full suite (single process); CLAUDE.md section; commit; deploy (migration: check script out first);
      `alembic current`.
- [ ] Production: Find on one keyword ad group and one product ad group (read + Claude only); report
      kept/rejected to the owner. Create nothing.
