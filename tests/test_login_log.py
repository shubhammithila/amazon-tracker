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
