# Sponsored Brands mid-poll token expiry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the nightly Sponsored Brands report failing with `401 Invalid token` by refreshing the LWA access token *during* the report poll loop instead of binding it once, so a report that takes longer to generate than a token lives can still be polled to completion.

**Architecture:** One new helper, `poll_get`, in `app/portfolio/ads.py` (the module that already owns `_access_token`, `_headers` and the `_token` cache). It rebuilds auth headers per poll — cheap, because `_access_token` returns the cached token until near expiry — and on a `401` invalidates the cache, re-mints and retries the same GET, bounded. Both poll loops (`app/ads/reports.py:_poll_report` and `app/portfolio/ads.py`'s own) route through it. No migration, no schema change, no new dependency.

**Tech Stack:** Python 3.14, httpx (async), FastAPI, pytest (async).

## Global Constraints

- **The pre-signed S3 download must stay header-free.** Both modules comment on this: sending the bearer token to S3 leaks it to a different host. `poll_get` is for the ads host only and must never be used for the download call.
- **`_token` stays module-level and stays separate from `shipment.spapi`'s token cache.** Different client ids; a shared cache would hand an SP-API token to the ads host — the exact 401 `_access_token`'s docstring warns about.
- **SP-before-SB ordering is unchanged.** CLAUDE.md records it as load-bearing: it is why 482,578 Sponsored Products rows survived the night SB stored zero.
- `POLL_MAX = 135` and `POLL_INTERVAL = 20.0` are unchanged. 45 minutes of polling was never the problem.
- Each caller keeps its own error prose (`"Polling the targeting report failed: ..."` vs `"Polling the ad report failed: ..."`) and its own `FAILURE`/`CANCELLED`/no-url handling. `poll_get` returns the response; it does not interpret report state.
- Every new test must fail against current code. A test that passes both before and after proves nothing here.

---

## Task 1: `poll_get` in `app/portfolio/ads.py`

**Files:**
- Modify: `app/portfolio/ads.py` (add `poll_get` after `_headers`, which ends around line 205)
- Test: `tests/test_portfolio_ads.py`

**Interfaces:**
- Consumes: existing `_access_token(client)`, `_headers(token)`, `_token` (module-level `_Token` dataclass with `.value` and `.expires_at`), `AdsError`.
- Produces: `async def poll_get(client, url, *, force_refresh_attempts: int = 3) -> httpx.Response`. Task 2 imports this into `app/ads/reports.py`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_portfolio_ads.py`. These use the same `_Ads`/`_patch` fake-client pattern already in that file, extended so the fake can return a 401 on a chosen poll and count token mints:

```python
# ─── poll_get: a token that expires mid-poll must not kill the report ──────────


async def test_a_401_mid_poll_is_retried_with_a_fresh_token(monkeypatch):
    """**The bug this test exists to catch — Sponsored Brands failed every night for a week.**

    Measured on production: `POLL_MAX * POLL_INTERVAL` is 45 minutes of polling, on a token that
    lives 3600s and had already been spent for ~17 minutes by the preceding Sponsored Products
    reports. The token expired mid-poll, the next poll returned
    `401 {"message":"Unauthorized exception while handling 3P Request: Invalid token"}`, and the
    loop treated that as a fatal report failure — discarding a report Amazon had already produced.
    """
    from app.portfolio import ads

    mints = []
    polls = []

    class _Response:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text
            self.content = b""

        def json(self):
            return self._payload

    class _Client:
        async def post(self, url, content=None, headers=None):
            assert "auth/o2/token" in url
            mints.append(url)
            return _Response(200, {"access_token": f"tok-{len(mints)}", "expires_in": 3600})

        async def get(self, url, headers=None):
            polls.append(headers.get("Authorization"))
            # First poll 401s (token expired mid-poll), the retry succeeds.
            if len(polls) == 1:
                return _Response(401, text='{"message":"Invalid token"}')
            return _Response(200, {"status": "COMPLETED"})

    ads._token.value = ""          # start with no cached token
    ads._token.expires_at = 0.0
    monkeypatch.setattr(ads, "get_settings", _ads_settings())

    response = await ads.poll_get(_Client(), "https://ads.invalid/reporting/reports/rep-1")

    assert response.status_code == 200, "a 401 mid-poll was not retried"
    assert len(mints) == 2, "the token was not re-minted after the 401"
    assert polls[0] != polls[1], "the retry reused the same expired token"


async def test_a_permanent_401_fails_after_the_retry_bound(monkeypatch):
    """A genuinely revoked refresh token must surface as an error, not loop for 45 minutes."""
    from app.portfolio import ads

    mints = []

    class _Response:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text
            self.content = b""

        def json(self):
            return self._payload

    class _Client:
        async def post(self, url, content=None, headers=None):
            mints.append(url)
            return _Response(200, {"access_token": f"tok-{len(mints)}", "expires_in": 3600})

        async def get(self, url, headers=None):
            return _Response(401, text='{"message":"Invalid token"}')

    ads._token.value = ""
    ads._token.expires_at = 0.0
    monkeypatch.setattr(ads, "get_settings", _ads_settings())

    response = await ads.poll_get(
        _Client(), "https://ads.invalid/reporting/reports/rep-1", force_refresh_attempts=3,
    )
    assert response.status_code == 401, "a permanent 401 should be returned, not raised"
    assert len(mints) <= 4, f"unbounded re-minting: {len(mints)} mints"


async def test_the_happy_path_mints_only_once(monkeypatch):
    """A report that completes without a 401 must NOT re-authenticate per poll. `_access_token`
    returns the cached token until near expiry, so rebuilding headers per call is cheap — but a
    mistake here would turn one mint into one mint PER POLL (135 LWA calls per report)."""
    from app.portfolio import ads

    mints = []

    class _Response:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text
            self.content = b""

        def json(self):
            return self._payload

    class _Client:
        async def post(self, url, content=None, headers=None):
            mints.append(url)
            return _Response(200, {"access_token": "tok", "expires_in": 3600})

        async def get(self, url, headers=None):
            return _Response(200, {"status": "COMPLETED"})

    ads._token.value = ""
    ads._token.expires_at = 0.0
    monkeypatch.setattr(ads, "get_settings", _ads_settings())

    client = _Client()
    for _ in range(5):
        await ads.poll_get(client, "https://ads.invalid/reporting/reports/rep-1")

    assert len(mints) == 1, f"the cached token was not reused: {len(mints)} mints for 5 polls"
```

These need a small settings helper. Add it near the top of the file's helper section (check first whether an equivalent already exists in this file and reuse it if so, rather than adding a duplicate):

```python
def _ads_settings():
    """A settings stub with ads credentials configured, for the poll_get tests."""
    class _S:
        ads_configured = True
        ads_refresh_token = "refresh"
        ads_client_id = "client"
        ads_client_secret = "secret"
        ads_profile_id = "profile"
        ads_endpoint = "https://ads.invalid"
    return lambda: _S()
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_portfolio_ads.py -q -p no:randomly -k "poll_get or mid_poll or retry_bound or mints_only_once"`
Expected: FAIL — `AttributeError: module 'app.portfolio.ads' has no attribute 'poll_get'`.

- [ ] **Step 3: Write `poll_get`**

In `app/portfolio/ads.py`, add immediately after the `_headers` function:

```python
async def poll_get(
    client: httpx.AsyncClient, url: str, *, force_refresh_attempts: int = 3,
) -> httpx.Response:
    """GET an ads URL with a token refreshed as needed, retrying a 401 with a fresh one.

    **The headers are rebuilt per call rather than bound once by the caller, and that is the
    whole point of this function.** `POLL_MAX * POLL_INTERVAL` is 45 minutes of polling, while an
    LWA access token lives 3600s — and by the time the Sponsored Brands report starts polling,
    the preceding Sponsored Products reports have already spent 17 of those minutes. Measured on
    production: a real SB report needed ~9 minutes of polling and the token expired underneath
    it, so every nightly run returned
    `401 {"message":"Unauthorized exception while handling 3P Request: Invalid token"}` and the
    poll loop discarded a report Amazon had already produced. Sponsored Brands was stale for a
    week and no window could be summed, so no bid rule could be previewed at all.

    **Calling `_access_token` per poll is cheap, not a stampede**: it returns the cached token
    until `expires_at - _TOKEN_SAFETY_MARGIN`, so only a genuinely near-expiry token costs an
    LWA round trip. A test pins that five polls mint once.

    **A 401 is retried, every other status is returned untouched.** The caller owns its own error
    prose and its own reading of `FAILURE`/`CANCELLED`/no-url — this function must not interpret
    report state, or the two callers' messages would drift into one.

    `force_refresh_attempts` bounds the retry so a genuinely revoked refresh token surfaces as a
    401 the caller can report, rather than looping for the full 45 minutes.

    NOT for the report download: that url is pre-signed and must be fetched with NO ads headers,
    or the bearer token leaks to S3. See both callers' download step.
    """
    response = None
    for attempt in range(force_refresh_attempts + 1):
        token = await _access_token(client)
        response = await client.get(url, headers=_headers(token))
        if response.status_code != 401:
            return response
        # The token died mid-poll. Drop it so the next `_access_token` mints a fresh one, and
        # try the SAME poll again — Amazon's report keeps generating regardless of our auth.
        logger.info(
            "ads: poll returned 401, re-minting the access token (attempt %d of %d)",
            attempt + 1, force_refresh_attempts,
        )
        _token.value = ""
        _token.expires_at = 0.0
    return response
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_portfolio_ads.py -q -p no:randomly`
Expected: all PASS, including the pre-existing tests in that file.

- [ ] **Step 5: Commit**

```bash
git add app/portfolio/ads.py tests/test_portfolio_ads.py
git commit -m "fix(ads): refresh the access token mid-poll, so a slow report is not lost to a 401"
```

---

## Task 2: Route both poll loops through `poll_get`

**Files:**
- Modify: `app/ads/reports.py` (`_poll_report`, the loop at ~line 327; and its import block at ~line 34)
- Modify: `app/portfolio/ads.py` (its own poll loop at ~line 410)
- Test: `tests/test_ads_sb.py`

**Interfaces:**
- Consumes: Task 1's `poll_get`.
- Produces: no new API. Both loops behave identically except that a mid-poll 401 now recovers.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ads_sb.py` — a source-level assertion plus a behavioural one. The source assertion matters because a runtime test on the Portfolio path would pass today for the wrong reason (its reports are simply fast enough that the token never expires):

```python
def test_both_poll_loops_go_through_poll_get():
    """**Asserted at SOURCE level, and that is deliberate.**

    A runtime test cannot distinguish "the Portfolio poll loop refreshes its token" from "the
    Portfolio poll loop is never slow enough to need to" — its economics reports finish in ~30s,
    which is why this bug hit Sponsored Brands first and sat latent in the other module. The
    property that actually matters is structural: neither loop may bind `headers=head` once and
    reuse it across `POLL_MAX` iterations.
    """
    import pathlib

    for path in ("app/ads/reports.py", "app/portfolio/ads.py"):
        source = pathlib.Path(path).read_text(encoding="utf-8")
        # Find the poll loop and confirm it does not carry a pre-bound header dict.
        assert "poll_get(" in source, f"{path} does not use poll_get for its report poll"
        loop_start = source.index("for attempt in range(POLL_MAX)")
        loop_body = source[loop_start:loop_start + 900]
        assert "headers=head" not in loop_body, (
            f"{path}'s poll loop still reuses a header dict bound before the loop — that is "
            "exactly the token-expiry bug that made Sponsored Brands fail every night"
        )
```

And the behavioural test, that a 401 mid-poll no longer kills an SB fetch:

```python
async def test_an_sb_report_survives_a_token_expiry_mid_poll(monkeypatch):
    """End to end on the Sponsored Brands path: the token dies on the third poll and the report
    still lands. Against the old code this raised
    'Polling the targeting report failed: ... Invalid token' and stored 0 rows."""
    from app.portfolio import ads as ads_mod
    from app.ads import reports

    mints, polls = [], []

    class _Response:
        def __init__(self, status_code, payload=None, text="", content=b""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text
            self.content = content

        def json(self):
            return self._payload

    rows = b'{"date":"2026-09-01","campaignId":"c1","keywordId":"k1","cost":1.5}\n'

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            if "auth/o2/token" in url:
                mints.append(url)
                return _Response(200, {"access_token": f"tok-{len(mints)}", "expires_in": 3600})
            return _Response(200, {"reportId": "rep-sb-1"})

        async def get(self, url, headers=None):
            if "offline-report-storage" in url or url.startswith("https://dl.invalid"):
                return _Response(200, content=rows)
            polls.append(1)
            if len(polls) == 3:                       # token expires mid-poll
                return _Response(401, text='{"message":"Invalid token"}')
            if len(polls) >= 4:
                return _Response(200, {"status": "COMPLETED", "url": "https://dl.invalid/r"})
            return _Response(200, {"status": "PENDING"})

    ads_mod._token.value = ""
    ads_mod._token.expires_at = 0.0
    monkeypatch.setattr(ads_mod, "get_settings", _ads_settings_for_sb())
    monkeypatch.setattr(reports, "get_settings", _ads_settings_for_sb())
    monkeypatch.setattr(ads_mod.httpx, "AsyncClient", lambda *a, **k: _Client())

    async def _no_sleep(_seconds):
        return None

    out = await reports.fetch_targeting(
        "2026-08-26", "2026-09-01", ad_product="sb", sleep=_no_sleep,
    )
    assert out, "the report was lost to the mid-poll 401"
    assert len(mints) >= 2, "the token was not re-minted"
```

This needs a settings stub for the SB path. Before writing a new one, check `tests/test_ads_sb.py` for an existing settings fixture and reuse it — that file already fakes ads settings for its other tests. Only add `_ads_settings_for_sb()` if nothing suitable exists:

```python
def _ads_settings_for_sb():
    class _S:
        ads_configured = True
        ads_refresh_token = "refresh"
        ads_client_id = "client"
        ads_client_secret = "secret"
        ads_profile_id = "profile"
        ads_endpoint = "https://ads.invalid"
    return lambda: _S()
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/Scripts/python -m pytest tests/test_ads_sb.py -q -p no:randomly -k "poll_get or token_expiry"`
Expected: FAIL — the source test fails on `"poll_get(" in source`, and the behavioural test fails with `AdsError: Polling the targeting report failed: ... Invalid token`.

- [ ] **Step 3: Update `app/ads/reports.py`**

Add `poll_get` to the import block (currently importing `AdsError, AdsNotConfigured, CREATE_CONTENT_TYPE, POLL_INTERVAL, POLL_MAX, REPORT_PATH, _access_token, _headers, split_window` from `app.portfolio.ads`):

```python
from app.portfolio.ads import (
    AdsError,
    AdsNotConfigured,
    CREATE_CONTENT_TYPE,
    POLL_INTERVAL,
    POLL_MAX,
    REPORT_PATH,
    _access_token,
    _headers,
    poll_get,
    split_window,
)
```

Then replace the poll call in `_poll_report`. **Note the GET currently spans three lines** — this
exact text (verified against `app/ads/reports.py:327-332`) is what must be replaced, and the
replacement collapses it to one line so the mutation harness in Task 3 can match it:

```python
    for attempt in range(POLL_MAX):
        await sleep(POLL_INTERVAL)
        status = await client.get(
            f"{settings.ads_endpoint}{REPORT_PATH}/{report_id}", headers=head
        )
        if status.status_code >= 400:
```

Change the three-line GET to this single line (keeping the surrounding lines as they are):

```python
    for attempt in range(POLL_MAX):
        await sleep(POLL_INTERVAL)
        # `poll_get`, not `client.get(..., headers=head)`: a report can take longer to generate
        # than the token lives, and reusing `head` across 45 minutes of polling is what made
        # Sponsored Brands 401 every night. See app/portfolio/ads.py:poll_get.
        status = await poll_get(client, f"{settings.ads_endpoint}{REPORT_PATH}/{report_id}")
        if status.status_code >= 400:
```

`head` is still needed for the create call above the loop, so leave its assignment alone.

- [ ] **Step 4: Update `app/portfolio/ads.py`'s own poll loop**

Its loop (around line 410) reads:

```python
    for attempt in range(POLL_MAX):
        await sleep(POLL_INTERVAL)
        status = await client.get(
            f"{settings.ads_endpoint}{REPORT_PATH}/{report_id}", headers=head
        )
        if status.status_code >= 400:
```

Change to:

```python
    for attempt in range(POLL_MAX):
        await sleep(POLL_INTERVAL)
        # Same reason as app/ads/reports.py: see poll_get's docstring. This path has not been bitten
        # yet only because the economics reports finish in ~30s — the defect was identical.
        status = await poll_get(client, f"{settings.ads_endpoint}{REPORT_PATH}/{report_id}")
        if status.status_code >= 400:
```

Again leave the `head` assignment above the loop alone — the create call still uses it.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `venv/Scripts/python -m pytest tests/test_ads_sb.py tests/test_portfolio_ads.py tests/test_ads_one_source.py tests/test_ads_bid_recommendations.py tests/test_portfolio_economics.py -q -p no:randomly`
Expected: all PASS. These five files are every test touching the report/token paths.

- [ ] **Step 6: Run the FULL suite**

Run: `venv/Scripts/python -m pytest -q -p no:randomly`
Expected: zero failures. This change touches a shared module, so a narrower run is not sufficient evidence.

- [ ] **Step 7: Commit**

```bash
git add app/ads/reports.py app/portfolio/ads.py tests/test_ads_sb.py
git commit -m "fix(ads): both report poll loops refresh their token, fixing nightly SB 401"
```

---

## Task 3: Mutation coverage

**Files:**
- Create: `scripts/mutate_ads_token.py`

**Interfaces:** none — standalone throwaway, same shape as `scripts/mutate_projections.py`.

**Why:** CLAUDE.md records four cases where a real bug survived a fully green suite. "The tests pass" is not this project's bar; "a deliberate mutation of the decision is caught" is. This bug itself shipped past a green suite.

- [ ] **Step 1: Write the harness**

```python
"""Mutation harness for the ads token-refresh fix. Throwaway; not imported by the app.

Each entry breaks ONE decision and names the test that must catch it.

    venv/Scripts/python scripts/mutate_ads_token.py
"""
import pathlib
import subprocess
import sys

ADS = pathlib.Path("app/portfolio/ads.py")
REPORTS = pathlib.Path("app/ads/reports.py")

MUTATIONS = [
    (
        "poll_get stops retrying a 401 (the original bug, restored)",
        ADS,
        "        if response.status_code != 401:\n            return response",
        "        return response",
        "test_a_401_mid_poll_is_retried_with_a_fresh_token",
    ),
    (
        "poll_get stops invalidating the cached token, so the retry reuses the dead one",
        ADS,
        '        _token.value = ""\n        _token.expires_at = 0.0',
        "        pass",
        "test_a_401_mid_poll_is_retried_with_a_fresh_token",
    ),
    (
        "the retry bound is removed, so a revoked token loops forever",
        ADS,
        "    for attempt in range(force_refresh_attempts + 1):",
        "    while True:",
        "test_a_permanent_401_fails_after_the_retry_bound",
    ),
    (
        "the targeting poll goes back to a header dict bound once",
        REPORTS,
        "        status = await poll_get(client, f\"{settings.ads_endpoint}{REPORT_PATH}/{report_id}\")",
        "        status = await client.get(f\"{settings.ads_endpoint}{REPORT_PATH}/{report_id}\", headers=head)",
        "test_both_poll_loops_go_through_poll_get",
    ),
]

# NOTE on the last mutation's `old` string: it must match the SINGLE-LINE form written in Task 2
# Step 3. The ORIGINAL code spanned three lines —
#     status = await client.get(
#         f"{settings.ads_endpoint}{REPORT_PATH}/{report_id}", headers=head
#     )
# — so if Task 2 was applied by editing in place and left the call wrapped across lines, this
# entry will report SKIP rather than running. A SKIP here is a harness bug, not a passing test:
# fix the string to match the real source before trusting the "all caught" line.


def run(expression):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-k", expression, "tests"],
        capture_output=True, text=True,
    )
    last = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return result.returncode, last


def main():
    survivors = []
    for label, path, old, new, test_name in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"SKIP      {label}\n          target text not found in {path}")
            survivors.append((label, f"target text not found in {path}"))
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            code, summary = run(test_name)
        finally:
            path.write_text(original, encoding="utf-8")
        if code == 0:
            print(f"SURVIVED  {label}\n          {test_name} still passes -> {summary}")
            survivors.append((label, test_name))
        else:
            print(f"caught    {label}\n          {summary}")

    print()
    if survivors:
        print(f"{len(survivors)} SURVIVOR(S):")
        for label, detail in survivors:
            print(f"  - {label} ({detail})")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it**

Run: `venv/Scripts/python scripts/mutate_ads_token.py`
Expected: `all 4 mutations caught`. If the `while True:` mutation hangs rather than failing, the retry-bound test is not actually bounding anything — fix the test, do not weaken the mutation. If any `SKIP` appears, the `old` string does not match what was actually written in Task 1/2; correct the harness to match the real source.

- [ ] **Step 3: Commit**

```bash
git add scripts/mutate_ads_token.py
git commit -m "test(ads): mutation harness for the token-refresh fix"
```

---

## Task 4: Correct CLAUDE.md's 429 attribution

**Files:**
- Modify: `CLAUDE.md`

**Why:** CLAUDE.md currently states that Sponsored Brands staleness is caused by report-creation rate limiting (429) and describes a short retry built on that basis. That was true when written. Today's failure is a `401` token expiry — a different bug behind the same banner — and the stale explanation actively misdirects. It cost this investigation a detour; leaving it costs the next reader the same.

- [ ] **Step 1: Find the passage**

Run: `grep -n "rate-limited over HOURS\|429 three times\|sbTargeting.*report creation is rate-limited" CLAUDE.md`

- [ ] **Step 2: Add the correction immediately after that passage**

Do **not** delete the 429 text — it is a real, separately-measured finding about report *creation*, and the throttle retry it justifies is still in the code. Add a distinct block after it:

```markdown
> **A SECOND, different cause of "Sponsored Brands is stale" — and it is the one that ran for a
> week.** Reported as *"I am seeing the same message daily and unable to optimize my ads."* The
> banner was identical to the throttle case above, so the throttle note above is where anyone
> would look. It was wrong: the error was a **401, not a 429**.
>
> ```
> GET .../reporting/reports/e1db257d-... "HTTP/1.1 401 Unauthorized"
> SB report failed: {"message":"Unauthorized exception while handling 3P Request: Invalid token"}
> ```
>
> **`_poll_report` bound its auth headers ONCE and reused them for `POLL_MAX * POLL_INTERVAL` =
> 45 minutes**, on an access token that lives 3600s and had already been spent for ~17 minutes by
> the preceding Sponsored Products reports. Measured: exactly **one** LWA mint
> (`grep -c 'o2/token'` = 1) for a whole nightly run. The token expired mid-poll, the next poll
> 401'd, and the loop discarded a report Amazon had already produced.
>
> **The tell that made it diagnosable: one run in the middle of the week SUCCEEDED** — a *manual
> 7-day* refresh (02 Sep 11:11, `sb_rows=14447`). Short runs leave the token nearly full when SB
> starts polling; the nightly 60-day run burns 30–50 minutes on Sponsored Products first, so it
> **could never** succeed. "Fails nightly, works when I press the button" is the signature of a
> token-lifetime bug, not a rate limit.
>
> `app.portfolio.ads.poll_get` now rebuilds the headers per poll (cheap — `_access_token` returns
> the cached token until near expiry, and a test pins that five polls mint once) and retries a 401
> with a freshly minted token, bounded so a genuinely revoked credential still fails loudly.
> **Both** poll loops use it: `app/portfolio/ads.py` carried the identical defect and had simply
> never been slow enough to hit it.
```

- [ ] **Step 3: Confirm the scanning tests still pass**

Run: `venv/Scripts/python -m pytest tests/test_local_dates.py tests/test_theme.py -q -p no:randomly`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: the SB failure was a 401 token expiry, not the documented 429 throttle"
```

---

## Final verification

**Automated**

1. `venv/Scripts/python -m pytest -q -p no:randomly` → zero failures.
2. `venv/Scripts/python scripts/mutate_ads_token.py` → `all 4 mutations caught`.
3. `venv/Scripts/python scripts/mutate_projections.py` → `all 14 mutations caught` (unchanged; confirms no collateral damage).

**Manual, on production after deploy** — this is the part that actually proves the fix, because the bug only reproduces on a long run against the real API.

4. Deploy: `ssh ubuntu@13.233.144.148`, then `cd /opt/amazon-tracker && bash deploy/update-ec2.sh` (answer `y` to the `hsn_master.json` stash prompt).
5. Open `/ads-page`, press **Refresh from Amazon**, and watch the log:
   `sudo journalctl -u tracker -f | grep -iE 'sb |sponsored brands|401|re-minting'`
   Expect either no 401 at all, or a `re-minting the access token` line **followed by the poll
   continuing** — not an `SB report failed`.
6. Confirm `sb_rows > 0` and `status = "done"`:
   ```sql
   SELECT started_at, status, sp_rows, sb_rows FROM ads_refresh ORDER BY id DESC LIMIT 3;
   ```
7. Confirm the banner clears: the 7d/14d/30d presets should no longer say "cannot be summed", and
   `sb` days held should start catching up toward `sp`'s 59.
8. Confirm a bid rule preview returns rows **including SB targets** — the actual user-facing goal.
9. Next morning, confirm the 08:00 IST nightly run recorded `done` with `sb_rows > 0` rather than
   `partial`. **This is the real proof**: every manual run has always been able to succeed, so only
   a nightly run demonstrates the fix.

**Known limitation to state, not hide:** SB is currently missing 24 Aug → today, and the 60-day
purge has been deleting old SB days throughout. Step 5's refresh should backfill the window it
covers, but any SB day already purged is gone from Amazon's reach only if it falls outside the
60-day window — verify at step 7 how far back SB actually recovers rather than assuming full
recovery.
