"""Search-term negatives: which overspending search terms become negatives, and where. Pure.

Asked for as *"negative targeting based on search terms… in auto campaigns there is close match, loose
match etc… some of the keywords that it targets might be overspending… add that keyword as a negative
target… negative exact only… for the asins there is a separate negative"*. Spec:
`docs/superpowers/specs/2026-10-25-ads-search-term-negatives-design.md`.

**Measured first (25 Oct 2026, 30 days of SP):** 413 search terms spent ₹200+ with no order, ₹1.46
lakh — and **301 of them (₹1.08 lakh) were the target itself**: an exact keyword's own words, an ASIN
target's own ASIN. Negating one of those is a pause by another name, and the turn-off rule already does
that better (and reversibly). So `plan` skips them, naming why; the real candidates were 112 terms, ₹37k.

A word term becomes a negative EXACT keyword in its ad group; an ASIN term (`b0` + 8) a negative
PRODUCT target ("ASIN same as"). Never phrase.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence

from app.ads import logic

#: The writers a negative goes to (`ads_mutation.writer`, 12 characters at most).
W_SP_KEYWORD = "neg_sp_kw"
W_SP_PRODUCT = "neg_sp_tgt"
W_SB_KEYWORD = "neg_sb_kw"
W_SB_PRODUCT = "neg_sb_tgt"
WRITERS = (W_SP_KEYWORD, W_SP_PRODUCT, W_SB_KEYWORD, W_SB_PRODUCT)
#: Sponsored Brands negatives cannot be undone: Amazon's archive is permanent there.
NO_UNDO = frozenset({W_SB_KEYWORD, W_SB_PRODUCT})

KIND_KEYWORD = "keyword"
KIND_PRODUCT = "product"
KIND_LABELS = {KIND_KEYWORD: "negative exact", KIND_PRODUCT: "negative product"}

SKIP_IS_TARGET = "is_target"
SKIP_ALREADY = "already_negative"
SKIP_MANAGED = "managed_elsewhere"
SKIP_LOW_SPEND = "below_floor"
SKIP_NO_GROUP = "no_ad_group"
SKIP_REASONS = {
    SKIP_IS_TARGET: "this is one of the ad group's own keywords or targets — turn the target off instead",
    SKIP_ALREADY: "already a negative in this ad group",
    SKIP_MANAGED: "campaign is managed by M19 or Amazon",
    SKIP_LOW_SPEND: "spent less than the negative-rule floor",
    SKIP_NO_GROUP: "the report gave no ad group for it",
}

_ASIN = re.compile(r"^b0[0-9a-z]{8}$", re.I)
_ASIN_IN = re.compile(r'asin(?:-expanded)?="?([a-z0-9]{10})"?', re.I)
_AUTO = {"close-match", "loose-match", "substitutes", "complements"}


def is_asin(term) -> bool:
    return bool(_ASIN.match(str(term or "").strip()))


def via_of(entry: Mapping, ad_product: str = "sp") -> str:
    """How one target brought the term in, as a `logic.VIA_LABELS` key ("" when unrecognised)."""
    match = str(entry.get("match_type") or "").upper()
    targeting = str(entry.get("targeting") or entry.get("text") or "").strip().lower()
    if match == "TARGETING_EXPRESSION_PREDEFINED":
        return targeting if targeting in _AUTO else ""
    if match in ("BROAD", "PHRASE", "EXACT"):
        return match.lower()
    if match == "THEME":
        return "theme"
    if match == "TARGETING_EXPRESSION":
        return "product" if _ASIN_IN.search(targeting) else "category"
    return ""


def term_is_target(term: str, own: set[str]) -> bool:
    """Is the term one of the ad group's own keywords (any match type) or ASIN targets?"""
    return str(term or "").strip().lower() in own


def metrics(row: Mapping) -> dict:
    """The rule fields for one (ad group, search term) row. A ratio with no denominator is None."""
    spend, sales = float(row.get("spend") or 0), float(row.get("sales") or 0)
    clicks, imps, orders = int(row.get("clicks") or 0), int(row.get("impressions") or 0), int(row.get("orders") or 0)
    vias = sorted(row.get("via") or (), key=lambda v: -float(v.get("spend") or 0))
    return {
        "spend": spend, "sales": sales, "clicks": clicks, "impressions": imps, "orders": orders,
        "roas": logic._ratio(sales, spend), "acos": logic._ratio(spend, sales),
        "cpc": logic._ratio(spend, clicks), "ctr": logic._ratio(clicks, imps),
        "cvr": logic._ratio(orders, clicks),
        # The target that spent most on this term decides "came in via"; the preview names them all.
        "via": via_of(vias[0], row.get("ad_product")) if vias else "",
        "term_kind": "asin" if is_asin(row.get("search_term")) else "word",
        "term": str(row.get("search_term") or ""),
    }


def kind_and_writer(term: str, ad_product: str) -> tuple[str, str]:
    asin = is_asin(term)
    sb = (ad_product or "sp").lower() == "sb"
    kind = KIND_PRODUCT if asin else KIND_KEYWORD
    writer = (W_SB_PRODUCT if sb else W_SP_PRODUCT) if asin else (W_SB_KEYWORD if sb else W_SP_KEYWORD)
    return kind, writer


def change_key(ad_product: str, ad_group_id: str, term: str) -> str:
    return f"{ad_product}|{ad_group_id}|{str(term).strip().lower()}"


def provisional_id(key: str) -> str:
    """The ledger id of a negative not created yet: `neg-` + 20 hex, replaced by Amazon's id."""
    return "neg-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def plan(
    rows: Sequence[Mapping],
    conditions: Sequence[Mapping],
    *,
    guardrails: Mapping,
    campaign_names: Mapping[str, str],
    positives: Mapping[str, set],
    negatives: set,
) -> dict:
    """The negatives a rule would add, and every matching term it would NOT, each with its reason.

    ``positives`` is `{ad_group_id: {own keyword text or ASIN}}`; ``negatives`` is
    `{(ad_group_id, "neg_keyword"|"neg_target", text or asin)}` already at Amazon.
    """
    floor = float(guardrails.get("min_negative_spend") or 0)
    changes, skipped = [], []
    matched = 0
    for row in rows:
        m = metrics(row)
        if not logic.matches(m, conditions):
            continue
        matched += 1
        term = str(row.get("search_term") or "").strip().lower()
        product = (row.get("ad_product") or "sp").lower()
        group = str(row.get("ad_group_id") or "")
        kind, writer = kind_and_writer(term, product)
        entry = {
            "key": change_key(product, group, term), "search_term": term, "kind": kind,
            "kind_label": KIND_LABELS[kind], "writer": writer, "ad_product": product,
            "campaign_id": str(row.get("campaign_id") or ""), "ad_group_id": group,
            "campaign_name": row.get("campaign_name") or campaign_names.get(str(row.get("campaign_id")), ""),
            "ad_group_name": row.get("ad_group_name") or "",
            "spend": round(m["spend"], 2), "sales": round(m["sales"], 2), "clicks": m["clicks"],
            "orders": m["orders"], "impressions": m["impressions"], "roas": m["roas"], "acos": m["acos"],
            "via": [{"label": logic.VIA_LABELS.get(via_of(v, product), v.get("match_type") or "?"),
                     "target": v.get("targeting") or v.get("text") or "", "spend": v.get("spend")}
                    for v in sorted(row.get("via") or (), key=lambda v: -float(v.get("spend") or 0))],
            "no_undo": writer in NO_UNDO,
        }
        reason = None
        cname = entry["campaign_name"]
        if not group:
            reason = SKIP_NO_GROUP
        elif logic.manager_of(cname) != logic.MANAGER_US:
            reason = SKIP_MANAGED
        elif term_is_target(term, positives.get(group, set())):
            reason = SKIP_IS_TARGET
        elif (group, "neg_target" if kind == KIND_PRODUCT else "neg_keyword", term) in negatives:
            reason = SKIP_ALREADY
        elif m["spend"] < floor:
            reason = SKIP_LOW_SPEND
        if reason:
            skipped.append({**entry, "reason": reason, "reason_text": SKIP_REASONS[reason]})
        else:
            changes.append(entry)

    changes.sort(key=lambda c: -c["spend"])
    limit = int(guardrails.get("max_rows") or 0)
    blocked = None
    if limit and len(changes) > limit:
        blocked = (f"{len(changes)} search terms match, over the {limit}-row limit. Narrow the rule "
                   "(a higher spend floor, or fewer campaigns) and preview again.")
    counts: dict[str, int] = {}
    for s in skipped:
        counts[s["reason"]] = counts.get(s["reason"], 0) + 1
    return {
        "changes": changes, "skipped": skipped, "matched": matched, "blocked": blocked,
        "approved_ids": [] if blocked else [c["key"] for c in changes],
        "totals": {"changes": len(changes), "spend": round(sum(c["spend"] for c in changes), 2),
                   "skipped": counts, "sb": sum(1 for c in changes if c["no_undo"])},
        "skip_reasons": SKIP_REASONS,
    }
