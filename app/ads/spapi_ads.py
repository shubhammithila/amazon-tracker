"""The only caller of the Advertising API's ENTITY endpoints — and the only module in this app
that writes to Amazon.

Everything else here reads Amazon and writes our own records. `apply_changes` changes live bids and
live states — and therefore live spend, which is why the read and write halves of this module are
documented to different standards: a read bug shows a wrong number, a write bug spends money.

**Reuses `app.portfolio.ads`'s token cache deliberately.** Same LWA client, same refresh token,
same host (`advertising-api-eu.amazon.com`) — a second cache would mint a second token for the same
credentials and double the LWA calls for no benefit. What is NOT shared is `shipment.spapi`'s
token: that is a different application entirely, and using it here returns a bare 401 that reads
exactly like "no advertising access".

Four things measured on the live account on 2026-08-28, each of which the obvious code gets wrong:

* **Every list endpoint needs its own `Content-Type` AND `Accept` vendor header.** `/sp/keywords`
  wants `application/vnd.spKeyword.v3+json`, `/sp/targets` wants
  `application/vnd.spTargetingClause.v3+json`. Plain `application/json` is refused.
* **`maxResults` caps at 500 and pagination is mandatory.** Measured: 148,291 keywords over 297
  pages, and targeting clauses did not finish inside 400 pages. A single unpaginated call silently
  returns the first 500 rows, which looks like a small account rather than an error.
* **Writes answer `207 Multi-Status`, never 200.** The body is
  `{"keywords": {"success": [...], "error": [...]}}`, so PARTIAL FAILURE IS THE NORMAL RESPONSE.
  Treating 207 as success is how a refused bid becomes invisible.
* **Amazon enforces a bid floor but effectively no ceiling.** `bid=0.5` was rejected with a
  `rangeError`; `bid=1000.0` was ACCEPTED on an account whose median bid is Rs 6.39. The ceiling
  lives in `ads.logic`, because Amazon will not supply one.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence

import httpx

from app.config import get_settings
from app.portfolio.ads import AdsError, AdsNotConfigured, _access_token, _headers

logger = logging.getLogger(__name__)

# ─── Endpoints and their vendor media types ──────────────────────────────────
#
# Each entity has its own versioned media type and Amazon refuses a mismatch. Kept as one table so
# a reader can see that the keyword and target paths differ in BOTH url and content type — the
# difference that makes a misrouted write fail.

CAMPAIGNS = ("/sp/campaigns", "application/vnd.spCampaign.v3+json", "campaigns")
AD_GROUPS = ("/sp/adGroups", "application/vnd.spAdGroup.v3+json", "adGroups")
KEYWORDS = ("/sp/keywords", "application/vnd.spKeyword.v3+json", "keywords")
TARGETS = ("/sp/targets", "application/vnd.spTargetingClause.v3+json", "targetingClauses")
#: Sponsored Products PRODUCT ADS — one SKU in one ad group. Measured 23 Oct 2026 (read-only): 6,404
#: ads, each `{adId, adGroupId, campaignId, asin, sku, state}`; 952 ad groups hold exactly ONE enabled
#: ad, which is why a pause is checked against its siblings (`logic.last_active_refusals`).
PRODUCT_ADS = ("/sp/productAds", "application/vnd.spProductAd.v3+json", "productAds")

# ─── Sponsored Brands: a different API, not a variant of the one above ───────
#
# **SB was invisible in this tab because nothing ever asked for it.** Measured on the live account:
# 6 campaigns (5 enabled, budgets Rs 4,000-20,000), 66 ad groups, 4,939 keywords all with bids.
#
# Four differences from SP, each verified by probing and each capable of failing silently:
#
# * **The list endpoints are `/sb/v4/...` and the deprecated `/sb/campaigns` returns 403** naming its
#   replacement. But keywords and targets are the OPPOSITE way round: `/sb/keywords` is a plain GET
#   that works, while `/sb/v4/keywords/list` 403s. Not a pattern — measured per endpoint.
# * **Campaign and ad group lists are POST with their own vendor media types**; keywords and targets
#   are GET with no special type.
# * **The write payload needs `adGroupId`.** SP does not. Omitting it returns `207` with
#   `KEYWORD_MISSING_AD_GROUP_ID` for every row — nothing applied, HTTP says success.
# * **The 207 body is a bare ARRAY**, not `{success: [...], error: [...]}`. See `_parse_sb_outcome`.

SB_CAMPAIGNS = ("/sb/v4/campaigns/list", "application/vnd.sbcampaignresource.v4+json", "campaigns")
SB_AD_GROUPS = ("/sb/v4/adGroups/list", "application/vnd.sbadgroupresource.v4+json", "adGroups")

#: Plain GET, no vendor media type, no `/list` suffix, no pagination body. `/sb/v4/keywords/list`
#: exists but 403s on this account, so this is the working path rather than the tidy-looking one.
SB_KEYWORDS_PATH = "/sb/keywords"
SB_TARGETS_PATH = "/sb/targets/list"

#: **`/sb/targets/list` takes NO vendor media type, and that is measured rather than assumed.** It is
#: its own case, not a missing value: sending `application/json` returns
#: `406 "No match for accept header"`, and all four plausible `vnd.sbtarget*.v4+json` spellings return
#: `415 "Cannot consume content type"`. Only sending neither header answers 200.
#:
#: Named rather than left as a bare `""` at the call site, because `""` reads as "not filled in yet"
#: and was in fact threaded straight into `Content-Type: ""` / `Accept: ""` — which produced a live
#: `406 Not Acceptable` on page 1 of every SB target fetch. `_media` now treats it as "omit both".
SB_NO_MEDIA_TYPE = ""

#: Writing an SB target bid. Note it is `/sb/targets` (no `/list`) and the payload is a DICT under
#: `targets` — where SB KEYWORDS take a bare list. Measured: the list form here returns
#: `422 "A JSON parsing error was encountered"`; the dict form returns 200.
SB_TARGETS_WRITE_PATH = "/sb/targets"

#: Amazon's hard cap for Sponsored Products. Asking for more is not an error — it silently returns 500.
PAGE_SIZE = 500

#: **Sponsored Brands caps `maxResults` at 100, not 500**, and it REFUSES rather than clamping:
#:
#:     INVALID_ARGUMENT ... "rangeError": {"cause": {"location": "$.maxResults", "trigger": "500"},
#:                           "lowerLimit": "1", "upperLimit": "100",
#:                           "reason": "LIST_REQUEST_MAX_RESULTS_OUT_OF_RANGE"}
#:
#: Found by calling the real endpoint after the unit tests were green — a fake that echoes whatever
#: it is sent could never have caught it. Amazon names the limit in the error, which is why it is a
#: constant rather than a guess. 66 SB ad groups need one page; the pagination below exists so a
#: growing account does not silently truncate at 100.
SB_PAGE_SIZE = 100

#: Pages before we stop and say so. 400 pages x 500 = 200,000 rows, which is where the targeting
#: clause enumeration was still unfinished. A ceiling rather than an expectation: every caller
#: filters by state or campaign, so a real request stops long before this.
#:
#: **Truncation is REPORTED, never silent.** The Orders tab shipped a 4-page cap that truncated on
#: every run while claiming orders were missing from the sheet; the lesson recorded there is that a
#: cap must name what it dropped.
MAX_PAGES = 400

#: Writes per request. Amazon documents 1,000 for these endpoints; 500 keeps a single 207 response
#: readable and means a network failure loses less work. The owner's real rule matched 299 rows, so
#: a typical run is one or two batches.
WRITE_BATCH = 500

#: Seconds between write batches. The entity endpoints did not advertise a rate limit header
#: (measured: `x-amzn-RateLimit-Limit` absent), so this is deliberate politeness rather than a
#: measured floor — a bulk run is not urgent, and a 429 mid-run leaves a half-applied rule.
WRITE_INTERVAL = 1.0


def _media(vnd: str, token: str) -> dict:
    """Headers for one entity call. `Accept` matters as much as `Content-Type`: omitting it returns
    a 406 that looks like a routing error.

    **An EMPTY `vnd` means send neither header, not send them empty** — and that distinction was a
    live 406. `/sb/targets/list` takes no vendor media type, which was expressed by threading `""`
    through here; the result was literal `Content-Type: ""` and `Accept: ""`, which Amazon rejects
    with `{"code":"406","details":"HTTP 406 Not Acceptable"}`.

    Measured against the live endpoint, and the answer is not the obvious one:

        (no Content-Type or Accept at all)              200  <- what it wants
        application/json                               406  "No match for accept header"
        application/vnd.sbtargetingresource.v4+json    415  "Cannot consume content type"
        application/vnd.sbtargetresource.v4+json       415
        application/vnd.sbtargeting.v4+json            415
        application/vnd.sbtargetingclause.v4+json      415

    So "no type" is a real, distinct case from "this type" — not a value to be defaulted — and even
    `application/json` is refused. httpx also sets `Content-Type: application/json` itself when
    given `json=`, which is fine here because the 406 came from `Accept`.
    """
    if not vnd:
        return dict(_headers(token))
    return {**_headers(token), "Content-Type": vnd, "Accept": vnd}


async def _list(
    client: httpx.AsyncClient,
    endpoint: tuple[str, str, str],
    *,
    filters: Mapping | None = None,
) -> list[dict]:
    """Page one list endpoint to exhaustion (or `MAX_PAGES`) and return every row.

    `filters` are Amazon's own server-side filters — `stateFilter`, `campaignIdFilter`,
    `adGroupIdFilter`. **Using them is not an optimisation, it is what makes this usable**: this
    account has 148,291 keywords, and fetching one campaign's enabled keywords is one page where
    fetching all of them is 297.
    """
    settings = get_settings()
    path, vnd, key = endpoint
    token = await _access_token(client)
    head = _media(vnd, token)

    rows: list[dict] = []
    next_token = None
    for page in range(MAX_PAGES):
        body: dict = {"maxResults": PAGE_SIZE}
        if filters:
            body.update(filters)
        if next_token:
            body["nextToken"] = next_token

        response = await client.post(settings.ads_endpoint + path + "/list",
                                     json=body, headers=head)
        if response.status_code >= 400:
            raise AdsError(
                f"Listing {key} failed on page {page + 1}: {response.text[:200]}",
                status=response.status_code,
            )
        payload = response.json() or {}
        rows.extend(payload.get(key) or [])
        next_token = payload.get("nextToken")
        if not next_token:
            return rows

    logger.warning(
        "ads: %s truncated at %d pages (%d rows) — there is more, so narrow the filter",
        key, MAX_PAGES, len(rows),
    )
    return rows


# ─── Reads ───────────────────────────────────────────────────────────────────


async def fetch_campaigns(client, *, states=("ENABLED", "PAUSED")) -> list[dict]:
    """All campaigns, normalised. 24 on this account, so no filtering is needed for scale.

    ARCHIVED is excluded by default: an archived campaign cannot be edited and showing it on a
    screen whose purpose is editing would offer an action that always fails.
    """
    raw = await _list(client, CAMPAIGNS, filters={"stateFilter": {"include": list(states)}})
    return [
        {
            "entity_type": "campaign",
            "entity_id": str(c.get("campaignId") or ""),
            "parent_id": None,
            "campaign_id": str(c.get("campaignId") or ""),
            "name": c.get("name") or "",
            "state": c.get("state") or "",
            "match_type": None,
            "bid": None,
            "default_bid": None,
            # `budget` is a nested object: {"budget": 5000.0, "budgetType": "DAILY"}. Flattened
            # here so the rest of the app never has to know that shape.
            "daily_budget": (c.get("budget") or {}).get("budget"),
            "targeting_type": c.get("targetingType") or "",
            "portfolio_id": str(c.get("portfolioId") or "") or None,
        }
        for c in raw
    ]


async def fetch_ad_groups(client, *, campaign_ids=None,
                          states=("ENABLED", "PAUSED")) -> list[dict]:
    """Ad groups, optionally for specific campaigns. 2,542 account-wide, so the filter matters."""
    filters: dict = {"stateFilter": {"include": list(states)}}
    if campaign_ids:
        filters["campaignIdFilter"] = {"include": [str(c) for c in campaign_ids]}
    raw = await _list(client, AD_GROUPS, filters=filters)
    return [
        {
            "entity_type": "ad_group",
            "entity_id": str(a.get("adGroupId") or ""),
            "parent_id": str(a.get("campaignId") or ""),
            "campaign_id": str(a.get("campaignId") or ""),
            "name": a.get("name") or "",
            "state": a.get("state") or "",
            "match_type": None,
            "bid": None,
            # What an inheriting keyword or target actually spends.
            "default_bid": a.get("defaultBid"),
            "daily_budget": None,
        }
        for a in raw
    ]


async def fetch_keywords(client, *, campaign_ids=None, ad_group_ids=None,
                         states=("ENABLED",)) -> list[dict]:
    """Keywords. **Defaults to ENABLED only**, because a rule must not resurrect paused spend.

    148,291 exist on this account, so calling this without a filter is a 297-page request. Every
    caller in this app passes either a campaign or an ad group.
    """
    filters: dict = {"stateFilter": {"include": list(states)}}
    if campaign_ids:
        filters["campaignIdFilter"] = {"include": [str(c) for c in campaign_ids]}
    if ad_group_ids:
        filters["adGroupIdFilter"] = {"include": [str(a) for a in ad_group_ids]}
    raw = await _list(client, KEYWORDS, filters=filters)
    return [
        {
            "entity_type": "keyword",
            "entity_id": str(k.get("keywordId") or ""),
            "parent_id": str(k.get("adGroupId") or ""),
            "campaign_id": str(k.get("campaignId") or ""),
            "name": k.get("keywordText") or "",
            "state": k.get("state") or "",
            # EXACT / PHRASE / BROAD — what routes a write back to THIS endpoint.
            "match_type": k.get("matchType") or "",
            "bid": k.get("bid"),
            "default_bid": None,
            "daily_budget": None,
        }
        for k in raw
    ]


async def fetch_targets(client, *, campaign_ids=None, ad_group_ids=None,
                        states=("ENABLED",)) -> list[dict]:
    """Targeting clauses: auto targets and manual product/category targets.

    **A different endpoint from keywords, and the report does not distinguish them** — it labels
    both id columns `keywordId`. `expressionType` (`AUTO`/`MANUAL`) is Amazon's own split; the
    `match_type` recorded here is what `logic.writer_for` uses to route a write, so it is set to
    the `TARGETING_EXPRESSION*` form the report also uses. Keeping the two vocabularies aligned is
    what stops a target being written to the keyword endpoint.
    """
    filters: dict = {"stateFilter": {"include": list(states)}}
    if campaign_ids:
        filters["campaignIdFilter"] = {"include": [str(c) for c in campaign_ids]}
    if ad_group_ids:
        filters["adGroupIdFilter"] = {"include": [str(a) for a in ad_group_ids]}
    raw = await _list(client, TARGETS, filters=filters)

    out = []
    for t in raw:
        expression_type = (t.get("expressionType") or "").upper()
        out.append({
            "entity_type": "target",
            "entity_id": str(t.get("targetId") or ""),
            "parent_id": str(t.get("adGroupId") or ""),
            "campaign_id": str(t.get("campaignId") or ""),
            "name": _describe_expression(t),
            "state": t.get("state") or "",
            "match_type": ("TARGETING_EXPRESSION_PREDEFINED" if expression_type == "AUTO"
                           else "TARGETING_EXPRESSION"),
            "bid": t.get("bid"),
            "default_bid": None,
            "daily_budget": None,
        })
    return out


#: Amazon's auto-target type names, translated to the words Seller Central shows. Measured on this
#: account: these four are the whole vocabulary in use.
AUTO_TARGET_NAMES = {
    "QUERY_HIGH_REL_MATCHES": "close-match",
    "QUERY_BROAD_REL_MATCHES": "loose-match",
    "ASIN_ACCESSORY_RELATED": "complements",
    "ASIN_SUBSTITUTE_RELATED": "substitutes",
}


def _describe_expression(target: Mapping) -> str:
    """A human label for a targeting clause.

    Amazon's raw form is a list of `{"type": ..., "value": ...}`. Rendering the JSON would put
    `[{"type": "QUERY_HIGH_REL_MATCHES"}]` on screen where the owner expects "close-match" — the
    name Seller Central uses and the one the report returns.
    """
    expression = target.get("resolvedExpression") or target.get("expression") or []
    parts = []
    for clause in expression:
        kind = (clause.get("type") or "").upper()
        value = clause.get("value")
        if kind in AUTO_TARGET_NAMES:
            parts.append(AUTO_TARGET_NAMES[kind])
        elif value:
            parts.append(f"{kind.lower()}=\"{value}\"")
        elif kind:
            parts.append(kind.lower())
    return " / ".join(parts) or (str(target.get("targetId") or ""))


# ─── Sponsored Brands reads ──────────────────────────────────────────────────


async def _sb_list(client, endpoint: tuple[str, str, str], filters: Mapping,
                   label: str) -> list[dict]:
    """Page one Sponsored Brands list endpoint. Its own pager because SB differs from SP twice.

    `maxResults` caps at **100** here (SP allows 500) and SB REFUSES an over-limit request rather
    than clamping it, so borrowing `_list` would fail every call. And the paths already include
    `/list`, where `_list` appends it.
    """
    settings = get_settings()
    path, vnd, key = endpoint
    token = await _access_token(client)
    head = _media(vnd, token)

    rows: list[dict] = []
    next_token = None
    for page in range(MAX_PAGES):
        body = {"maxResults": SB_PAGE_SIZE, **dict(filters)}
        if next_token:
            body["nextToken"] = next_token
        response = await client.post(settings.ads_endpoint + path, json=body, headers=head)
        if response.status_code >= 400:
            raise AdsError(
                f"Listing Sponsored Brands {label} failed on page {page + 1}: "
                f"{response.text[:200]}",
                status=response.status_code,
            )
        payload = response.json() or {}
        rows.extend(payload.get(key) or [])
        next_token = payload.get("nextToken")
        if not next_token:
            return rows

    logger.warning("ads: Sponsored Brands %s truncated at %d pages", label, MAX_PAGES)
    return rows


async def fetch_sb_campaigns(client, *, states=("ENABLED", "PAUSED")) -> list[dict]:
    """Sponsored Brands campaigns. 6 on this account, so no paging needed.

    A POST to `/sb/v4/campaigns/list` with its own vendor media type — the deprecated
    `/sb/campaigns` GET returns 403 naming this replacement.
    """
    raw = await _sb_list(
        client, SB_CAMPAIGNS, {"stateFilter": {"include": list(states)}}, "campaigns"
    )
    return [
        {
            "entity_type": "campaign",
            "ad_product": "sb",
            "entity_id": str(c.get("campaignId") or ""),
            "parent_id": None,
            "campaign_id": str(c.get("campaignId") or ""),
            "name": c.get("name") or "",
            "state": (c.get("state") or "").upper(),
            "match_type": None,
            "bid": None,
            "default_bid": None,
            # SB returns `budget` as a plain number, where SP nests it in an object. Flattened at
            # both call sites so nothing downstream has to know either shape.
            "daily_budget": c.get("budget"),
        }
        for c in raw
    ]


async def fetch_sb_ad_groups(client, *, campaign_ids=None,
                             states=("ENABLED", "PAUSED")) -> list[dict]:
    """Sponsored Brands ad groups. 66 on this account."""
    filters: dict = {"stateFilter": {"include": list(states)}}
    if campaign_ids:
        filters["campaignIdFilter"] = {"include": [str(c) for c in campaign_ids]}
    raw = await _sb_list(client, SB_AD_GROUPS, filters, "ad groups")
    return [
        {
            "entity_type": "ad_group",
            "ad_product": "sb",
            "entity_id": str(a.get("adGroupId") or ""),
            "parent_id": str(a.get("campaignId") or ""),
            "campaign_id": str(a.get("campaignId") or ""),
            "name": a.get("name") or "",
            "state": (a.get("state") or "").upper(),
            "match_type": None,
            "bid": None,
            "default_bid": None,
            "daily_budget": None,
        }
        for a in raw
    ]


async def fetch_sb_keywords(client, *, states=("enabled",)) -> list[dict]:
    """Sponsored Brands keywords — 4,939 on this account, ALL with an editable bid.

    A plain GET with no vendor media type and no pagination body, which is the opposite convention
    from the campaign and ad group lists above. `/sb/v4/keywords/list` exists but returns 403 here,
    so this is the working path rather than the consistent-looking one.

    **No server-side state filter**, so filtering happens locally. Acceptable at 4,939 rows in one
    response; if SB ever reaches SP's 148,291 this needs revisiting.

    `state` arrives LOWERCASE from this endpoint (`enabled`) where SP returns `ENABLED`. Normalised
    here, because two spellings of one value is how a filter silently matches nothing.
    """
    settings = get_settings()
    token = await _access_token(client)
    response = await client.get(
        settings.ads_endpoint + SB_KEYWORDS_PATH, headers=_headers(token)
    )
    if response.status_code >= 400:
        raise AdsError(
            f"Listing Sponsored Brands keywords failed: {response.text[:200]}",
            status=response.status_code,
        )
    raw = response.json() or []
    wanted = {str(s).upper() for s in states}

    out = []
    for k in raw:
        state = (k.get("state") or "").upper()
        if wanted and state not in wanted:
            continue
        out.append({
            "entity_type": "keyword",
            "ad_product": "sb",
            "entity_id": str(k.get("keywordId") or ""),
            "parent_id": str(k.get("adGroupId") or ""),
            "campaign_id": str(k.get("campaignId") or ""),
            "name": k.get("keywordText") or "",
            "state": state,
            # Upper-cased so `logic.writer_for` sees the same vocabulary from both products —
            # SB sends `exact`, SP sends `EXACT`.
            "match_type": (k.get("matchType") or "").upper(),
            "bid": k.get("bid"),
            "default_bid": None,
            "daily_budget": None,
        })
    return out


# ─── Amazon's suggested bid ──────────────────────────────────────────────────
#
# **The endpoint had to be found by probing, and three of four candidates do not work.** Called for
# real against the live account:
#
#   /v2/sp/adGroups/{id}/bidRecommendations   404  "Method Not Found"
#   /v2/sp/keywords/{id}/bidRecommendations   404
#   /sp/keywords/bid/recommendations          403  with a spurious SigV4 error, twice
#   /sp/targets/bid/recommendations           200  real bids
#
# The last one takes `targetingExpressions` and handles keywords too, batched per AD GROUP — which is
# what makes this affordable: the owner's real rule matched 1,005 rows across a few dozen ad groups.

BID_RECS_PATH = "/sp/targets/bid/recommendations"
BID_RECS_VND = "application/vnd.spthemebasedbidrecommendation.v3+json"

#: Match type -> the `targetingExpression.type` the recommendation endpoint expects.
KEYWORD_EXPRESSION_TYPES = {
    "EXACT": "KEYWORD_EXACT_MATCH",
    "PHRASE": "KEYWORD_PHRASE_MATCH",
    "BROAD": "KEYWORD_BROAD_MATCH",
}

#: Why a row has no suggestion. Shown on screen, because a blank cell in a bid column reads as "no
#: suggestion, so bid low" — a different claim from "Amazon does not offer one here".
SB_UNAVAILABLE = "Amazon offers no suggested bid for Sponsored Brands"
NO_MATCH_UNAVAILABLE = "Amazon returned no suggestion for this target"


async def fetch_bid_recommendations(client, rows: Sequence[Mapping]) -> dict[str, dict]:
    """`{entity_id: {suggested_bid, low, high, unavailable}}` for the rows Amazon will answer for.

    **Amazon returns THREE bids per expression** — measured `[140.33, 182.83, 225.33]`. The middle
    value is the suggestion and the outer two are its range; both travel, because picking one of three
    silently is exactly the kind of choice that later reads as a fact.

    **Sponsored Brands rows are excluded and LABELLED, never guessed.** Three SB endpoints were probed
    (`/sb/recommendations/bids/keyword`, `/sb/recommendations/bids/targets`,
    `/sb/targets/bid/recommendations`) and all three 404. Borrowing an SP figure for an SB row would
    put a number next to a bid Amazon never suggested for it.

    **Never raises.** A suggestion is context beside the bid; losing it must not lose the preview that
    is the safety mechanism for the only feature in this app that spends money. Every failure path
    returns a per-row reason instead.
    """
    # Imported here rather than at module scope, matching `fetch_current_bids` and `apply_changes` —
    # `app.ads.logic` is pure and this module is its caller, so the import stays inside the functions
    # that need it.
    from app.ads.logic import AD_PRODUCT_SP

    out: dict[str, dict] = {}
    by_ad_group: dict[str, list] = {}

    for row in rows:
        entity_id = str(row.get("entity_id") or "")
        if not entity_id:
            continue
        if (row.get("ad_product") or AD_PRODUCT_SP) != AD_PRODUCT_SP:
            out[entity_id] = {"suggested_bid": None, "low": None, "high": None,
                              "unavailable": SB_UNAVAILABLE}
            continue
        expression = _expression_for(row)
        if expression is None:
            out[entity_id] = {"suggested_bid": None, "low": None, "high": None,
                              "unavailable": NO_MATCH_UNAVAILABLE}
            continue
        by_ad_group.setdefault(str(row.get("ad_group_id") or ""), []).append((row, expression))

    if not by_ad_group:
        return out

    def _unavailable(group, reason):
        for row, _ in group:
            out[str(row["entity_id"])] = {"suggested_bid": None, "low": None, "high": None,
                                          "unavailable": reason}

    try:
        token = await _access_token(client)
    except Exception as exc:                      # noqa: BLE001 - context, never fatal
        logger.warning("ads: no token for bid recommendations: %s", exc)
        for group in by_ad_group.values():
            _unavailable(group, f"Could not ask Amazon: {exc}")
        return out

    settings = get_settings()
    headers = {**_headers(token), "Content-Type": BID_RECS_VND, "Accept": BID_RECS_VND}

    for ad_group_id, group in by_ad_group.items():
        if not ad_group_id:
            _unavailable(group, NO_MATCH_UNAVAILABLE)
            continue
        body = {
            "campaignId": str(group[0][0].get("campaign_id") or ""),
            "adGroupId": ad_group_id,
            "recommendationType": "BIDS_FOR_EXISTING_AD_GROUP",
            "targetingExpressions": [expression for _, expression in group],
        }
        try:
            response = await client.request(
                "POST", settings.ads_endpoint + BID_RECS_PATH, json=body, headers=headers,
            )
            if response.status_code == 200:
                payload, reason = response.json(), ""
            else:
                payload = {}
                reason = f"Amazon returned {response.status_code} for this ad group"
        except Exception as exc:                  # noqa: BLE001 - context, never fatal
            payload, reason = {}, f"Could not ask Amazon: {exc}"

        suggestions = _parse_bid_recommendations(payload)
        for row, expression in group:
            key = (expression.get("value") or "").casefold()
            out[str(row["entity_id"])] = suggestions.get(key) or {
                "suggested_bid": None, "low": None, "high": None,
                "unavailable": reason or NO_MATCH_UNAVAILABLE,
            }
    return out


def _expression_for(row: Mapping) -> dict | None:
    """The `targetingExpression` for one row, or None when Amazon has no expression type for it.

    None rather than a guess: an expression built from a match type Amazon does not price would
    return a suggestion for a DIFFERENT target, and that number would sit beside a live bid. The same
    rule `logic.writer_for` follows — excluded and named, never inferred.
    """
    match_type = (row.get("match_type") or "").upper()
    text = (row.get("text") or "").strip()
    kind = KEYWORD_EXPRESSION_TYPES.get(match_type)
    if kind and text:
        return {"type": kind, "value": text}
    if match_type == "TARGETING_EXPRESSION_PREDEFINED" and text:
        # An auto target names its own type in the report text ("close-match", "loose-match" ...).
        return {"type": text.replace("-", "_").upper(), "value": None}
    return None


def _parse_bid_recommendations(payload: Mapping) -> dict[str, dict]:
    """`{expression value casefolded: {suggested_bid, low, high, unavailable}}`.

    Amazon nests these three deep — themes, then expressions, then a LIST of bid values — and returns
    three bids per expression. The middle one is the suggestion.
    """
    out: dict[str, dict] = {}
    for theme in payload.get("bidRecommendations") or []:
        for entry in theme.get("bidRecommendationsForTargetingExpressions") or []:
            expression = entry.get("targetingExpression") or {}
            bids = sorted(
                float(value["suggestedBid"])
                for value in (entry.get("bidValues") or [])
                if value.get("suggestedBid") is not None
            )
            if not bids:
                continue
            out[(expression.get("value") or "").casefold()] = {
                "suggested_bid": round(bids[len(bids) // 2], 2),
                "low": round(bids[0], 2),
                "high": round(bids[-1], 2),
                "unavailable": "",
            }
    return out


async def fetch_product_ads(client, *, ad_group_ids=None,
                            states=("ENABLED", "PAUSED")) -> list[dict]:
    """SP product ads as entity rows (`entity_type="product_ad"`, `name` = the SKU).

    With ``ad_group_ids`` only those ad groups are listed (500 per filter), which is how `/ads/apply`
    reads a row's live state AND its siblings in one pass. Without, the whole account (~13 pages).
    """
    filters_list: list[dict] = []
    if ad_group_ids:
        ids = sorted({str(g) for g in ad_group_ids if g})
        filters_list = [{"adGroupIdFilter": {"include": ids[i:i + PAGE_SIZE]}}
                        for i in range(0, len(ids), PAGE_SIZE)]
    else:
        filters_list = [{}]

    out: list[dict] = []
    for extra in filters_list:
        filters = {**extra}
        if states:
            filters["stateFilter"] = {"include": [str(x).upper() for x in states]}
        for row in await _list(client, PRODUCT_ADS, filters=filters):
            out.append({
                "entity_type": "product_ad",
                "ad_product": "sp",
                "entity_id": str(row.get("adId") or ""),
                "parent_id": str(row.get("adGroupId") or ""),
                "campaign_id": str(row.get("campaignId") or ""),
                "name": row.get("sku") or row.get("asin") or "",
                "sku": row.get("sku") or "",
                "asin": row.get("asin") or "",
                "state": (row.get("state") or "").upper(),
            })
    return out


async def fetch_current_bids(client, changes: Sequence[Mapping]) -> dict[str, dict]:
    """Re-read the LIVE bid **and state** for the rows a run is about to change.

    Returns `{entity_id: {"bid": float|None, "state": str}}`.

    **Two guards from one call, which is why the state check lives here.**

    * *The bid* stops a stale percentage. The plan is computed from a performance report that may be
      hours old, and a bid edited in Seller Central since then would be overwritten with a percentage
      of a number that no longer exists.
    * *The state* stops a rule editing something that is not serving. **The `spTargeting` report
      carries no state column at all** — measured, its 15 columns include none — so a plan built from
      the report cannot tell an enabled target from a paused one. Measured on the live account: 168 of
      12,205 report rows (1.4%) are PAUSED or ARCHIVED, because Amazon reports whatever had activity
      in the window regardless of what it is now.

    Reading both together is deliberate: the entity API returns them in the same response, so the
    state check costs **no extra requests** and is exactly as fresh as the bid it is checked beside.
    Fetching state at preview time instead would add a round trip to every preview for a 1.4%
    correction, and previews are cheap and frequent.

    `bid` is `None` for a row Amazon no longer prices (archived, or switched back to inheriting the
    ad group default).
    """
    from app.ads.logic import (
        WRITER_KEYWORD,
        WRITER_PRODUCT_AD,
        WRITER_SB_KEYWORD,
        WRITER_SB_TARGET,
        WRITER_TARGET,
    )

    # Grouped by WRITER, not by a keyword/target guess. The previous version sent everything that was
    # not an SP keyword to `/sp/targets`, which meant Sponsored Brands rows were looked up on the
    # wrong API and came back missing — and a missing row is treated as "moved", so every SB change
    # would have been silently dropped at apply time.
    by_writer: dict[str, list[str]] = {}
    for change in changes:
        writer = change.get("writer") or WRITER_KEYWORD
        by_writer.setdefault(writer, []).append(str(change["entity_id"]))

    live: dict[str, dict] = {}

    # ── Sponsored Products: filter by id, one page per 500 ──
    for writer, endpoint, id_field, filter_key in (
        (WRITER_KEYWORD, KEYWORDS, "keywordId", "keywordIdFilter"),
        (WRITER_TARGET, TARGETS, "targetId", "targetIdFilter"),
    ):
        ids = by_writer.get(writer) or []
        for start in range(0, len(ids), PAGE_SIZE):
            chunk = ids[start:start + PAGE_SIZE]
            rows = await _list(client, endpoint, filters={filter_key: {"include": chunk}})
            for row in rows:
                identifier = str(row.get(id_field) or "")
                if identifier:
                    bid = row.get("bid")
                    live[identifier] = {
                        "bid": float(bid) if bid is not None else None,
                        "state": (row.get("state") or "").upper(),
                    }

    # ── Sponsored Brands: no id filter on these endpoints, so fetch and match locally ──
    #
    # `/sb/keywords` is a plain GET with no filter body at all, and `/sb/targets/list` pages without
    # one. 4,939 keywords come back in a single response, so filtering here is cheaper than it looks
    # and there is no alternative anyway.
    sb_keyword_ids = set(by_writer.get(WRITER_SB_KEYWORD) or [])
    if sb_keyword_ids:
        for row in await fetch_sb_keywords(client, states=()):
            if row["entity_id"] in sb_keyword_ids:
                live[row["entity_id"]] = {
                    "bid": float(row["bid"]) if row.get("bid") is not None else None,
                    "state": row.get("state") or "",
                }

    # ── Product ads: listed by AD GROUP, so the answer carries every SIBLING too ──
    #
    # A pause must not leave an ad group with no enabled product ad, and that can only be judged
    # with the other ads of the group in hand — so the groups of the rows being changed are listed
    # whole, and each entry carries its `ad_group_id`. Lookups elsewhere are by id, so the extra
    # siblings are harmless to every other caller.
    if by_writer.get(WRITER_PRODUCT_AD):
        groups = {str(c.get("ad_group_id") or "") for c in changes
                  if c.get("writer") == WRITER_PRODUCT_AD}
        for row in await fetch_product_ads(client, ad_group_ids=groups, states=()):
            live[row["entity_id"]] = {"bid": None, "state": row["state"],
                                      "ad_group_id": row["parent_id"], "writer": WRITER_PRODUCT_AD}

    sb_target_ids = set(by_writer.get(WRITER_SB_TARGET) or [])
    if sb_target_ids:
        for row in await fetch_sb_targets(client, states=()):
            if row["entity_id"] in sb_target_ids:
                live[row["entity_id"]] = {
                    "bid": float(row["bid"]) if row.get("bid") is not None else None,
                    "state": row.get("state") or "",
                }

    return live


async def fetch_sb_targets(client, *, states=("ENABLED",)) -> list[dict]:
    """Sponsored Brands product/category targets and brand themes, with their bids and states.

    `states=()` means "every state", used by `fetch_current_bids` — it needs to SEE a paused row in
    order to skip it, so filtering it out at the API would hide the very thing being checked.
    """
    raw = await _sb_list(
        client, (SB_TARGETS_PATH, SB_NO_MEDIA_TYPE, "targets"), {}, "targets",
    )
    wanted = {str(s).upper() for s in states}

    out = []
    for t in raw:
        state = (t.get("state") or "").upper()
        if wanted and state not in wanted:
            continue
        expression_type = (t.get("expressionType") or "").upper()
        out.append({
            "entity_type": "target",
            "ad_product": "sb",
            "entity_id": str(t.get("targetId") or ""),
            "parent_id": str(t.get("adGroupId") or ""),
            "campaign_id": str(t.get("campaignId") or ""),
            "name": _describe_expression({
                "resolvedExpression": t.get("resolvedExpressions") or t.get("expressions") or [],
            }),
            "state": state,
            # `THEME` for a brand theme, otherwise a product/category target. Both route to
            # `/sb/targets` — see `logic.SB_TARGET_MATCH_TYPES`.
            "match_type": "THEME" if expression_type == "THEME" else "TARGETING_EXPRESSION",
            "bid": t.get("bid"),
            "default_bid": None,
            "daily_budget": None,
        })
    return out


# ─── Writes ──────────────────────────────────────────────────────────────────


async def apply_changes(
    client: httpx.AsyncClient,
    changes: Sequence[Mapping],
    *,
    writer: str,
    sleep=asyncio.sleep,
) -> list[dict]:
    """Send one writer's changes — a bid edit OR a pause. Per-row results, never a success flag.

    `[{"entity_id": ..., "ok": bool, "error": str|None}, ...]`, one entry per input row and in no
    guaranteed order — the caller matches on `entity_id`.

    **`207 Multi-Status` is the expected status, and both arrays must be read.** Measured shape:

        {"keywords": {"success": [{"index": 0, "keywordId": "..."}],
                      "error":   [{"index": 3, "errors": [{"errorType": "rangeError", ...}]}]}}

    Amazon reports per-ROW outcomes inside a single response, and it identifies failures by
    `index` into the request array — so the request order is the only thing linking a failure back
    to a row. That is why this builds `payload` and `order` in one pass and never re-sorts.

    A row Amazon does not mention at all is reported as failed with a note, rather than assumed to
    have worked. Silence about a bid change is not evidence that it happened.

    **Sponsored Brands differs in TWO ways, and both fail silently if missed.** Its payload needs
    `adGroupId` (SP does not), and its 207 body is a bare array rather than
    `{success: [...], error: [...]}`. See `_sb_payload_row` and `_parse_sb_outcome`.

    **Two KINDS of change go through this one function, deliberately.** A row carrying `new_state` is
    a pause or an enable; a row carrying `new_bid` is a bid edit. Everything that is hard to get right
    — the four endpoints, the 500-row batching, request-array order as the only link back to a row,
    and three mutually-unreadable 207 body shapes — is identical for both, so a second writer would
    mean two copies of the code whose own comment says that getting the order wrong makes the ledger
    blame the wrong keyword. `fetch_current_bids` already had to be regrouped by writer after exactly
    that kind of divergence.

    **A state row never also carries a bid.** Each payload builder picks one field, so a pause cannot
    apply an unpreviewed bid change as a side effect.
    """
    from app.ads.logic import (
        WRITER_KEYWORD,
        WRITER_PRODUCT_AD,
        WRITER_SB_KEYWORD,
        WRITER_SB_TARGET,
        WRITER_TARGET,
    )

    if not changes:
        return []

    settings = get_settings()
    is_sb_keyword = writer == WRITER_SB_KEYWORD
    is_sb_target = writer == WRITER_SB_TARGET
    is_sb = is_sb_keyword or is_sb_target

    if is_sb_keyword:
        path, head_type, id_field = SB_KEYWORDS_PATH, None, "keywordId"
    elif is_sb_target:
        path, head_type, id_field = SB_TARGETS_WRITE_PATH, None, "targetId"
    elif writer == WRITER_KEYWORD:
        path, head_type, id_field = KEYWORDS[0], KEYWORDS[1], "keywordId"
        body_key = "keywords"
    elif writer == WRITER_PRODUCT_AD:
        # `{"productAds": [{adId, state}]}` and SP's own 207 shape, keyed by `adId`. A product ad has
        # no bid, so `_sp_payload_row` only ever writes `state` here (`plan_run` makes product rules
        # state-only).
        path, head_type, id_field = PRODUCT_ADS[0], PRODUCT_ADS[1], "adId"
        body_key = "productAds"
    else:
        path, head_type, id_field = TARGETS[0], TARGETS[1], "targetId"
        body_key = "targetingClauses"

    token = await _access_token(client)
    head = _headers(token) if is_sb else _media(head_type, token)
    results: list[dict] = []

    for batch_start in range(0, len(changes), WRITE_BATCH):
        batch = list(changes[batch_start:batch_start + WRITE_BATCH])
        # Built together so index N of `order` is index N of the request Amazon validates.
        order = [str(c["entity_id"]) for c in batch]

        if is_sb_keyword:
            # A bare LIST, and every row carries `adGroupId`.
            payload = [_sb_payload_row(c) for c in batch]
        elif is_sb_target:
            # A DICT under `targets`, unlike SB keywords' bare list. Measured: the list form returns
            # `422 "A JSON parsing error was encountered"`, the dict form returns 200. Three
            # endpoints, three payload shapes — none of them guessable.
            payload = {"targets": [_sb_target_payload_row(c) for c in batch]}
        else:
            payload = {body_key: [_sp_payload_row(c, id_field) for c in batch]}

        if batch_start:
            await sleep(WRITE_INTERVAL)

        response = await client.put(settings.ads_endpoint + path, json=payload, headers=head)

        # A transport-level failure (401, 429, 500) is different from a per-row refusal: NOTHING in
        # the batch was applied, and every row must be reported as failed rather than left silent.
        if response.status_code >= 400 and response.status_code != 207:
            message = f"Amazon refused the batch ({response.status_code}): {response.text[:200]}"
            logger.warning("ads: write batch failed: %s", message)
            results.extend({"entity_id": i, "ok": False, "error": message} for i in order)
            continue

        body = response.json()
        if is_sb_target:
            results.extend(_parse_sb_target_outcome(body, order))
        elif is_sb_keyword:
            results.extend(_parse_sb_outcome(body, order, id_field))
        else:
            results.extend(_parse_sp_outcome(body, order, id_field, body_key))

    return results


async def create_product_ads(
    client: httpx.AsyncClient,
    rows: Sequence[Mapping],
    *,
    sleep=asyncio.sleep,
) -> list[dict]:
    """Create SP product ads — the SKU-coverage Add. Per-row results, never a success flag.

    `rows`: `[{"entity_id": provisional id, "campaign_id", "ad_group_id", "sku"}]`. Returns
    `[{"entity_id": provisional id, "ok", "error", "ad_id"}]`, one per row.

    `POST /sp/productAds {"productAds": [{campaignId, adGroupId, sku, state: "ENABLED"}]}`. A seller
    names the SKU (the ASIN form is for vendors). **Measured with a deliberately invalid ad group**, so
    nothing was created: `207` with SP's own shape,

        {"productAds": {"success": [], "error": [{"index": 0, "errors": [{"errorType":
            "entityNotFoundError", "errorValue": {"entityNotFoundError": {"message":
            "no campaign found", "reason": "ENTITY_NOT_FOUND"}}}]}]}}

    so failures are matched back by `index`, the same as every other SP write. A row Amazon does not
    mention, or a success without an `adId`, is FAILED: without the id there is nothing to undo.
    """
    if not rows:
        return []
    settings = get_settings()
    token = await _access_token(client)
    head = _media(PRODUCT_ADS[1], token)
    results: list[dict] = []
    for batch_start in range(0, len(rows), WRITE_BATCH):
        batch = list(rows[batch_start:batch_start + WRITE_BATCH])
        order = [str(r["entity_id"]) for r in batch]
        payload = {"productAds": [{
            "campaignId": str(r["campaign_id"]), "adGroupId": str(r["ad_group_id"]),
            "sku": str(r["sku"]), "state": "ENABLED"} for r in batch]}
        if batch_start:
            await sleep(WRITE_INTERVAL)
        response = await client.post(settings.ads_endpoint + PRODUCT_ADS[0], json=payload, headers=head)
        if response.status_code >= 400 and response.status_code != 207:
            message = f"Amazon refused the batch ({response.status_code}): {response.text[:200]}"
            logger.warning("ads: product-ad create batch failed: %s", message)
            results.extend({"entity_id": i, "ok": False, "error": message, "ad_id": None} for i in order)
            continue
        results.extend(_parse_create_outcome(response.json(), order))
    return results


def _parse_create_outcome(body, order: list[str]) -> list[dict]:
    """SP's 207 for a CREATE: `success` items carry the new `adId` (on the item or under `productAd`)."""
    outcome = body.get("productAds") if isinstance(body, Mapping) else None
    if not isinstance(outcome, Mapping):
        logger.warning("ads: unexpected product-ad create response (%s)", type(body).__name__)
        return [{**r, "ad_id": None} for r in _unmentioned(order, set())]
    results, seen = [], set()
    for item in outcome.get("success") or []:
        index = item.get("index")
        if not (isinstance(index, int) and 0 <= index < len(order)):
            continue
        ad = item.get("productAd") if isinstance(item.get("productAd"), Mapping) else {}
        ad_id = str(item.get("adId") or ad.get("adId") or "")
        seen.add(order[index])
        results.append({"entity_id": order[index], "ok": bool(ad_id), "ad_id": ad_id or None,
                        "error": None if ad_id else "Amazon did not return the new ad's id."})
    for item in outcome.get("error") or []:
        index = item.get("index")
        if not (isinstance(index, int) and 0 <= index < len(order)):
            continue
        seen.add(order[index])
        results.append({"entity_id": order[index], "ok": False, "ad_id": None,
                        "error": _error_message(item)})
    results.extend({**r, "ad_id": None} for r in _unmentioned(order, seen))
    return results


def _sp_payload_row(change: Mapping, id_field: str) -> dict:
    """One row of a Sponsored Products write — a bid change OR a state change, never both.

    Amazon's update schemas require only the id and treat every other attribute as a partial update,
    so sending `state` alone leaves the bid untouched and vice versa. **Exactly one is sent**, because
    a pause that also carried a bid would apply a change nobody previewed.

    Its own function for the same reason `_sb_payload_row` is: the requirement is then stated once and
    pinned by a test, rather than living as an easily-dropped key inside a comprehension.
    """
    from app.ads.logic import AD_PRODUCT_SP, normalise_state

    row = {id_field: str(change["entity_id"])}
    if change.get("new_state"):
        row["state"] = normalise_state(change["new_state"], AD_PRODUCT_SP)
    else:
        row["bid"] = round(float(change["new_bid"]), 2)
    return row


def _sb_payload_row(change: Mapping) -> dict:
    """One row of a Sponsored Brands bid write.

    **`adGroupId` is REQUIRED and Sponsored Products does not need it.** Measured: sending SP's shape
    to `/sb/keywords` returns

        207 [{"code": "INVALID_ARGUMENT",
              "description": "Keyword was specified without an ad group id",
              "errors": [{"KeywordError": {"reason": "KEYWORD_MISSING_AD_GROUP_ID"}}], ...}]

    — every row refused, while the HTTP status says success. Verified corrected: with `adGroupId`,
    `[{"code": "SUCCESS", "keywordId": ...}]`.

    Kept as its own function so the requirement is stated once and pinned by a test, rather than
    living as an easily-dropped extra key inside a comprehension.
    """
    from app.ads.logic import AD_PRODUCT_SB, normalise_state

    def _as_id(value):
        text = str(value or "")
        return int(text) if text.isdigit() else value

    row = {
        "keywordId": _as_id(change["entity_id"]),
        "adGroupId": _as_id(change.get("ad_group_id")),
    }
    if change.get("new_state"):
        # **`campaignId` is required for an SB keyword STATE write and is NOT required for a bid
        # write.** Amazon's update schema lists all three ids as required on the state path; omitting
        # it is a per-row refusal inside a 207 whose HTTP status says success — the same silent shape
        # as the missing `adGroupId` this function was written for.
        row["campaignId"] = _as_id(change.get("campaign_id"))
        # **Lower case.** SB's state enum is `enabled|paused|archived|draft` where SP's is upper.
        row["state"] = normalise_state(change["new_state"], AD_PRODUCT_SB)
    else:
        row["bid"] = round(float(change["new_bid"]), 2)
    return row


def _sb_target_payload_row(change: Mapping) -> dict:
    """One row of a Sponsored Brands TARGET bid write.

    Same `adGroupId` requirement as SB keywords, but wrapped in a dict rather than sent as a bare
    list — verified, the list form is refused with a JSON parsing error.
    """
    from app.ads.logic import AD_PRODUCT_SB, normalise_state

    identifier = str(change["entity_id"])
    ad_group = str(change.get("ad_group_id") or "")
    row = {
        "targetId": int(identifier) if identifier.isdigit() else identifier,
        "adGroupId": int(ad_group) if ad_group.isdigit() else ad_group,
    }
    if change.get("new_state"):
        row["state"] = normalise_state(change["new_state"], AD_PRODUCT_SB)
    else:
        row["bid"] = round(float(change["new_bid"]), 2)
    return row


def _parse_sb_target_outcome(body, order: list[str]) -> list[dict]:
    """Sponsored Brands TARGETS use a THIRD response shape, keyed by request index.

    Measured live:

        {"updateTargetSuccessResults": [{"targetRequestIndex": 0, "targetId": 126516214884382}],
         "updateTargetErrorResults":   []}

    So this app now parses three different 207-style bodies — SP's `{success, error}`, SB keywords'
    bare array, and this. Each is its own function because none of them can read another's shape, and
    a parser that finds nothing reports every row as an unknown outcome, which reads as an Amazon
    fault rather than our bug.

    `targetRequestIndex` indexes the REQUEST array, so request order is the only link back to a row.
    """
    if not isinstance(body, Mapping):
        logger.warning("ads: unexpected SB target response shape (%s)", type(body).__name__)
        return _unmentioned(order, set())

    results: list[dict] = []
    seen: set[str] = set()

    def resolve(item: Mapping) -> str:
        identifier = str(item.get("targetId") or "")
        if not identifier:
            index = item.get("targetRequestIndex")
            if isinstance(index, int) and index < len(order):
                identifier = order[index]
        return identifier

    for item in body.get("updateTargetSuccessResults") or []:
        if not isinstance(item, Mapping):
            continue
        identifier = resolve(item)
        if identifier:
            seen.add(identifier)
            results.append({"entity_id": identifier, "ok": True, "error": None})

    for item in body.get("updateTargetErrorResults") or []:
        if not isinstance(item, Mapping):
            continue
        identifier = resolve(item)
        if identifier:
            seen.add(identifier)
            results.append({
                "entity_id": identifier, "ok": False,
                "error": _sb_error_message(item) or str(item.get("code") or "refused"),
            })

    results.extend(_unmentioned(order, seen))
    return results


def _parse_sp_outcome(body, order: list[str], id_field: str, body_key: str) -> list[dict]:
    """Sponsored Products' 207: `{"<key>": {"success": [...], "error": [...]}}`.

    Failures are identified by `index` into the REQUEST array, so request order is the only link back
    to a row — getting it wrong makes the ledger blame the wrong keyword.
    """
    # Defensive against the WRONG SHAPE rather than trusting the caller: if Amazon (or a future
    # refactor) hands this the SB bare-array form, every row must come back as "no outcome" —
    # reported and recoverable — rather than raising `AttributeError` mid-run, after some batches
    # have already been sent and the ledger is half-written.
    outcome = body.get(body_key) if isinstance(body, Mapping) else None
    if not isinstance(outcome, Mapping):
        logger.warning(
            "ads: unexpected %s response shape (%s) — reporting every row as unknown",
            body_key, type(body).__name__,
        )
        return _unmentioned(order, set())

    results: list[dict] = []
    seen: set[str] = set()

    for item in outcome.get("success") or []:
        identifier = str(item.get(id_field) or "")
        if not identifier:
            index = item.get("index")
            identifier = order[index] if isinstance(index, int) and index < len(order) else ""
        if identifier:
            seen.add(identifier)
            results.append({"entity_id": identifier, "ok": True, "error": None})

    for item in outcome.get("error") or []:
        index = item.get("index")
        identifier = str(item.get(id_field) or "")
        if not identifier and isinstance(index, int) and index < len(order):
            identifier = order[index]
        if identifier:
            seen.add(identifier)
            results.append({
                "entity_id": identifier, "ok": False, "error": _error_message(item),
            })

    results.extend(_unmentioned(order, seen))
    return results


def _parse_sb_outcome(body, order: list[str], id_field: str) -> list[dict]:
    """Sponsored Brands' 207: a **bare ARRAY** of `{"code": "SUCCESS"|..., "keywordId": ...}`.

    A separate function rather than a branch inside the SP parser, because the two shapes share no
    structure: feeding SB's array to `_parse_sp_outcome` finds neither `success` nor `error` and
    reports "Amazon did not report an outcome" for every single row — a total failure that reads as
    an Amazon problem rather than our parsing.

    Measured shapes, both from live probes:

        success:  [{"code": "SUCCESS", "keywordId": 102932256635969}]
        refusal:  [{"code": "INVALID_ARGUMENT",
                    "description": "Keyword was specified without an ad group id",
                    "errors": [{"KeywordError": {"message": "...", "reason": "..."}}],
                    "keywordId": 102932256635969}]
    """
    results: list[dict] = []
    seen: set[str] = set()

    for index, item in enumerate(body or []):
        if not isinstance(item, Mapping):
            continue
        identifier = str(item.get(id_field) or "")
        if not identifier and index < len(order):
            identifier = order[index]
        if not identifier:
            continue
        seen.add(identifier)

        code = str(item.get("code") or "").upper()
        if code == "SUCCESS":
            results.append({"entity_id": identifier, "ok": True, "error": None})
        else:
            results.append({
                "entity_id": identifier, "ok": False,
                "error": _sb_error_message(item) or code or "refused without a reason",
            })

    results.extend(_unmentioned(order, seen))
    return results


def _unmentioned(order: list[str], seen: set[str]) -> list[dict]:
    """Rows Amazon said nothing about, reported as FAILED.

    An unmentioned row is an unknown outcome, and recording it as applied would put a wrong `old_bid`
    chain in the ledger — so a later undo would write a bid Amazon never held.
    """
    return [
        {"entity_id": identifier, "ok": False,
         "error": "Amazon did not report an outcome for this row."}
        for identifier in order if identifier not in seen
    ]


def _sb_error_message(item: Mapping) -> str:
    """Flatten Sponsored Brands' error shape, which nests differently from Sponsored Products'.

    SB: `{"description": ..., "errors": [{"KeywordError": {"message": ..., "reason": ...}}]}` —
    the inner key is the error CLASS name, so it cannot be looked up by a fixed key.
    """
    parts: list[str] = []
    description = item.get("description")
    if description:
        parts.append(str(description))
    for error in item.get("errors") or []:
        if not isinstance(error, Mapping):
            continue
        for value in error.values():
            if isinstance(value, Mapping):
                detail = value.get("message") or value.get("reason")
                if detail and str(detail) not in parts:
                    parts.append(str(detail))
    return "; ".join(parts)


def _error_message(item: Mapping) -> str:
    """Amazon's own refusal, flattened to one line.

    Kept verbatim rather than mapped to our own text: their messages name the cause, and they are
    how both the 31-day report cap and the bid floor were found. The nesting is real —
    `{"errors": [{"errorType": "rangeError", "errorValue": {"rangeError": {"message": ...}}}]}`.
    """
    parts: list[str] = []
    for error in item.get("errors") or []:
        kind = error.get("errorType") or "error"
        value = error.get("errorValue") or {}
        detail = ""
        if isinstance(value, Mapping):
            inner = value.get(kind) if isinstance(value.get(kind), Mapping) else None
            if inner:
                detail = inner.get("message") or inner.get("reason") or ""
            if not detail:
                for candidate in value.values():
                    if isinstance(candidate, Mapping) and candidate.get("message"):
                        detail = candidate["message"]
                        break
        parts.append(f"{kind}: {detail}" if detail else str(kind))
    return "; ".join(parts) or "Amazon refused this row without giving a reason."


# ─── Negatives (search-term rules) ───────────────────────────────────────────
#
# Probed 25 Oct 2026 with a deliberately invalid ad group, so nothing was created:
#
#   POST /sp/negativeKeywords   207  {"negativeKeywords": {"success": [], "error": [{index, errors}]}}
#   POST /sp/negativeTargets    207  {"negativeTargetingClauses": {"success": [], "error": [...]}}
#   POST /sb/negativeKeywords   207  [{"code": "INVALID_ARGUMENT", "description": ...}]   bare list
#   POST /sb/negativeTargets    200  {"createTargetSuccessResults": [], "createTargetErrorResults":
#                                     [{"targetRequestIndex": 0, "code": ..., "details": ...}]}
#   POST /sp/negativeKeywords/delete, /sp/negativeTargets/delete   207, SP's shape
#
# Note the last SB one: **a refusal inside a 200**, so the status code says nothing about the rows.

SP_NEG_KEYWORDS = ("/sp/negativeKeywords", "application/vnd.spNegativeKeyword.v3+json", "negativeKeywords")
SP_NEG_TARGETS = ("/sp/negativeTargets", "application/vnd.spNegativeTargetingClause.v3+json",
                  "negativeTargetingClauses")
SB_NEG_KEYWORDS_PATH = "/sb/negativeKeywords"
SB_NEG_TARGETS_PATH = "/sb/negativeTargets"
#: Sponsored Brands' create batches are kept small; its list endpoints cap at 100 too.
SB_NEG_BATCH = 100


def _neg_row(entity_type, product, ident, group, campaign, name, state) -> dict:
    return {"entity_type": entity_type, "ad_product": product, "entity_id": str(ident or ""),
            "parent_id": str(group or ""), "campaign_id": str(campaign or ""),
            "name": str(name or "").strip().lower(), "state": str(state or "").upper()}


def _asin_of(expressions) -> str:
    for e in expressions or ():
        if str(e.get("type") or "").upper() in ("ASIN_SAME_AS", "ASINSAMEAS"):
            return str(e.get("value") or "")
    return ""


async def fetch_negatives(client, *, ad_group_ids=None) -> list[dict]:
    """Every ENABLED ad-group negative, SP and SB, as `ads_entity` rows (`neg_keyword` / `neg_target`,
    `name` = lower-case keyword text or ASIN). With ``ad_group_ids``, only those groups (SP filters at
    Amazon; SB is listed whole and filtered here — it is a few hundred rows)."""
    from app.ads.search_store import NEG_KEYWORD, NEG_TARGET

    settings = get_settings()
    groups = sorted({str(g) for g in ad_group_ids or () if g})
    out: list[dict] = []
    filter_sets = ([{"adGroupIdFilter": {"include": groups[i:i + PAGE_SIZE]}}
                    for i in range(0, len(groups), PAGE_SIZE)] if groups else [{}])
    for extra in filter_sets:
        flt = {**extra, "stateFilter": {"include": ["ENABLED"]}}
        for r in await _list(client, SP_NEG_KEYWORDS, filters=flt):
            out.append(_neg_row(NEG_KEYWORD, "sp", r.get("keywordId"), r.get("adGroupId"),
                                r.get("campaignId"), r.get("keywordText"), r.get("state")))
        for r in await _list(client, SP_NEG_TARGETS, filters=flt):
            out.append(_neg_row(NEG_TARGET, "sp", r.get("targetId"), r.get("adGroupId"), r.get("campaignId"),
                                _asin_of(r.get("expression") or r.get("resolvedExpression")), r.get("state")))

    token = await _access_token(client)
    start = 0
    for _ in range(MAX_PAGES):
        response = await client.get(settings.ads_endpoint + SB_NEG_KEYWORDS_PATH,
                                    params={"startIndex": start, "count": 5000}, headers=_headers(token))
        if response.status_code >= 400:
            raise AdsError(f"Listing Sponsored Brands negative keywords failed: {response.text[:200]}",
                           status=response.status_code)
        page = response.json() or []
        for r in page:
            out.append(_neg_row(NEG_KEYWORD, "sb", r.get("keywordId"), r.get("adGroupId"),
                                r.get("campaignId"), r.get("keywordText"), r.get("state")))
        if len(page) < 5000:
            break
        start += len(page)
    for r in await _sb_list(client, (SB_NEG_TARGETS_PATH + "/list", SB_NO_MEDIA_TYPE, "negativeTargets"),
                            {}, "negative targets"):
        out.append(_neg_row(NEG_TARGET, "sb", r.get("targetId"), r.get("adGroupId"), r.get("campaignId"),
                            _asin_of(r.get("expressions") or r.get("resolvedExpressions")), r.get("state")))
    out = [r for r in out if r["state"] in ("ENABLED", "") and r["entity_id"]]
    if groups:
        wanted = set(groups)
        out = [r for r in out if r["parent_id"] in wanted]
    return out


def _sp_outcome(body, order: list[str], key: str, id_fields: Sequence[str]) -> list[dict]:
    """SP's 207 for a negative create or delete: `{key: {success: [{index, <id>}], error: [...]}}`."""
    outcome = body.get(key) if isinstance(body, Mapping) else None
    if not isinstance(outcome, Mapping):
        return [{**r, "new_id": None} for r in _unmentioned(order, set())]
    results, seen = [], set()
    for item in outcome.get("success") or []:
        index = item.get("index")
        if not (isinstance(index, int) and 0 <= index < len(order)):
            continue
        new_id = next((str(item[f]) for f in id_fields if item.get(f)), "")
        seen.add(order[index])
        results.append({"entity_id": order[index], "ok": bool(new_id), "new_id": new_id or None,
                        "error": None if new_id else "Amazon did not return the new negative's id."})
    for item in outcome.get("error") or []:
        index = item.get("index")
        if isinstance(index, int) and 0 <= index < len(order):
            seen.add(order[index])
            results.append({"entity_id": order[index], "ok": False, "new_id": None,
                            "error": _error_message(item)})
    results.extend({**r, "new_id": None} for r in _unmentioned(order, seen))
    return results


def _sb_keyword_outcome(body, order: list[str]) -> list[dict]:
    """SB's bare list: one `{code, keywordId}` per request row, in request order."""
    results, seen = [], set()
    for index, item in enumerate(body if isinstance(body, list) else []):
        if index >= len(order) or not isinstance(item, Mapping):
            continue
        seen.add(order[index])
        ok = str(item.get("code") or "").upper() == "SUCCESS" and item.get("keywordId")
        results.append({"entity_id": order[index], "ok": bool(ok),
                        "new_id": str(item["keywordId"]) if ok else None,
                        "error": None if ok else (_sb_error_message(item) or str(item.get("code") or "refused"))})
    results.extend({**r, "new_id": None} for r in _unmentioned(order, seen))
    return results


def _sb_target_outcome(body, order: list[str]) -> list[dict]:
    """SB negative targets: **200 with the refusals inside** (`createTargetErrorResults`)."""
    results, seen = [], set()
    body = body if isinstance(body, Mapping) else {}
    for item in body.get("createTargetSuccessResults") or []:
        index = item.get("targetRequestIndex")
        if isinstance(index, int) and 0 <= index < len(order) and item.get("targetId"):
            seen.add(order[index])
            results.append({"entity_id": order[index], "ok": True, "new_id": str(item["targetId"]), "error": None})
    for item in body.get("createTargetErrorResults") or []:
        index = item.get("targetRequestIndex")
        if isinstance(index, int) and 0 <= index < len(order):
            seen.add(order[index])
            results.append({"entity_id": order[index], "ok": False, "new_id": None,
                            "error": str(item.get("details") or item.get("code") or "refused")})
    results.extend({**r, "new_id": None} for r in _unmentioned(order, seen))
    return results


def _negative_payload(row: Mapping, writer: str) -> dict:
    from app.ads.negatives import W_SB_KEYWORD, W_SB_PRODUCT, W_SP_KEYWORD

    term = str(row["search_term"])
    if writer == W_SP_KEYWORD:
        return {"campaignId": str(row["campaign_id"]), "adGroupId": str(row["ad_group_id"]),
                "keywordText": term, "matchType": "NEGATIVE_EXACT", "state": "ENABLED"}
    if writer == W_SB_KEYWORD:
        return {"campaignId": int(row["campaign_id"]), "adGroupId": int(row["ad_group_id"]),
                "keywordText": term, "matchType": "negativeExact"}
    if writer == W_SB_PRODUCT:
        return {"campaignId": int(row["campaign_id"]), "adGroupId": int(row["ad_group_id"]),
                "expressions": [{"type": "asinSameAs", "value": term.upper()}]}
    return {"campaignId": str(row["campaign_id"]), "adGroupId": str(row["ad_group_id"]),
            "expression": [{"type": "ASIN_SAME_AS", "value": term.upper()}], "state": "ENABLED"}


async def create_negatives(client, rows: Sequence[Mapping], *, writer: str, sleep=asyncio.sleep) -> list[dict]:
    """Create ad-group negatives for one writer. Per-row results `{entity_id, ok, new_id, error}`.

    `rows`: `[{"entity_id": provisional id, "campaign_id", "ad_group_id", "search_term"}]`. A word term
    is sent as NEGATIVE EXACT, an ASIN as "ASIN same as". A row Amazon does not mention, or a success
    without an id, is FAILED: without the id there is nothing to undo.
    """
    from app.ads.negatives import W_SB_KEYWORD, W_SB_PRODUCT, W_SP_KEYWORD

    if not rows:
        return []
    settings = get_settings()
    token = await _access_token(client)
    sb = writer in (W_SB_KEYWORD, W_SB_PRODUCT)
    batch_size = SB_NEG_BATCH if sb else WRITE_BATCH
    results: list[dict] = []
    for batch_start in range(0, len(rows), batch_size):
        batch = list(rows[batch_start:batch_start + batch_size])
        order = [str(r["entity_id"]) for r in batch]
        items = [_negative_payload(r, writer) for r in batch]
        if batch_start:
            await sleep(WRITE_INTERVAL)
        if writer == W_SP_KEYWORD:
            path, head, payload = SP_NEG_KEYWORDS[0], _media(SP_NEG_KEYWORDS[1], token), {"negativeKeywords": items}
        elif writer == W_SB_KEYWORD:
            path, head, payload = SB_NEG_KEYWORDS_PATH, _headers(token), items
        elif writer == W_SB_PRODUCT:
            path, head, payload = SB_NEG_TARGETS_PATH, _headers(token), {"negativeTargets": items}
        else:
            path, head, payload = (SP_NEG_TARGETS[0], _media(SP_NEG_TARGETS[1], token),
                                   {"negativeTargetingClauses": items})
        response = await client.post(settings.ads_endpoint + path, json=payload, headers=head)
        if response.status_code >= 400 and response.status_code != 207:
            message = f"Amazon refused the batch ({response.status_code}): {response.text[:200]}"
            logger.warning("ads: negative create batch failed: %s", message)
            results.extend({"entity_id": i, "ok": False, "new_id": None, "error": message} for i in order)
            continue
        body = response.json()
        if writer == W_SP_KEYWORD:
            results.extend(_sp_outcome(body, order, "negativeKeywords", ("negativeKeywordId", "keywordId")))
        elif writer == W_SB_KEYWORD:
            results.extend(_sb_keyword_outcome(body, order))
        elif writer == W_SB_PRODUCT:
            results.extend(_sb_target_outcome(body, order))
        else:
            results.extend(_sp_outcome(body, order, "negativeTargetingClauses", ("targetId", "negativeTargetId")))
    return results


async def delete_negatives(client, ids: Sequence[str], *, writer: str) -> list[dict]:
    """Remove SP ad-group negatives (the undo of a negative rule). Sponsored Brands is refused: its
    archive is permanent, so there is no undo there."""
    from app.ads.negatives import W_SP_KEYWORD, W_SP_PRODUCT

    if not ids:
        return []
    if writer not in (W_SP_KEYWORD, W_SP_PRODUCT):
        return [{"entity_id": str(i), "ok": False,
                 "error": "A Sponsored Brands negative cannot be removed (Amazon's archive is permanent)."}
                for i in ids]
    settings = get_settings()
    token = await _access_token(client)
    results: list[dict] = []
    for start in range(0, len(ids), WRITE_BATCH):
        order = [str(i) for i in ids[start:start + WRITE_BATCH]]
        if writer == W_SP_KEYWORD:
            path, vnd, key, flt = (SP_NEG_KEYWORDS[0] + "/delete", SP_NEG_KEYWORDS[1], "negativeKeywords",
                                   "negativeKeywordIdFilter")
        else:
            path, vnd, key, flt = (SP_NEG_TARGETS[0] + "/delete", SP_NEG_TARGETS[1], "negativeTargetingClauses",
                                   "negativeTargetIdFilter")
        response = await client.post(settings.ads_endpoint + path, json={flt: {"include": order}},
                                     headers=_media(vnd, token))
        if response.status_code >= 400 and response.status_code != 207:
            results.extend({"entity_id": i, "ok": False, "error": response.text[:200]} for i in order)
            continue
        parsed = _sp_outcome(response.json(), order, key, ("negativeKeywordId", "targetId", "keywordId"))
        # A delete answers with the SAME id it was given, so success is "Amazon named it".
        results.extend({"entity_id": r["entity_id"], "ok": r["ok"] or bool(r.get("new_id")),
                        "error": r["error"]} for r in parsed)
    return results


__all__ = [
    "AdsError", "AdsNotConfigured",
    "fetch_campaigns", "fetch_ad_groups", "fetch_keywords", "fetch_targets",
    "fetch_current_bids", "apply_changes", "create_product_ads",
    "fetch_negatives", "create_negatives", "delete_negatives",
]
