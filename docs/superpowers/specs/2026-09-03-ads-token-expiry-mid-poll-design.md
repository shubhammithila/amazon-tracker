# Sponsored Brands has failed every night for a week, and it is NOT the rate limit

## The report

> *"I am seeing the same message daily and unable to optimize my ads."*

The Ads tab shows, every day: **"This window cannot be summed yet. Sponsored Brands is missing 1
of these days, holds 2026-08-24 to 2026-09-01."** Every window — 7d, 14d, 30d — is unsummable, so
no bid rule can be previewed and the tab is unusable for its one purpose.

## Measured on production before designing anything

```
sb: 9 days,  2026-08-24 -> 2026-09-01
sp: 59 days, 2026-07-06 -> 2026-09-02
```

Sponsored Brands is **two days stale and eroding** — the 60-day purge removes old SB days while
every nightly run adds none. Recent runs:

| Started | Status | SP rows | SB rows |
|---|---|---|---|
| 03 Sep 10:32 | `partial` | 41,383 | **0** |
| 03 Sep 02:30 | `partial` | 476,483 | **0** |
| **02 Sep 11:11** | **`done`** | 40,869 | **14,447** ✅ |
| 02 Sep 02:30 | `partial` | 479,630 | **0** |

**One run succeeded.** That single `done` row is what makes this diagnosable: the credentials, the
request shape, the headers and the SB endpoints all work. Something is intermittent.

## The actual error, and why CLAUDE.md sent me the wrong way

```
GET https://advertising-api-eu.amazon.com/reporting/reports/e1db257d-... "HTTP/1.1 401 Unauthorized"
ads refresh: SB report failed: Polling the targeting report failed:
  {"message":"Unauthorized exception while handling 3P Request: Invalid token"}
```

**A 401, not a 429.** CLAUDE.md's Ads section states at length that SB staleness is caused by
report-creation rate limiting — *"sbTargeting report creation is rate-limited over HOURS... still
429 after a further 15 minutes completely idle"* — and prescribes a short retry on that basis.
That was true when written and is **not what is happening now**. Anyone reading the banner and
then CLAUDE.md will investigate throttling and find nothing. The doc has to be corrected or this
costs the next reader the same detour it cost me.

## Root cause

`app/ads/reports.py:_poll_report` binds its auth headers **once**:

```python
token = await _access_token(client)
head = _headers(token)              # ← built ONCE
...
for attempt in range(POLL_MAX):     # POLL_MAX = 135
    await sleep(POLL_INTERVAL)      # POLL_INTERVAL = 20.0
    status = await client.get(..., headers=head)   # ← same token for 45 minutes
    if status.status_code >= 400:
        raise AdsError(...)         # ← a 401 is fatal
```

`POLL_MAX × POLL_INTERVAL` = **45 minutes** of polling on a token that lives **3600 s** and is
already partly spent. Measured on the failing nightly run: **exactly one** LWA token mint
(`grep -c 'o2/token'` = 1) for the whole sequence — ~17 minutes of Sponsored Products reports
(02:44 → 03:01) and then the SB poll window.

So the token expires *mid-poll*. The polls before it return `200`; the one after expiry returns
`401`; the loop treats that as a fatal report failure and discards a report Amazon had already
started producing.

**Why the manual run on 02 Sep succeeded:** it was a 7-day refresh. SP finished in seconds, so the
token still had almost its full hour when SB began polling. The nightly 60-day run always burns
30–50 minutes on SP first, so **it can never succeed** — which matches "the same message daily"
exactly.

Confirming arithmetic from the successful run: SB polled 11:56 → 12:05, about **27 polls / 9
minutes**, before `COMPLETED`. A report that legitimately needs ~9 minutes of polling will always
outlive a token that has ~5 minutes left.

## Three defects, not one

1. **No token refresh inside the poll loop.** The root cause.
2. **A mid-poll 401 is fatal.** Even a freshly minted token can expire during a long report; the
   correct response is to re-authenticate and carry on, not to abandon the run.
3. **The same bug exists in `app/portfolio/ads.py`** (its own `for attempt in range(POLL_MAX)`
   loop, structurally identical, `headers=head` bound once). It has not bitten yet only because
   Portfolio's economics reports finish faster. It is latent, not absent.

## Decisions taken (yours)

- **Fix both loops through one shared helper**, rather than patching only what broke today.
- **On a 401: re-mint the token and retry the same poll**, bounded — so a genuinely revoked
  credential still fails loudly instead of looping for 45 minutes.
- **Leave report ordering alone.** With refresh working, SB no longer cares how old the token is
  when it starts. CLAUDE.md records that SP-commits-before-SB-is-attempted is load-bearing — it is
  why 482,578 SP rows survived the night SB stored zero — so reversing it would trade a fixed bug
  for a worse one.

## Design

### One helper, used by both poll loops

In `app/portfolio/ads.py` (which already owns `_access_token`, `_headers` and the `_token` cache):

```python
async def poll_get(client, url, *, force_refresh_attempts: int = 3) -> httpx.Response:
    """GET an ads URL with a token that is refreshed as needed, retrying a 401 once per re-mint.

    **The headers are rebuilt per call, not bound once by the caller.** A report can legitimately
    take longer to generate than an access token lives (measured: a real SB report needed ~9
    minutes of polling, on a token that the preceding Sponsored Products reports had already
    spent ~17 minutes of), so a loop that reuses one header dict cannot poll a slow report to
    completion. That is exactly the 401 that made Sponsored Brands fail every night.
    """
```

Behaviour:
- Calls `_access_token(client)` **per poll** — which is cheap, because it returns the cached token
  until `expires_at - _TOKEN_SAFETY_MARGIN`. Only a genuinely near-expiry token triggers a mint.
- On `401`, invalidates the cached token, re-mints, and retries the same GET. Bounded by
  `force_refresh_attempts` (default 3) so a revoked refresh token surfaces as an error rather
  than an infinite loop.
- Any other status is returned unchanged, so each caller keeps its own error prose and its own
  handling of `FAILURE`/`CANCELLED`/no-url.

### Why per-call rather than pre-emptive-only

`_access_token` already implements the pre-emptive half (the 60-second safety margin). What it
cannot do is notice a token that Amazon invalidated early, or a clock skew that makes our expiry
estimate optimistic. The bounded 401 retry covers exactly that gap, and does nothing at all on
the happy path.

### What must NOT change

- **The pre-signed S3 download stays header-free.** Both modules comment on this: sending the
  bearer token to S3 leaks it to another host. `poll_get` is for the ads host only and is not used
  for the download.
- **`_token` stays module-level and separate from `shipment.spapi`'s.** The two applications have
  different client ids; sharing the cache would hand an SP-API token to the ads host — the very
  401 that module's docstring warns about.
- **SP-before-SB ordering**, per the decision above.
- `POLL_MAX` and `POLL_INTERVAL` are unchanged. 45 minutes was never the problem.

## Recovering the missing data

SB is missing 24 Aug → today. Once fixed, a manual refresh over the full window should backfill
it — the SB report itself was never rejected, only unreadable. Worth verifying on production
rather than assuming, since the purge has been deleting old SB days throughout.

## Verification

**Automated** (each must fail against current code)

- A poll that returns `401` once and then `200 COMPLETED` **succeeds**, and mints a second token.
  This is the regression test for the reported bug.
- A poll that returns `401` forever fails after exactly `force_refresh_attempts` mints — no
  infinite loop.
- The happy path mints **once**: a report that completes without a 401 does not re-authenticate
  per poll (guards against turning a cached-token check into a token stampede).
- The download call carries **no** ads headers (existing property, re-asserted since this change
  touches the surrounding code).
- Both `app/ads/reports.py` and `app/portfolio/ads.py` route their polls through the helper —
  asserted at source level, because a runtime test on the Portfolio path would pass today for the
  wrong reason (its reports are simply fast).
- Mutations: helper stops refreshing on 401; retry bound removed; download given headers.

**Manual, on production after deploy**

Press *Refresh from Amazon* on the Ads tab and confirm SB rows land where they have been 0 for a
week; confirm the "cannot be summed" banner clears for 7d/14d/30d; confirm a bid rule preview
returns rows including SB targets. Then confirm the next nightly run records `status="done"` with
`sb_rows > 0` rather than `partial`.

## Files expected to change

`app/portfolio/ads.py` (add `poll_get`, use it in its own poll loop) ·
`app/ads/reports.py` (use `poll_get` in `_poll_report`) ·
`tests/test_portfolio_ads.py` · `tests/test_ads_sb.py` ·
`scripts/mutate_ads_token.py` (new) · `CLAUDE.md` (correct the 429 attribution)
