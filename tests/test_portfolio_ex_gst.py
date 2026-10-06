"""Every figure on the Portfolio tab is ex-GST.

Asked for as *"we do claim itc on amazon fees. keep everything ex-gst only in the app"*. Measured
over 30 days: Amazon's fee totals held ₹2.41 lakh of 18% GST on ₹15.33 lakh, which F2D claims back
as input tax credit, so counting it as a cost understated Net % by about 5.5 points. Sales were
already ex-GST, and the ad charge carries zero tax (it matches the Advertising API's `cost` to 0.1%).
"""
import pytest

from app.portfolio import economics, logic

pytestmark = pytest.mark.regression


def _raw(fee_total, fee_tax, net, *, reimbursement=0.0):
    fees = [{"feeTypeName": "WeightBasedFee", "charges": [{"aggregatedDetail": {
        "totalAmount": {"amount": fee_total}, "taxAmount": {"amount": fee_tax}}}]}]
    if reimbursement:
        fees.append({"feeTypeName": "FBAInventoryReimbursement", "charges": [{"aggregatedDetail": {
            "totalAmount": {"amount": reimbursement}, "taxAmount": {"amount": 0}}}]})
    return {"childAsin": "B0AAA00001", "parentAsin": "B0P", "startDate": "2026-10-01",
            "sales": {"orderedProductSales": {"amount": 1000}, "refundedProductSales": {"amount": 0},
                      "unitsOrdered": 5, "netUnitsSold": 5},
            "fees": fees,
            "ads": [{"adTypeName": "SponsoredProductFee", "charge": {"totalAmount": {"amount": 100}}}],
            "netProceeds": {"total": {"amount": net}}}


def test_a_fee_is_shown_WITHOUT_its_gst_and_net_gets_the_gst_back():
    # Amazon: fee 236 = 200 + 36 GST; net = 1000 - 236 - 100 = 664.
    row = logic.size_row(_raw(236.0, 36.0, 664.0), {})
    assert row["fees"]["WeightBasedFee"] == 200.0
    assert row["fees_total"] == 200.0
    assert row["net"] == 700.0                     # 1000 - 200 - 100
    assert row["net_pct"] == pytest.approx(0.70)
    assert row["fees_pct"] == pytest.approx(0.20)


def test_the_sum_still_adds_to_100_percent_ex_gst():
    row = logic.size_row(_raw(236.0, 36.0, 714.0, reimbursement=-50.0), {})   # Amazon nets the -50 too
    assert row["fees_total"] == 150.0              # 200 - 50; a reimbursement carries no GST
    total = row["refunds_pct"] + row["fees_pct"] + row["tacos"] + row["net_pct"]
    assert total == pytest.approx(1.0)


def test_a_STORED_row_has_no_tax_field_and_passes_through_unchanged():
    """Stored rows are written through `size_row`, so they are already ex-GST. Read back, they
    carry no `taxAmount`; adding GST again would double-count it."""
    stored = _raw(200.0, 0.0, 700.0)
    for fee in stored["fees"]:
        for ch in fee["charges"]:
            del ch["aggregatedDetail"]["taxAmount"]
    row = logic.size_row(stored, {})
    assert row["fees_total"] == 200.0 and row["net"] == 700.0


async def test_storing_and_reading_back_keeps_the_ex_gst_figures(db):
    from app.portfolio import repository

    await repository.save_economics_daily(db, [_raw(236.0, 36.0, 664.0)])
    back = await repository.load_snapshot(db, ("2026-10-01", "2026-10-01"))
    row = logic.size_row(back[0], {})
    assert row["fees_total"] == 200.0 and row["net"] == 700.0


def test_the_query_asks_amazon_for_the_tax_part_of_each_fee():
    q = economics.build_query("2026-10-01", "2026-10-01", "A21TJRUUN4KGV")
    fees_part = q[q.index("fees {"): q.index("ads {")]
    assert "taxAmount" in fees_part and "totalAmount" in fees_part


def test_the_merchant_FBA_split_is_ex_gst_too():
    out = logic.channel_split([dict(_raw(236.0, 36.0, 664.0), msku="0.5kg cs 1 FBA")])
    assert out["B0AAA00001"]["fba"]["net"] == 700.0
