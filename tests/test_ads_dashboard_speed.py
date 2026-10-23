"""The 7d / 14d / 30d buttons must be quick (23 Oct 2026).

Reported as *"when I click from 7d to 30d or 14d the transition is very slow"*. Measured on
production: GET /ads took 2.0 / 2.6 / 3.8 s, of which `sum_daily` was 0.5 / 1.1 / 2.4 s — every keyword
row of the window fetched only to make 29 campaign totals — and ~1 s more was ten separate scans
asking which days are held. The totals are now summed in SQL and the held days read once.
"""
from app.ads import repository


def _rows(day, campaign, n, product="sp"):
    return [{"date": day, "keywordId": f"{campaign}-{product}-{i}", "matchType": "EXACT",
             "keyword": f"kw{i}", "campaignId": campaign, "adGroupId": "g1",
             "cost": 10.0 + i, "sales7d": 5.0 * i, "purchases7d": i % 2, "clicks": i,
             "impressions": 10 * i, "keywordBid": 5.0, "sales": 5.0 * i, "purchases": i % 2}
            for i in range(n)]


async def _seed(db):
    for day in ("2026-10-20", "2026-10-21", "2026-10-22"):
        await repository.save_daily(db, _rows(day, "c1", 4) + _rows(day, "c2", 3), ad_product="sp")
        await repository.save_daily(db, _rows(day, "c3", 2, "sb"), ad_product="sb")
    await repository.save_entities(db, [
        {"entity_type": "campaign", "entity_id": c, "ad_product": p, "name": f"camp {c}", "state": "ENABLED"}
        for c, p in (("c1", "sp"), ("c2", "sp"), ("c3", "sb"))])


async def test_campaign_totals_equal_the_old_rollup_of_every_keyword_row(db):
    await _seed(db)
    rows = await repository.sum_daily(db, "2026-10-21", "2026-10-22")
    expected: dict = {}
    for r in rows:
        b = expected.setdefault(r["campaign_id"], {"spend": 0.0, "sales": 0.0, "clicks": 0,
                                                    "impressions": 0, "orders": 0, "rows": 0})
        for k in ("spend", "sales", "clicks", "impressions", "orders"):
            b[k] += r[k]
        b["rows"] += 1
    got = await repository.campaign_totals(db, "2026-10-21", "2026-10-22")
    assert set(got) == set(expected) == {"c1", "c2", "c3"}
    for cid, b in expected.items():
        for k, v in b.items():
            assert round(got[cid][k], 2) == round(v, 2), (cid, k)
    only_sp = await repository.campaign_totals(db, "2026-10-21", "2026-10-22", ad_product="sp")
    assert set(only_sp) == {"c1", "c2"}


async def test_the_held_days_are_read_once_and_give_the_same_answer(db):
    await _seed(db)
    held = await repository.held_days_by_product(db)
    assert held == {"sp": {"2026-10-20", "2026-10-21", "2026-10-22"},
                    "sb": {"2026-10-20", "2026-10-21", "2026-10-22"}}
    for start, end in (("2026-10-20", "2026-10-22"), ("2026-10-19", "2026-10-22")):
        assert (await repository.range_completeness(db, start, end, held=held)
                == await repository.range_completeness(db, start, end))


async def test_the_campaign_list_does_not_read_every_keyword_row(auth_client, db, monkeypatch):
    await _seed(db)
    calls = []
    original = repository.sum_daily

    async def counting(*a, **kw):
        calls.append(kw.get("campaign_ids"))
        return await original(*a, **kw)

    monkeypatch.setattr(repository, "sum_daily", counting)
    body = (await auth_client.get("/ads?start=2026-10-20&end=2026-10-22")).json()
    assert calls == [], "the campaign list summed every keyword row again"
    assert body["totals"]["spend"] == round(3 * (sum(10.0 + i for i in range(4))
                                                 + sum(10.0 + i for i in range(3))
                                                 + sum(10.0 + i for i in range(2))), 2)
    await auth_client.get("/ads?start=2026-10-20&end=2026-10-22&campaign_id=c1")
    assert calls == [["c1"]]
