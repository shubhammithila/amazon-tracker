"""The hold lifecycle over HTTP: open → held → combined → released → verified.

Requirement 9 in the owner's words:

    "sometimes what happens is when the packing is less say 20 cartons and 400
    units, we do not create a shipment, instead we combine it with next day
    packing and then create a shipment."

``tests/test_shipment_logic.py`` pins the arithmetic and
``tests/test_shipment_plan_db.py`` pins the single-day hold. What is left, and
what this file covers, is the sentence's *second half* travelling all the way
through the API — because that is where it was actually missing.

``is_held`` judges one day in isolation, which is correct: a small Tuesday must
be held too, not shipped short. But per-day judgement alone gives the combining
half of the instruction no trigger. Monday is held, Tuesday is held, together
they are a shipment, and nothing anywhere said so. Held stock then accumulated
until somebody happened to add the columns up. ``logic.carry_over`` answers the
question ``is_held`` structurally cannot ("is the *backlog* shippable now?") and
the tests below check the API actually reports it.

Two properties matter more than the rest and are easy to lose in a refactor:

* **The prompt never acts.** ``carry_over.clears`` going true must not change a
  single status. A big backlog may still be worth holding for a fuller truck, so
  releasing stays the owner's decision — the app's job is only to make the
  situation impossible to miss.
* **Held stock cannot vanish.** ``/active`` shows only the active plan, so a day
  held on Saturday would silently drop off every screen when Monday's plan is
  generated. The boxes are real. Generating warns.

Database reads go through ``read_committed`` for the reason given at the top of
tests/test_shipment_plan_db.py: the ``db`` fixture holds its own transaction, so
reading through it after an HTTP request can return pre-request values and make
a broken write look successful.
"""
import pytest

from app.shipment import logic, repository

pytestmark = pytest.mark.regression

#: One SKU from the plan_factory default items, planned 500 units.
ASIN = "B0AAA00001"

MONDAY = "2026-07-30"
TUESDAY = "2026-07-31"
WEDNESDAY = "2026-08-01"


async def _pack(client, pack_date, units, cartons, asin=ASIN):
    """Record and submit a day. Returns the submit response body.

    ``cartons`` is posted at the top level of the body, not inside the entry: it is
    the whole day's box count. A carton holds whatever was being packed when it was
    filled, so it cannot be attributed to one ASIN.
    """
    r = await client.post(
        f"/shipment/packing/{pack_date}",
        json={"entries": [{"asin": asin, "units": units}], "cartons": cartons},
    )
    assert r.status_code == 200, r.text
    r = await client.post(f"/shipment/packing/{pack_date}/submit")
    assert r.status_code == 200, r.text
    return r.json()


async def _carry_over(client):
    r = await client.get("/shipment/active")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "carry_over" in body, (
        "/shipment/active no longer reports carry_over — the owner would have to "
        "add the held day columns up by hand every morning"
    )
    return body["carry_over"]


# ─── The combining half of requirement 9 ─────────────────────────────────────

async def test_two_held_days_are_reported_as_a_shipment_ready_to_go(
    auth_client, ops_client, plan_factory
):
    """The exact scenario, end to end. Neither day ships; together they do.

    This is the assertion that was missing entirely before: both days correctly
    held, and the API silent about the fact that 30 cartons / 600 units of packed
    stock is now sitting in the warehouse ready to ship.
    """
    await plan_factory()

    assert (await _pack(ops_client, MONDAY, 400, 20))["status"] == logic.STATUS_HELD
    after_monday = await _carry_over(auth_client)
    assert after_monday["clears"] is False, "one small day is not a shipment yet"
    assert after_monday["shortfall_cartons"] == 5, "does not say how far short it is"

    assert (await _pack(ops_client, TUESDAY, 200, 10))["status"] == logic.STATUS_HELD

    combined = await _carry_over(auth_client)
    assert combined["clears"] is True, (
        "two held days totalling 30 cartons / 600 units are a shipment, but the "
        "API does not say so — the packed stock would sit unnoticed"
    )
    assert (combined["cartons"], combined["units"]) == (30, 600)
    assert combined["days"] == 2
    assert combined["dates"] == [MONDAY, TUESDAY], "the owner is not told which dates"
    assert combined["shortfall_cartons"] == 0 and combined["shortfall_units"] == 0


async def test_a_backlog_still_too_small_is_not_reported_as_ready(
    auth_client, ops_client, plan_factory
):
    """No false prompt. Two tiny days are still one tiny shipment."""
    await plan_factory()
    await _pack(ops_client, MONDAY, 400, 20)
    await _pack(ops_client, TUESDAY, 40, 2)

    carry = await _carry_over(auth_client)
    assert carry["clears"] is False
    assert (carry["cartons"], carry["units"]) == (22, 440)
    assert carry["shortfall_cartons"] == 3, "should say 3 more cartons would do it"
    assert carry["shortfall_units"] == 60


async def test_the_carry_over_prompt_does_not_release_anything_by_itself(
    auth_client, ops_client, plan_factory, read_committed
):
    """The threshold suggests; the owner decides. Even when it clears.

    A backlog big enough to ship may still be worth holding for a fuller truck,
    so `clears` must stay advice. If a future change made this auto-release, a
    day would ship the instant it crossed the line and the owner would lose the
    decision he explicitly asked to keep.
    """
    plan = await plan_factory()
    plan_id = plan.id
    await _pack(ops_client, MONDAY, 400, 20)
    await _pack(ops_client, TUESDAY, 200, 10)

    assert (await _carry_over(auth_client))["clears"] is True

    for pack_date in (MONDAY, TUESDAY):
        day = await read_committed(repository.get_day, plan_id, pack_date)
        assert day.status == logic.STATUS_HELD, (
            f"{pack_date} was released automatically when the backlog cleared — "
            "releasing is the owner's decision, not the threshold's"
        )

    # And the units are still correctly excluded from shippable until he acts.
    r = await auth_client.get("/shipment/active")
    item = next(i for i in r.json()["items"] if i["asin"] == ASIN)
    assert item["shippable"] == 0, "a cleared backlog shipped itself"
    assert item["packed"] == 600, "held units stopped counting as packed"


async def test_a_third_day_that_clears_alone_does_not_hide_the_backlog(
    auth_client, ops_client, plan_factory
):
    """A good day does not absolve the parked ones.

    Wednesday ships on its own merits. Monday and Tuesday are still in the
    warehouse and must still be reported, because a plausible-looking bug here is
    to compute the backlog from all days rather than only the held ones — which
    would make the hold disappear the moment one normal day was packed.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, 400, 20)
    await _pack(ops_client, TUESDAY, 40, 2)
    assert (await _pack(ops_client, WEDNESDAY, 900, 40))["status"] == logic.STATUS_SUBMITTED

    carry = await _carry_over(auth_client)
    assert carry["days"] == 2, "a submitted day was counted as carry-over"
    assert (carry["cartons"], carry["units"]) == (22, 440)
    assert carry["dates"] == [MONDAY, TUESDAY]
    assert carry["clears"] is False, (
        "Wednesday's units leaked into the backlog total — the held days would "
        "look shippable when nothing about them changed"
    )


async def test_releasing_the_backlog_empties_it(
    auth_client, ops_client, plan_factory
):
    """After the owner acts, the prompt goes away and the units become shippable."""
    await plan_factory()
    await _pack(ops_client, MONDAY, 400, 20)
    await _pack(ops_client, TUESDAY, 200, 10)
    assert (await _carry_over(auth_client))["clears"] is True

    for pack_date in (MONDAY, TUESDAY):
        r = await auth_client.post(f"/shipment/packing/{pack_date}/release")
        assert r.status_code == 200, r.text

    carry = await _carry_over(auth_client)
    assert carry["days"] == 0, "released days are still counted as parked"
    assert carry["clears"] is False, "an empty backlog must not prompt again"
    assert carry["units"] == 0 and carry["cartons"] == 0

    r = await auth_client.get("/shipment/active")
    item = next(i for i in r.json()["items"] if i["asin"] == ASIN)
    assert item["shippable"] == 600, "the combined days did not become shippable"


async def test_verifying_a_held_day_also_clears_it_from_the_backlog(
    auth_client, ops_client, plan_factory
):
    """Verifying IS the owner deciding the units are good — one click, not two.

    Requiring release-then-verify would be two clicks for one decision, so
    /verify accepts a held day directly. The backlog must reflect that, or the
    page would keep advertising a hold the owner has already resolved.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, 400, 20)

    r = await auth_client.post(f"/shipment/packing/{MONDAY}/verify")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == logic.STATUS_VERIFIED

    carry = await _carry_over(auth_client)
    assert carry["days"] == 0, "a verified day is still being reported as held"
    assert carry["units"] == 0


async def test_the_backlog_follows_the_owners_thresholds(
    auth_client, ops_client, plan_factory
):
    """Moving the minimum must move the backlog check with it.

    Two rules that disagree is the failure mode: the page prompting the owner to
    ship a backlog the server would still treat as held.
    """
    plan = await plan_factory()
    await _pack(ops_client, MONDAY, 400, 20)
    assert (await _carry_over(auth_client))["clears"] is False

    r = await auth_client.patch(
        f"/shipment/plan/{plan.id}/thresholds",
        json={"min_cartons": 10, "min_units": 100},
    )
    assert r.status_code == 200, r.text

    carry = await _carry_over(auth_client)
    assert carry["clears"] is True, (
        "the backlog check ignored the new thresholds — it would disagree with "
        "the hold rule the server applies on the next submit"
    )
    assert carry["min_cartons"] == 10


async def test_ops_sees_the_same_hold_state_as_the_owner(ops_client, plan_factory):
    """Ops reads /active too, and must not be shown a different answer.

    The packer needs to know the day was parked — otherwise he reports "done" and
    the owner sees a hold, and they disagree about what happened on the floor.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, 400, 20)

    carry = await _carry_over(ops_client)
    assert carry["days"] == 1
    assert carry["units"] == 400


# ─── Held stock must not silently vanish when the plan rolls over ────────────

def _csvs(rows):
    """The (sales, stock) CSV pair. rows: [(asin, units_sold, sku)]."""
    sales = "(Child) ASIN,Units Ordered\n" + "".join(
        f"{asin},{units}\n" for asin, units, _ in rows
    )
    stock = "asin,sku,afn-fulfillable-quantity\n" + "".join(
        f"{asin},{sku},0\n" for asin, _, sku in rows
    )
    return {
        "sales_csv": ("sales.csv", sales.encode(), "text/csv"),
        "stock_csv": ("stock.csv", stock.encode(), "text/csv"),
    }


@pytest.fixture
def real_asin():
    """An ASIN the generate route will actually emit a row for."""
    from app.routers.shipment import FAMILIES

    asins = sorted(FAMILIES)
    assert asins, "product_families.json is empty"
    return asins[0]


async def test_generating_a_new_plan_warns_about_stock_left_on_hold(
    auth_client, ops_client, plan_factory, real_asin
):
    """The silent-disappearance bug, which is the worst shape this could take.

    /active only ever returns the *active* plan. So a day held on Saturday drops
    off every screen the moment Monday's plan is generated — the boxes are still
    on the warehouse floor and nothing in the app mentions them again. That is
    exactly the "held stock becomes lost stock" outcome the hold exists to
    prevent, arrived at by a different route.

    Generating is not blocked: the owner may well have shipped those boxes and
    simply not marked it. But he is told, by name and by date.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, 400, 20)

    r = await auth_client.post(
        "/shipment/generate",
        files=_csvs([(real_asin, 100, "SKU-1")]),
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body.get("abandoned_holds"), (
        "a new plan was generated over a held day and nothing was reported — "
        "400 packed units just became invisible in every screen"
    )
    assert body["abandoned_holds"][0]["pack_date"] == MONDAY
    assert body["abandoned_holds"][0]["units"] == 400

    warning = body.get("warning") or ""
    assert MONDAY in warning, "the warning does not name the date to go and look at"
    assert "400" in warning, "the warning does not say how many units are parked"


async def test_generating_over_a_clean_plan_warns_about_nothing(
    auth_client, ops_client, plan_factory, real_asin
):
    """No cry-wolf. A submitted day is going out; it is not abandoned stock.

    If every rollover warned, the warning would be ignored by the second week and
    the one that mattered would be missed with it.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, 900, 40)  # clears both thresholds

    r = await auth_client.post(
        "/shipment/generate",
        files=_csvs([(real_asin, 100, "SKU-1")]),
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["abandoned_holds"] == [], (
        "a submitted day was reported as abandoned held stock"
    )


async def test_the_first_ever_plan_does_not_warn(auth_client, real_asin):
    """There is no previous plan to have left anything on. Must not 500."""
    r = await auth_client.post(
        "/shipment/generate",
        files=_csvs([(real_asin, 100, "SKU-1")]),
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["abandoned_holds"] == []


async def test_generating_a_draft_leaves_the_old_backlog_visible(
    auth_client, ops_client, plan_factory, real_asin
):
    """A draft must not change what the owner is currently looking at.

    Generating no longer closes the active plan, so /active still describes the
    plan being packed — including its held days. That is the point: the held stock
    is real and must not disappear from view just because a replacement plan is
    being prepared.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, 400, 20)

    await auth_client.post(
        "/shipment/generate",
        files=_csvs([(real_asin, 100, "SKU-1")]),
        data={"multiplier": "5"},
    )

    carry = await _carry_over(auth_client)
    assert carry["days"] == 1, (
        "generating a draft hid the active plan's held stock — 400 packed units "
        "would drop off the screen while still sitting in the warehouse"
    )
    assert carry["units"] == 400


async def test_the_finalised_plan_starts_with_an_empty_backlog(
    auth_client, ops_client, plan_factory, real_asin
):
    """Once the new plan IS active, the old plan's holds must not follow it.

    Leaking them would be worse than saying nothing: the owner would be prompted
    to release days that are not even on the plan he is looking at. The
    abandoned-holds warning at generate is what tells him about them instead.
    """
    await plan_factory()
    await _pack(ops_client, MONDAY, 400, 20)

    r = await auth_client.post(
        "/shipment/generate",
        files=_csvs([(real_asin, 100, "SKU-1")]),
        data={"multiplier": "5"},
    )
    new_id = r.json()["plan"]["id"]
    r = await auth_client.post(f"/shipment/plan/{new_id}/finalise")
    assert r.status_code == 200, r.text

    carry = await _carry_over(auth_client)
    assert carry["days"] == 0, "the old plan's held days leaked into the new plan"
    assert carry["clears"] is False


async def test_the_held_stock_is_still_in_the_database_after_the_rollover(
    auth_client, ops_client, plan_factory, read_committed, real_asin
):
    """Warning about it is only useful if the record survives to be acted on.

    The owner is told to go and deal with 400 units; if closing the plan had
    deleted the rows he would have nothing to reconcile against.
    """
    plan = await plan_factory()
    old_plan_id = plan.id
    await _pack(ops_client, MONDAY, 400, 20)

    await auth_client.post(
        "/shipment/generate",
        files=_csvs([(real_asin, 100, "SKU-1")]),
        data={"multiplier": "5"},
    )

    held = await read_committed(repository.load_held_days, old_plan_id)
    assert [d.pack_date for d in held] == [MONDAY], "the held day was destroyed"
    assert held[0].total_units == 400
