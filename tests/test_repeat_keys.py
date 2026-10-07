import pytest

from app.repeat import keys

pytestmark = pytest.mark.regression


def test_the_key_ignores_case_and_surrounding_space():
    assert keys.customer_key(" AbC@marketplace.amazon.in ", "s") == \
        keys.customer_key("abc@marketplace.amazon.in", "s")


def test_a_different_salt_gives_a_different_key():
    assert keys.customer_key("a@x", "s1") != keys.customer_key("a@x", "s2")


def test_blank_email_has_no_key():
    assert keys.customer_key("", "s") is None and keys.customer_key(None, "s") is None


def test_the_key_never_contains_the_email():
    k = keys.customer_key("abc123@marketplace.amazon.in", "s")
    assert len(k) == 32 and "abc123" not in k and "@" not in k


async def test_the_salt_is_created_ONCE_and_then_reused(db):
    first = await keys.load_or_create_salt(db)
    second = await keys.load_or_create_salt(db)
    assert first == second and len(first) >= 32


async def test_the_salt_row_does_not_disturb_the_verdict_thresholds(db):
    from app.portfolio import logic, repository
    await keys.load_or_create_salt(db)
    assert await repository.load_settings(db) == logic.thresholds_or_default({})
