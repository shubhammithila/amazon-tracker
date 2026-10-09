"""Customer value: LTV, CAC, payback, the cohort grid and reorder days. Hand-built, unequal values."""
import random
from datetime import date, timedelta

import pytest

from app.repeat import value

pytestmark = pytest.mark.regression
AS_OF = date(2026, 10, 5)
JAN = date(2026, 1, 1)


def line(buyer, day, product, revenue, units=1, order=None):
    return {"amazon_order_id": order or f"{buyer}-{day.isoformat()}", "buyer_key": buyer,
            "day": day, "parent_asin": product, "child_asin": product, "units": units,
            "revenue": revenue}


def run(lines, *, priced=JAN, econ=None, days=None, scope=lambda p: True):
    return value.compute(lines, scope, AS_OF, priced, econ or {}, days or set())


def crowd(n, product, first, revenue, later=None, prefix="c"):
    """`n` customers whose first order is `product` on `first`; `later` = [(offset, revenue)]."""
    out = []
    for i in range(n):
        out.append(line(f"{prefix}{product}{i}", first, product, revenue))
        for off, rev in later or []:
            out.append(line(f"{prefix}{product}{i}", first + timedelta(days=off), product, rev))
    return out


# ── LTV ─────────────────────────────────────────────────────────────────────────────────────────

def test_ltv_is_what_a_customer_paid_within_N_days_including_the_first_order():
    lines = crowd(20, "CS", date(2026, 3, 10), 200, later=[(25, 150), (70, 100)])
    ltv = run(lines)["total"]["ltv"]
    assert ltv["30"] == 350 and ltv["60"] == 350 and ltv["90"] == 450 and ltv["180"] == 450


def test_a_cohort_counts_only_once_ALL_its_customers_have_had_N_days():
    """A September cohort cannot have a 30-day LTV on 5 Oct: its last customer joined on the 30th."""
    lines = crowd(20, "CS", date(2026, 9, 2), 200, later=[(10, 100)])
    assert run(lines)["total"]["ltv"]["30"] is None
    lines = crowd(20, "CS", date(2026, 8, 2), 200, later=[(10, 100)])
    assert run(lines)["total"]["ltv"]["30"] == 300


def test_an_LTV_never_spans_lines_stored_before_their_price_was_read():
    lines = crowd(20, "CS", date(2026, 3, 10), 200)
    assert run(lines, priced=date(2026, 4, 1))["total"]["ltv"]["30"] is None
    assert run(lines, priced=None)["total"]["ltv"]["30"] is None


def test_fewer_than_twenty_customers_is_a_dash_not_a_figure():
    assert run(crowd(19, "CS", date(2026, 3, 10), 200))["total"]["ltv"]["30"] is None


# ── who brought the customer in ──────────────────────────────────────────────────────────────────

def test_the_first_product_is_the_one_that_took_the_most_money_in_the_first_order():
    lines = [line("a", date(2026, 3, 1), "CS", 300, order="o1"),
             line("a", date(2026, 3, 1), "JS", 120, order="o1")]
    rows = {r["parent_asin"]: r for r in run(lines)["rows"]}
    assert rows["CS"]["new_customers"] == 1 and "JS" not in rows


def test_each_customer_is_new_once_so_product_rows_add_up_to_the_brand():
    lines = (crowd(30, "CS", date(2026, 3, 1), 200, later=[(40, 200)])
             + crowd(25, "JS", date(2026, 4, 1), 150))
    # a CS customer later buys JS: still a CS customer, never a second "new" one
    lines.append(line("cCS0", date(2026, 5, 20), "JS", 150))
    r = run(lines)
    assert sum(x["new_customers"] for x in r["rows"]) == r["total"]["new_customers"] == 55


def test_product_ltv_counts_EVERYTHING_its_customers_went_on_to_buy():
    """A gateway product's value includes the other products it led to."""
    lines = crowd(20, "CS", date(2026, 3, 1), 100, later=[(20, 0)])
    lines += [line(f"cCS{i}", date(2026, 3, 15), "JS", 250) for i in range(20)]
    cs = next(r for r in run(lines)["rows"] if r["parent_asin"] == "CS")
    assert cs["ltv"]["30"] == 350


# ── CAC ─────────────────────────────────────────────────────────────────────────────────────────

AUG = {(date(2026, 8, 1) + timedelta(days=i)).isoformat() for i in range(31)}


def test_cac_charges_only_the_FBA_share_of_the_ads_to_the_customers_we_see():
    """₹10,000 of ads, 70% of units by FBA, 35 new FBA customers -> ₹200."""
    lines = [line(f"n{i}", date(2026, 8, 5), "CS", 300, units=2) for i in range(35)]
    econ = {("2026-08", "CS", "CS"): (10000.0, 100)}          # all-channel units 100, FBA 70
    t = run(lines, econ=econ, days=AUG)["total"]
    assert t["cac"] == 200.0 and t["new_customers"] == 35 and t["cac_customers"] == 35


def test_a_month_without_every_day_of_ad_data_is_not_used():
    lines = [line(f"n{i}", date(2026, 8, 5), "CS", 300, units=2) for i in range(35)]
    econ = {("2026-08", "CS", "CS"): (10000.0, 100)}
    assert run(lines, econ=econ, days=AUG - {"2026-08-17"})["total"]["cac"] is None


def test_a_flavour_split_row_is_charged_its_own_childs_ads():
    lines = [line(f"n{i}", date(2026, 8, 5), "PERI", 300) for i in range(20)]
    econ = {("2026-08", "RCPARENT", "PERI"): (4000.0, 20), ("2026-08", "RCPARENT", "HING"): (9000.0, 30)}
    r = value.compute(lines, lambda p: True, AS_OF, JAN, econ, AUG,
                      child_key=lambda child, parent: child)
    peri = next(x for x in r["rows"] if x["parent_asin"] == "PERI")
    assert peri["cac"] == 200.0                                # 4000 x 20/20 / 20, not HING's spend


def test_ltv_cac_is_the_90_day_ltv_over_cac():
    lines = crowd(40, "CS", date(2026, 3, 2), 600)
    lines += [line(f"n{i}", date(2026, 8, 5), "CS", 300, units=1) for i in range(40)]
    econ = {("2026-08", "CS", "CS"): (8000.0, 40)}
    t = run(lines, econ=econ, days=AUG)["total"]
    assert t["cac"] == 200.0 and t["ltv"]["90"] == 600 and t["ltv_cac"] == 3.0


# ── payback ─────────────────────────────────────────────────────────────────────────────────────

def test_payback_is_the_first_day_cumulative_spend_reaches_cac():
    """First order ₹100, ₹150 more on day 12: a ₹200 CAC is covered on day 12."""
    lines = crowd(20, "CS", date(2026, 3, 1), 100, later=[(12, 150)])
    lines += crowd(20, "CS", date(2026, 8, 5), 100, later=[(12, 150)], prefix="n")
    econ = {("2026-08", "CS", "CS"): (4000.0, 20)}
    assert run(lines, econ=econ, days=AUG)["total"]["payback_days"] == 12


def test_payback_on_the_first_order_reads_zero():
    lines = crowd(20, "CS", date(2026, 3, 1), 500)
    econ = {("2026-08", "CS", "CS"): (2000.0, 20)}
    lines += [line(f"n{i}", date(2026, 8, 5), "CS", 500) for i in range(20)]
    assert run(lines, econ=econ, days=AUG)["total"]["payback_days"] == 0


# ── the grid and reorder days ───────────────────────────────────────────────────────────────────

def test_the_grid_shows_who_ordered_again_in_each_later_complete_month():
    lines = crowd(10, "CS", date(2026, 3, 5), 100, later=[(35, 100)])      # back in April
    lines += crowd(10, "JS", date(2026, 3, 9), 100)                         # never back
    row = next(r for r in run(lines)["grid"] if r["month"] == "2026-03")
    assert row["customers"] == 20
    assert row["cells"][0]["active_pct"] == 1.0
    assert row["cells"][1]["active_pct"] == 0.5 and row["cells"][2]["active_pct"] == 0.0
    assert row["cells"][1]["revenue_per_customer"] == 150.0                 # (20x100 + 10x100)/20
    assert row["cells"][7] is None                                         # Oct is not complete


def test_reorder_days_is_the_median_gap_between_a_customers_orders_of_a_product():
    lines = []
    for i in range(20):
        lines += [line(f"r{i}", date(2026, 5, 1), "CS", 100),
                  line(f"r{i}", date(2026, 5, 1) + timedelta(days=30 + i), "CS", 100)]
    got = value.reorder_days(lines)["CS"]
    assert got["gaps"] == 20 and got["median"] == 39.5
    assert value.reorder_days(lines[:20])["CS"]["median"] is None          # 10 gaps: too few


# ── a deliberately naive restatement, on random histories ──────────────────────────────────────

def _naive_ltv(lines, n):
    first = {}
    for l in sorted(lines, key=lambda l: l["day"]):
        first.setdefault(l["buyer_key"], l["day"])
    eligible = [b for b, d in first.items()
                if value.month_end(d.isoformat()[:7]) + timedelta(days=n) <= AS_OF]
    if len(eligible) < value.MIN_CUSTOMERS:
        return None
    total = sum(l["revenue"] for l in lines
                if l["buyer_key"] in eligible and l["day"] <= first[l["buyer_key"]] + timedelta(days=n))
    return total / len(eligible)


@pytest.mark.parametrize("seed", range(25))
def test_ltv_agrees_with_a_naive_restatement(seed):
    rnd = random.Random(seed)
    lines = []
    for i in range(rnd.randint(60, 300)):
        b = f"b{rnd.randint(0, 80)}"
        d = JAN + timedelta(days=rnd.randint(0, 270))
        lines.append(line(b, d, rnd.choice(["CS", "JS", "RC"]), float(rnd.randint(50, 900)),
                          order=f"o{i}"))
    got = run(lines)["total"]["ltv"]
    for n in value.HORIZONS:
        exp = _naive_ltv(lines, n)
        assert (got[str(n)] is None) == (exp is None), (seed, n)
        if exp is not None:
            assert got[str(n)] == pytest.approx(exp), (seed, n)



def test_payback_averages_over_every_cohort_old_enough_for_that_day():
    """A young cohort that has NOT reordered pulls the early days down until it drops out of the
    average; day 12 is not yet covered when August has had only 100 per customer."""
    lines = crowd(20, "CS", date(2026, 3, 1), 100, later=[(12, 150)])
    lines += [line(f"n{i}", date(2026, 8, 5), "CS", 100) for i in range(20)]
    econ = {("2026-08", "CS", "CS"): (4000.0, 20)}
    assert run(lines, econ=econ, days=AUG)["total"]["payback_days"] > 12
