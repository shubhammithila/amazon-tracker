"""Ads → Keyword growth: which keywords and competitor targets an ad group is offered, and what is sent.
Pure — no database, no Amazon, no Claude.

Spec: `docs/superpowers/specs/2026-10-26-ads-keyword-growth-design.md`. Asked for as *"Keyword/target
addition… at a bid I choose… very relevant and close to the product detail page… leave out the keywords
which are in negative or archived or paused… exact and phrase"*.

A **keyword** ad group (it holds exact/phrase/broad keywords) is offered keywords; a **product** ad group
(product or category targets) is offered competitor ASINs; an **auto** ad group gets nothing, because
Amazon allows no manual targets there. Every keyword goes as EXACT and PHRASE — whichever of the two the
group does not already hold — and never BROAD.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence

from app.ads import logic

KIND_KEYWORD = "keyword"
KIND_PRODUCT = "product"
KIND_AUTO = "auto"

SRC_SOLD = "sold"
SRC_AMAZON = "amazon"
SRC_AUTOCOMPLETE = "autocomplete"
SRC_PAGE = "page"
SOURCE_LABELS = {SRC_SOLD: "sold here", SRC_AMAZON: "Amazon suggests",
                 SRC_AUTOCOMPLETE: "shoppers type", SRC_PAGE: "from the page"}

MATCHES = ("EXACT", "PHRASE")
#: The reason a suggestion carries once the owner moves it by hand (`POST /ads/growth/keep`).
MOVED_KEEP = "Moved to relevant by you"
MOVED_DROP = "Moved to left out by you"
#: A KEPT verdict on another brand's name carries this prefix, and the screen tags it "competitor".
#: Kept in the reason rather than a column, so the stored verdicts and suggestions need no migration.
COMPETITOR_PREFIX = "Competitor brand: "


def is_competitor(reason) -> bool:
    return str(reason or "").startswith(COMPETITOR_PREFIX)
BID_LOW = "low"
BID_CUSTOM = "custom"

REFUSE_NO_BID = "no_bid"
REFUSE_OVER_MAX = "over_max_bid"
REFUSE_REASONS = {REFUSE_NO_BID: "no Amazon suggestion and no custom bid",
                  REFUSE_OVER_MAX: "the bid is above the max-bid guardrail"}

_ASIN = re.compile(r"^b0[0-9a-z]{8}$", re.I)
_SPACE = re.compile(r"\s+")
_KEYWORD_MATCHES = {"EXACT", "PHRASE", "BROAD"}
_PRODUCT_MATCHES = {"TARGETING_EXPRESSION", "THEME"}


def is_asin(term) -> bool:
    return bool(_ASIN.match(str(term or "").strip()))


def norm(term) -> str:
    """Lower-cased, single-spaced. An ASIN is upper-cased, the way Amazon writes it."""
    value = _SPACE.sub(" ", str(term or "").strip().lower())
    return value.upper() if is_asin(value) else value


def group_kind(match_types: Iterable[str]) -> str:
    """What an ad group targets, from the match types its report rows carry."""
    types = {str(m or "").upper() for m in match_types}
    if types & _KEYWORD_MATCHES:
        return KIND_KEYWORD
    if types & _PRODUCT_MATCHES:
        return KIND_PRODUCT
    if types:
        return KIND_AUTO
    return KIND_KEYWORD


def cards(groups: Mapping[str, Mapping], campaigns: Mapping[str, Mapping], kinds: Mapping[str, str],
          asins_by_group: Mapping[str, Sequence[str]]) -> list[dict]:
    """One card per ENABLED ad group in an ENABLED campaign we manage, SP and SB, auto groups left out.
    Ordered by campaign then ad group name."""
    out = []
    for group_id, group in groups.items():
        campaign = campaigns.get(str(group.get("campaign_id") or "")) or {}
        if str(group.get("state") or "").upper() != logic.STATE_ENABLED:
            continue
        if str(campaign.get("state") or "").upper() != logic.STATE_ENABLED:
            continue
        if logic.manager_of(campaign.get("name")) != logic.MANAGER_US:
            continue
        kind = kinds.get(group_id, KIND_KEYWORD)
        if kind == KIND_AUTO:
            continue
        out.append({
            "ad_group_id": group_id, "ad_group_name": group.get("name") or "",
            "campaign_id": str(group.get("campaign_id") or ""), "campaign_name": campaign.get("name") or "",
            "ad_product": (campaign.get("ad_product") or group.get("ad_product") or "sp").lower(),
            "kind": kind, "asins": sorted({str(a).upper() for a in asins_by_group.get(group_id) or [] if a}),
        })
    out.sort(key=lambda c: (c["campaign_name"].lower(), c["ad_group_name"].lower()))
    return out


def merge_candidates(kind: str, sources: Mapping[str, Sequence[Mapping]]) -> list[dict]:
    """One row per term across every source, with the sources that named it and their figures summed.

    A keyword card takes word terms only, a product card ASINs only: an ASIN search term is a product
    page the shopper was on, which is a product target, not a keyword.
    """
    rows: dict[str, dict] = {}
    for source, items in sources.items():
        for item in items or ():
            term = norm(item.get("term") or item.get("asin"))
            if not term or len(term) > 200:
                continue
            if (kind == KIND_PRODUCT) != is_asin(term):
                continue
            row = rows.setdefault(term, {"term": term, "sources": [], "orders": 0, "clicks": 0,
                                         "spend": 0.0, "sales": 0.0, "amazon_rank": None, "themes": []})
            if source not in row["sources"]:
                row["sources"].append(source)
            row["orders"] += int(item.get("orders") or 0)
            row["clicks"] += int(item.get("clicks") or 0)
            row["spend"] = round(row["spend"] + float(item.get("spend") or 0), 2)
            row["sales"] = round(row["sales"] + float(item.get("sales") or 0), 2)
            rank = item.get("rank")
            if rank is not None and (row["amazon_rank"] is None or int(rank) < row["amazon_rank"]):
                row["amazon_rank"] = int(rank)
            for theme in item.get("themes") or ():
                if theme not in row["themes"]:
                    row["themes"].append(theme)
    order = {SRC_SOLD: 0, SRC_AMAZON: 1, SRC_AUTOCOMPLETE: 2, SRC_PAGE: 3}
    for row in rows.values():
        row["sources"].sort(key=lambda s: order.get(s, 9))
    return sorted(rows.values(), key=lambda r: (-r["orders"], -r["spend"],
                                                 r["amazon_rank"] if r["amazon_rank"] is not None else 10**6,
                                                 r["term"]))


def exclude_existing(candidates: Sequence[Mapping], *, kind: str, held_keywords: set[tuple[str, str]],
                     held_asins: set[str], negatives: set[str]) -> tuple[list[dict], dict]:
    """Drop what the ad group already holds in ANY state (enabled, paused, archived) or has as a negative.

    ``held_keywords`` is `{(text, MATCH)}` across every state; a keyword is still offered when the group
    holds only one of EXACT and PHRASE, and then only the missing one is sent (`missing_matches`).
    ``negatives`` are lower-cased negative keyword texts and upper-cased negative ASINs of the group.
    """
    kept, dropped = [], {"already": 0, "negative": 0}
    held_asins = {a.upper() for a in held_asins}
    for cand in candidates:
        term = cand["term"]
        if kind == KIND_PRODUCT:
            if term.upper() in held_asins:
                dropped["already"] += 1
                continue
            if term.upper() in negatives:
                dropped["negative"] += 1
                continue
            kept.append({**cand, "missing_matches": []})
            continue
        if term in negatives:
            dropped["negative"] += 1
            continue
        missing = [m for m in MATCHES if (term, m) not in held_keywords]
        if not missing:
            dropped["already"] += 1
            continue
        kept.append({**cand, "missing_matches": missing})
    return kept, dropped


def key_of(ad_product: str, ad_group_id: str, kind: str, term: str) -> str:
    return f"{ad_product}|{ad_group_id}|{kind}|{norm(term)}"


def provisional_id(key: str, match: str = "") -> str:
    """The ledger id of a keyword or target not created yet: `new-` + 20 hex, replaced by Amazon's id.
    `new-` because `build_undo` already skips that prefix (nothing at Amazon to pause)."""
    return "new-" + hashlib.sha1(f"{key}|{match}".encode("utf-8")).hexdigest()[:20]


def writer_for(ad_product: str, kind: str) -> str:
    sb = (ad_product or "sp").lower() == "sb"
    if kind == KIND_PRODUCT:
        return logic.WRITER_SB_TARGET if sb else logic.WRITER_TARGET
    return logic.WRITER_SB_KEYWORD if sb else logic.WRITER_KEYWORD


def choose_bid(bids: Mapping | None, mode: str, custom) -> float | None:
    """The bid for one row: Amazon's low end when asked for and offered, else the custom ₹."""
    try:
        custom_value = round(float(custom), 2) if custom not in (None, "") else None
    except (TypeError, ValueError):
        custom_value = None
    if custom_value is not None and custom_value <= 0:
        custom_value = None
    low = (bids or {}).get("low")
    if mode == BID_LOW and low:
        return round(float(low), 2)
    return custom_value


def rows_to_send(selected: Sequence[Mapping], *, mode: str, custom, guardrails: Mapping) -> tuple[list[dict], list[dict]]:
    """The ledger/writer rows for the ticked suggestions, and the ones refused with a reason.

    A keyword becomes one row per missing match (EXACT and/or PHRASE); an ASIN one row. Every bid is
    checked against `max_bid` — Amazon enforces a floor but effectively no ceiling (₹1000 was accepted)."""
    max_bid = float(guardrails.get("max_bid") or 0)
    rows, refused = [], []
    for s in selected:
        bid = choose_bid(s.get("bids"), mode, custom)
        if bid is None:
            refused.append({**s, "reason": REFUSE_NO_BID, "reason_text": REFUSE_REASONS[REFUSE_NO_BID]})
            continue
        if max_bid and bid > max_bid:
            refused.append({**s, "bid": bid, "reason": REFUSE_OVER_MAX,
                            "reason_text": REFUSE_REASONS[REFUSE_OVER_MAX]})
            continue
        matches = (s.get("missing_matches") or list(MATCHES)) if s["kind"] == KIND_KEYWORD else [""]
        for match in matches:
            rows.append({
                "entity_id": provisional_id(s["key"], match), "create": True,
                "writer": writer_for(s["ad_product"], s["kind"]), "ad_product": s["ad_product"],
                "campaign_id": s["campaign_id"], "ad_group_id": s["ad_group_id"],
                "text": s["term"], "match_type": match or "ASIN_SAME_AS",
                "new_bid": bid, "new_state": logic.STATE_ENABLED, "key": s["key"], "kind": s["kind"],
            })
    return rows, refused
