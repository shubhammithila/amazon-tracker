"""Named logins and per-area permissions.

The app began with two shared passwords, and which one you typed decided everything.
That cannot express the case the owner actually has — someone who prints the packed
sheet for accounts but must not see projections or purchase costs — so permission is now
a set of areas granted per user.

Most of this file is about the failure modes, because the happy path of "tick a box, see
a tab" is the easy half:

* **Revocation is immediate.** The grant is read from the database on every request
  rather than baked into the week-long session cookie. If it were in the cookie,
  "I removed his access" would be untrue for up to seven days.
* **The last administrator cannot be removed.** There is no password-reset email and no
  admin console in this app; an account nobody can sign into as admin is recovered with
  a sqlite3 shell on the EC2 box.
* **The shared passwords still work.** Deliberately, and tested — they are the recovery
  path if anything is wrong with the users table after a deploy.
* **A password is shown exactly once** and is never stored in readable form.
* **The login form leaks nothing** about which usernames exist.
"""
import pytest

from app import credentials, permissions
from app.models import User
from app.routers.auth import SESSION_COOKIE, serializer

pytestmark = pytest.mark.regression


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _make_admin(client, db):
    """An administrator account, created directly so tests can then use the API.

    Direct insert rather than through the API because the very first admin has to exist
    before any /admin/users call can be authorised — the same bootstrap problem the
    shared APP_PASSWORD solves in production.
    """
    from app import users as users_repo

    user, password = await users_repo.create(
        db, username="owner", full_name="The Owner", is_admin=True, created_by="test"
    )
    return user, password


async def _signed_in(client, username, password):
    r = await client.post("/login", data={"username": username, "password": password})
    assert r.status_code == 303, r.text
    return r


def _cookie_for(username):
    """A validly signed cookie for a named user, minted the way /login does."""
    return serializer.dumps({"authenticated": True, "username": username, "role": "admin"})


# ─── The areas themselves ────────────────────────────────────────────────────

def test_an_unknown_area_is_denied():
    """Deny by default. A new area is invisible to everyone until granted.

    The opposite default would widen access silently on deploy, which is the worst
    possible direction for this kind of mistake.
    """
    assert permissions.has("invoice", "some_new_area") is False
    assert permissions.has("", permissions.INVOICE) is False


def test_a_removed_area_stops_granting_anything():
    """A key no longer in AREAS is dropped on read, not honoured.

    Stored grants outlive the code that defined them; a stale key must not keep working.
    """
    assert "retired_area" not in permissions.parse("invoice,retired_area")
    assert permissions.INVOICE in permissions.parse("invoice,retired_area")


def test_shipment_implies_packing():
    """The owner supervises packing, so gating his view of it behind a second tick
    would be a papercut with no security value."""
    granted = permissions.parse(permissions.SHIPMENT)
    assert permissions.PACKING in granted


def test_implication_is_applied_on_read_not_stored():
    """So changing what an area implies takes effect for existing users immediately,
    with no data migration. Same reasoning as JOINing the shipment sort priority."""
    stored = permissions.serialise([permissions.SHIPMENT])
    assert "packing" not in stored, f"the implication was baked into storage: {stored!r}"
    assert permissions.PACKING in permissions.parse(stored)


def test_an_admin_reaches_every_area_regardless_of_ticks():
    """The owner must not be able to lock himself out of a tab by mis-ticking.

    A self-inflicted lockout on a single-owner app has no recovery path short of a
    sqlite3 shell on the server.
    """
    for key, _label, _help in permissions.AREAS:
        assert permissions.has("", key, is_admin=True), key


def test_serialise_drops_junk_rather_than_raising():
    """The grant arrives from an HTTP form; a stale checkbox name must not 500 a save."""
    assert permissions.serialise(["invoice", "not-a-real-area", ""]) == "invoice"
    assert permissions.serialise(None) == ""


def test_serialise_is_canonical_so_equivalent_grants_compare_equal():
    """Order-independent, so "did this actually change?" is a string comparison."""
    assert permissions.serialise(["packing", "invoice"]) == \
           permissions.serialise(["invoice", "packing"])


# ─── Passwords ───────────────────────────────────────────────────────────────

def test_a_password_hash_is_not_the_password():
    stored = credentials.hash_password("correct horse")
    assert "correct horse" not in stored
    assert credentials.verify_password("correct horse", stored)
    assert not credentials.verify_password("Correct Horse", stored)


def test_the_same_password_hashes_differently_every_time():
    """A per-hash salt, so a stolen database cannot be scanned for users who share a
    password."""
    assert credentials.hash_password("same") != credentials.hash_password("same")


@pytest.mark.parametrize("junk", [None, "", "garbage", "scrypt$bad", "a$b$c$d$e$f"])
def test_verifying_a_malformed_hash_fails_rather_than_raising(junk):
    """A truncated or hand-edited hash column must fail that login, not 500 the login
    page for everybody."""
    assert credentials.verify_password("anything", junk) is False


def test_the_hash_records_its_own_cost_parameters():
    """So raising the cost later does not lock out every existing user at once —
    old hashes keep verifying with the parameters they were made with."""
    stored = credentials.hash_password("x")
    assert stored.startswith("scrypt$")
    assert len(stored.split("$")) == 6


def test_a_generated_password_avoids_ambiguous_characters():
    """These get written on paper and read across a warehouse. 0/O and 1/l/I are where
    that goes wrong, and a failed login says nothing about which character was misread.
    """
    for _ in range(40):
        assert not (set(credentials.generate_password()) & set("0O1lI5S"))


def test_a_generated_password_is_long_enough_to_be_worth_having():
    for _ in range(10):
        assert len(credentials.generate_password()) >= 14


def test_generated_passwords_do_not_repeat():
    assert len({credentials.generate_password() for _ in range(50)}) == 50


# ─── Usernames ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,why", [
    ("", "empty"),
    ("ab", "too short"),
    ("x" * 40, "too long"),
    ("ravi kumar", "contains a space"),
    ("ravi!", "contains punctuation"),
    (".ravi", "starts with a separator"),
    ("ravi@example.com", "an email, not a username"),
])
def test_bad_usernames_are_refused_with_a_reason(raw, why):
    """Narrow on purpose: a username reaches a URL, a log line and eventually a shell
    command, so anything needing quoting is refused rather than escaped."""
    assert credentials.username_error(raw), f"{raw!r} was accepted ({why})"


def test_a_username_is_suggested_from_a_persons_name():
    assert credentials.suggest_username("Ravi Kumar") == "ravi.kumar"


def test_a_taken_username_gets_a_random_suffix_not_a_number():
    """`ravi.kumar2` invites "who is ravi.kumar1 and do they still work here", and the
    answer is usually that nobody remembers."""
    suggested = credentials.suggest_username("Ravi Kumar", {"ravi.kumar"})
    assert suggested != "ravi.kumar"
    assert not suggested.endswith("2")
    assert credentials.username_error(suggested) is None


def test_a_suggested_username_is_always_valid():
    """It is offered to the owner as a working default, so it must pass validation —
    including for names that are entirely punctuation or far too long."""
    for name in ("A", "!!!", "Ravi", "x" * 80, "श्री", "", "  "):
        assert credentials.username_error(credentials.suggest_username(name)) is None, name


def test_usernames_are_case_insensitive():
    """Two people who believe they have separate accounts and share one is a confusing
    bug, and worse when one of them has fewer permissions."""
    assert credentials.normalise_username("  Ravi.Kumar ") == "ravi.kumar"


# ─── Creating accounts over HTTP ─────────────────────────────────────────────

async def test_an_admin_can_create_a_login_and_is_shown_the_password_once(
    auth_client, db_schema
):
    """The shared APP_PASSWORD session is admin, which is what bootstraps the first
    account — there has to be a way in before any account exists."""
    r = await auth_client.post(
        "/admin/users",
        json={"full_name": "Ravi Kumar", "preset": "packer"},
    )
    assert r.status_code == 201, r.text
    body = r.json()

    assert body["user"]["username"] == "ravi.kumar"
    assert body["password"], "no password was returned, so the owner has nothing to give"
    assert body["user"]["areas"] == ["packing"]
    assert body["user"]["never_signed_in"] is True


async def test_the_password_is_never_returned_again(auth_client, db_schema):
    """Shown once and unrecoverable. "Just show it again" is the obvious-looking
    feature request and it is the one thing the hash exists to prevent."""
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Ravi Kumar", "preset": "packer"}
    )).json()
    password = created["password"]

    listing = (await auth_client.get("/admin/users")).json()
    assert password not in r_text(listing), "the password came back in the user list"
    for user in listing["users"]:
        assert "password" not in user
        assert "password_hash" not in user


def r_text(payload) -> str:
    import json

    return json.dumps(payload)


async def test_a_hash_never_reaches_the_browser(auth_client, db_schema):
    """A hash in a JSON response is a hash in the network log and in any error
    reporter the page ever gains."""
    await auth_client.post("/admin/users", json={"full_name": "Ravi Kumar"})
    listing = (await auth_client.get("/admin/users")).json()
    assert "scrypt$" not in r_text(listing)


async def test_a_created_user_can_sign_in(auth_client, client, db_schema):
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Ravi Kumar", "preset": "packer"}
    )).json()

    r = await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )
    assert r.status_code == 303, r.text
    # Sent to the one screen they can actually use, not to a 403 on the dashboard.
    assert r.headers["location"] == "/ops-page"


async def test_a_duplicate_username_is_refused(auth_client, db_schema):
    await auth_client.post("/admin/users", json={"username": "ravi", "full_name": "R"})
    r = await auth_client.post("/admin/users", json={"username": "ravi", "full_name": "R2"})
    assert r.status_code == 400
    assert "taken" in r.json()["error"].lower()


async def test_a_preset_is_expanded_server_side(auth_client, db_schema):
    """Not trusted from the client, so the stored grant always matches what
    app/permissions.py says the preset means."""
    r = await auth_client.post(
        "/admin/users",
        json={"full_name": "Accounts Person", "preset": "accounts", "areas": ["dashboard"]},
    )
    assert r.status_code == 201, r.text
    # `areas` in the body is ignored when a known preset is named.
    assert set(r.json()["user"]["areas"]) == {"invoice", "packing"}


# ─── Permissions actually gate the pages ─────────────────────────────────────

async def test_a_packer_cannot_open_the_owners_pages(auth_client, client, db_schema):
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Ravi Kumar", "preset": "packer"}
    )).json()
    await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )

    for page in ("/", "/invoice-page", "/churn-page", "/projections-page", "/shipment-page"):
        r = await client.get(page)
        assert r.status_code == 403, f"a packer opened {page} ({r.status_code})"


async def test_a_packer_can_open_the_packing_screen(auth_client, client, db_schema):
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Ravi Kumar", "preset": "packer"}
    )).json()
    await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )
    assert (await client.get("/ops-page")).status_code == 200


async def test_the_accounts_preset_sees_invoices_but_not_projections(
    auth_client, client, db_schema
):
    """The case that motivated per-area permissions in the first place."""
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Accounts Person", "preset": "accounts"}
    )).json()
    await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )

    assert (await client.get("/invoice-page")).status_code == 200
    assert (await client.get("/projections-page")).status_code == 403
    assert (await client.get("/shipment-page")).status_code == 403


async def test_a_user_with_no_areas_lands_somewhere_that_explains_itself(
    auth_client, client, db_schema
):
    """Reachable when the owner creates an account and ticks nothing. A 403 loop or a
    bounce to /login reads as a broken account, and the response is to retry the
    password repeatedly."""
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Nobody", "areas": []}
    )).json()
    r = await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )
    assert r.headers["location"] == "/no-access"

    page = await client.get("/no-access")
    assert page.status_code == 200
    assert "no sections" in page.text.lower()


async def test_the_nav_hides_tabs_the_user_cannot_open(auth_client, client, db_schema):
    """A link that answers 403 is how an app earns "it's broken" — the user cannot tell
    a permission boundary from a bug."""
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Accounts Person", "preset": "accounts"}
    )).json()
    await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )

    page = await client.get("/invoice-page")
    assert page.status_code == 200
    assert 'href="/invoice-page"' in page.text, "their own tab is missing"
    for hidden in ('href="/projections-page"', 'href="/churn-page"', 'href="/shipment-page"'):
        assert hidden not in page.text, f"the nav offers {hidden} to a user who gets 403"


async def test_a_non_admin_cannot_reach_the_users_screen(auth_client, client, db_schema):
    """Otherwise a packer could grant himself the rest."""
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Ravi Kumar", "preset": "packer"}
    )).json()
    await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )

    assert (await client.get("/users-page")).status_code == 403
    assert (await client.get("/admin/users")).status_code == 403
    r = await client.post("/admin/users", json={"full_name": "Sneaky", "is_admin": True})
    assert r.status_code == 403, "a non-admin created an account"


async def test_the_users_page_shows_its_own_nav_tab(auth_client, db_schema):
    """Found in a browser, not by a test: the panel had no Users link of its own.

    Its route passed `grant=None`, and nav.html gates the Users tab on a real grant — so
    the one screen you navigate FROM had no way back to itself and was reachable only by
    typing the URL. Every other page links to itself, so this was invisible in review.
    """
    page = await auth_client.get("/users-page")
    assert page.status_code == 200
    assert 'href="/users-page"' in page.text, "the Users page does not link to itself"
    assert 'class="active"' in page.text, "no tab is highlighted on the Users page"


def test_the_create_form_ticks_the_boxes_its_dropdown_claims():
    """The trap the ordering bug would have set.

    renderPresets() called applyPreset() before renderCreateAreas() had built the boxes,
    so the form read "Packer" above six EMPTY tick boxes — and pressing Create would have
    made an account with no access at all, which looks like a broken login rather than a
    mis-set permission.

    Asserted on the source because the bug was one of call order: applyPreset must run
    after the boxes exist, and must not be called from inside renderPresets.
    """
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "templates" / "users.html"
    ).read_text(encoding="utf-8")

    # Comments are blanked first. The comment ABOVE these calls explains the ordering
    # rule by naming the two functions in the wrong order on purpose — scanning the raw
    # text matches the prose and fails on the documentation of the rule it enforces.
    # Same trap the ops.html sort guard hit.
    body = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    body = re.sub(r"//[^\n]*", " ", body)

    render_presets = body[body.index("function renderPresets()"):]
    render_presets = render_presets[:render_presets.index("\n}")]
    assert "applyPreset()" not in render_presets, (
        "renderPresets() calls applyPreset() again — that runs before the tick boxes "
        "exist, leaving the dropdown claiming a preset it did not apply"
    )

    # And load() must apply it AFTER building them.
    load_body = body[body.index("async function load()"):]
    load_body = load_body[:load_body.index("\nfunction renderPresets")]
    order = [
        load_body.index("renderCreateAreas()"),
        load_body.index("applyPreset()"),
    ]
    assert order == sorted(order), (
        "applyPreset() runs before renderCreateAreas() in load(), so there are no boxes "
        "to tick yet"
    )


async def test_the_nav_shows_the_users_tab_only_to_an_admin(auth_client, client, db_schema):
    created = (await auth_client.post(
        "/admin/users",
        json={"full_name": "Second Owner", "preset": "owner", "is_admin": True},
    )).json()
    await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )
    assert 'href="/users-page"' in (await client.get("/")).text


# ─── Revocation must be immediate ────────────────────────────────────────────

async def test_revoking_an_area_takes_effect_on_the_next_request(
    auth_client, client, db_schema
):
    """The reason the grant is read from the database rather than the cookie.

    A week-long session that kept its permissions would make "I removed his access"
    untrue for up to seven days — which is the entire point of the feature.
    """
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Accounts Person", "preset": "accounts"}
    )).json()
    await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )
    assert (await client.get("/invoice-page")).status_code == 200

    r = await auth_client.patch(
        f"/admin/users/{created['user']['id']}", json={"areas": ["packing"]}
    )
    assert r.status_code == 200, r.text

    # Same session, same cookie, no re-login.
    assert (await client.get("/invoice-page")).status_code == 403, (
        "the old permissions survived in the session cookie, so revoking access does "
        "nothing until it expires"
    )


async def test_disabling_an_account_ends_the_session_immediately(
    auth_client, client, db_schema
):
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Ravi Kumar", "preset": "accounts"}
    )).json()
    await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )
    assert (await client.get("/invoice-page")).status_code == 200

    await auth_client.patch(
        f"/admin/users/{created['user']['id']}", json={"is_active": False}
    )
    r = await client.get("/invoice-page")
    assert r.status_code == 303, "a disabled account kept working from its live session"


async def test_disabling_a_packer_ends_their_session_on_the_packing_screen(
    auth_client, client, db_schema
):
    """Found on PRODUCTION by testing the loop, not by reading the code.

    /ops-page was on `require_ops_or_admin`, which reads only the cookie — so a named
    account that had been disabled kept the packing screen for up to a week, while every
    other page cut it off immediately. The most privileged-feeling revocation ("this
    person no longer works here") was the one that did not take effect.

    Asserted separately from the other pages precisely because this route is the
    exception: it deliberately keeps a looser guard for the shared password, and that is
    exactly how the gap opened.
    """
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Ravi Kumar", "preset": "packer"}
    )).json()
    await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )
    assert (await client.get("/ops-page")).status_code == 200

    await auth_client.patch(
        f"/admin/users/{created['user']['id']}", json={"is_active": False}
    )
    assert (await client.get("/ops-page")).status_code == 303, (
        "a disabled packer still has the packing screen from their live session"
    )


async def test_revoking_the_packing_area_closes_the_packing_screen(
    auth_client, client, db_schema
):
    """The same route, the narrower case: still enabled, but no longer a packer."""
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Ravi Kumar", "preset": "packer"}
    )).json()
    await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )
    assert (await client.get("/ops-page")).status_code == 200

    await auth_client.patch(
        f"/admin/users/{created['user']['id']}", json={"areas": ["invoice"]}
    )
    assert (await client.get("/ops-page")).status_code == 403


async def test_the_shared_packing_password_still_opens_the_packing_screen(
    client, db_schema
):
    """The reason that route keeps a looser guard than the rest.

    OPS_PASSWORD must work even if the users table is missing or a migration has not run —
    the warehouse depends on this screen daily. So a shared-password session is accepted
    on the cookie alone; only NAMED accounts are re-checked against the database.
    """
    r = await client.post("/login", data={"password": "test-ops-password"})
    assert r.status_code == 303
    assert (await client.get("/ops-page")).status_code == 200


async def test_a_disabled_account_cannot_sign_in(auth_client, client, db_schema):
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Ravi Kumar", "preset": "packer"}
    )).json()
    await auth_client.patch(
        f"/admin/users/{created['user']['id']}", json={"is_active": False}
    )

    r = await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )
    assert r.status_code == 401


async def test_a_deleted_account_cannot_keep_using_its_session(
    auth_client, client, db_schema
):
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Ravi Kumar", "preset": "accounts"}
    )).json()
    await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )
    await auth_client.delete(f"/admin/users/{created['user']['id']}")

    assert (await client.get("/invoice-page")).status_code == 303


# ─── The last administrator ──────────────────────────────────────────────────

async def test_the_only_admin_cannot_be_demoted(auth_client, db, db_schema):
    """No password-reset email, no console. An app with no admin is recovered by
    editing the database by hand on the server."""
    user, _password = await _make_admin(None, db)

    r = await auth_client.patch(f"/admin/users/{user.id}", json={"is_admin": False})
    assert r.status_code == 409, r.text
    assert "only administrator" in r.json()["error"].lower()


async def test_the_only_admin_cannot_be_disabled(auth_client, db, db_schema):
    user, _password = await _make_admin(None, db)
    r = await auth_client.patch(f"/admin/users/{user.id}", json={"is_active": False})
    assert r.status_code == 409, r.text


async def test_the_only_admin_cannot_be_deleted(auth_client, db, db_schema):
    user, _password = await _make_admin(None, db)
    r = await auth_client.delete(f"/admin/users/{user.id}")
    assert r.status_code == 409, r.text


async def test_an_admin_can_be_demoted_once_another_exists(auth_client, db, db_schema):
    """The guard must be narrow, or the first admin is permanent."""
    first, _ = await _make_admin(None, db)
    second = (await auth_client.post(
        "/admin/users", json={"full_name": "Second Owner", "is_admin": True}
    )).json()
    assert second["user"]["is_admin"] is True

    r = await auth_client.patch(f"/admin/users/{first.id}", json={"is_admin": False})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["is_admin"] is False


# ─── Password reset ──────────────────────────────────────────────────────────

async def test_resetting_a_password_invalidates_the_old_one(
    auth_client, client, db_schema
):
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Ravi Kumar", "preset": "packer"}
    )).json()
    username = created["user"]["username"]

    reset = await auth_client.post(f"/admin/users/{created['user']['id']}/password")
    assert reset.status_code == 200, reset.text
    new_password = reset.json()["password"]
    assert new_password != created["password"]

    assert (await client.post(
        "/login", data={"username": username, "password": created["password"]}
    )).status_code == 401, "the old password still works"
    assert (await client.post(
        "/login", data={"username": username, "password": new_password}
    )).status_code == 303


# ─── The shared passwords remain the recovery path ───────────────────────────

async def test_the_shared_owner_password_still_signs_in(client, db_schema):
    """Deliberate. Removing it in the same change that adds this table would mean that
    if anything is wrong with the table on the EC2 box, nobody can sign in at all — on
    an app with no password reset and no console.
    """
    r = await client.post("/login", data={"password": "test-password"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"


async def test_the_shared_ops_password_still_signs_in(client, db_schema):
    r = await client.post("/login", data={"password": "test-ops-password"})
    assert r.status_code == 303
    assert r.headers["location"] == "/ops-page"


async def test_the_shared_owner_password_still_reaches_every_area(auth_client, db_schema):
    for page in ("/", "/invoice-page", "/churn-page", "/projections-page",
                 "/shipment-page", "/ops-page", "/users-page"):
        assert (await auth_client.get(page)).status_code == 200, page


async def test_a_pre_roles_cookie_is_still_admin(client, session_cookie, db_schema):
    """Every session issued before roles existed is exactly {"authenticated": True}.

    Treating an absent role as admin is what keeps those sessions working. Privilege can
    only ever be REDUCED by an explicit role or username, never escalated, because
    forging either needs the signing key.
    """
    client.cookies.set(SESSION_COOKIE, session_cookie)
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/users-page")).status_code == 200


async def test_the_panel_says_the_shared_password_is_still_live(auth_client, db_schema):
    """The natural assumption after creating named logins is that they replaced the
    shared password. They did not, and anyone who knows it is a full admin."""
    body = (await auth_client.get("/admin/users")).json()
    assert body["shared_password_active"] is True


# ─── The login form gives nothing away ───────────────────────────────────────

async def test_signing_in_with_an_unknown_username_looks_like_a_wrong_password(
    auth_client, client, db_schema
):
    """Identical status and body, so the form cannot be used to enumerate usernames."""
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Ravi Kumar", "preset": "packer"}
    )).json()

    wrong_user = await client.post(
        "/login", data={"username": "does.not.exist", "password": "whatever"}
    )
    wrong_pass = await client.post(
        "/login", data={"username": created["user"]["username"], "password": "whatever"}
    )
    assert wrong_user.status_code == wrong_pass.status_code == 401
    assert wrong_user.text == wrong_pass.text, (
        "the two failures differ, so the form reveals which usernames exist"
    )


async def test_a_username_is_matched_case_insensitively(auth_client, client, db_schema):
    created = (await auth_client.post(
        "/admin/users", json={"username": "ravi.kumar", "full_name": "R", "preset": "packer"}
    )).json()
    r = await client.post(
        "/login", data={"username": "  RAVI.KUMAR ", "password": created["password"]}
    )
    assert r.status_code == 303, "a capitalised username was rejected"


async def test_the_login_page_only_offers_a_username_field_once_accounts_exist(
    auth_client, client, db_schema
):
    """Before then it is a puzzle: the only working credential is the shared password,
    and an empty username box invites the owner to invent one."""
    before = await client.get("/login")
    assert 'name="username"' not in before.text

    await auth_client.post("/admin/users", json={"full_name": "Ravi Kumar"})

    after = await client.get("/login")
    assert 'name="username"' in after.text


async def test_last_login_is_recorded(auth_client, client, db_schema):
    """So the panel can say "not signed in yet" honestly, rather than leaving the owner
    wondering whether the credentials ever arrived."""
    created = (await auth_client.post(
        "/admin/users", json={"full_name": "Ravi Kumar", "preset": "packer"}
    )).json()
    assert created["user"]["never_signed_in"] is True

    await client.post(
        "/login",
        data={"username": created["user"]["username"], "password": created["password"]},
    )
    listing = (await auth_client.get("/admin/users")).json()
    row = next(u for u in listing["users"] if u["id"] == created["user"]["id"])
    assert row["never_signed_in"] is False
    assert row["last_login_at"]
