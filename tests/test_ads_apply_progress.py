"""Ads → a progress bar while Apply runs (26 Oct 2026).

Reported as *"when a rule is applied, and its a lot of keywords to change nothing happens on the screen
and after 30 sec it says xxx applied. lets make a % or a processing bar"*. The Apply request carries an
id the screen made up; the server counts rows through its two slow Amazon steps, and the screen polls
`GET /ads/apply/progress/{id}`.
"""
from pathlib import Path

import pytest

from app.ads import logic, progress, spapi_ads

PAGE = (Path(__file__).resolve().parent.parent / "templates" / "ads.html").read_text(encoding="utf-8")


# ─── The counter ─────────────────────────────────────────────────────────────


def test_the_percentage_walks_through_the_phases_and_never_goes_back():
    progress.track("t-unit-0001")
    progress.phase("check", 1000)
    progress.tick(500)
    assert progress.get("t-unit-0001")["percent"] == 22
    progress.tick(500)
    assert progress.get("t-unit-0001")["percent"] == 45
    progress.phase("send", 2000)
    progress.tick(1000)
    state = progress.get("t-unit-0001")
    assert (state["phase"], state["done"], state["total"], state["percent"]) == ("send", 1000, 2000, 71)
    assert state["label"] == "Sending changes to Amazon"
    progress.phase("check", 10)            # a phase can never pull the bar backwards
    assert progress.get("t-unit-0001")["percent"] == 71
    progress.finish()
    assert progress.get("t-unit-0001")["finished"] and progress.get("t-unit-0001")["percent"] == 100


def test_ticks_cannot_overshoot_a_phase():
    progress.track("t-unit-0002")
    progress.phase("send", 10)
    progress.tick(500)
    assert progress.get("t-unit-0002")["done"] == 10
    assert progress.get("t-unit-0002")["percent"] == 97


def test_an_unusable_id_tracks_nothing_and_never_raises():
    for bad in (None, "", "x", "../etc", "a" * 80, "<script>"):
        progress.track(bad)
        progress.phase("send", 5)
        progress.tick(5)
        progress.finish()
        assert progress.get(bad) is None


# ─── The Amazon calls tick ───────────────────────────────────────────────────


async def test_the_real_writer_ticks_once_per_batch_of_500(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ads_endpoint", "https://ads.test", raising=False)

    async def token(client):
        return "t"
    monkeypatch.setattr(spapi_ads, "_access_token", token)
    seen = []

    class Client:
        async def put(self, url, json=None, headers=None):
            seen.append(progress.get("t-writer-01")["done"])

            class R:
                status_code = 207
                text = ""

                def json(self):
                    return {"keywords": {"success": [], "error": []}}
            return R()

    async def no_sleep(_):
        return None

    rows = [{"entity_id": str(i), "new_bid": 1.5, "writer": logic.WRITER_KEYWORD} for i in range(1200)]
    progress.track("t-writer-01")
    progress.phase("send", len(rows))
    await spapi_ads.apply_changes(Client(), rows, writer=logic.WRITER_KEYWORD, sleep=no_sleep)
    assert seen == [0, 500, 1000], "each batch is counted as it is answered"
    assert progress.get("t-writer-01")["done"] == 1200


# ─── The route ───────────────────────────────────────────────────────────────


@pytest.fixture
def ads_endpoint(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ads_endpoint", "https://ads.test", raising=False)


async def test_apply_reports_its_phases_and_finishes(monkeypatch, auth_client, ads_endpoint):
    snapshots = {}

    async def live(_client, changes):
        progress.tick(len(changes))
        snapshots["check"] = dict(progress.get("t-route-0001"))
        return {str(c["entity_id"]): {"bid": 12.0, "state": "ENABLED"} for c in changes}

    async def send(_client, rows, *, writer):
        snapshots["send"] = dict(progress.get("t-route-0001"))
        progress.tick(len(rows))
        return [{"entity_id": r["entity_id"], "ok": True} for r in rows]

    monkeypatch.setattr(spapi_ads, "fetch_current_bids", live)
    monkeypatch.setattr(spapi_ads, "apply_changes", send)
    change = {"entity_id": "111", "writer": logic.WRITER_KEYWORD, "ad_product": "sp", "ad_group_id": "g1",
              "campaign_id": "c1", "text": "kw", "old_bid": 12.0, "new_state": "PAUSED"}
    response = await auth_client.post("/ads/apply", json={
        "changes": [change], "rule": "pause", "start": "2026-10-01", "end": "2026-10-07",
        "progress_id": "t-route-0001"})
    assert response.status_code == 200, response.text
    assert (snapshots["check"]["phase"], snapshots["check"]["total"]) == ("check", 1)
    assert (snapshots["send"]["phase"], snapshots["send"]["total"]) == ("send", 1)
    after = (await auth_client.get("/ads/apply/progress/t-route-0001")).json()
    assert after["finished"] and after["percent"] == 100
    assert (await auth_client.get("/ads/apply/progress/never-started")).status_code == 404


async def test_a_failed_apply_still_finishes_its_bar(monkeypatch, auth_client, ads_endpoint):
    async def boom(_client, changes):
        raise spapi_ads.AdsError("Amazon is down")
    monkeypatch.setattr(spapi_ads, "fetch_current_bids", boom)
    change = {"entity_id": "111", "writer": logic.WRITER_KEYWORD, "ad_product": "sp", "ad_group_id": "g1",
              "campaign_id": "c1", "text": "kw", "old_bid": 12.0, "new_state": "PAUSED"}
    response = await auth_client.post("/ads/apply", json={
        "changes": [change], "rule": "pause", "progress_id": "t-route-0002"})
    assert response.status_code == 502
    assert (await auth_client.get("/ads/apply/progress/t-route-0002")).json()["finished"]


# ─── The screen ──────────────────────────────────────────────────────────────


def test_both_apply_paths_send_an_id_poll_and_always_stop_the_bar():
    for start in ("async function applyPlan(", "async function applyNegatives("):
        body = PAGE[PAGE.index(start):][:2500]
        assert "const bar = startApplyProgress();" in body, start
        assert "progress_id: bar.id" in body, start
        assert "} finally {\n    bar.stop();" in body, start
    helper = PAGE[PAGE.index("function startApplyProgress("):][:2000]
    assert "/ads/apply/progress/${id}" in helper and "setTimeout(poll, 700)" in helper
    assert "randomUUID" not in helper, "production is plain http, where crypto.randomUUID is undefined"
