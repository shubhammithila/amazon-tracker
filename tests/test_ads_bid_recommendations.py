"""Amazon's suggested bid, and the three states it has.

**The endpoint had to be found by probing.** Four candidates were called against the live account:
the two documented v2 paths 404 ("Method Not Found"), `/sp/keywords/bid/recommendations` returns 403
with a spurious SigV4 error, and only `/sp/targets/bid/recommendations` answers 200 with real bids.

**Sponsored Brands has none.** Three SB candidates were probed and all three 404, so ~296 rows in a
typical preview have no suggestion — which must be SAID rather than left blank.

**Nothing here touches the network.** `get_settings().ads_configured` is TRUE in this repo (the local
`.env` holds real `AMAZON_*` credentials and `conftest.py` does not clear them), so a test that let a
real client through would call LWA and Amazon for every case. Every test passes a fake client and
patches `_access_token`.
"""
import pytest

from app.ads import spapi_ads

pytestmark = pytest.mark.regression


async def _fake_token(_client):
    """Stand in for the LWA round trip. Without this the suite authenticates against Amazon."""
    return "token"


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeClient:
    """Records requests and replays canned responses, keyed by the ad group in the body."""

    def __init__(self, by_ad_group):
        self.by_ad_group = by_ad_group
        self.calls = []

    async def request(self, method, url, json=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": json})
        ad_group = (json or {}).get("adGroupId")
        return FakeResponse(200, self.by_ad_group.get(ad_group, {"bidRecommendations": []}))


#: The real response shape, captured from the live account. Note the THREE bid values.
LIVE_SHAPE = {
    "bidRecommendations": [{
        "theme": "CONVERSION_OPPORTUNITIES",
        "bidRecommendationsForTargetingExpressions": [{
            "targetingExpression": {"type": "KEYWORD_EXACT_MATCH", "value": "usna chawal bihar"},
            "bidValues": [{"suggestedBid": 140.33}, {"suggestedBid": 182.83},
                          {"suggestedBid": 225.33}],
        }],
    }]
}


def _row(entity_id, text, *, product="sp", match_type="EXACT", ad_group="A1"):
    return {
        "entity_id": entity_id, "text": text, "ad_product": product,
        "match_type": match_type, "ad_group_id": ad_group, "campaign_id": "C1",
    }


async def test_the_middle_of_three_bids_is_the_suggestion(monkeypatch):
    """**Amazon returns THREE bids, not one:** [140.33, 182.83, 225.33].

    The middle value is the suggestion and the outer two are its range. Recording one number as "the
    suggested bid" without saying which would be a silent choice between three, so the range travels
    alongside it.
    """
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = FakeClient({"A1": LIVE_SHAPE})

    result = await spapi_ads.fetch_bid_recommendations(
        client, [_row("KW1", "usna chawal bihar")]
    )

    assert result["KW1"]["suggested_bid"] == pytest.approx(182.83)
    assert result["KW1"]["low"] == pytest.approx(140.33)
    assert result["KW1"]["high"] == pytest.approx(225.33)
    assert result["KW1"]["unavailable"] == ""


async def test_sponsored_brands_rows_are_reported_as_unavailable_not_blank(monkeypatch):
    """**SB has no bid-recommendation endpoint — three probed, all 404.**

    A blank cell in a bid column reads as "no suggestion, so bid low". The honest answer is that
    Amazon does not offer one here, which is the same three-state discipline the Portfolio tab's ACOS
    column follows. Critically it must never borrow an SP figure for an SB row.
    """
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = FakeClient({"A1": LIVE_SHAPE})

    result = await spapi_ads.fetch_bid_recommendations(
        client, [_row("SB1", "roast chana", product="sb")]
    )

    assert result["SB1"]["suggested_bid"] is None
    assert "Sponsored Brands" in result["SB1"]["unavailable"]
    assert not client.calls, "an SB row was sent to the Sponsored Products endpoint"


async def test_one_call_per_ad_group_not_per_row(monkeypatch):
    """The endpoint is batched per ad group, which is what makes a 1,005-row preview affordable.

    One call per row would be 1,005 calls; one per distinct ad group is a few dozen.
    """
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = FakeClient({"A1": LIVE_SHAPE, "A2": LIVE_SHAPE})

    rows = [_row(f"KW{i}", f"kw {i}", ad_group="A1") for i in range(20)]
    rows += [_row(f"KX{i}", f"kx {i}", ad_group="A2") for i in range(15)]
    await spapi_ads.fetch_bid_recommendations(client, rows)

    assert len(client.calls) == 2, f"expected one call per ad group, made {len(client.calls)}"


async def test_a_failed_recommendation_call_does_not_fail_the_preview(monkeypatch):
    """A suggestion is CONTEXT. Losing it must not lose the 1,005 bid changes beside it.

    The preview is the safety mechanism for the only feature that spends money; degrading it because
    a nice-to-have column errored would be the wrong trade.
    """
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)

    class Boom(FakeClient):
        async def request(self, *args, **kwargs):
            raise RuntimeError("Amazon said no")

    result = await spapi_ads.fetch_bid_recommendations(Boom({}), [_row("KW1", "chana")])
    assert result["KW1"]["suggested_bid"] is None
    assert result["KW1"]["unavailable"], "a failure must carry a reason, not an empty string"


async def test_a_missing_token_is_reported_per_row_rather_than_raising(monkeypatch):
    """The credentials round trip is the other way this can fail, and it fails BEFORE any request.

    Same rule: every row gets a reason and the preview survives.
    """
    async def boom(_client):
        raise RuntimeError("LWA refused")

    monkeypatch.setattr(spapi_ads, "_access_token", boom)
    result = await spapi_ads.fetch_bid_recommendations(FakeClient({}), [_row("KW1", "chana")])
    assert result["KW1"]["suggested_bid"] is None
    assert "LWA refused" in result["KW1"]["unavailable"]


async def test_an_unroutable_match_type_gets_a_reason_not_a_wrong_expression(monkeypatch):
    """Amazon needs a `targetingExpression` type, and not every report row maps to one.

    Guessing would put a suggestion for the wrong target beside a live bid. Excluded and named, the
    same rule `logic.writer_for` follows for a match type it does not recognise.
    """
    monkeypatch.setattr(spapi_ads, "_access_token", _fake_token)
    client = FakeClient({"A1": LIVE_SHAPE})

    result = await spapi_ads.fetch_bid_recommendations(
        client, [_row("X1", "", match_type="SOMETHING_NEW")]
    )
    assert result["X1"]["suggested_bid"] is None
    assert result["X1"]["unavailable"]
    assert not client.calls, "an unroutable row was still sent to Amazon"


# ─── The route ────────────────────────────────────────────────────────────────


async def test_the_route_returns_suggestions_without_touching_the_database(auth_client, monkeypatch):
    """A SEPARATE endpoint from `/preview`, so the preview never waits on Amazon.

    A 1,005-row plan spans a few dozen ad groups; fetching inside the preview would make the one
    safety mechanism for a money-spending feature slower and able to fail for a nice-to-have column.
    """
    async def fake(_client, rows):
        return {str(r["entity_id"]): {"suggested_bid": 12.5, "low": 10.0, "high": 15.0,
                                      "unavailable": ""} for r in rows}

    monkeypatch.setattr(spapi_ads, "fetch_bid_recommendations", fake)

    response = await auth_client.post("/ads/suggested-bids", json={
        "rows": [{"entity_id": "111", "text": "makhana", "ad_product": "sp",
                  "match_type": "EXACT", "ad_group_id": "g1", "campaign_id": "c1"}],
    })
    body = response.json()
    assert response.status_code == 200, body
    assert body["suggestions"]["111"]["suggested_bid"] == 12.5


async def test_the_route_never_500s_when_amazon_refuses(auth_client, monkeypatch):
    """**The failure mode that matters: this must degrade, never break the screen.**

    The preview beside it holds real bid changes the owner is about to approve.
    """
    async def boom(_client, rows):
        raise RuntimeError("Amazon said no")

    monkeypatch.setattr(spapi_ads, "fetch_bid_recommendations", boom)

    response = await auth_client.post("/ads/suggested-bids", json={
        "rows": [{"entity_id": "111", "text": "kw", "ad_product": "sp",
                  "match_type": "EXACT", "ad_group_id": "g1", "campaign_id": "c1"}],
    })
    assert response.status_code == 200
    body = response.json()
    assert body["suggestions"] == {}
    assert "Amazon could not be asked" in body["error"]


async def test_no_rows_asks_amazon_nothing(auth_client, monkeypatch):
    """An empty preview must not spend a call. Guarded because the screen posts on every render."""
    called = []

    async def fake(_client, rows):
        called.append(rows)
        return {}

    monkeypatch.setattr(spapi_ads, "fetch_bid_recommendations", fake)
    response = await auth_client.post("/ads/suggested-bids", json={"rows": []})
    assert response.status_code == 200
    assert response.json()["suggestions"] == {}
    assert not called, "Amazon was asked about an empty row list"


def test_the_preview_route_does_not_fetch_suggestions_itself():
    """Pinned as source, because the cost is invisible in a passing test.

    If `/preview` grew a `fetch_bid_recommendations` call, every preview would block on a few dozen
    Amazon calls and every preview TEST would authenticate against LWA for real — `ads_configured` is
    True in this repo.
    """
    from pathlib import Path

    source = (Path(__file__).parent.parent / "app" / "routers" / "ads.py").read_text(
        encoding="utf-8")
    preview = source[source.index('@router.post("/preview")'):]
    preview = preview[:preview.index('@router.post("/suggested-bids")')]
    assert "fetch_bid_recommendations" not in preview, (
        "the preview fetches suggested bids inline, so it now blocks on Amazon"
    )
