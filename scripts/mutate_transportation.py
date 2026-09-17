"""Mutation harness for the transportation-confirmation feature.

Each mutation is a plausible way to get this wrong. Every one MUST be caught by the suite, or
the test that should have caught it is missing.

Run: ``venv/Scripts/python scripts/mutate_transportation.py``

Written because this codebase has a documented record of green suites hiding real bugs: the SB
``daily=True`` fetch, the deploy detector's revision id, the scheduler guard's literal names,
and the pause feature shipping broken three times.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: (file, find, replace, why this would be a real bug)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ── The IST date conversion: six historical instances of this defect ──
    (
        "app/ist.py",
        'anchored = datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)\n    as_utc = anchored.astimezone(timezone.utc)\n    return as_utc.strftime("%Y-%m-%dT%H:%MZ")',
        'return f"{day.isoformat()}T00:00Z"',
        "midnight UTC instead of midnight IST — declares the PREVIOUS Indian day",
    ),
    (
        "app/ist.py",
        "anchored = datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)",
        "anchored = datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)",
        "treats the ship date as UTC — 5.5 hours out, wrong day for half the window",
    ),
    (
        "app/ist.py",
        'return as_utc.strftime("%Y-%m-%dT%H:%MZ")',
        "return as_utc.isoformat()",
        "emits +00:00 and seconds — a shape this endpoint has not been seen to accept",
    ),
    # ── The cost placeholder: "$cost.amount" is a string that looks like data ──
    (
        "app/shipment/spapi.py",
        "    try:\n        return float(raw)\n    except (TypeError, ValueError):\n        return None",
        "    return float(raw) if raw else None",
        "float('$cost.amount') raises — a 500 at the last step of a working shipment",
    ),
    (
        "app/shipment/spapi.py",
        "    except (TypeError, ValueError):\n        return None",
        "    except (TypeError, ValueError):\n        return 0.0",
        "0.0 reads as 'Amazon is carrying this free' rather than 'no quote exists'",
    ),
    # ── Listing requires one of two ids ──
    (
        "app/shipment/spapi.py",
        "    if not placement_option_id and not shipment_id:\n        raise SpApiError(",
        "    if False:\n        raise SpApiError(",
        "sends a call Amazon refuses with a message that reads like a malformed request",
    ),
    # ── The route: the ship date must be validated against the IST day ──
    (
        "app/routers/shipment.py",
        "if ship_date and date.fromisoformat(ship_date) < ist.today():",
        "if ship_date and date.fromisoformat(ship_date) < date.today():",
        "server UTC day, not IST — refuses the owner's real today for 5.5 hours",
    ),
    (
        "app/routers/shipment.py",
        "    if ship_date and not _valid_date(ship_date):",
        "    if False and not _valid_date(ship_date):",
        "a malformed ship date reaches date.fromisoformat and 500s",
    ),
    # ── The ready-to-ship window must go through ist ──
    (
        "app/routers/shipment.py",
        "            instant = ist.utc_instant(date.fromisoformat(ship_date))",
        '            instant = f"{ship_date}T00:00Z"',
        "the bug this feature exists to avoid, inlined at the call site",
    ),
    # The schema mistake that nearly shipped: Amazon silently DROPS readyToShipWindow on the
    # confirmation, so sending it there looks like success and leaves the shipment at dates: {}.
    (
        "app/routers/shipment.py",
        '                        "readyToShipWindow": {"start": instant},\n',
        "",
        "the ship date is never sent — Amazon leaves the shipment with dates: {}",
    ),
    (
        "app/routers/shipment.py",
        "            await spapi.generate_transportation_options(",
        "            await _skip_generate(",
        "confirming an option that was never generated with a ship date",
    ),
    # ── A transportation failure must not fail the confirm ──
    (
        "app/routers/shipment.py",
        '            transportation = {\n                "confirmed": False,\n                "error": exc.message,',
        '            return JSONResponse({"error": exc.message}, status_code=502)\n            transportation = {\n                "confirmed": False,\n                "error": exc.message,',
        "reports failure for a shipment that EXISTS — the owner retries and creates a second",
    ),
    # ── Contact details must not be a second copy ──
    (
        "app/routers/shipment.py",
        '    "phoneNumber": AMAZON_SOURCE_ADDRESS["phoneNumber"],',
        '    "phoneNumber": "9999999999",',
        "a hardcoded phone number drifts from the ship-from address",
    ),
    # ── The client must send the key the route reads ──
    (
        "templates/shipment.html",
        "        ship_date: shipDate,",
        "        shipDate: shipDate,",
        "the pause bug exactly: server contract tested, client contract not",
    ),
    (
        "templates/shipment.html",
        '  const shipDateEl = document.getElementById("ib-ship-date");',
        "  const shipDateEl = null;",
        "the date input is never read — renderInvoiceBar's failure mode",
    ),
]


def run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        [str(ROOT / "venv/Scripts/python"), "-m", "pytest", "-q", "-x", "-p", "no:randomly",
         "tests/test_shipment_transportation.py", "tests/test_shipment_spapi.py",
         "tests/test_local_dates.py", "tests/test_ist.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return proc.returncode == 0, proc.stdout[-400:]


def main() -> int:
    survivors: list[str] = []
    for index, (rel, find, replace, why) in enumerate(MUTATIONS, 1):
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        if find not in original:
            print(f"[{index:2}/{len(MUTATIONS)}] SKIP  target text not found in {rel}")
            print(f"          looked for: {find[:80]!r}")
            survivors.append(f"{rel}: target not found — {why}")
            continue

        backup = tempfile.mktemp(suffix=".bak")
        shutil.copy2(path, backup)
        try:
            path.write_text(original.replace(find, replace, 1), encoding="utf-8")
            passed, tail = run_suite()
            if passed:
                print(f"[{index:2}/{len(MUTATIONS)}] SURVIVED  {why}")
                survivors.append(f"{rel}: {why}")
            else:
                print(f"[{index:2}/{len(MUTATIONS)}] caught    {why}")
        finally:
            shutil.copy2(backup, path)
            Path(backup).unlink(missing_ok=True)

    print()
    if survivors:
        print(f"{len(survivors)} MUTATION(S) SURVIVED — a test is missing:")
        for s in survivors:
            print("  -", s)
        return 1
    print(f"All {len(MUTATIONS)} mutations caught.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
