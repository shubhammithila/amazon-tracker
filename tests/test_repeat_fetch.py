import gzip
from datetime import date

import pytest

from app.repeat import fetch

pytestmark = pytest.mark.regression


def test_split_days_never_exceeds_30_and_covers_every_day_once():
    parts = fetch.split_days(date(2026, 1, 1), date(2026, 3, 15))
    assert all((b - a).days + 1 <= 30 for a, b in parts)
    days = [a.toordinal() + i for a, b in parts for i in range((b - a).days + 1)]
    assert days == list(range(date(2026, 1, 1).toordinal(), date(2026, 3, 15).toordinal() + 1))


async def test_fetch_creates_polls_and_parses_a_gzipped_tsv(monkeypatch):
    calls = []
    tsv = "amazon-order-id\tsku\n171-1\t1kg cs FBA\n".encode()

    async def fake_post(path, body=None, client=None, method="POST"):
        calls.append(("POST", path, body))
        return {"reportId": "R1"}

    states = iter([{"processingStatus": "IN_QUEUE"},
                   {"processingStatus": "DONE", "reportDocumentId": "D1"}])

    async def fake_get(path, params=None, client=None):
        calls.append(("GET", path, params))
        if path.endswith("/reports/R1"):
            return next(states)
        return {"url": "https://s3/doc", "compressionAlgorithm": "GZIP"}

    class Resp:
        content = gzip.compress(tsv)

    class Client:
        """The S3 download: called with the URL alone, never with SP-API headers."""
        async def get(self, url, **kwargs):
            assert not kwargs
            return Resp()

    async def no_sleep(_):
        pass

    monkeypatch.setattr(fetch.spapi, "_post", fake_post)
    monkeypatch.setattr(fetch.spapi, "_get", fake_get)
    rows = await fetch.fetch_rows(date(2026, 9, 1), date(2026, 9, 7), client=Client(), sleep=no_sleep)
    assert rows == [{"amazon-order-id": "171-1", "sku": "1kg cs FBA"}]
    body = calls[0][2]
    assert body["reportType"] == "GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL"
    assert body["dataStartTime"] == "2026-08-31T18:30Z"      # midnight IST starting 1 Sep
    assert body["dataEndTime"] == "2026-09-07T18:30Z"        # midnight IST ending 7 Sep


async def test_a_window_over_30_days_is_refused_before_any_call():
    with pytest.raises(ValueError):
        await fetch.fetch_rows(date(2026, 1, 1), date(2026, 2, 15), client=None)


async def test_a_FATAL_report_raises(monkeypatch):
    async def fake_post(*a, **k):
        return {"reportId": "R1"}

    async def fake_get(*a, **k):
        return {"processingStatus": "FATAL"}

    async def no_sleep(_):
        pass

    monkeypatch.setattr(fetch.spapi, "_post", fake_post)
    monkeypatch.setattr(fetch.spapi, "_get", fake_get)
    with pytest.raises(fetch.ReportFailed):
        await fetch.fetch_rows(date(2026, 9, 1), date(2026, 9, 2), client=object(), sleep=no_sleep)


async def test_a_QUOTA_refusal_waits_and_retries_instead_of_failing(monkeypatch):
    """8 Oct: "You exceeded your quota for the requested resource" ended a run outright."""
    tries, waits = [], []

    async def fake_post(*a, **k):
        tries.append(1)
        if len(tries) < 3:
            raise fetch.spapi.SpApiError(
                "Amazon said: You exceeded your quota for the requested resource.", status=429)
        return {"reportId": "R1"}

    async def fake_get(path, *a, **k):
        if path.endswith("/reports/R1"):
            return {"processingStatus": "DONE", "reportDocumentId": "D1"}
        return {"url": "https://s3/doc"}

    class Client:
        async def get(self, url, **kw):
            class R:
                content = b"amazon-order-id\n171-1\n"
            return R()

    async def sleep(s):
        waits.append(s)

    monkeypatch.setattr(fetch.spapi, "_post", fake_post)
    monkeypatch.setattr(fetch.spapi, "_get", fake_get)
    rows = await fetch.fetch_rows(date(2026, 9, 1), date(2026, 9, 2), client=Client(), sleep=sleep)
    assert rows == [{"amazon-order-id": "171-1"}] and len(tries) == 3
    assert waits == list(fetch.QUOTA_WAITS[:2])


async def test_a_quota_refusal_that_never_clears_still_raises(monkeypatch):
    async def fake_post(*a, **k):
        raise fetch.spapi.SpApiError("Amazon said: You exceeded your quota", status=429)

    async def sleep(_):
        pass
    monkeypatch.setattr(fetch.spapi, "_post", fake_post)
    with pytest.raises(fetch.spapi.SpApiError):
        await fetch.fetch_rows(date(2026, 9, 1), date(2026, 9, 2), client=object(), sleep=sleep)


async def test_any_other_error_is_NOT_retried(monkeypatch):
    tries = []

    async def fake_post(*a, **k):
        tries.append(1)
        raise fetch.spapi.SpApiError("Amazon said: Access denied", status=403)

    async def sleep(_):
        raise AssertionError("must not wait on a non-quota error")
    monkeypatch.setattr(fetch.spapi, "_post", fake_post)
    with pytest.raises(fetch.spapi.SpApiError):
        await fetch.fetch_rows(date(2026, 9, 1), date(2026, 9, 2), client=object(), sleep=sleep)
    assert len(tries) == 1
