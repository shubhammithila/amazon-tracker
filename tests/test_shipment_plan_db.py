"""The shipment plan in the database: concurrent writes, ordering, role limits.

Why this file exists. The plan used to be one JSON blob at repo root, and
``POST /shipment/save`` overwrote the whole thing. With two people now working at
once — the owner adjusting quantities while the packer records what was boxed —
that design loses data: whoever saves second wins, and the other's work vanishes
with no error and no trace.

The fix is write separation, not locking: plan rows and packing rows are separate
records written by separate endpoints under separate roles. The tests below are
what prove that property actually holds, in both interleavings. If someone later
"simplifies" the API back to one save-everything endpoint, the clobber tests fail.

The other two properties pinned here:

  * **One ORDER BY.** ``repository.load_plan_items`` is the only SELECT of items.
    Row order must be product-then-weight-then-ASIN, case-insensitively, or the
    screen and the downloads drift apart.
  * **UNIQUE means upsert.** A repeated save from a flaky warehouse phone must
    update the row, never double-count the units.

A note on how these tests read the database. Anything the app wrote over HTTP is
asserted through the ``read_committed`` / ``count_rows`` fixtures, which open a
fresh session. The ``db`` fixture is for *arranging* only: it holds its own read
transaction and identity map, so reading through it after an HTTP request can
return the values from before that request. That failure mode is silent in both
directions, which makes it worse than a crash — a stale read can also make a
broken write look like it succeeded.
"""
import pytest

from app.models import ShipmentPackingDay, ShipmentPackingEntry, ShipmentPlan, ShipmentPlanItem
from tests.conftest import CANONICAL_ORDER
from app.shipment import logic, repository

pytestmark = pytest.mark.regression


# ─── The clobber tests: the whole reason storage moved to the database ────────

async def test_admin_edit_then_ops_packing_both_survive(
    auth_client, ops_client, plan_factory, read_committed
):
    """Owner edits a quantity, then ops records packing. Neither is lost."""
    plan = await plan_factory()
    plan_id = plan.id

    r = await auth_client.post(
        "/shipment/items",
        json={"plan_id": plan_id, "items": [{"asin": "B0AAA00001", "shipment_plan": 640}]},
    )
    assert r.status_code == 200, r.text

    r = await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 120, "cartons": 6}]},
    )
    assert r.status_code == 200, r.text

    items = await read_committed(repository.load_plan_items, plan_id)
    edited = next(i for i in items if i.asin == "B0AAA00001")
    assert edited.shipment_plan == 640, "ops packing overwrote the owner's quantity"

    day = await read_committed(repository.get_day, plan_id, "2026-07-30")
    assert day is not None and day.total_units == 120


async def test_ops_packing_then_admin_edit_both_survive(
    auth_client, ops_client, plan_factory, read_committed
):
    """The reverse order. This is the interleaving the JSON blob actually lost.

    Ops saved first, the owner's browser still held the pre-packing plan, and his
    save wrote the whole file back with day1..day6 as they were before ops typed
    anything.
    """
    plan = await plan_factory()
    plan_id = plan.id

    r = await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 90, "cartons": 5}]},
    )
    assert r.status_code == 200, r.text

    r = await auth_client.post(
        "/shipment/items",
        json={"plan_id": plan_id, "items": [{"asin": "B0AAA00001", "shipment_plan": 700}]},
    )
    assert r.status_code == 200, r.text

    day = await read_committed(repository.get_day, plan_id, "2026-07-30")
    assert day is not None, "the owner's plan edit deleted the packing day"
    assert day.total_units == 90, "the owner's plan edit overwrote ops' units"

    items = await read_committed(repository.load_plan_items, plan_id)
    assert next(i for i in items if i.asin == "B0AAA00001").shipment_plan == 700


async def test_admin_cannot_write_packing_through_the_items_route(
    auth_client, ops_client, plan_factory, read_committed
):
    """Even a stale full-row POST from the admin UI must not touch packing.

    The repository whitelists editable columns precisely so that a frontend
    sending back a whole row it read minutes ago cannot resurrect old packing
    numbers.
    """
    plan = await plan_factory()
    plan_id = plan.id
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 150, "cartons": 8}]},
    )

    await auth_client.post(
        "/shipment/items",
        json={
            "plan_id": plan_id,
            "items": [
                {
                    "asin": "B0AAA00001",
                    "shipment_plan": 500,
                    # A stale client trying to send packing fields:
                    "units": 0,
                    "cartons": 0,
                    "packed": 0,
                    "day1": 0,
                }
            ],
        },
    )

    day = await read_committed(repository.get_day, plan_id, "2026-07-30")
    assert day.total_units == 150, "an /items POST wrote packing data"
    assert day.total_cartons == 8


# ─── UNIQUE indexes: a double-save upserts, never double-counts ───────────────

async def test_repeated_packing_save_upserts_one_row(
    ops_client, plan_factory, read_committed, count_rows
):
    """The warehouse is on a phone; a retried request must not double the units."""
    plan = await plan_factory()
    plan_id = plan.id

    for _ in range(3):
        r = await ops_client.post(
            "/shipment/packing/2026-07-30",
            json={"entries": [{"asin": "B0AAA00001", "units": 100, "cartons": 5}]},
        )
        assert r.status_code == 200, r.text

    assert await count_rows(ShipmentPackingDay, plan_id=plan_id) == 1, (
        "a repeated save created duplicate packing days"
    )

    day = await read_committed(repository.get_day, plan_id, "2026-07-30")
    assert await count_rows(ShipmentPackingEntry, day_id=day.id) == 1, (
        "a repeated save created duplicate entry rows"
    )
    assert day.total_units == 100, f"units were double-counted: {day.total_units}"
    assert day.total_cartons == 5


async def test_a_later_save_corrects_rather_than_adds(
    ops_client, plan_factory, read_committed
):
    """Fixing a typo must replace the number, not add to it."""
    plan = await plan_factory()
    plan_id = plan.id
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 1000, "cartons": 50}]},
    )
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 100, "cartons": 5}]},
    )

    day = await read_committed(repository.get_day, plan_id, "2026-07-30")
    assert day.total_units == 100
    assert day.total_cartons == 5


async def test_zeroing_an_entry_removes_it_from_the_totals(
    ops_client, plan_factory, read_committed
):
    """A row corrected to zero must stop counting, not linger as a touched SKU."""
    plan = await plan_factory()
    plan_id = plan.id
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={
            "entries": [
                {"asin": "B0AAA00001", "units": 100, "cartons": 5},
                {"asin": "B0AAA00002", "units": 60, "cartons": 3},
            ]
        },
    )
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00002", "units": 0, "cartons": 0}]},
    )

    day = await read_committed(repository.get_day, plan_id, "2026-07-30")
    entries = await read_committed(repository.load_entries, day.id)
    assert [e.asin for e in entries] == ["B0AAA00001"]
    assert day.total_units == 100
    assert day.total_cartons == 5


async def test_two_dates_are_separate_days(ops_client, plan_factory, read_committed):
    """Dated entries are the point of the new model (requirement 9 needs them)."""
    plan = await plan_factory()
    plan_id = plan.id
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 100, "cartons": 5}]},
    )
    await ops_client.post(
        "/shipment/packing/2026-07-31",
        json={"entries": [{"asin": "B0AAA00001", "units": 80, "cartons": 4}]},
    )

    days = await read_committed(repository.load_days, plan_id)
    assert [d.pack_date for d in days] == ["2026-07-30", "2026-07-31"], "days not chronological"
    assert [d.total_units for d in days] == [100, 80]


# ─── Role limits, verified at the database, not just the status code ──────────

async def test_ops_cannot_edit_plan_items_and_the_db_is_unchanged(
    ops_client, plan_factory, read_committed
):
    """A 403 is not enough — assert the write did not happen.

    A route could return 403 from a dependency after an earlier line already
    wrote, or write and then fail; only re-reading the rows rules that out.
    """
    plan = await plan_factory()
    plan_id = plan.id
    before = {
        i.asin: i.shipment_plan
        for i in await read_committed(repository.load_plan_items, plan_id)
    }

    r = await ops_client.post(
        "/shipment/items",
        json={"plan_id": plan_id, "items": [{"asin": "B0AAA00001", "shipment_plan": 99999}]},
    )
    assert r.status_code == 403, r.status_code

    after = {
        i.asin: i.shipment_plan
        for i in await read_committed(repository.load_plan_items, plan_id)
    }
    assert after == before, "an ops request changed plan quantities despite the 403"


async def test_ops_cannot_verify_a_day(ops_client, plan_factory, read_committed):
    """Verification is the owner's approval and gates the GST number."""
    plan = await plan_factory()
    plan_id = plan.id
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 900, "cartons": 40}]},
    )
    await ops_client.post("/shipment/packing/2026-07-30/submit")

    r = await ops_client.post("/shipment/packing/2026-07-30/verify")
    assert r.status_code == 403, r.status_code

    day = await read_committed(repository.get_day, plan_id, "2026-07-30")
    assert day.status == logic.STATUS_SUBMITTED, f"ops verified a day: {day.status}"
    assert day.verified_at is None


async def test_ops_cannot_generate_or_delete_a_plan(
    ops_client, plan_factory, read_committed
):
    plan = await plan_factory()
    plan_id = plan.id

    r = await ops_client.delete(f"/shipment/plan/{plan_id}")
    assert r.status_code == 403

    r = await ops_client.patch(
        f"/shipment/plan/{plan_id}/thresholds", json={"min_cartons": 1}
    )
    assert r.status_code == 403

    still_there = await read_committed(repository.get_plan, plan_id)
    assert still_there is not None, "ops deleted a plan"
    assert still_there.min_cartons == 25, "ops changed a threshold despite the 403"


async def test_ops_can_read_the_active_plan(ops_client, plan_factory):
    """Ops must see the plan — that is the whole point of the ops screen."""
    await plan_factory()
    r = await ops_client.get("/shipment/active")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "ops"
    assert len(body["items"]) == 4


# ─── Canonical ordering: brand → category → product → weight → ASIN ───────────

async def test_items_are_ordered_brand_then_category_then_product_then_weight(
    plan_factory, db
):
    """The requested ordering, with four properties in one assertion.

    * **Brand first.** 'aloe vera juice' is the only HF row and must be last.
    * **Category beats alphabet.** That row also sorts FIRST alphabetically, so if
      the category rank were dropped it would jump to the front. It is P6 Rest.
    * **Product above weight**, so the two chana sattu rows stay adjacent and in
      weight order.
    * **Case-insensitive.** SQLite's default collation is binary, so a naive ORDER
      BY on the raw name puts every capitalised name before 'jau sattu'.
      ``sort_product`` is stored casefolded to prevent that.

    ``db`` rather than ``read_committed`` because plan_factory writes through that
    same session and nothing else has written since.
    """
    plan = await plan_factory()
    items = await repository.load_plan_items(db, plan.id)

    assert [i.asin for i in items] == CANONICAL_ORDER, [
        (i.brand, i.item, float(i.weight)) for i in items
    ]


async def test_the_sql_order_matches_the_pure_sort_function(plan_factory, db):
    """The DB ordering and logic.sort_items must not be able to disagree.

    Two implementations of one rule is the drift risk; this pins them together so
    a future index or column change that breaks the equivalence fails here.

    This is also what makes the JOINed category safe: load_plan_items attaches the
    rank SQL sorted by, and sort_key reads that same attached value rather than
    re-deriving it from the product name. If it re-derived, an owner's override
    would apply in SQL and be ignored in Python.
    """
    plan = await plan_factory()
    items = await repository.load_plan_items(db, plan.id)
    assert [i.asin for i in items] == [i.asin for i in logic.sort_items(items)]


async def test_a_category_override_reorders_the_plan_with_no_row_rewrite(
    plan_factory, db
):
    """Re-classifying a product must move it immediately, everywhere.

    The reason the priority is JOINed rather than stored on the item row: there is
    no copy to go stale. Moving 'chana sattu' from P1 to P6 must reorder this plan
    without touching a single shipment_plan_items row — and SQL and Python must
    still agree afterwards, which a stored composite sort key would not guarantee.
    """
    plan = await plan_factory()
    before = [i.asin for i in await repository.load_plan_items(db, plan.id)]
    assert before == CANONICAL_ORDER

    await repository.set_categories(db, {"chana sattu": 6})

    items = await repository.load_plan_items(db, plan.id)
    after = [i.asin for i in items]
    assert after != before, (
        "a category override changed nothing — the priority is not being read at "
        "query time, so overrides would not reach the screen or the downloads"
    )
    # jau sattu is still P1, so it now leads; the demoted chana rows follow.
    assert after[0] == "B0BBB00001", after
    assert [i.asin for i in logic.sort_items(items)] == after, (
        "SQL and logic.sort_items disagree after an override"
    )


async def test_the_api_returns_items_in_canonical_order(auth_client, plan_factory):
    """The endpoint must not re-sort; the JS renders array order as-is."""
    await plan_factory()
    r = await auth_client.get("/shipment/active")
    assert r.status_code == 200, r.text
    assert [i["asin"] for i in r.json()["items"]] == CANONICAL_ORDER


# ─── Generate: rounding is persisted, previous plan is closed ─────────────────

# /generate builds its row set by iterating the product catalogue, not the CSV —
# the plan must list every SKU the business sells, including ones that did not
# sell last week. So a CSV naming an ASIN outside product_families.json produces
# no row at all. These tests therefore read real ASINs out of the same dict the
# router uses rather than hardcoding them, so the tests keep working when the
# catalogue changes and cannot silently pass against a plan of all zeroes.

@pytest.fixture
def real_asins():
    """Two ASINs the router will actually emit rows for.

    Sorted so the pair is deterministic across runs.
    """
    from app.routers.shipment import FAMILIES

    asins = sorted(FAMILIES)
    assert len(asins) >= 2, "product_families.json is too small for these tests"
    return asins[0], asins[1]


def _csvs(rows, *, with_sku=True):
    """Build the (sales, stock) CSV pair. rows: [(asin, units_sold, sku)]."""
    sales = "(Child) ASIN,Units Ordered\n" + "".join(
        f"{asin},{units}\n" for asin, units, _ in rows
    )
    if with_sku:
        stock = "asin,sku,afn-fulfillable-quantity\n" + "".join(
            f"{asin},{sku},0\n" for asin, _, sku in rows
        )
    else:
        stock = "asin,afn-fulfillable-quantity\n" + "".join(
            f"{asin},0\n" for asin, _, _ in rows
        )
    return {
        "sales_csv": ("sales.csv", sales.encode(), "text/csv"),
        "stock_csv": ("stock.csv", stock.encode(), "text/csv"),
    }


async def test_generate_rounds_to_ten_and_persists_it(
    auth_client, read_committed, real_asins
):
    """Requirement 4. Rounding happens once, at generate, and is stored.

    Checked at the database rather than in the response so a renderer cannot be
    what makes it look right — the whole point of rounding at generate time is
    that the stored number is the rounded one.
    """
    first, second = real_asins
    # 5 sold * 5 multiplier = 25 projected, 0 in stock -> deficit 25 -> 30.
    # 1 sold -> 5 -> 10. Both are cases plain round() gets wrong.
    r = await auth_client.post(
        "/shipment/generate",
        files=_csvs([(first, 5, "SKU-1"), (second, 1, "SKU-2")]),
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text

    items = await read_committed(repository.load_plan_items, r.json()["plan"]["id"])
    stored = {i.asin: i.shipment_plan for i in items}

    # 25 must become 30, not 20 — plain round() does banker's rounding.
    assert stored.get(first) == 30, f"{first}: {stored.get(first)}"
    # 5 must become 10, not 0 — a real need must never round away.
    assert stored.get(second) == 10, f"{second}: {stored.get(second)}"

    for asin, qty in stored.items():
        assert qty % 10 == 0, f"{asin} stored unrounded quantity {qty}"


async def test_generate_captures_the_merchant_sku(
    auth_client, read_committed, real_asins
):
    """fba_sku used to be filled inside a bare `except: pass`.

    Amazon's shipment upload keys on the merchant SKU, so a blank one means the
    row is rejected on their side. It is now parsed explicitly and counted.
    """
    first, _ = real_asins
    r = await auth_client.post(
        "/shipment/generate",
        files=_csvs([(first, 10, "MF-CHANA-1KG")]),
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text

    items = await read_committed(repository.load_plan_items, r.json()["plan"]["id"])
    match = next((i for i in items if i.asin == first), None)
    assert match is not None, f"{first} produced no plan row"
    assert match.fba_sku == "MF-CHANA-1KG", f"merchant SKU lost: {match.fba_sku!r}"


async def test_generate_reports_items_missing_a_merchant_sku(auth_client, real_asins):
    """A silent failure becomes a visible count.

    The stock CSV has no sku column at all, so the item to be shipped has no
    merchant SKU — exactly the case Amazon rejects, and exactly what the old bare
    `except Exception: pass` hid.
    """
    first, _ = real_asins
    r = await auth_client.post(
        "/shipment/generate",
        files=_csvs([(first, 10, "")], with_sku=False),
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["missing_sku_count"] > 0, (
        "an item to ship has no merchant SKU but nothing was reported"
    )
    assert "warning" in body


async def test_generate_does_not_disturb_the_plan_being_packed(
    auth_client, plan_factory, read_committed, count_rows, real_asins
):
    """The trap this whole draft design exists to avoid.

    ``create_plan`` used to call ``close_active_plans()``. With drafts that would
    mean uploading a CSV instantly closed the plan the warehouse was packing: the
    packer's screen empties mid-shift, with no warning and no explanation, while
    the replacement sits invisible in draft.

    So generate must leave the active plan completely alone, and the new plan must
    NOT be active yet.
    """
    old = await plan_factory()
    old_id = old.id
    first, _ = real_asins

    r = await auth_client.post(
        "/shipment/generate",
        files=_csvs([(first, 10, "SKU-1")]),
        data={"multiplier": "5"},
    )
    assert r.status_code == 200, r.text

    assert (await read_committed(repository.get_plan, old_id)).status == "active", (
        "generating a draft closed the plan the warehouse is packing — the "
        "packer's screen would empty mid-shift with nothing to explain it"
    )
    assert await count_rows(ShipmentPlan, status="active") == 1, "more than one active plan"
    assert await count_rows(ShipmentPlan, status="draft") == 1, "the new plan is not a draft"

    assert r.json()["plan"]["status"] == "draft", (
        "generate returned an active plan — the owner would have no chance to "
        "remove rows before the packer saw them"
    )


async def test_the_packer_cannot_see_a_draft(ops_client, auth_client, plan_factory, real_asins):
    """The point of the draft state, asserted from the packer's side.

    get_active_plan() matches 'active' alone, which is what makes every
    pre-existing packing endpoint draft-blind without touching any of them.
    """
    await plan_factory()          # active, 4 items
    first, _ = real_asins
    await auth_client.post(
        "/shipment/generate",
        files=_csvs([(first, 10, "SKU-1")]),
        data={"multiplier": "5"},
    )

    r = await ops_client.get("/shipment/packing/2026-07-30")
    assert r.status_code == 200, r.text
    asins = {row["asin"] for row in r.json()["items"]}
    assert first not in asins, "the packer can see a draft plan's rows"
    assert "B0AAA00001" in asins, "the packer lost the plan he was working on"


async def test_finalise_promotes_the_draft_and_closes_the_old_plan(
    auth_client, plan_factory, read_committed, count_rows, real_asins
):
    """Exactly one plan may be active, or /active becomes ambiguous.

    The close happens HERE — at the moment the owner decides — rather than at
    generate.
    """
    old = await plan_factory()
    old_id = old.id
    first, _ = real_asins

    r = await auth_client.post(
        "/shipment/generate",
        files=_csvs([(first, 10, "SKU-1")]),
        data={"multiplier": "5"},
    )
    new_id = r.json()["plan"]["id"]

    r = await auth_client.post(f"/shipment/plan/{new_id}/finalise")
    assert r.status_code == 200, r.text
    assert r.json()["plan"]["status"] == "active"

    assert await count_rows(ShipmentPlan, status="active") == 1, "more than one active plan"
    assert (await read_committed(repository.get_plan, old_id)).status == "closed"
    assert (await read_committed(repository.get_plan, new_id)).status == "active"


async def test_finalising_twice_is_harmless(auth_client, plan_factory, real_asins):
    """A double-click must not close the plan it just activated."""
    await plan_factory()
    first, _ = real_asins
    r = await auth_client.post(
        "/shipment/generate",
        files=_csvs([(first, 10, "SKU-1")]),
        data={"multiplier": "5"},
    )
    new_id = r.json()["plan"]["id"]

    for _ in range(2):
        r = await auth_client.post(f"/shipment/plan/{new_id}/finalise")
        assert r.status_code == 200, r.text
        assert r.json()["plan"]["status"] == "active"


async def test_ops_cannot_finalise_a_plan(ops_client, auth_client, plan_factory, real_asins):
    """Deciding the warehouse's plan has changed is the owner's call."""
    await plan_factory()
    first, _ = real_asins
    r = await auth_client.post(
        "/shipment/generate",
        files=_csvs([(first, 10, "SKU-1")]),
        data={"multiplier": "5"},
    )
    new_id = r.json()["plan"]["id"]

    r = await ops_client.post(f"/shipment/plan/{new_id}/finalise")
    assert r.status_code == 403, r.text


async def test_generating_twice_replaces_the_draft(
    auth_client, plan_factory, count_rows, real_asins
):
    """Two uploads must not leave two drafts for get_draft_plan to choose between.

    Safe to discard the first: no packing endpoint can reach a draft, so a draft
    never carries packing rows worth keeping.
    """
    await plan_factory()
    first, second = real_asins
    for asin in (first, second):
        r = await auth_client.post(
            "/shipment/generate",
            files=_csvs([(asin, 10, "SKU-1")]),
            data={"multiplier": "5"},
        )
        assert r.status_code == 200, r.text

    assert await count_rows(ShipmentPlan, status="draft") == 1, "an orphan draft was left"


async def test_a_closed_plan_keeps_its_packing_history(
    auth_client, ops_client, plan_factory, read_committed, real_asins
):
    """Closing must not destroy what the invoices were generated from."""
    old = await plan_factory()
    old_id = old.id
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 100, "cartons": 5}]},
    )

    first, _ = real_asins
    await auth_client.post(
        "/shipment/generate",
        files=_csvs([(first, 10, "SKU-1")]),
        data={"multiplier": "5"},
    )

    days = await read_committed(repository.load_days, old_id)
    assert [d.total_units for d in days] == [100], "closing the plan lost its packing"


# ─── Packed vs shippable, and the hold ───────────────────────────────────────

async def test_a_small_day_is_held_on_submit(ops_client, plan_factory, read_committed):
    """Requirement 9: 20 cartons / 400 units is not worth shipping alone."""
    plan = await plan_factory()
    plan_id = plan.id
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 400, "cartons": 20}]},
    )
    r = await ops_client.post("/shipment/packing/2026-07-30/submit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["held"] is True
    assert body["status"] == logic.STATUS_HELD
    assert body["hold_reason"]

    day = await read_committed(repository.get_day, plan_id, "2026-07-30")
    assert day.status == logic.STATUS_HELD


@pytest.mark.parametrize(
    "units,cartons",
    [
        (300, 30),   # heavy bags: carton count is fine
        (900, 15),   # small pouches: unit count is fine
        (600, 30),   # comfortably over both
    ],
)
async def test_a_day_over_either_threshold_ships(ops_client, plan_factory, units, cartons):
    """AND semantics: being short on only one axis must not hold the day."""
    await plan_factory()
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": units, "cartons": cartons}]},
    )
    r = await ops_client.post("/shipment/packing/2026-07-30/submit")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == logic.STATUS_SUBMITTED, r.json()


async def test_held_units_count_as_packed_but_not_shippable(
    auth_client, ops_client, plan_factory
):
    """The crux of requirement 9, and the subtlest thing in this feature.

    A held day's boxes physically exist, so the packer must not be told to pack
    them again — they count as packed. But they must not appear in a shipment
    until released, so they are not shippable. One number cannot express both.
    """
    await plan_factory()
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 400, "cartons": 20}]},
    )
    await ops_client.post("/shipment/packing/2026-07-30/submit")  # -> held

    r = await auth_client.get("/shipment/active")
    item = next(i for i in r.json()["items"] if i["asin"] == "B0AAA00001")

    assert item["packed"] == 400, "held units vanished from packed — would be packed twice"
    assert item["shippable"] == 0, "held units are shippable — the hold does nothing"
    assert item["remaining"] == 100, "remaining must subtract held units (500 planned - 400)"
    assert r.json()["held"]["units"] == 400


async def test_release_makes_a_held_day_shippable(
    auth_client, ops_client, plan_factory, read_committed
):
    """The threshold suggests; the owner decides. Force-ship must work."""
    plan = await plan_factory()
    plan_id = plan.id
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 400, "cartons": 20}]},
    )
    await ops_client.post("/shipment/packing/2026-07-30/submit")

    r = await auth_client.post("/shipment/packing/2026-07-30/release")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == logic.STATUS_SUBMITTED

    day = await read_committed(repository.get_day, plan_id, "2026-07-30")
    assert day.hold_reason is None, "the hold reason survived the release"

    r = await auth_client.get("/shipment/active")
    item = next(i for i in r.json()["items"] if i["asin"] == "B0AAA00001")
    assert item["shippable"] == 400, "released units are still not shippable"


async def test_ops_cannot_release_a_held_day(ops_client, plan_factory, read_committed):
    """Ops must not be able to overrule the hold — that is the owner's call."""
    plan = await plan_factory()
    plan_id = plan.id
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 400, "cartons": 20}]},
    )
    await ops_client.post("/shipment/packing/2026-07-30/submit")

    r = await ops_client.post("/shipment/packing/2026-07-30/release")
    assert r.status_code == 403

    day = await read_committed(repository.get_day, plan_id, "2026-07-30")
    assert day.status == logic.STATUS_HELD, "ops released a held day despite the 403"


async def test_two_held_days_combine_into_one_shippable_total(
    auth_client, ops_client, plan_factory
):
    """'We combine it with next day packing and then create a shipment.'

    No separate carry-over bookkeeping: releasing both days makes the same
    aggregation add them up.
    """
    await plan_factory()
    for pack_date in ("2026-07-30", "2026-07-31"):
        await ops_client.post(
            f"/shipment/packing/{pack_date}",
            json={"entries": [{"asin": "B0AAA00001", "units": 200, "cartons": 10}]},
        )
        r = await ops_client.post(f"/shipment/packing/{pack_date}/submit")
        assert r.json()["status"] == logic.STATUS_HELD

    r = await auth_client.get("/shipment/active")
    item = next(i for i in r.json()["items"] if i["asin"] == "B0AAA00001")
    assert item["packed"] == 400
    assert item["shippable"] == 0

    for pack_date in ("2026-07-30", "2026-07-31"):
        await auth_client.post(f"/shipment/packing/{pack_date}/release")

    r = await auth_client.get("/shipment/active")
    item = next(i for i in r.json()["items"] if i["asin"] == "B0AAA00001")
    assert item["shippable"] == 400, "combined days did not aggregate"


# ─── Verified days are frozen ────────────────────────────────────────────────

async def test_a_verified_day_cannot_be_edited(
    auth_client, ops_client, plan_factory, read_committed
):
    """An invoice may already carry these numbers.

    Letting a later edit through would put the GST document and the warehouse
    record out of agreement, which is the kind of discrepancy that is discovered
    during an audit rather than in the app.
    """
    plan = await plan_factory()
    plan_id = plan.id
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 900, "cartons": 40}]},
    )
    await ops_client.post("/shipment/packing/2026-07-30/submit")
    r = await auth_client.post("/shipment/packing/2026-07-30/verify")
    assert r.status_code == 200, r.text

    r = await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 1, "cartons": 1}]},
    )
    assert r.status_code == 409, r.status_code

    day = await read_committed(repository.get_day, plan_id, "2026-07-30")
    assert day.total_units == 900, "a verified day was edited"


async def test_an_unsubmitted_day_cannot_be_verified(auth_client, ops_client, plan_factory):
    """Verifying is approving ops' work — there must be work to approve."""
    await plan_factory()
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 900, "cartons": 40}]},
    )
    r = await auth_client.post("/shipment/packing/2026-07-30/verify")
    assert r.status_code == 400, r.status_code


async def test_submitting_an_empty_day_is_rejected(ops_client, plan_factory):
    """An empty day is not 'held', it is simply not done."""
    await plan_factory()
    r = await ops_client.post("/shipment/packing/2026-07-30/submit")
    assert r.status_code == 400, r.status_code


# ─── The packing screen's own view ───────────────────────────────────────────

async def test_packing_view_lists_every_planned_sku(ops_client, plan_factory):
    """The packer works down this list, so untouched SKUs must appear.

    Items with a zero plan are excluded — there is nothing to pack.
    """
    await plan_factory()
    r = await ops_client.get("/shipment/packing/2026-07-30")
    assert r.status_code == 200, r.text
    body = r.json()

    asins = [i["asin"] for i in body["items"]]
    assert asins == ["B0AAA00002", "B0AAA00001", "B0BBB00001"], asins
    assert all(i["units"] == 0 for i in body["items"])
    assert body["status"] == logic.STATUS_OPEN


async def test_packing_view_remaining_excludes_only_other_days(ops_client, plan_factory):
    """Today's own entry must not shrink today's target as it is typed.

    Otherwise the number the packer is working toward moves while they type,
    which reads as the app losing their input.
    """
    await plan_factory()
    await ops_client.post(
        "/shipment/packing/2026-07-29",
        json={"entries": [{"asin": "B0AAA00001", "units": 200, "cartons": 10}]},
    )
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 150, "cartons": 8}]},
    )

    r = await ops_client.get("/shipment/packing/2026-07-30")
    row = next(i for i in r.json()["items"] if i["asin"] == "B0AAA00001")
    assert row["packed_before"] == 200, "prior days not counted"
    assert row["remaining"] == 300, "today's own units were subtracted (500 - 200)"
    assert row["units"] == 150, "today's entry not shown for editing"


async def test_a_bad_date_is_rejected(ops_client, plan_factory):
    """The date is a path segment, so it must be validated, not trusted."""
    await plan_factory()
    for bad in ("not-a-date", "2026-13-45", "30-07-2026"):
        r = await ops_client.get(f"/shipment/packing/{bad}")
        assert r.status_code == 400, f"{bad} -> {r.status_code}"


async def test_endpoints_report_no_active_plan_rather_than_500(auth_client, ops_client):
    """A fresh install has no plan; every route must say so cleanly."""
    r = await ops_client.get("/shipment/active")
    assert r.status_code == 200
    assert r.json()["plan"] is None

    r = await ops_client.get("/shipment/packing/2026-07-30")
    assert r.status_code == 404

    r = await ops_client.post(
        "/shipment/packing/2026-07-30", json={"entries": []}
    )
    assert r.status_code == 404

    r = await auth_client.post("/shipment/items", json={"items": []})
    assert r.status_code == 404


# ─── Thresholds and deletion ─────────────────────────────────────────────────

async def test_admin_can_change_the_thresholds_and_they_take_effect(
    auth_client, ops_client, plan_factory
):
    """A threshold change must alter the hold decision, not just the stored row."""
    plan = await plan_factory()
    r = await auth_client.patch(
        f"/shipment/plan/{plan.id}/thresholds",
        json={"min_cartons": 10, "min_units": 100},
    )
    assert r.status_code == 200, r.text

    # 400 units / 20 cartons was held at the defaults; now it clears both.
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 400, "cartons": 20}]},
    )
    r = await ops_client.post("/shipment/packing/2026-07-30/submit")
    assert r.json()["status"] == logic.STATUS_SUBMITTED


async def test_deleting_a_plan_removes_its_items_and_packing(
    auth_client, ops_client, plan_factory, count_rows
):
    """Cascade must be real, or deleted plans leave orphan packing rows behind."""
    plan = await plan_factory()
    await ops_client.post(
        "/shipment/packing/2026-07-30",
        json={"entries": [{"asin": "B0AAA00001", "units": 100, "cartons": 5}]},
    )

    r = await auth_client.delete(f"/shipment/plan/{plan.id}")
    assert r.status_code == 200, r.text

    for model in (ShipmentPlanItem, ShipmentPackingDay, ShipmentPackingEntry):
        assert await count_rows(model) == 0, (
            f"{model.__name__} rows survived the plan delete"
        )


async def test_deleting_a_missing_plan_is_a_404(auth_client):
    r = await auth_client.delete("/shipment/plan/999999")
    assert r.status_code == 404


# ─── The retired JSON endpoints ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/shipment/last"),
        ("post", "/shipment/save"),
        ("delete", "/shipment/clear"),
        ("get", "/shipment/download-packing-plan"),
        ("get", "/shipment/download-shipment-file"),
    ],
)
async def test_the_json_blob_endpoints_are_gone(auth_client, method, path):
    """`POST /save` overwrote the whole plan — that is the bug being removed.

    Pinned so nobody reintroduces a save-everything endpoint alongside the new
    split writes, which would quietly restore the clobbering.
    """
    r = await getattr(auth_client, method)(path)
    assert r.status_code == 404, f"{method.upper()} {path} still exists ({r.status_code})"
