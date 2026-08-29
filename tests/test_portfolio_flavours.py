"""A parent can vary by FLAVOUR as well as by weight, and the tab only knew about weight.

**The shape is measured, not invented.** On the live amazon.in account, 5 of the 90 parents hold
more than one flavour, and the worst is a single parent holding **15 child ASINs = 5 flavours x 3
weights**:

    Cheese & Cream Roasted Chana   1301 units   Rs 2,87,970   15 sizes
      Nimbu Pudina Roasted Chana      0.5 / 0.25 / 1.0 kg
      Peri Peri Roasted Chana         0.5 / 0.25 / 1.0 kg
      Hing Jeera Roasted Chana        0.25 / 0.5 / 1.0 kg
      Chatpata Masala Roasted Chana   0.25 / 0.5 / 1.0 kg
      Cheese & Cream Roasted Chana    0.25 / 0.5 / 1.0 kg

Two consequences, and both were live defects:

1. Expanding it rendered 15 rows labelled by weight ALONE — "500 g" four times, "250 g" five
   times — with nothing saying which flavour any row was. Reported as *"the cheese and cream
   chana hai two variant grouping. flavours and weight. they should be shown prpoerly"*.
2. **The parent was named after its SMALLEST-selling flavour.** `portfolio()` took the first
   non-empty child name it happened to iterate, and Cheese & Cream is 3 of the 15 sizes and the
   least sold of the five. Requested as *"the parent name can be flavours of chana"*, which is
   also the correct fix: name the parent for what its flavours SHARE.

The five real cases need three different label shapes, which is why `family_label` reads both
ends of the names rather than one.
"""
import pytest

from app.portfolio import logic

pytestmark = pytest.mark.regression


def _size(asin, product, weight, *, sales=1000.0, units=10, net=200.0, ads=100.0):
    """A `size_row`-shaped dict. Built directly rather than through `size_row` because these
    tests are about grouping, and going via the economics shape would test the parser too."""
    return {
        "asin": asin, "product": product, "weight": weight, "brand": "Mithila Foods",
        "sales": sales, "units": units, "units_ordered": units, "units_refunded": 0,
        "net": net, "net_pct": net / sales if sales else None,
        "ad_spend": ads, "tacos": ads / sales if sales else None,
        "ads_cost": ads, "ad_attributed_sales": ads * 2,
        "acos": 0.5, "acos_infinite": False, "ad_clicks": 0, "ad_impressions": 0,
        "fees": {}, "fees_total": 0.0, "returns_pct": None, "channels": {}, "known": True,
    }


# ─── The label: what a set of flavours have in common ────────────────────────


@pytest.mark.parametrize("names,expected", [
    # END only — the real Roasted Chana family. The shared words trail the flavour.
    (["Nimbu Pudina Roasted Chana", "Peri Peri Roasted Chana", "Hing Jeera Roasted Chana",
      "Chatpata Masala Roasted Chana", "Cheese & Cream Roasted Chana"], "Roasted Chana"),
    # START only — the real Desi Tilkut pair, where the flavour is a SUFFIX.
    (["Desi Tilkut", "Desi Tilkut - Jaggery"], "Desi Tilkut"),
    # BOTH ends — the real bori pair. Taking one end only would answer "dal bori", dropping
    # "Bengali", which is the line these products are actually sold under.
    (["Bengali Moong dal bori", "Bengali Urad dal bori"], "Bengali dal bori"),
    # END only, mixed case — the real badi family. "urad dal badi" is lowercase in the sheet
    # while "Chana dal badi" is not, so a case-SENSITIVE compare finds nothing in common.
    (["Chawli badi", "Chana dal badi", "urad dal badi", "moong dal badi"], "badi"),
    # END only — the real sesame laddoo family.
    (["White Sesame Laddoo - Jaggery", "Mix Sesame Laddoo - Jaggery",
      "Black Sesame Laddoo - Jaggery"], "Sesame Laddoo - Jaggery"),
])
def test_the_family_label_is_what_the_flavours_share(names, expected):
    """All five multi-flavour parents on the live account, and they need three different shapes.

    Parametrised over the REAL names rather than invented ones: the mixed-case badi family and the
    both-ends bori pair are exactly the cases a hand-written fixture would not have contained, and
    each of them broke a simpler version of this function.
    """
    assert logic.family_label(names) == expected


def test_a_single_flavour_is_its_own_label():
    """85 of the 90 parents. The name must pass through untouched, not get trimmed to a fragment."""
    assert logic.family_label(["Bengali Moori"]) == "Bengali Moori"


def test_unrelated_names_fall_back_rather_than_inventing_a_family():
    """Nothing shared means there IS no family name, and a fabricated one would be worse.

    Falls back to the FIRST name, which callers pass as the biggest seller's — so the row is named
    after the product most of the money is in rather than an arbitrary one.
    """
    assert logic.family_label(["Bengali Moori", "Herbal Gulal"]) == "Bengali Moori"


def test_a_shared_run_is_never_emitted_twice():
    """The overlap guard — and finding a case that actually TRIGGERS it took a mutation.

    My first version of this test used ["Roasted Chana", "Roasted Chana Masala"] and passed with the
    guard deleted, so it was testing nothing: the runs there are genuinely disjoint (head
    ["Roasted", "Chana"], tail []). A mutation replacing the guard with `if False` survived, which
    is the only reason the gap surfaced.

    The real trigger is a **short name that appears at BOTH ends of a longer one**: "Sattu" against
    "Sattu Mix Sattu" gives head ["Sattu"] and tail ["Sattu"], and summing them yields "Sattu
    Sattu" — a label with one word in it twice, which reads as corrupt data rather than as a family
    name. The shortest name bounds how many tokens may be claimed in total.
    """
    label = logic.family_label(["Sattu", "Sattu Mix Sattu"])
    assert label == "Sattu", f"a shared word was emitted twice: {label!r}"

    # The guard must not be so eager that it discards a legitimate BOTH-ends label — the real
    # "Bengali dal bori" pair claims one token from each end and must survive.
    assert logic.family_label(
        ["Bengali Moong dal bori", "Bengali Urad dal bori"]
    ) == "Bengali dal bori"


def test_the_label_keeps_the_biggest_sellers_spelling():
    """Casing comes from the leading name, so the label reads how the best-known product spells it.

    The badi family mixes "urad dal badi" with "Chana dal badi". Matching is case-insensitive or
    nothing groups; the OUTPUT still has to pick one casing, and the biggest seller's is the one a
    human recognises.

    The shared run here is TWO words ("dal badi"), not one — these two names differ only in their
    first token — so the assertion is about which casing wins, and the pair is deliberately given
    in both orders.
    """
    assert logic.family_label(["Chana Dal Badi", "urad dal badi"]) == "Dal Badi"
    assert logic.family_label(["urad dal badi", "Chana Dal Badi"]) == "dal badi"


# ─── The grouping ────────────────────────────────────────────────────────────


def test_a_single_flavour_parent_is_not_grouped_at_all():
    """85 of 90 parents, so this is the common case and it must stay a FLAT list of weights.

    Empty rather than one group of everything: the template checks for emptiness to decide whether
    to draw a heading level, and a single group would add a heading that repeats the parent's own
    name on every product in the portfolio.
    """
    sizes = [_size("B0A1", "Bengali Moori", 0.5), _size("B0A2", "Bengali Moori", 1.0)]
    assert logic.flavour_groups(sizes) == []


def test_two_dimensions_become_flavour_then_weight():
    """The real Cheese & Cream shape, at 5 flavours x 3 weights.

    Fails against the old code, which had no `flavour_groups` at all — the 15 sizes were one flat
    list labelled by weight, where "250 g" named five different products.
    """
    sizes = []
    for flavour, base in [("Nimbu Pudina Roasted Chana", 54325.52),
                          ("Peri Peri Roasted Chana", 39035.47),
                          ("Hing Jeera Roasted Chana", 26872.14),
                          ("Chatpata Masala Roasted Chana", 19586.57),
                          ("Cheese & Cream Roasted Chana", 8325.21)]:
        for i, weight in enumerate((0.5, 0.25, 1.0)):
            sizes.append(_size(f"B0{flavour[:2]}{i}", flavour, weight, sales=base / (i + 1)))

    groups = logic.flavour_groups(sizes)
    assert len(groups) == 5, f"expected one group per flavour, got {len(groups)}"
    assert [len(g["sizes"]) for g in groups] == [3, 3, 3, 3, 3]
    # Biggest seller first, matching how products and sizes are ordered everywhere else here.
    assert groups[0]["flavour"] == "Nimbu Pudina Roasted Chana"
    assert groups[-1]["flavour"] == "Cheese & Cream Roasted Chana"
    # And within a flavour, heaviest revenue first.
    for group in groups:
        sales = [s["sales"] for s in group["sizes"]]
        assert sales == sorted(sales, reverse=True), f"{group['flavour']} sizes are not by revenue"


def test_a_flavour_group_is_exactly_the_sum_of_its_sizes():
    """The same invariant `test_a_parent_is_exactly_the_sum_of_its_sizes` pins one level up.

    The group heading sits directly above its own weight rows on screen, so a figure computed any
    other way would visibly not add up — and this is a NEW row, so nothing else was guarding it.
    """
    sizes = [
        _size("B0A1", "Peri Peri Roasted Chana", 0.5, sales=39035.47, units=158, net=15264.0),
        _size("B0A2", "Peri Peri Roasted Chana", 0.25, sales=23107.34, units=148, net=4300.0),
        _size("B0B1", "Hing Jeera Roasted Chana", 0.25, sales=26872.14, units=162, net=9000.0),
    ]
    groups = {g["flavour"]: g for g in logic.flavour_groups(sizes)}
    peri = groups["Peri Peri Roasted Chana"]
    assert peri["sales"] == pytest.approx(39035.47 + 23107.34, abs=0.011)
    assert peri["units"] == 158 + 148
    assert peri["net"] == pytest.approx(15264.0 + 4300.0, abs=0.011)


def test_group_percentages_are_recomputed_from_the_sums_never_averaged():
    """Built so the two answers differ sharply, or the test would prove nothing.

    A big size at 10% TACOS beside a tiny one at 90%: the mean is 50%, the money-weighted truth is
    ~10.8%. Averaging is the classic error here and it looks entirely plausible in a summary row.
    """
    sizes = [
        _size("B0A1", "Peri Peri Roasted Chana", 1.0, sales=100000.0, ads=10000.0, net=30000.0),
        _size("B0A2", "Peri Peri Roasted Chana", 0.25, sales=1000.0, ads=900.0, net=-500.0),
        _size("B0B1", "Hing Jeera Roasted Chana", 0.25, sales=5000.0, ads=500.0, net=1000.0),
    ]
    peri = logic.flavour_groups(sizes)[0]
    assert peri["flavour"] == "Peri Peri Roasted Chana"
    assert peri["tacos"] == pytest.approx(10900.0 / 101000.0, abs=1e-6)
    assert peri["tacos"] != pytest.approx(0.50, abs=0.01), "the ratios were averaged"
    assert peri["net_pct"] == pytest.approx(29500.0 / 101000.0, abs=1e-6)


# ─── How it reaches the dashboard ────────────────────────────────────────────


def _econ(child, parent, name, *, sales, units, net, ads=0.0):
    return {
        "parentAsin": parent, "childAsin": child,
        "sales": {
            "orderedProductSales": {"amount": sales},
            "refundedProductSales": {"amount": 0.0},
            "unitsOrdered": units, "unitsRefunded": 0, "netUnitsSold": units,
        },
        "fees": [],
        "ads": [{"adTypeName": "SponsoredProductFee",
                 "charge": {"totalAmount": {"amount": ads}}}] if ads else [],
        "netProceeds": {"total": {"amount": net}},
    }


#: One parent, three flavours, mirroring the live shape. Cheese & Cream is deliberately the
#: SMALLEST seller and appears FIRST, which is what named the real parent after it.
CATALOGUE = {
    "B0CC1": {"name": "Cheese & Cream Roasted Chana", "weight": 0.25, "brand": "Mithila Foods"},
    "B0NP1": {"name": "Nimbu Pudina Roasted Chana", "weight": 0.5, "brand": "Mithila Foods"},
    "B0NP2": {"name": "Nimbu Pudina Roasted Chana", "weight": 0.25, "brand": "Mithila Foods"},
    "B0PP1": {"name": "Peri Peri Roasted Chana", "weight": 0.5, "brand": "Mithila Foods"},
}

ECON = [
    _econ("B0CC1", "B0PARENT", "cc", sales=8325.21, units=54, net=1200.0, ads=2000.0),
    _econ("B0NP1", "B0PARENT", "np", sales=54325.52, units=218, net=25900.0, ads=12057.0),
    _econ("B0NP2", "B0PARENT", "np", sales=32022.73, units=196, net=-512.0, ads=20497.0),
    _econ("B0PP1", "B0PARENT", "pp", sales=39035.47, units=158, net=15264.0, ads=8217.0),
]


def test_the_parent_is_named_for_the_shared_family_not_the_first_child():
    """**The name was an accident of iteration order, and it picked the worst seller.**

    Fails loudly against the old code, which returned "Cheese & Cream Roasted Chana" — 1 of the 4
    sizes here and the smallest of them. The fixture puts that ASIN first precisely so the old
    behaviour is reproduced rather than avoided by luck.
    """
    result = logic.portfolio(ECON, CATALOGUE, ratings={}, decisions={})
    parent = result["parents"][0]
    assert parent["product"] == "Roasted Chana", (
        f"named {parent['product']!r} — the shared family name is what the row should carry"
    )


def test_a_single_flavour_parent_keeps_its_own_full_name():
    """The 85-parent case, and the reason `family_label` is only consulted when grouping happens.

    Applied unconditionally it would trim a lone "Bengali Moori" to nothing useful, renaming 85
    products to fix 5.
    """
    catalogue = {
        "B0M1": {"name": "Bengali Moori", "weight": 0.5, "brand": "Mithila Foods"},
        "B0M2": {"name": "Bengali Moori", "weight": 1.0, "brand": "Mithila Foods"},
    }
    econ = [
        _econ("B0M1", "B0PM", "m", sales=10000.0, units=50, net=2000.0),
        _econ("B0M2", "B0PM", "m", sales=5000.0, units=20, net=1000.0),
    ]
    result = logic.portfolio(econ, catalogue, ratings={}, decisions={})
    parent = result["parents"][0]
    assert parent["product"] == "Bengali Moori"
    assert parent["flavour_groups"] == [], "a single-flavour parent grew a grouping level"
    assert parent["flavours"] == []


def test_the_dashboard_carries_the_groups_and_they_still_sum_to_the_parent():
    """The groups are presentation, so they must not become a second source of the parent's money.

    Asserted through the full `portfolio()` rather than `flavour_groups` alone, because the risk is
    in the wiring: a grouping built from a different list than the parent sums would look right and
    add up wrong.
    """
    result = logic.portfolio(ECON, CATALOGUE, ratings={}, decisions={})
    parent = result["parents"][0]
    groups = parent["flavour_groups"]
    assert [g["flavour"] for g in groups] == [
        "Nimbu Pudina Roasted Chana", "Peri Peri Roasted Chana", "Cheese & Cream Roasted Chana",
    ], "groups are not ordered by revenue"
    for field in ("sales", "units", "net", "ad_spend"):
        total = round(sum(g[field] for g in groups), 2)
        assert abs(total - parent[field]) < 0.011, (
            f"{field}: the flavour groups sum to {total} but the parent says {parent[field]}"
        )
    # Every size appears exactly once across the groups — no row lost, none double-counted.
    grouped = [s["asin"] for g in groups for s in g["sizes"]]
    assert sorted(grouped) == sorted(s["asin"] for s in parent["sizes"])


def test_a_surgical_reason_names_the_flavour_when_there_is_more_than_one():
    """**"2 size(s) lose money (250 g, 250 g)" names two different products identically.**

    The reason exists so the owner can act on it, and a weight alone is not actionable on a parent
    holding five flavours at three weights each. Fails against the old `_size_label`, which had no
    flavour at all.
    """
    result = logic.portfolio(ECON, CATALOGUE, ratings={}, decisions={})
    parent = result["parents"][0]
    assert parent["verdict"] == logic.VERDICT_SURGICAL, parent["verdict_reason"]
    assert "Nimbu Pudina" in parent["verdict_reason"], (
        f"the losing size is not named by flavour: {parent['verdict_reason']}"
    )


def test_a_single_flavour_reason_does_not_repeat_the_product_name():
    """The other half of the same rule: for 85 parents the flavour IS the parent name.

    Prefixing it would render "the product earns 6.3% overall, but 1 size(s) lose money (Bengali
    Moori 500 g)" under a row already titled "Bengali Moori" — noise on the common case.
    """
    catalogue = {
        "B0M1": {"name": "Bengali Moori", "weight": 0.5, "brand": "Mithila Foods"},
        "B0M2": {"name": "Bengali Moori", "weight": 1.0, "brand": "Mithila Foods"},
    }
    econ = [
        _econ("B0M1", "B0PM", "m", sales=10000.0, units=50, net=-2000.0),
        _econ("B0M2", "B0PM", "m", sales=50000.0, units=200, net=20000.0),
    ]
    result = logic.portfolio(econ, catalogue, ratings={}, decisions={})
    parent = result["parents"][0]
    assert parent["verdict"] == logic.VERDICT_SURGICAL, parent["verdict_reason"]
    assert "Bengali Moori 500" not in parent["verdict_reason"], (
        f"the parent's own name is repeated on its size: {parent['verdict_reason']}"
    )
    # `shipment.logic.weight_label` renders 0.5 as "500g" (no space) — asserted as it really is,
    # so this test cannot pass against a label that stopped naming the weight at all.
    assert "500g" in parent["verdict_reason"]


def test_the_sku_view_still_carries_each_flavours_own_name():
    """The SKU grain must NOT be relabelled to the family, or 15 rows read as one product.

    The flat view is where a per-flavour kill decision is read, so `product` there stays the
    child's own name while `parent_product` carries the family.
    """
    result = logic.portfolio(ECON, CATALOGUE, ratings={}, decisions={})
    names = {row["product"] for row in result["skus"]}
    assert "Nimbu Pudina Roasted Chana" in names
    assert "Cheese & Cream Roasted Chana" in names
    assert all(row["parent_product"] == "Roasted Chana" for row in result["skus"])


def test_a_derived_name_that_collides_with_a_real_product_is_disambiguated():
    """**Shortening a family name can collide with a product that already has that name.**

    Measured on the live account after the rename: B0DWFC3QT9 holds five flavours whose shared name
    is "Roasted Chana", and B0CY8HFJT9 is a DIFFERENT parent already called exactly that — 1,309
    units against 1,301. They sat adjacent in the table as apparent duplicates with no way to tell
    them apart, and the ambiguity was entirely created by the rename.

    Only the DERIVED name is touched. The product genuinely called "Roasted Chana" keeps it, because
    renaming what Amazon and the MRP sheet agree on would be this function overreaching.
    """
    catalogue = dict(CATALOGUE)
    catalogue["B0RC1"] = {"name": "Roasted Chana", "weight": 0.5, "brand": "Mithila Foods"}
    catalogue["B0RC2"] = {"name": "Roasted Chana", "weight": 1.0, "brand": "Mithila Foods"}
    econ = ECON + [
        _econ("B0RC1", "B0PLAIN", "rc", sales=200000.0, units=800, net=60000.0),
        _econ("B0RC2", "B0PLAIN", "rc", sales=90000.0, units=509, net=20000.0),
    ]
    result = logic.portfolio(econ, catalogue, ratings={}, decisions={})
    names = {p["parent_asin"]: p["product"] for p in result["parents"]}

    assert names["B0PLAIN"] == "Roasted Chana", (
        "the product whose real catalogue name is 'Roasted Chana' was renamed"
    )
    assert names["B0PARENT"] == "Roasted Chana (3 flavours)", (
        f"the derived name still collides: {names['B0PARENT']!r}"
    )
    # And the whole point: no two products on screen share a name.
    labels = [p["product"] for p in result["parents"]]
    assert len(labels) == len(set(labels)), f"two products share a name: {labels}"


def test_a_derived_name_is_left_alone_when_nothing_collides():
    """The suffix is a remedy, not decoration — it must not appear on the 5 normal cases.

    "Roasted Chana (3 flavours)" everywhere would be noise, and the count is already on the row as
    its own badge.
    """
    result = logic.portfolio(ECON, CATALOGUE, ratings={}, decisions={})
    assert result["parents"][0]["product"] == "Roasted Chana", (
        "a flavour count was appended with no collision to resolve"
    )


def test_catalogue_duplicates_are_not_renamed_by_this_function():
    """Two parents the SHEET calls the same thing are left as they are.

    Measured: `Singhara Atta` appears four times on the live account and `Govindbhog Rice` twice,
    all single-flavour and all from the catalogue itself. Not introduced by the rename, so not this
    function's to fix — and inventing a distinction Amazon does not make would be worse than the
    duplicate, because the owner could not match either row against Seller Central.
    """
    catalogue = {
        "B0S1": {"name": "Singhara Atta", "weight": 0.5, "brand": "Mithila Foods"},
        "B0S2": {"name": "Singhara Atta", "weight": 1.0, "brand": "Mithila Foods"},
    }
    econ = [
        _econ("B0S1", "B0PA", "s", sales=1000.0, units=5, net=100.0),
        _econ("B0S2", "B0PB", "s", sales=2000.0, units=9, net=200.0),
    ]
    result = logic.portfolio(econ, catalogue, ratings={}, decisions={})
    assert [p["product"] for p in result["parents"]] == ["Singhara Atta", "Singhara Atta"]
