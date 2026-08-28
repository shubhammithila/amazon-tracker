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

#: Amazon's hard cap. Asking for more is not an error — it silently returns 500.
PAGE_SIZE = 500

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


async def fetch_current_bids(client, changes: Sequence[Mapping]) -> dict[str, float | None]:
    """Re-read the LIVE bid for the rows a run is about to change.

    **This is the check that stops a stale percentage.** The plan is computed from a performance
    report that may be hours old, and a bid edited in Seller Central since then would be
    overwritten with a percentage of a number that no longer exists. Fetching by id is cheap —
    one page per 500 rows — and the alternative is silently undoing someone's manual work.

    Returns `{entity_id: bid}`, with `None` for a row Amazon no longer has a bid for (archived, or
    switched to inheriting the ad group default).
    """
    from app.ads.logic import WRITER_KEYWORD

    keyword_ids = [str(c["entity_id"]) for c in changes if c.get("writer") == WRITER_KEYWORD]
    target_ids = [str(c["entity_id"]) for c in changes if c.get("writer") != WRITER_KEYWORD]

    live: dict[str, float | None] = {}

    for ids, endpoint, id_field, filter_key in (
        (keyword_ids, KEYWORDS, "keywordId", "keywordIdFilter"),
        (target_ids, TARGETS, "targetId", "targetIdFilter"),
    ):
        for start in range(0, len(ids), PAGE_SIZE):
            chunk = ids[start:start + PAGE_SIZE]
            if not chunk:
                continue
            rows = await _list(client, endpoint, filters={filter_key: {"include": chunk}})
            for row in rows:
                identifier = str(row.get(id_field) or "")
                if identifier:
                    bid = row.get("bid")
                    live[identifier] = float(bid) if bid is not None else None

    return live


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
    """
    from app.ads.logic import WRITER_KEYWORD

    if not changes:
        return []

    settings = get_settings()
    path, vnd, key = KEYWORDS if writer == WRITER_KEYWORD else TARGETS
    id_field = "keywordId" if writer == WRITER_KEYWORD else "targetId"
    body_key = "keywords" if writer == WRITER_KEYWORD else "targetingClauses"

    token = await _access_token(client)
    head = _media(vnd, token)
    results: list[dict] = []

    for batch_start in range(0, len(changes), WRITE_BATCH):
        batch = list(changes[batch_start:batch_start + WRITE_BATCH])
        # Built together so index N of `order` is index N of the request Amazon validates.
        order = [str(c["entity_id"]) for c in batch]
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

        outcome = (response.json() or {}).get(body_key) or {}
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
                    "entity_id": identifier, "ok": False,
                    "error": _error_message(item),
                })

        # Amazon said nothing about these. Reported as failed, deliberately: an unmentioned row is
        # an unknown outcome, and recording it as applied would put a wrong `old_bid` chain in the
        # ledger and make a later undo restore the wrong value.
        for identifier in order:
            if identifier not in seen:
                results.append({
                    "entity_id": identifier, "ok": False,
                    "error": "Amazon did not report an outcome for this row.",
                })

    return results


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
