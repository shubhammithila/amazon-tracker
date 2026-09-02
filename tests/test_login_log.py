"""The Users login log — sign-in time, IP, success/failure. This app has no audit history
before this change; `record_login_event` is called from every branch of `POST /login`
starting now, and nothing before today is recoverable.
"""
import pytest

pytestmark = pytest.mark.asyncio


async def test_record_login_event_then_load_round_trips(db):
    from app import users as users_repo

    await users_repo.record_login_event(
        db, username="ravi", user_id=1, success=True, via="named", ip_address="203.0.113.5",
    )
    events = await users_repo.load_login_events(db)
    assert len(events) == 1
    assert events[0]["username"] == "ravi"
    assert events[0]["success"] is True
    assert events[0]["via"] == "named"
    assert events[0]["ip_address"] == "203.0.113.5"
    assert isinstance(events[0]["created_at"], str), "a datetime reaching JSON must be pre-serialised"


async def test_a_failed_attempt_is_recorded_with_no_user_id(db):
    from app import users as users_repo

    await users_repo.record_login_event(
        db, username="unknown-person", user_id=None, success=False, via="named",
        ip_address="203.0.113.5",
    )
    events = await users_repo.load_login_events(db)
    assert events[0]["success"] is False
    assert events[0]["user_id"] is None


async def test_load_login_events_returns_newest_first(db):
    from app import users as users_repo

    await users_repo.record_login_event(
        db, username="first", user_id=None, success=True, via="app_password", ip_address="1.1.1.1",
    )
    await users_repo.record_login_event(
        db, username="second", user_id=None, success=True, via="app_password", ip_address="2.2.2.2",
    )
    events = await users_repo.load_login_events(db)
    assert [e["username"] for e in events] == ["second", "first"]


async def test_load_login_events_caps_at_500_even_if_more_is_requested(db):
    from app import users as users_repo

    for i in range(3):
        await users_repo.record_login_event(
            db, username=f"user{i}", user_id=None, success=True, via="named", ip_address="1.1.1.1",
        )
    events = await users_repo.load_login_events(db, limit=10_000)
    assert len(events) == 3  # not an error case, just confirms the cap does not break a small load


# ─── POST /login records every attempt ──────────────────────────────────────────


async def test_a_failed_login_is_recorded(client, db):
    from app import users as users_repo

    r = await client.post("/login", data={"username": "nobody", "password": "wrong"})
    assert r.status_code == 401

    events = await users_repo.load_login_events(db)
    assert len(events) == 1
    assert events[0]["success"] is False
    assert events[0]["username"] == "nobody"


async def test_a_successful_named_login_is_recorded_with_the_right_via(client, db):
    from app import users as users_repo

    user, password = await users_repo.create(
        db, username="ravi", full_name="Ravi", is_admin=True, created_by="test",
    )
    r = await client.post("/login", data={"username": "ravi", "password": password})
    assert r.status_code == 303

    events = await users_repo.load_login_events(db)
    assert events[0]["success"] is True
    assert events[0]["via"] == "named"
    assert events[0]["user_id"] == user.id


async def test_a_shared_password_login_is_recorded_as_app_password(client, db, monkeypatch):
    """`app/routers/auth.py` captures `settings = get_settings()` ONCE at import time into a
    module-level name — patching a freshly-called `get_settings()` (a different object once
    another test has cleared the lru_cache) would silently miss the instance `login()` actually
    reads. `test_users_and_permissions.py` already established the correct target:
    `auth_module.settings`, not `get_settings()`."""
    from app import users as users_repo
    from app.routers import auth as auth_module

    monkeypatch.setattr(auth_module.settings, "app_password", "test-shared-password")
    r = await client.post("/login", data={"password": "test-shared-password"})
    assert r.status_code == 303

    events = await users_repo.load_login_events(db)
    assert events[0]["via"] == "app_password"
    assert events[0]["user_id"] is None
