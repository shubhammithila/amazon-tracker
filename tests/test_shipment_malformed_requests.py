"""Hostile request bodies must produce a 4xx, never a 500.

Found by QA, not by foresight. Every mutating route in this router reads
``body.get(...)`` straight after ``await request.json()``, and JSON has four
top-level shapes that are not objects — ``null``, ``[]``, ``"str"``, ``5``. On all of
them ``.get`` raises AttributeError, which FastAPI turns into
``500 Internal Server Error``. Six endpoints did it. Two more 500'd one level deeper:
``{"entries": "oops"}`` reached ``raw.get("asin")`` on a character, and
``{"categories": "oops"}`` reached ``.items()`` on a string.

**Why a 500 here is worse than untidy.** It is indistinguishable from the server
being down, so the packer's honest reaction is to press Save again. On a flaky
warehouse connection, retrying a route that already half-committed is how duplicate
packing rows appear — and packing rows feed a GST invoice. A 400 says "your request
was wrong", which ends the retry loop.

These are not theoretical inputs. `JSON.stringify(undefined)` is the string
``"undefined"``; a fetch that posts a bare array instead of ``{entries: [...]}`` is
one refactor away; and a half-typed body from a debugging session is exactly the
shape that finds this.

The fix is one helper, ``_json_object``, plus per-field shape checks where a nested
value gets iterated. These tests pin the contract rather than the helper, so the
implementation can change.
"""
import pytest

pytestmark = pytest.mark.regression

MONDAY = "2026-07-30"

#: Valid JSON that is not an object. Every mutating route must reject all of these
#: the same way, because the very next line reads a key off the body.
NON_OBJECT_BODIES = ["null", "[]", '"str"', "5", "true", '[{"asin":"B0AAA00001"}]']


def _routes(plan_id: int):
    """(method, path, client-fixture-name) for every route that parses a JSON body."""
    return [
        ("POST", "/shipment/items", "admin"),
        ("PATCH", f"/shipment/plan/{plan_id}/thresholds", "admin"),
        ("POST", f"/shipment/plan/{plan_id}/items/exclude", "admin"),
        ("PATCH", "/shipment/categories", "admin"),
        ("POST", "/shipment/invoice-payload", "admin"),
        ("POST", "/shipment/attach-invoice", "admin"),
        ("POST", f"/shipment/packing/{MONDAY}", "ops"),
    ]


JSON_HEADERS = {"Content-Type": "application/json"}


# ─── No route may 500 on a body that is not an object ────────────────────────

@pytest.mark.parametrize("body", NON_OBJECT_BODIES)
async def test_no_route_500s_on_a_non_object_body(
    auth_client, ops_client, plan_factory, body
):
    """Parametrised over every route AND every non-object shape, in one test.

    Deliberately one test rather than seven: this is a property of the router, and a
    new endpoint that forgets the guard should fail here without anyone remembering
    to add a case. The loop names the offender in the assertion message.
    """
    plan = await plan_factory()
    clients = {"admin": auth_client, "ops": ops_client}

    offenders = []
    for method, path, who in _routes(plan.id):
        r = await clients[who].request(method, path, content=body, headers=JSON_HEADERS)
        if r.status_code >= 500:
            offenders.append(f"{method} {path} -> {r.status_code}")

    assert not offenders, (
        f"body {body!r} caused a 500 on: {offenders}. A body that is not a JSON "
        "object must be a 400 — .get() on it raises AttributeError, and a 500 reads "
        "as the server being down, so the packer retries and risks a double-save."
    )


@pytest.mark.parametrize("body", NON_OBJECT_BODIES)
async def test_the_rejection_explains_the_expected_shape(auth_client, body):
    """A 400 with no message is only marginally better than a 500.

    Whoever is looking at this is a developer with a broken fetch call, and the fix
    they need is "send an object".
    """
    r = await auth_client.request(
        "POST", "/shipment/items", content=body, headers=JSON_HEADERS
    )
    assert r.status_code == 400, r.text
    assert "object" in r.json()["error"].lower(), r.json()


async def test_a_body_that_is_not_json_at_all_is_rejected(ops_client, plan_factory):
    """Truncated by a dropped connection mid-POST, which a warehouse phone does."""
    await plan_factory()
    r = await ops_client.post(
        f"/shipment/packing/{MONDAY}", content="not json{", headers=JSON_HEADERS
    )
    assert r.status_code == 400, r.text
    assert "json" in r.json()["error"].lower()


# ─── Nested values that get iterated ─────────────────────────────────────────

@pytest.mark.parametrize("entries", ['"oops"', "5", "true", '{"asin":"B0AAA00001"}'])
async def test_entries_must_be_a_list(ops_client, plan_factory, entries):
    """``{"entries": "oops"}`` iterated the STRING and called .get() on 'o'.

    Rejected rather than tolerated: treating a wrong-shaped `entries` as "no entries"
    would answer 200 "saved" to a request that saved nothing, and the packer would
    believe his counts were recorded.
    """
    await plan_factory()
    r = await ops_client.post(
        f"/shipment/packing/{MONDAY}",
        content='{"entries": %s}' % entries,
        headers=JSON_HEADERS,
    )
    assert r.status_code == 400, f"entries={entries} -> {r.status_code}: {r.text[:120]}"
    assert "list" in r.json()["error"].lower()


@pytest.mark.parametrize("junk", ['["oops"]', "[null]", "[5]", '[[]]', '[true]'])
async def test_junk_inside_the_entries_list_is_skipped_not_fatal(
    ops_client, plan_factory, junk
):
    """A LIST of the wrong things is different from a wrong-shaped `entries`.

    The list shape is right, so the request is honoured and the unusable elements are
    skipped. Rejecting the whole save would throw away the packer's valid rows over
    one malformed neighbour — the same reasoning as the stale-ASIN drop.
    """
    await plan_factory()
    r = await ops_client.post(
        f"/shipment/packing/{MONDAY}",
        content='{"entries": %s}' % junk,
        headers=JSON_HEADERS,
    )
    assert r.status_code == 200, f"entries={junk} -> {r.status_code}: {r.text[:120]}"
    assert r.json()["total_units"] == 0


async def test_valid_entries_still_save_alongside_junk(
    ops_client, plan_factory, read_committed
):
    """The point of skipping rather than rejecting: the real counts must land."""
    from app.shipment import repository

    plan = await plan_factory()
    r = await ops_client.post(
        f"/shipment/packing/{MONDAY}",
        content='{"entries": [null, {"asin": "B0AAA00001", "units": 120}, "oops"]}',
        headers=JSON_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["total_units"] == 120, (
        "the valid entry was discarded along with the junk"
    )
    day = await read_committed(repository.get_day, plan.id, MONDAY)
    entries = await read_committed(repository.load_entries, day.id)
    assert [(e.asin, e.units) for e in entries] == [("B0AAA00001", 120)]


@pytest.mark.parametrize("categories", ['"oops"', "[1,2]", "5", "true"])
async def test_categories_must_be_a_mapping(auth_client, plan_factory, categories):
    """``set_categories`` calls ``.items()``, which a list and a string lack."""
    await plan_factory()
    r = await auth_client.request(
        "PATCH",
        "/shipment/categories",
        content='{"categories": %s}' % categories,
        headers=JSON_HEADERS,
    )
    assert r.status_code == 400, f"{categories} -> {r.status_code}: {r.text[:120]}"
    assert "object" in r.json()["error"].lower()


# ─── Numbers that decide whether a day is held ───────────────────────────────

@pytest.mark.parametrize("value", ['"abc"', "[1]", '{"a":1}', '"12abc"'])
async def test_a_threshold_that_cannot_be_parsed_is_refused(
    auth_client, plan_factory, value
):
    """``int("abc")`` in the repository was a 500.

    Refused rather than defaulted, because these two numbers are what
    ``logic.is_held`` compares against: a silently-substituted 0 would mean no day is
    ever held again, and held days are how small shipments get combined. That failure
    would surface weeks later as stock sitting in the warehouse.
    """
    plan = await plan_factory()
    r = await auth_client.request(
        "PATCH",
        f"/shipment/plan/{plan.id}/thresholds",
        content='{"min_cartons": %s}' % value,
        headers=JSON_HEADERS,
    )
    assert r.status_code == 400, f"{value} -> {r.status_code}: {r.text[:120]}"
    assert "whole number" in r.json()["error"]


async def test_a_refused_threshold_does_not_change_the_stored_value(
    auth_client, plan_factory, read_committed
):
    """A 400 is only honest if nothing was written. Asserted at the database."""
    from app.shipment import repository

    plan = await plan_factory(min_cartons=25, min_units=500)
    await auth_client.request(
        "PATCH",
        f"/shipment/plan/{plan.id}/thresholds",
        content='{"min_cartons": "abc", "min_units": 900}',
        headers=JSON_HEADERS,
    )
    stored = await read_committed(repository.get_plan, plan.id)
    assert (stored.min_cartons, stored.min_units) == (25, 500), (
        "a rejected request still wrote the field it could parse, so the thresholds "
        "are now half-updated"
    )


async def test_valid_thresholds_still_save(auth_client, plan_factory, read_committed):
    """Guarding the guard: the validation must not reject good input."""
    from app.shipment import repository

    plan = await plan_factory()
    r = await auth_client.patch(
        f"/shipment/plan/{plan.id}/thresholds",
        json={"min_cartons": 30, "min_units": 700},
    )
    assert r.status_code == 200, r.text
    stored = await read_committed(repository.get_plan, plan.id)
    assert (stored.min_cartons, stored.min_units) == (30, 700)


async def test_a_string_number_is_still_accepted(auth_client, plan_factory):
    """An HTML number input posts "30", not 30. Rejecting that would break the form
    the thresholds are actually edited from."""
    plan = await plan_factory()
    r = await auth_client.request(
        "PATCH",
        f"/shipment/plan/{plan.id}/thresholds",
        content='{"min_cartons": "30"}',
        headers=JSON_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["min_cartons"] == 30
