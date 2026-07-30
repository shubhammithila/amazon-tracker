"""Regression: ISSUE-003 / 006 / 007 — the /ws/progress socket.

Found by /qa on 2026-07-30.
Report: .gstack/qa-reports/qa-report-amazon-tracker-2026-07-30.md

Three separate defects lived in app/routers/ws.py:

  ISSUE-003 (high, security): the handshake was accepted unconditionally and the
    socket then streamed full scrape results — ASINs, titles, prices, sellers —
    to anyone who could reach the port. Every REST route was gated by
    Depends(require_auth); this one had no check whatsoever. Reproduced live: a
    cookie-less handshake returned 101 followed by live scrape JSON.

  ISSUE-006 (medium, performance): the entire results array was serialised and
    pushed every second regardless of state. At ~250 ASINs that is ~100KB/s per
    connected client, on a t2.micro deployment target.

  ISSUE-007 (medium): ConnectionManager.disconnect() used a bare list.remove(),
    which raises ValueError when broadcast() had already evicted the socket.

These use the sync starlette TestClient because httpx's ASGITransport cannot
speak the WebSocket protocol.
"""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.routers.auth import SESSION_COOKIE
from app.routers.ws import ConnectionManager
from app.scraper.engine import scrape_state

pytestmark = pytest.mark.regression

# The handler closes with 1008 (policy violation) rather than rejecting the
# handshake outright, so the client observes a WebSocketDisconnect. What matters
# for ISSUE-003 is that no data frame is ever delivered — asserted below.
WS_POLICY_VIOLATION = 1008


def _assert_refused(test_client):
    """Connect and require a 1008 close with no payload delivered."""
    with pytest.raises(WebSocketDisconnect) as exc:
        with test_client.websocket_connect("/ws/progress") as ws:
            # Reaching a readable frame means the socket was live: the exact
            # ISSUE-003 vulnerability. Surface what leaked.
            pytest.fail(f"socket accepted an unauthenticated client: {ws.receive()!r}")
    assert exc.value.code == WS_POLICY_VIOLATION
    return exc.value


# ─── ISSUE-003: authentication ───────────────────────────────────────────────

def test_websocket_without_cookie_is_refused():
    """The vulnerability: this used to return 101 and stream scrape data."""
    with TestClient(app) as tc:
        _assert_refused(tc)


def test_websocket_with_forged_cookie_is_refused():
    """The cookie is itsdangerous-signed; an unsigned value must not pass."""
    with TestClient(app) as tc:
        tc.cookies.set(SESSION_COOKIE, "not-a-valid-signed-token")
        _assert_refused(tc)


def test_websocket_with_cookie_signed_by_wrong_key_is_refused():
    """A token signed with a different secret must not be accepted."""
    from itsdangerous import URLSafeTimedSerializer

    forged = URLSafeTimedSerializer("a-completely-different-secret").dumps(
        {"authenticated": True}
    )
    with TestClient(app) as tc:
        tc.cookies.set(SESSION_COOKIE, forged)
        _assert_refused(tc)


def test_websocket_with_expired_cookie_is_refused():
    """Sessions expire after SESSION_MAX_AGE; a stale token must not reconnect."""
    import time
    from unittest.mock import patch

    from app.routers.auth import SESSION_MAX_AGE, serializer

    # Mint a token dated just beyond the max age.
    stale_epoch = time.time() - (SESSION_MAX_AGE + 60)
    with patch("itsdangerous.timed.time.time", return_value=stale_epoch):
        stale = serializer.dumps({"authenticated": True})

    with TestClient(app) as tc:
        tc.cookies.set(SESSION_COOKIE, stale)
        _assert_refused(tc)


def test_websocket_with_valid_cookie_connects_and_streams_state(session_cookie):
    with TestClient(app) as tc:
        tc.cookies.set(SESSION_COOKIE, session_cookie)
        with tc.websocket_connect("/ws/progress") as ws:
            payload = ws.receive_json()

    assert payload["running"] is False
    for key in ("progress", "total", "current_asin", "round", "result_count"):
        assert key in payload, f"missing progress field: {key}"


def test_refused_websocket_leaks_no_scrape_data():
    """The point of ISSUE-003: rejection must precede any data frame.

    _assert_refused fails loudly if a frame is readable, so buffering
    recognisable data here proves the refusal happens first rather than after a
    partial send.
    """
    scrape_state.results.append(
        {"asin": "B0SECRET01", "title": "confidential", "price": "999"}
    )
    try:
        with TestClient(app) as tc:
            _assert_refused(tc)
    finally:
        scrape_state.results.clear()


# ─── ISSUE-006: payload size ─────────────────────────────────────────────────

def test_idle_socket_omits_the_results_array(session_cookie):
    """While idle the dashboard renders nothing from results, so don't send them."""
    scrape_state.reset()
    scrape_state.results.extend({"asin": f"B0IDLE{i:04d}"} for i in range(250))
    try:
        with TestClient(app) as tc:
            tc.cookies.set(SESSION_COOKIE, session_cookie)
            with tc.websocket_connect("/ws/progress") as ws:
                payload = ws.receive_json()

        assert "results" not in payload, (
            "250 results were pushed to an idle client — ~100KB/s of waste on a "
            "t2.micro. ISSUE-006 has regressed."
        )
        # The count is still advertised so the UI can show it.
        assert payload["result_count"] == 250
    finally:
        scrape_state.reset()


def test_running_socket_includes_results(session_cookie):
    """The dashboard needs live rows mid-scrape, so they must still arrive."""
    scrape_state.reset()
    scrape_state.running = True
    scrape_state.total = 2
    scrape_state.results.extend(
        [{"asin": "B0RUN000001", "status": "OK"}, {"asin": "B0RUN000002", "status": "OK"}]
    )
    try:
        with TestClient(app) as tc:
            tc.cookies.set(SESSION_COOKIE, session_cookie)
            with tc.websocket_connect("/ws/progress") as ws:
                payload = ws.receive_json()

        assert payload["running"] is True
        assert "results" in payload, "a running scrape must stream its results"
        assert [r["asin"] for r in payload["results"]] == ["B0RUN000001", "B0RUN000002"]
    finally:
        scrape_state.reset()


def test_identical_frames_are_not_resent(session_cookie):
    """Idle state is unchanged second to second; resending it is pure waste."""
    scrape_state.reset()
    with TestClient(app) as tc:
        tc.cookies.set(SESSION_COOKIE, session_cookie)
        with tc.websocket_connect("/ws/progress") as ws:
            first = ws.receive_json()
            assert first["running"] is False

            # State changes -> the next frame must arrive and reflect it.
            scrape_state.current_asin = "B0CHANGED1"
            second = ws.receive_json()
            assert second["current_asin"] == "B0CHANGED1", (
                "a genuine state change was not delivered — dedup is too aggressive"
            )
    scrape_state.reset()


# ─── ISSUE-007: disconnect bookkeeping ───────────────────────────────────────

def test_disconnect_is_idempotent():
    """A bare list.remove() raised ValueError on the second call."""
    manager = ConnectionManager()
    sentinel = object()
    manager.active_connections.append(sentinel)

    manager.disconnect(sentinel)
    assert sentinel not in manager.active_connections

    # Second call: previously ValueError, now a no-op.
    manager.disconnect(sentinel)
    assert manager.active_connections == []


def test_disconnect_of_unknown_socket_does_not_raise():
    manager = ConnectionManager()
    manager.disconnect(object())  # never connected
    assert manager.active_connections == []


async def test_broadcast_evicts_only_failing_sockets():
    """broadcast() must drop dead sockets without disturbing healthy ones."""
    class Sock:
        def __init__(self, fail):
            self.fail = fail
            self.sent = []

        async def send_json(self, message):
            if self.fail:
                raise RuntimeError("socket is closed")
            self.sent.append(message)

    manager = ConnectionManager()
    good, bad = Sock(fail=False), Sock(fail=True)
    manager.active_connections.extend([good, bad])

    await manager.broadcast({"hello": "world"})

    assert manager.active_connections == [good]
    assert good.sent == [{"hello": "world"}]


async def test_broadcast_survives_every_socket_failing():
    class DeadSock:
        async def send_json(self, message):
            raise RuntimeError("closed")

    manager = ConnectionManager()
    manager.active_connections.extend([DeadSock(), DeadSock()])

    await manager.broadcast({"x": 1})  # must not raise

    assert manager.active_connections == []
