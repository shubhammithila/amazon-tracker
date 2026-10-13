"""FBA stock from Amazon's FBA Inventory API (13 Oct 2026).

Measured that day: the Manage FBA Inventory CSV's nine summed columns read 7,711 units while
Amazon's own total (minus damaged) read 10,636. The 2,925-unit gap sat on 17 SKUs as inbound
SHIPPED stock, which the API carries and the CSV's `afn-inbound-shipped-quantity` did not.
"""
import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.inventory import fetch, logic, refresh, repository
from app.database import async_session
from app.models import FbaInventoryRefresh, FbaInventoryRow

pytestmark = pytest.mark.regression
ROOT = Path(__file__).resolve().parent.parent


def summary(sku, asin="B0TEST00001", *, f=0, w=0, s=0, r=0, res=0, rch=0, unf=0, fut=(0, 0)):
    return {"sellerSku": sku, "asin": asin, "fnSku": "X0", "lastUpdatedTime": "2026-10-13T02:00:00Z",
            "inventoryDetails": {
                "fulfillableQuantity": f, "inboundWorkingQuantity": w, "inboundShippedQuantity": s,
                "inboundReceivingQuantity": r,
                "reservedQuantity": {"totalReservedQuantity": res},
                "researchingQuantity": {"totalResearchingQuantity": rch},
                "unfulfillableQuantity": {"totalUnfulfillableQuantity": unf},
                "futureSupplyQuantity": {"reservedFutureSupplyQuantity": fut[0],
                                         "futureSupplyBuyableQuantity": fut[1]}}}


# ── the rule ────────────────────────────────────────────────────────────────────────────────────

def test_every_held_state_counts_and_damaged_does_not():
    s = summary("1kg cs FBA", f=1, w=2, s=4, r=8, res=16, rch=32, unf=999, fut=(64, 128))
    assert logic.held_units(s["inventoryDetails"]) == 255


def test_the_13_oct_under_count_comes_out_at_amazons_figure():
    """HF_gbrice_1kg_FBA: the CSV's nine columns read 0, Amazon's total 340, all inbound shipped."""
    rows = logic.reduce_summaries([summary("HF_gbrice_1kg_FBA", s=340)])
    assert rows[0]["held"] == 340 and rows[0]["inbound_shipped"] == 340


def test_flex_rows_and_their_999_placeholder_are_not_fba_stock():
    rows = logic.reduce_summaries([summary("0.25 fc ch FBA", f=102), summary("0.25 fc ch flex", f=999),
                                   summary("0.2kg mk fba", f=3)])
    assert [r["seller_sku"] for r in rows] == ["0.25 fc ch FBA", "0.2kg mk fba"]


def test_two_fba_skus_on_one_asin_are_summed():
    rows = logic.reduce_summaries([summary("a FBA", "B0X0000001", f=5), summary("b_FBA", "B0X0000001", s=7)])
    assert logic.units_by_asin(rows) == {"B0X0000001": 12}


def test_missing_nested_blocks_read_as_zero_not_a_crash():
    assert logic.held_units({"fulfillableQuantity": 4}) == 4


def test_dozens_of_skus_all_zero_is_a_failed_read_but_a_few_is_real():
    many = logic.reduce_summaries([summary(f"s{i} FBA", f"B0Z{i:07d}") for i in range(12)])
    few = logic.reduce_summaries([summary(f"s{i} FBA", f"B0Z{i:07d}") for i in range(3)])
    assert logic.looks_empty(many) and not logic.looks_empty(few)
    many[0]["held"] = 1
    assert not logic.looks_empty(many)


# ── fetch, store, refresh ───────────────────────────────────────────────────────────────────────

def pager(pages):
    calls = []

    async def get(path, params):
        calls.append(dict(params))
        i = len(calls) - 1
        body = {"payload": {"inventorySummaries": pages[i]}}
        if i + 1 < len(pages):
            body["pagination"] = {"nextToken": f"t{i + 1}"}
        return body
    return get, calls


async def test_fetch_pages_to_the_end_and_asks_for_details():
    get, calls = pager([[summary("a FBA")], [summary("b FBA")]])
    out = await fetch.fetch_summaries(get=get)
    assert [s["sellerSku"] for s in out] == ["a FBA", "b FBA"]
    assert calls[0]["details"] == "true" and calls[1]["nextToken"] == "t1"


async def test_a_page_cap_that_is_hit_is_an_error_not_a_short_answer():
    async def endless(path, params):
        return {"payload": {"inventorySummaries": [summary("a FBA")]}, "pagination": {"nextToken": "more"}}
    with pytest.raises(fetch.InventoryFetchError):
        await fetch.fetch_summaries(get=endless)


async def test_a_run_stores_fba_rows_and_becomes_current(db_schema):
    get, _ = pager([[summary("1kg cs FBA", "B0DK1LJ3N3", f=10, s=5),
                     summary("1kg cs flex", "B0DK1LJ3N3", f=999)]])
    out = await refresh.run("button", get=get)
    assert out["status"] == "done" and out["units"] == 15 and out["sku_count"] == 1
    async with async_session() as s:
        run = await repository.latest_good(s)
        assert run.id == out["id"]
        assert [r["seller_sku"] for r in await repository.rows_for(s, run.id)] == ["1kg cs FBA"]


async def test_a_failed_or_refused_run_never_becomes_current(db_schema):
    good, _ = pager([[summary("a FBA", f=3)]])
    first = await refresh.run("nightly", get=good)

    async def boom(path, params):
        raise RuntimeError("Amazon 503")
    failed = await refresh.run("nightly", get=boom)
    zeros, _ = pager([[summary(f"s{i} FBA", f"B0Z{i:07d}") for i in range(12)]])
    refused = await refresh.run("nightly", get=zeros)
    assert failed["status"] == "failed" and "503" in failed["error"]
    assert refused["status"] == "refused"
    async with async_session() as s:
        assert (await repository.latest_good(s)).id == first["id"]
        assert (await repository.latest(s)).status == "refused"


async def test_purge_keeps_the_newest_30_runs(db_schema):
    for _ in range(32):
        get, _ = pager([[summary("a FBA", f=1)]])
        await refresh.run("nightly", get=get)
    async with async_session() as s:
        assert (await s.execute(select(func.count()).select_from(FbaInventoryRefresh))).scalar() == 30
        assert (await s.execute(select(func.count()).select_from(FbaInventoryRow))).scalar() == 30


async def test_a_second_refresh_while_one_runs_is_refused():
    refresh.STATE["running"] = True
    try:
        assert (await refresh.run("button")) == {"refused": True}
    finally:
        refresh.STATE["running"] = False


@pytest.mark.parametrize("module", ["logic", "fetch", "repository", "refresh"])
def test_inventory_never_writes_to_amazon(module):
    tree = ast.parse((ROOT / "app" / "inventory" / f"{module}.py").read_text(encoding="utf-8"))
    names = ({n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
             | {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names})
    assert not names & {"_post", "create_inbound_plan", "confirm_placement", "apply_changes",
                        "save_economics_daily", "set_packing_information"}


def test_the_zero_guard_is_the_shipment_tabs_own_threshold():
    from app.routers.shipment import ALL_ZERO_STOCK_THRESHOLD
    assert logic.ALL_ZERO_STOCK_THRESHOLD == ALL_ZERO_STOCK_THRESHOLD


async def test_pages_are_spaced_and_a_quota_refusal_waits_and_retries():
    """13 Oct, first production read: eight pages back to back were refused on page 3 with
    "You exceeded your quota for the requested resource"."""
    waits, state = [], {"calls": 0}

    async def sleep(seconds):
        waits.append(seconds)

    async def get(path, params):
        state["calls"] += 1
        if state["calls"] == 2:
            raise RuntimeError("Amazon said: You exceeded your quota for the requested resource.")
        page = 0 if "nextToken" not in params else 1
        body = {"payload": {"inventorySummaries": [summary(f"p{page} FBA")]}}
        if page == 0:
            body["pagination"] = {"nextToken": "t1"}
        return body
    out = await fetch.fetch_summaries(get=get, sleep=sleep)
    assert [s["sellerSku"] for s in out] == ["p0 FBA", "p1 FBA"]
    assert waits == [fetch.PAGE_INTERVAL, fetch.QUOTA_WAITS[0]]


async def test_any_other_error_is_not_retried():
    async def sleep(seconds):
        raise AssertionError("must not wait on a non-quota error")

    async def get(path, params):
        raise RuntimeError("Amazon 403 Unauthorized")
    with pytest.raises(RuntimeError, match="403"):
        await fetch.fetch_summaries(get=get, sleep=sleep)
