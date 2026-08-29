"""The only caller of the Advertising API's ENTITY endpoints — and the only module in this app
that writes to Amazon.

Everything else here reads Amazon and writes our own records. `apply_bids` changes live bids, and
therefore live spend, which is why the read and write halves of this module are documented to
different standards: a read bug shows a wrong number, a write bug spends money.

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
    a 406 that looks like a routing error."""
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
    raw = await _sb_list(client, (SB_TARGETS_PATH, "", "targets"), {}, "targets")
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


async def apply_bids(
    client: httpx.AsyncClient,
    changes: Sequence[Mapping],
    *,
    writer: str,
    sleep=asyncio.sleep,
) -> list[dict]:
    """Send one writer's bid changes. Returns a per-row result, never a bare success flag.

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
    """
    from app.ads.logic import (
        WRITER_KEYWORD,
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
            payload = {body_key: [
                {id_field: str(c["entity_id"]), "bid": round(float(c["new_bid"]), 2)}
                for c in batch
            ]}

        if batch_start:
            await sleep(WRITE_INTERVAL)

        response = await client.put(settings.ads_endpoint + path, json=payload, headers=head)

        # A transport-level failure (401, 429, 500) is different from a per-row refusal: NOTHING in
        # the batch was applied, and every row must be reported as failed rather than left silent.
        if response.status_code >= 400 and response.status_code != 207:
            message = f"Amazon refused the batch ({response.status_code}): {response.text[:200]}"
            logger.warning("ads: bid write batch failed: %s", message)
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
    return {
        "keywordId": int(change["entity_id"]) if str(change["entity_id"]).isdigit()
        else change["entity_id"],
        "adGroupId": int(change["ad_group_id"]) if str(change.get("ad_group_id", "")).isdigit()
        else change.get("ad_group_id"),
        "bid": round(float(change["new_bid"]), 2),
    }


def _sb_target_payload_row(change: Mapping) -> dict:
    """One row of a Sponsored Brands TARGET bid write.

    Same `adGroupId` requirement as SB keywords, but wrapped in a dict rather than sent as a bare
    list — verified, the list form is refused with a JSON parsing error.
    """
    identifier = str(change["entity_id"])
    ad_group = str(change.get("ad_group_id") or "")
    return {
        "targetId": int(identifier) if identifier.isdigit() else identifier,
        "adGroupId": int(ad_group) if ad_group.isdigit() else ad_group,
        "bid": round(float(change["new_bid"]), 2),
    }


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


__all__ = [
    "AdsError", "AdsNotConfigured",
    "fetch_campaigns", "fetch_ad_groups", "fetch_keywords", "fetch_targets",
    "fetch_current_bids", "apply_bids",
]
