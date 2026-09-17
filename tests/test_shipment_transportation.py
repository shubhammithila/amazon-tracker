"""Confirming transportation — the step that turns a placed plan into a real shipment.

Measured on the live account (``docs/spapi-inbound-completion-probe.md``): a shipment this app
created sat at ``READY_TO_SHIP`` with ``dates: {}``, while one that actually went out held
``readyToShipWindow: {"start": "2026-09-18T18:30Z"}``. That single difference is what "the app
only makes a plan" meant.

Every test here fails against the code before this feature: `confirm_transportation_options`,
`list_transportation_options`, `option_cost` and `ist.utc_instant` did not exist.
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import ist
from app.shipment import spapi


# ── The date conversion: defects 1–4 of the IST list, waiting to happen again ──


def test_a_ship_date_becomes_midnight_IST_not_midnight_UTC():
    """The bug this codebase has shipped six times.

    Amazon holds ``2026-09-18T18:30Z`` for a ship date of the **19th**, because 18:30Z is
    midnight IST. The obvious ``f"{day}T00:00Z"`` declares the previous Indian day.
    """
    assert ist.utc_instant(date(2026, 9, 19)) == "2026-09-18T18:30Z"


def test_the_instant_is_exactly_what_amazon_returned_for_a_real_shipment():
    """Pinned against a measured value rather than recomputed arithmetic.

    `wf2744a751…`, which shipped, carries this exact string. Asserting the value Amazon itself
    stored is the only way to know the conversion direction is right — a sign error produces a
    plausible-looking timestamp 11 hours out.
    """
    assert ist.utc_instant(date(2026, 9, 19)) == "2026-09-18T18:30Z"
    # And the day genuinely moves back, which is the half a sign error gets wrong.
    assert ist.utc_instant(date(2026, 9, 19)).startswith("2026-09-18")


def test_the_format_is_minutes_and_a_literal_Z():
    """``isoformat()`` would emit ``+00:00`` and seconds — a different string, same instant.

    Amazon returns ``2026-09-18T18:30Z`` and has not been observed to accept the other shape,
    so the format is asserted rather than assumed interchangeable.
    """
    out = ist.utc_instant(date(2026, 1, 5))
    assert out.endswith("Z")
    assert "+00:00" not in out
    assert out.count(":") == 1, f"seconds should not be present: {out}"


def test_a_non_midnight_time_still_converts():
    """08:00 IST is 02:30 UTC the same day — the `utc_hhmm` case, on a real date."""
    assert ist.utc_instant(date(2026, 9, 19), 8, 0) == "2026-09-19T02:30Z"


# ── The cost placeholder: a string that looks like data ──


def test_amazons_unsubstituted_cost_placeholder_is_not_read_as_a_number():
    """Measured verbatim on both real plans::

        "quote": {"cost": {"amount": "$cost.amount", "code": ""}}

    ``float("$cost.amount")`` raises, so reading this the obvious way turns a working shipment
    into a 500 at the very last step. Same trap as the scraper's ``NO_OF_HOURS`` deal badge.
    """
    option = {"quote": {"cost": {"amount": "$cost.amount", "code": ""}}}
    assert spapi.option_cost(option) is None


def test_no_quote_is_None_and_never_zero():
    """A self-ship shipment has no Amazon quote. ``0.0`` would read as "carried for free".

    The same three-state discipline as the Portfolio tab's ACOS column, where 0% would rank an
    unadvertised product as the most efficient in the portfolio.
    """
    assert spapi.option_cost({}) is None
    assert spapi.option_cost({"quote": {}}) is None
    assert spapi.option_cost({"quote": {"cost": {}}}) is None


def test_a_real_cost_is_still_read():
    """The guard must not swallow a genuine quote — a partnered carrier would send one."""
    assert spapi.option_cost(
        {"quote": {"cost": {"amount": "211.84", "code": "INR"}}}
    ) == pytest.approx(211.84)


# ── Listing: one of the two ids is mandatory ──


@pytest.mark.asyncio
async def test_listing_with_neither_id_is_refused_before_reaching_amazon():
    """Amazon answers 400 *"neither shipment id nor placement option id was provided"*.

    Refused locally with a message naming the requirement, because Amazon's phrasing reads like
    a malformed request rather than a missing parameter.
    """
    with pytest.raises(spapi.SpApiError) as caught:
        await spapi.list_transportation_options("wf123")
    assert "placementOptionId" in str(caught.value)
    assert "shipmentId" in str(caught.value)


# ── Source-level properties no runtime test can observe ──


def test_the_ready_to_ship_window_goes_through_ist_never_a_bare_string():
    """A source assertion, and it is the honest kind here.

    A fake Amazon client accepts any string, so a runtime test cannot see the difference
    between a correctly converted instant and ``f"{ship_date}T00:00Z"``. This codebase already
    uses source assertions for exactly this in `test_ads_one_source.py` and
    `test_local_dates.py`.
    """
    text = Path("app/routers/shipment.py").read_text(encoding="utf-8")
    assert "ist.utc_instant" in text, (
        "the ready-to-ship window must be built through ist.utc_instant"
    )
    # Comments stripped first, for the reason `test_local_dates._code` documents: the fix is
    # EXPLAINED by naming the string it replaced, and an assertion that cannot coexist with its
    # own explanation forces the explanation out — and the explanation is what stops the next one.
    code = re.sub(r"#.*$", "", text, flags=re.M)
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    for forbidden in ("T00:00Z", "T00:00:00Z"):
        assert forbidden not in code, (
            f"{forbidden} hardcodes midnight UTC, which is 05:30 IST — the ship date would "
            "declare the previous Indian day"
        )


def test_transportation_failure_does_not_fail_the_confirm_request():
    """The shipment is already irreversible by then, so an error here must not read as retry.

    Asserted on the parsed tree, not on file text: the words appear in the explanatory comments
    too, and a substring check would pass with the real code deleted. That is precisely the
    mistake the deploy detector and the scheduler guard both made in this repo.
    """
    source = Path("app/routers/shipment.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "confirm_amazon_shipment"
    )

    # Find the try block that wraps the transportation work.
    transport_tries = [
        node for node in ast.walk(func)
        if isinstance(node, ast.Try)
        and "list_transportation_options" in ast.dump(node)
    ]
    assert transport_tries, "transportation must be attempted inside a try"

    for handler in transport_tries[0].handlers:
        returns = [n for n in ast.walk(handler) if isinstance(n, ast.Return)]
        assert not returns, (
            "the transportation handler must NOT return — the shipment exists and its labels "
            "print, so reporting failure would invite a retry that creates a second shipment"
        )


def test_the_contact_details_are_not_typed_a_second_time():
    """Built from AMAZON_SOURCE_ADDRESS, so the FC cannot be given a stale phone number."""
    from app.routers.shipment import AMAZON_CONTACT_INFORMATION, AMAZON_SOURCE_ADDRESS

    assert AMAZON_CONTACT_INFORMATION["phoneNumber"] == AMAZON_SOURCE_ADDRESS["phoneNumber"]
    assert AMAZON_CONTACT_INFORMATION["email"] == AMAZON_SOURCE_ADDRESS["email"]


def test_the_ship_date_is_sent_to_GENERATE_never_to_the_CONFIRMATION():
    """**The mistake that nearly shipped, and Amazon would not have told us.**

    Read from Amazon's own schema: `readyToShipWindow` is a REQUIRED field of
    `ShipmentTransportationConfiguration` (input to *generate*), while
    `TransportationSelection` (input to *confirm*) accepts exactly three fields —
    ``shipmentId``, ``transportationOptionId``, ``contactInformation``.

    The first version sent the window on the confirmation. **Measured against the live account,
    three different shapes all produced the identical error** — one about a deliberately
    invalid id, never about the field — so Amazon silently drops the extra key. The shipment
    would have been confirmed with ``dates: {}``, which is the precise state this whole feature
    exists to fix, and nothing would have failed.

    Asserted on the parsed call, not on file text: the field names appear in the explanatory
    comments too, and a substring check would pass with the real code reverted. That is the
    deploy-detector and scheduler-guard mistake this repo has made three times.
    """
    generate = inspect.getsource(spapi.generate_transportation_options)
    confirm = inspect.getsource(spapi.confirm_transportation_options)

    assert "shipmentTransportationConfigurations" in generate, (
        "generate must send the configurations that carry readyToShipWindow"
    )
    assert "placementOptionId" in generate, "generate requires the placement option id"

    # The confirmation body must be nothing but the selections list.
    tree = ast.parse(textwrap.dedent(confirm))
    posted_keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    posted_keys.add(key.value)
    assert posted_keys == {"transportationSelections"}, (
        f"the confirmation body must contain only transportationSelections, got {posted_keys}"
    )
    assert "readyToShipWindow" not in _strip_prose(confirm), (
        "the ready-to-ship window on a CONFIRMATION is silently ignored by Amazon — the "
        "shipment would be confirmed with dates: {} and nothing would fail"
    )
    # And it must be present in generate, which is the only place Amazon reads it.
    assert "readyToShipWindow" in _strip_prose(
        _confirm_route_source()
    ), "the route must send readyToShipWindow when it generates the options"


def test_the_selection_carries_only_the_three_fields_amazon_accepts():
    """Extra keys are dropped in silence, so sending them hides a mistake.

    Pinned against the schema: `TransportationSelection` has exactly `shipmentId`,
    `transportationOptionId` and `contactInformation`.
    """
    body = _confirm_route_source()
    tree = ast.parse(textwrap.dedent(body))

    selection_dicts = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and any(
            isinstance(k, ast.Constant) and k.value == "transportationOptionId"
            for k in node.keys
        )
    ]
    assert selection_dicts, "the route must build a transportation selection"
    for node in selection_dicts:
        keys = {
            k.value for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        assert keys == {"shipmentId", "transportationOptionId", "contactInformation"}, (
            f"a selection must carry exactly Amazon's three fields, got {sorted(keys)}"
        )


def test_generate_runs_before_confirm():
    """Order is the requirement: confirming an option that was never generated cannot work."""
    body = _confirm_route_source()
    generate = body.index("generate_transportation_options")
    listing = body.index("list_transportation_options")
    confirm = body.index("confirm_transportation_options")
    assert generate < listing < confirm, (
        "the sequence must be generate (with the ship date) -> list -> confirm"
    )


def test_no_ship_date_means_transportation_is_not_attempted_at_all():
    """`readyToShipWindow` is REQUIRED by the generate schema, so there is nothing to send.

    Attempting it without a date would post a body Amazon rejects, turning "the owner did not
    choose a date" into an error message about a validation failure. The shipment still exists
    and its labels still print, which is what the screen says instead.
    """
    body = _confirm_route_source()
    assert "if shipments and ship_date:" in body, (
        "transportation must only be attempted when a ship date was given"
    )


def test_packing_information_uses_MANUAL_PROCESS_and_sends_no_box_contents():
    """**This is what makes the feature possible at all.**

    `BOX_CONTENT_PROVIDED` needs an `items` array per box — msku and quantity for every carton.
    This app structurally cannot supply that: a carton is filled with whatever is being packed
    at the time, and `ShipmentPackingEntry.cartons` was REMOVED precisely because asking the
    packer what went in each box produced a guess that then prefilled a GST invoice.

    `MANUAL_PROCESS` requires `items` to be **empty**, so Amazon wants the box COUNT, dimensions
    and weight and nothing else. Measured: accepted 202, operation SUCCESS.

    Asserted on the parsed body rather than on file text, because the constant names appear in
    the surrounding prose and a substring check would pass with the real code reverted — the
    deploy-detector mistake this repo has made three times.
    """
    source = inspect.getsource(spapi.set_packing_information)
    assert "BOX_CONTENT_MANUAL" in source
    assert spapi.BOX_CONTENT_MANUAL == "MANUAL_PROCESS"

    tree = ast.parse(textwrap.dedent(source))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    assert "items" not in keys, (
        "MANUAL_PROCESS requires the box items list to be EMPTY — sending contents means "
        "claiming to know which SKUs are in which carton, which this app cannot know"
    )
    for required in ("contentInformationSource", "quantity", "shipmentId", "packageGroupings"):
        assert required in keys, f"{required} is required by Amazon's schema"


def test_the_carton_count_comes_from_the_days_not_the_line_count():
    """Cartons are per DAY in this app; units are per SKU. That asymmetry is deliberate.

    Counting plan lines would send Amazon "3 boxes" for a 14-carton shipment, and the FC
    reconciles received units per box.
    """
    body = _strip_prose(_confirm_route_source())
    assert "total_cartons" in body, (
        "the box count must come from the packer's own per-day carton count"
    )
    assert "set_packing_information" in body


def test_packing_information_is_sent_for_EVERY_shipment_on_the_plan():
    """Found by mutation: emptying the loop passed every other test here.

    `for shipment in []:` leaves the call present, the constants right and the ordering intact
    while sending nothing — and Amazon then refuses the confirmation with a message about
    packing information, which reads as an API problem rather than as our bug. A plan can also
    SPLIT into several shipments, and each needs its own packing information.

    So the loop's iterable is asserted, not merely the presence of the call: it must be the
    shipments read back from the plan.
    """
    body = _confirm_route_source()
    tree = ast.parse(textwrap.dedent(body))

    loops = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.For, ast.AsyncFor))
        and "set_packing_information" in ast.dump(node)
    ]
    assert loops, "packing information must be set inside a loop over the plan's shipments"

    iterated = loops[0].iter
    assert isinstance(iterated, ast.Name), (
        f"the loop must iterate a named collection of shipments, not {ast.dump(iterated)[:80]}"
    )
    assert "shipment" in iterated.id, (
        f"the loop iterates {iterated.id!r}, which is not the plan's shipments"
    )

    # And that name must be what plan_shipments returned, or it could be any empty list.
    assigned = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and "plan_shipments" in ast.dump(node)
        and any(isinstance(t, ast.Name) and t.id == iterated.id for t in node.targets)
    ]
    assert assigned, (
        f"{iterated.id!r} must come from spapi.plan_shipments — otherwise the loop can be "
        "silently emptied and Amazon refuses the confirmation instead"
    )


def test_a_packing_failure_refuses_BEFORE_anything_irreversible():
    """The opposite rule from the transportation step, and for a measured reason.

    Nothing is committed when packing information is set, so refusing early is safe and the
    message is specific. By the time transportation runs, placement is already irreversible, so
    a failure there must NOT fail the request or the owner retries and creates a second
    shipment. Both behaviours are asserted so neither can be "tidied" into the other.
    """
    body = _confirm_route_source()
    packing = body.index("set_packing_information")
    placement = body.index("confirm_placement")
    assert packing < placement, (
        "packing information must be set before the placement is confirmed — Amazon refuses "
        "the confirmation otherwise"
    )

    tree = ast.parse(textwrap.dedent(body))
    packing_tries = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Try) and "set_packing_information" in ast.dump(node)
    ]
    assert packing_tries, "the packing call must be guarded"
    returns = [
        n for handler in packing_tries[0].handlers
        for n in ast.walk(handler) if isinstance(n, ast.Return)
    ]
    assert returns, (
        "a packing failure MUST return — nothing is confirmed yet, so refusing early is the "
        "safe direction and the plan can still be cancelled"
    )


def test_setting_packing_information_is_a_write_not_a_read():
    source = inspect.getsource(spapi.set_packing_information)
    assert "_post(" in source
    assert "_get(" not in source


def test_the_packing_operation_is_polled():
    """202 means accepted, not done. Amazon's schema marks `operationId` as REQUIRED here.

    Without polling, a failure surfaces later as the confirmation refusing for a reason that
    looks unrelated to packing.
    """
    source = _strip_prose(inspect.getsource(spapi.set_packing_information))
    assert "wait_for_operation" in source


def test_confirming_transportation_is_a_write_not_a_read():
    """It must go through `_post`, the write path — `_get` cannot mutate.

    The module documents read/write separation and an existing test pins it; this extends the
    same guarantee to the new call rather than trusting it.
    """
    source = inspect.getsource(spapi.confirm_transportation_options)
    assert "_post(" in source
    assert "_get(" not in source


def test_listing_transportation_is_a_read_not_a_write():
    source = inspect.getsource(spapi.list_transportation_options)
    assert "_get(" in source
    assert "_post(" not in source


# ── The route contract ──


@pytest.mark.asyncio
async def test_a_past_ship_date_is_refused_against_the_IST_day(auth_client):
    """And the IST day, not the server's.

    On a UTC box `date.today()` is yesterday for 5.5 hours after IST midnight, so a shipment
    dated at 01:00 IST would have its own real today refused as past.
    """
    yesterday = (ist.today() - timedelta(days=1)).isoformat()
    response = await auth_client.post(
        "/shipment/amazon-shipment/confirm",
        json={
            "inbound_plan_id": "wf1",
            "placement_option_id": "pl1",
            "pack_dates": ["2026-09-15"],
            "ship_date": yesterday,
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert "past" in body["error"].lower()
    assert ist.today().isoformat() in body["error"], (
        "the refusal must name the IST today, so the owner can see which day it compared against"
    )


@pytest.mark.asyncio
async def test_todays_IST_date_is_accepted_as_a_ship_date(auth_client):
    """The boundary case the UTC comparison gets wrong.

    Not asserting success — there are no credentials in the suite — only that it is not refused
    as being in the past. Deriving the date from `ist.today()` rather than hardcoding it means
    no calendar day can make this vacuous, the fixture mistake that let two ads mutations
    survive.
    """
    response = await auth_client.post(
        "/shipment/amazon-shipment/confirm",
        json={
            "inbound_plan_id": "wf1",
            "placement_option_id": "pl1",
            "pack_dates": ["2026-09-15"],
            "ship_date": ist.today().isoformat(),
        },
    )
    body = response.json()
    assert "past" not in str(body.get("error", "")).lower()


def test_the_ship_date_is_compared_against_the_IST_day_not_the_servers():
    """A SOURCE assertion, because no value-based test can see this difference.

    Found by mutation: replacing `ist.today()` with `date.today()` passed the whole suite.
    The reason is the machine — this dev box is in IST, so the two functions return the same
    value and every value-based test agrees with itself. **Production runs UTC**, where they
    differ for 5.5 hours out of every 24, and in that window the owner's real today would be
    refused as being in the past.

    Patching the timezone is not an option: `date.today()` reads the process's local zone, which
    is fixed at interpreter start. So the guarantee is asserted where it is visible — in the
    source — exactly as `test_ads_one_source.py` does for the SB `daily=True` fetch.

    This is the same trap CLAUDE.md records for the ads window test, whose pretend date happened
    to equal the real one and let two mutations through.
    """
    body = _strip_prose(_confirm_route_source())
    assert "ist.today()" in body, (
        "the ship date must be compared against the IST calendar day"
    )
    assert "date.today()" not in body, (
        "date.today() is the SERVER's day — UTC on production, so it is yesterday for the 5.5 "
        "hours after IST midnight and would refuse the owner's real today"
    )


def _strip_prose(code: str) -> str:
    """Source with docstrings and comments removed.

    Every guard here is DOCUMENTED by naming the wrong call it replaced — that explanation is
    the part that stops the next occurrence, so an assertion that cannot coexist with its own
    reasoning would force the reasoning out. `test_local_dates._code` strips comments for
    exactly this reason and says so.
    """
    without_docstrings = re.sub(r'"""(?:.|\n)*?"""', "", code)
    return re.sub(r"#[^\n]*", "", without_docstrings)


def _confirm_route_source() -> str:
    """The confirm route's source, bounded by parsing rather than a character count."""
    source = Path("app/routers/shipment.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "confirm_amazon_shipment"
    )
    return ast.get_source_segment(source, func) or ""


def test_a_malformed_ship_date_is_validated_before_it_is_parsed():
    """`_valid_date` must gate `date.fromisoformat`, which raises on anything else.

    Asserted on ORDER, not on presence: both calls existing proves nothing if the parse happens
    first. The mutation that motivated this deleted the guard, and a presence check would have
    passed with `date.fromisoformat` left reachable by a malformed string.
    """
    body = _confirm_route_source()
    guard = body.index("_valid_date(ship_date)")
    parse = body.index("date.fromisoformat(ship_date)")
    assert guard < parse, (
        "the ship date is parsed before it is validated, so a malformed value 500s"
    )


@pytest.mark.asyncio
async def test_a_malformed_ship_date_is_refused(auth_client):
    response = await auth_client.post(
        "/shipment/amazon-shipment/confirm",
        json={
            "inbound_plan_id": "wf1",
            "placement_option_id": "pl1",
            "pack_dates": ["2026-09-15"],
            "ship_date": "19/09/2026",
        },
    )
    assert response.status_code == 400
    assert "19/09/2026" in response.json()["error"]


# ── The client sends what the server expects: the gap that shipped the pause bug ──


def test_the_screen_sends_the_ship_date_the_route_reads():
    """20 tests verified the pause feature's server contract and none checked the client.

    That shipped "Row … has no usable bid" on every pause. The same gap is available here: the
    route reads ``ship_date``, and nothing else would notice if the screen posted
    ``shipDate``.
    """
    template = Path("templates/shipment.html").read_text(encoding="utf-8")
    assert "ship_date: shipDate" in template, (
        "the confirm payload must carry `ship_date` — the exact key the route reads"
    )
    assert 'id="ib-ship-date"' in template, "the date input must exist in the markup"
    assert 'getElementById("ib-ship-date")' in template, (
        "and the confirm handler must read it — renderInvoiceBar was complete, tested and "
        "invisible because its target div did not exist"
    )


def test_the_ship_date_min_is_built_with_the_shared_local_helper():
    """`toISOString` shifted four dates a day early in this app, one of them a GST invoice.

    The BAN itself is not re-asserted here: `test_local_dates.py` already applies it to every
    template by glob, so `shipment.html` was covered the moment this input was added, and a
    second copy of that rule would be one more place to keep in step. What this pins is the
    thing that file cannot know — that the new date input's `min` goes through the helper rather
    than being left unbounded or built inline.
    """
    template = Path("templates/shipment.html").read_text(encoding="utf-8")
    assert "min=\"${localDate(new Date())}\"" in template, (
        "the ship-date picker's minimum must come from localDate()"
    )
