"""Portfolio sub-tabs answer from caches, and the caches drop the moment their data moves (24 Oct 2026).

Reported as *"this transition lag and the speed at which the page loads is also an issue in the
portfolio tab… everything should be seamless and as fast as possible"*. Measured on production: the
Profit data took 1.0 / 1.6 / 2.2 s for 7 / 30 / 60 days and Repeat customers 2.3-4.5 s on every
click, while the arithmetic itself was 0.04 s; reading the MRP sheet from Google was 1.1 s of it.
"""
from pathlib import Path

import pytest

from app.repeat import service as repeat_service
from app.routers import portfolio as pf_router
from app.shipment import catalogue

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def counted(monkeypatch):
    calls = []
    real = pf_router._build_dashboard

    async def counting(db, window=None, *, include_inactive=False):
        calls.append((window, include_inactive))
        return await real(db, window, include_inactive=include_inactive)

    monkeypatch.setattr(pf_router, "_build_dashboard", counting)
    return calls


W = ("2026-10-01", "2026-10-07")


async def test_the_same_window_is_built_once(db, counted):
    await pf_router._dashboard(db, W)
    await pf_router._dashboard(db, W)
    await pf_router._dashboard(db, W, include_inactive=True)
    assert counted == [(W, False), (W, True)]


async def test_a_decision_a_setting_or_a_category_drops_the_cache(db, counted):
    from app.models import PortfolioSettings, ProductCategory, ProductDecision
    await pf_router._dashboard(db, W)
    db.add(ProductDecision(parent_asin="B0TEST0001", decision="KILL"))
    await db.commit()
    await pf_router._dashboard(db, W)
    db.add(PortfolioSettings(name="verdict_thresholds", value_json="{}"))
    await db.commit()
    await pf_router._dashboard(db, W)
    db.add(ProductCategory(product_key="sattu", priority=1))
    await db.commit()
    await pf_router._dashboard(db, W)
    assert len(counted) == 4


async def test_an_edited_sheet_drops_the_cache(db, counted, monkeypatch):
    sheet = {"B0TEST0001": {"asin": "B0TEST0001", "name": "x", "weight": 1, "brand": "MF", "active": True}}

    async def load(**_):
        return {a: dict(r) for a, r in sheet.items()}, None, "sheet"

    monkeypatch.setattr(catalogue, "load_catalogue", load)
    await pf_router._dashboard(db, W)
    await pf_router._dashboard(db, W)
    sheet["B0TEST0001"]["active"] = False
    await pf_router._dashboard(db, W)
    assert len(counted) == 2


async def test_a_caller_changing_its_copy_cannot_change_the_cache(db):
    first = await pf_router._dashboard(db, W)
    first["parents"].append({"poisoned": True})
    first["window"] = "changed"
    again = await pf_router._dashboard(db, W)
    assert {"poisoned": True} not in again["parents"] and again["window"] != "changed"


async def test_custom_windows_cannot_grow_the_cache_without_bound(db):
    for day in range(1, 20):
        await pf_router._dashboard(db, (f"2026-09-{day:02d}", f"2026-09-{day:02d}"))
    assert len([k for k in pf_router._DASH_CACHE if k != "version"]) <= pf_router.DASHBOARD_CACHE_ENTRIES


async def test_repeat_customers_is_built_once_per_brand_and_dropped_by_a_refresh(db, monkeypatch):
    from datetime import date, datetime

    from app.models import RepeatRefresh
    calls = []
    real = repeat_service._build_payload

    async def counting(db_, brand, today):
        calls.append(brand)
        return await real(db_, brand, today)

    monkeypatch.setattr(repeat_service, "_build_payload", counting)
    today = date(2026, 10, 24)
    await repeat_service.build_payload(db, None, today)
    await repeat_service.build_payload(db, None, today)
    assert len(calls) == 1
    db.add(RepeatRefresh(started_at=datetime(2026, 10, 24), status="done",
                         window_start="2026-10-17", window_end="2026-10-23"))
    await db.commit()
    await repeat_service.build_payload(db, None, today)
    assert len(calls) == 2


@pytest.mark.real_catalogue
async def test_the_sheet_is_read_once_and_shared(monkeypatch):
    fetched = []

    async def fetch():
        fetched.append(1)
        return "ASIN,Name,Net Weight,Brand Name,Active\nB0TEST0001,Chana Sattu,1,MF,Y\n"

    monkeypatch.setattr(catalogue, "_fetch_sheet_text", fetch)
    monkeypatch.setattr(catalogue, "_write_cache", lambda *a, **k: None)
    catalogue.clear_memo()
    first = (await catalogue.load_catalogue())[0]
    second = (await catalogue.load_catalogue())[0]
    assert len(fetched) == 1 and first == second
    second["B0TEST0001"]["active"] = False          # a caller's copy
    assert (await catalogue.load_catalogue())[0]["B0TEST0001"]["active"] is True
    await catalogue.load_catalogue(fresh=True)
    assert len(fetched) == 2


def test_the_shipment_plan_reads_the_sheet_fresh():
    source = (ROOT / "app" / "routers" / "shipment.py").read_text(encoding="utf-8")
    assert "catalogue.clear_memo()\n    sheet_products, sheet_warning, sheet_source = await catalogue.load_catalogue()" in source


async def test_the_warm_up_builds_every_sub_tab_and_never_raises(db_schema, monkeypatch):
    from app.database import async_session
    from app.portfolio import warm
    done = await warm.warm(async_session)
    assert {"profit 7d", "profit 30d", "projections", "repeat", "customer value"} <= set(done)

    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(pf_router, "_dashboard", boom)
    await warm.warm(async_session)                     # logged, not raised


def test_projections_is_the_second_sub_tab():
    tabs = (ROOT / "templates" / "_portfolio_tabs.html").read_text(encoding="utf-8")
    order = [tabs.index(f'href="{h}"') for h in (
        "/portfolio-page", "/portfolio-page/projections", "/portfolio-page/repeat",
        "/portfolio-page/value", "/portfolio-page/analytics")]
    assert order == sorted(order)
