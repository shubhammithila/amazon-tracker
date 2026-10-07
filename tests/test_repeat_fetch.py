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
