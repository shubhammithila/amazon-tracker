"""SKU coverage: does every SP ad group hold every SKU of the products it advertises?

Asked for as *"check that all the sku's of a parent has been added to that particular ad group where
it should — whether it is active or inactive is a different thing but it should be added. in SP…
maybe we do the list in fba and flex but forgot to add it in the ad group where the easy ship sku was
added. the parent and its sku's dependency will be the same which we follow in the portfolio tab."*
Spec: `docs/superpowers/specs/2026-10-24-ads-sku-coverage-design.md`.

**The product is the Portfolio Profit row** — the flavour id where Amazon files several flavours under
one parent, else Amazon's parent, else the ASIN itself — so an ad group holding only Peri Peri chana
expects only Peri Peri sizes, and one holding all five flavours expects all five flavours' sizes.

`compute` is PURE: it takes the cached product ads, campaigns, ad groups, listings and the row map,
and decides. `build` gathers those from the database. Nothing here calls Amazon or writes.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence

from app.ads import logic
from app.ads.listings import INCOMPLETE

#: `product_key` meaning "the whole ad group" in an ignore mark.
WHOLE_GROUP = "*"

#: A SKU the group does not hold, of an ASIN it DOES hold — the main case ("FBA and Flex added,
#: Easy Ship forgotten").
KIND_SKU = "sku"
#: A pack size (or flavour) of an advertised product with no SKU in the group at all.
KIND_SIZE = "size"

ARCHIVED = "ARCHIVED"


def provisional_id(ad_group_id: str, sku: str) -> str:
    """The ledger id of a product ad not created yet: `new-` + 20 hex. Replaced by Amazon's `adId`.

    24 characters, inside `ads_mutation.entity_id`'s 32, and stable for the same (group, SKU) so a run
    cannot hold the same addition twice.
    """
    digest = hashlib.sha1(f"{ad_group_id}|{sku}".encode("utf-8")).hexdigest()[:20]
    return f"new-{digest}"


def _sheet_inactive(entry) -> bool:
    """Only an EXPLICIT falsy Active flag retires a size — missing data is not a decision."""
    return isinstance(entry, Mapping) and "active" in entry and not entry.get("active")


def _size(entry) -> str:
    from app.shipment.logic import weight_label

    weight = entry.get("weight") if isinstance(entry, Mapping) else None
    try:
        return weight_label(weight) if weight else ""
    except Exception:  # noqa: BLE001 - a label must never break the check
        return ""


def compute(
    product_ads: Sequence[Mapping],
    campaigns: Mapping[str, Mapping],
    ad_groups: Mapping[str, Mapping],
    listings: Sequence[Mapping],
    row_of: Callable[[str], str],
    catalogue: Mapping[str, Mapping],
    ignores: set[tuple[str, str]] | None = None,
) -> dict:
    """The gaps, campaign -> ad group -> product -> missing SKUs, with counts. Pure.

    * SP only; enabled campaigns WE manage (`logic.manager_of`); enabled ad groups.
    * Expected: every listing SKU of every child ASIN of each product the group advertises, except
      sizes the MRP sheet marks inactive and listings Amazon calls Incomplete.
    * Present: any product ad of the group in any state but ARCHIVED — a paused SKU is the owner's
      decision, an absent one is the gap.
    * An ignored product (or whole group) stays in the answer flagged ``ignored`` and out of the counts.
    """
    ignores = ignores or set()
    sku_asin = {str(l["seller_sku"]): str(l.get("asin") or "") for l in listings}
    skus_of: dict[str, list[Mapping]] = {}
    for listing in listings:
        if listing.get("asin"):
            skus_of.setdefault(str(listing["asin"]), []).append(listing)

    children: dict[str, set[str]] = {}
    for asin in set(skus_of) | {a for a in catalogue}:
        children.setdefault(row_of(asin), set()).add(asin)

    by_group: dict[str, list[Mapping]] = {}
    for ad in product_ads:
        if (ad.get("ad_product") or "sp") != "sp":
            continue
        by_group.setdefault(str(ad.get("parent_id") or ""), []).append(ad)

    skipped = {"campaign_not_enabled": 0, "managed_elsewhere": 0, "ad_group_paused": 0}
    totals = {"groups_checked": 0, "groups_with_gaps": 0, "missing": 0, "ignored": 0,
              "by_kind": {KIND_SKU: 0, KIND_SIZE: 0}, "by_channel": {}, "incomplete_skipped": 0,
              "products_checked": 0}
    out_campaigns: dict[str, dict] = {}

    for group_id, ads in sorted(by_group.items()):
        if not group_id:
            continue
        campaign_id = str(ads[0].get("campaign_id") or "")
        campaign = campaigns.get(campaign_id)
        group = ad_groups.get(group_id) or {}
        if not campaign or str(campaign.get("state") or "").upper() != logic.STATE_ENABLED:
            skipped["campaign_not_enabled"] += 1
            continue
        if logic.manager_of(campaign.get("name")) != logic.MANAGER_US:
            skipped["managed_elsewhere"] += 1
            continue
        if group and str(group.get("state") or "").upper() != logic.STATE_ENABLED:
            skipped["ad_group_paused"] += 1
            continue
        totals["groups_checked"] += 1

        live = [a for a in ads if str(a.get("state") or "").upper() != ARCHIVED]
        have_skus = {str(a.get("name") or "") for a in live}
        have_asins = {str(a.get("asin") or "") or sku_asin.get(str(a.get("name") or ""), "")
                      for a in live} - {""}
        group_ignored = (group_id, WHOLE_GROUP) in ignores

        products = []
        for key in sorted({row_of(a) for a in have_asins}):
            totals["products_checked"] += 1
            ignored = group_ignored or (group_id, key) in ignores
            held = sorted(a for a in have_asins if row_of(a) == key)
            name = (catalogue.get(held[0]) or {}).get("name") or held[0]
            missing = []
            for asin in sorted(children.get(key, set()) | set(held)):
                if _sheet_inactive(catalogue.get(asin)):
                    continue
                for listing in skus_of.get(asin, ()):
                    sku = str(listing["seller_sku"])
                    if sku in have_skus:
                        continue
                    if str(listing.get("status") or "").lower() == INCOMPLETE:
                        if not ignored:
                            totals["incomplete_skipped"] += 1
                        continue
                    entry = catalogue.get(asin) or {}
                    missing.append({
                        "sku": sku, "asin": asin,
                        "product": entry.get("name") or listing.get("item_name") or asin,
                        "size": _size(entry),
                        "channel": logic.sku_channel(sku),
                        "listing_status": listing.get("status") or "",
                        "kind": KIND_SKU if asin in have_asins else KIND_SIZE,
                    })
            if not missing:
                continue
            missing.sort(key=lambda m: (m["kind"] != KIND_SKU, m["product"], m["size"], m["sku"]))
            products.append({"product_key": key, "product": name, "ignored": ignored,
                             "missing": missing})
            if ignored:
                totals["ignored"] += len(missing)
            else:
                totals["missing"] += len(missing)
                for m in missing:
                    totals["by_kind"][m["kind"]] += 1
                    totals["by_channel"][m["channel"]] = totals["by_channel"].get(m["channel"], 0) + 1

        if not products:
            continue
        live_missing = sum(len(p["missing"]) for p in products if not p["ignored"])
        if live_missing:
            totals["groups_with_gaps"] += 1
        bucket = out_campaigns.setdefault(campaign_id, {
            "campaign_id": campaign_id, "campaign": campaign.get("name") or campaign_id,
            "missing": 0, "groups": []})
        bucket["missing"] += live_missing
        bucket["groups"].append({
            "ad_group_id": group_id, "campaign_id": campaign_id,
            "ad_group": group.get("name") or group_id, "ignored": group_ignored,
            "missing": live_missing, "products": products})

    ordered = sorted(out_campaigns.values(), key=lambda c: (-c["missing"], c["campaign"]))
    for c in ordered:
        c["groups"].sort(key=lambda g: (-g["missing"], g["ad_group"]))
    return {"campaigns": ordered, "totals": totals, "skipped": skipped}


def gaps_by_key(result: Mapping) -> dict[tuple[str, str], dict]:
    """`{(ad_group_id, sku): missing row + campaign_id}` for the gaps NOT ignored — what Add may send."""
    out = {}
    for campaign in result.get("campaigns") or ():
        for group in campaign["groups"]:
            for product in group["products"]:
                if product["ignored"]:
                    continue
                for m in product["missing"]:
                    out[(group["ad_group_id"], m["sku"])] = {
                        **m, "campaign_id": group["campaign_id"], "ad_group_id": group["ad_group_id"],
                        "ad_group": group["ad_group"], "campaign": campaign["campaign"]}
    return out


async def build(db) -> dict:
    """Gather the inputs from the database and `compute`. Read-only."""
    from sqlalchemy import select

    from app.ads import listings as listings_store
    from app.ads import repository
    from app.models import AdsEntity
    from app.portfolio import repository as pf_repo
    from app.shipment import catalogue as catalogue_mod

    ents = (await db.execute(select(
        AdsEntity.entity_type, AdsEntity.entity_id, AdsEntity.parent_id, AdsEntity.campaign_id,
        AdsEntity.name, AdsEntity.state, AdsEntity.ad_product, AdsEntity.asin,
    ).where(AdsEntity.entity_type.in_(("campaign", "ad_group", "product_ad"))))).all()
    campaigns, groups, ads = {}, {}, []
    for etype, eid, parent, camp, name, state, product, asin in ents:
        if etype == "campaign":
            campaigns[eid] = {"name": name, "state": state, "ad_product": product}
        elif etype == "ad_group":
            groups[eid] = {"name": name, "state": state, "campaign_id": parent}
        else:
            ads.append({"entity_id": eid, "parent_id": parent, "campaign_id": camp, "name": name,
                        "state": state, "ad_product": product, "asin": asin})
    # SB ad groups share the table; an SB campaign never owns a product ad, so they fall away.
    campaigns = {k: v for k, v in campaigns.items() if (v.get("ad_product") or "sp") == "sp"}

    listing_rows = await listings_store.load_listings(db)
    catalogue, _warning, _source = await catalogue_mod.load_catalogue()
    flav = await pf_repo.flavour_keys(db, catalogue)
    parent = await pf_repo.parent_of_children(db)
    ignores = await repository.load_coverage_ignores(db)

    result = compute(ads, campaigns, groups, listing_rows,
                     lambda a: flav.get(a) or parent.get(a) or a, catalogue, ignores)
    result["listings"] = await listings_store.load_status(db)
    result["listings"]["held"] = len(listing_rows)
    result["product_ads_cached"] = len(ads)
    return result
