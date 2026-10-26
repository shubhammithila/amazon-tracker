"""How far a write to Amazon has got, for the screen's progress bar while Apply runs.

Reported as *"when a rule is applied, and its a lot of keywords to change nothing happens on the screen
and after 30 sec it says xxx applied"*. A 4,567-row run is two slow steps at Amazon: re-reading every
row's live bid (pages of 500) and sending the changes (batches of 500, a second apart).

The route that writes calls `track(progress_id)`; the Amazon calls in `spapi_ads` call `tick(rows)`
after each page or batch. The counter travels in a CONTEXT VARIABLE rather than a new parameter, so no
signature changes and every test fake of those functions keeps working; with nothing being tracked a
tick does nothing. The screen polls `GET /ads/apply/progress/{id}`.

Phases, and their share of the bar: `check` (re-reading live bids) 0-45%, `send` 45-97%, `record`
97-100%. The percentage only ever moves forward.
"""
from __future__ import annotations

import re
import time
from contextvars import ContextVar

PHASE_BOUNDS = {"check": (0, 45), "send": (45, 97), "record": (97, 100)}
PHASE_LABELS = {"check": "Checking live bids at Amazon", "send": "Sending changes to Amazon",
                "record": "Recording the results"}
KEEP_SECONDS = 600
_ID = re.compile(r"^[A-Za-z0-9-]{8,40}$")

_RUNS: dict[str, dict] = {}
_current: ContextVar[str | None] = ContextVar("ads_progress_id", default=None)


def valid_id(progress_id) -> str | None:
    """The id if the screen sent a usable one, else None (no tracking — never an error)."""
    value = str(progress_id or "")
    return value if _ID.match(value) else None


def _prune(now: float) -> None:
    for key in [k for k, v in _RUNS.items() if now - v["updated"] > KEEP_SECONDS]:
        _RUNS.pop(key, None)


def track(progress_id) -> None:
    """Start tracking for this request. Ticks in the same task then count against it."""
    progress_id = valid_id(progress_id)
    _current.set(progress_id)
    if progress_id:
        now = time.time()
        _prune(now)
        _RUNS[progress_id] = {"phase": "check", "done": 0, "total": 0, "percent": 0,
                              "finished": False, "updated": now}


def phase(name: str, total: int) -> None:
    """Enter a phase with ``total`` rows to get through."""
    run = _RUNS.get(_current.get() or "")
    if run is None:
        return
    run.update(phase=name, done=0, total=max(int(total or 0), 0), updated=time.time())
    run["percent"] = max(run["percent"], PHASE_BOUNDS[name][0])


def tick(rows: int) -> None:
    """``rows`` more rows done in the current phase. A no-op when nothing is tracked."""
    run = _RUNS.get(_current.get() or "")
    if run is None:
        return
    run["done"] = min(run["done"] + max(int(rows or 0), 0), run["total"] or run["done"] + int(rows or 0))
    low, high = PHASE_BOUNDS[run["phase"]]
    share = run["done"] / run["total"] if run["total"] else 1
    run["percent"] = max(run["percent"], round(low + (high - low) * min(share, 1)))
    run["updated"] = time.time()


def finish() -> None:
    run = _RUNS.get(_current.get() or "")
    if run is not None:
        run.update(finished=True, percent=100, updated=time.time())


def get(progress_id) -> dict | None:
    run = _RUNS.get(valid_id(progress_id) or "")
    if run is None:
        return None
    return {**{k: v for k, v in run.items() if k != "updated"},
            "label": PHASE_LABELS.get(run["phase"], "")}
