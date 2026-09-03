"""Mutation harness for the ads token-refresh fix. Throwaway; not imported by the app.

Each entry breaks ONE decision and names the test that must catch it.

This bug — Sponsored Brands 401ing every night for a week — shipped past a fully green suite, so
"the tests pass" is not the bar. CLAUDE.md records four other cases of the same thing.

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
        "poll_get stops invalidating the dead token, so the retry reuses it",
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
        '        status = await poll_get(client, f"{settings.ads_endpoint}{REPORT_PATH}/{report_id}")',
        '        status = await client.get(\n            f"{settings.ads_endpoint}{REPORT_PATH}/{report_id}", headers=head\n        )',
        "test_both_poll_loops_refresh_their_token_rather_than_binding_it_once",
    ),
]


def run(expression):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-k", expression, "tests"],
        capture_output=True, text=True, timeout=300,
    )
    last = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return result.returncode, last


def main():
    survivors = []
    for label, path, old, new, test_name in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        if old not in original:
            # A SKIP is a HARNESS bug, not a pass. Reported as a survivor so "all caught" can
            # never be printed while a mutation silently never ran.
            print(f"SKIP      {label}\n          target text not found in {path}")
            survivors.append((label, f"target text not found in {path}"))
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        try:
            code, summary = run(test_name)
        except subprocess.TimeoutExpired:
            # The `while True:` mutation can hang rather than fail if the bound test does not
            # actually bound anything. A timeout is a SURVIVOR, not an inconclusive result.
            path.write_text(original, encoding="utf-8")
            print(f"SURVIVED  {label}\n          {test_name} HUNG (no bound) -> timeout")
            survivors.append((label, f"{test_name} hung"))
            continue
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
