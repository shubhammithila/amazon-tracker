"""One question in, one answer out: the tool loop, the system prompt, the log and the daily cap.

Cheap by construction: the system prompt and tool list sit before a `cachePoint`, so after the first
question of a five-minute spell they are read from cache; tools return small slices; and only the
last `HISTORY_TURNS` exchanges travel as plain text.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, time as dtime

from sqlalchemy import func, select

from app import ist
from app.assistant import bedrock, tools
from app.models import AssistantLog

MAX_ROUNDS = 5
HISTORY_TURNS = 3
MAX_QUESTION_CHARS = 1000
#: A tool result is cut to this many characters before it reaches the model. Rows are dropped
#: rather than the JSON being sliced mid-value, and the result says so.
MAX_RESULT_CHARS = 14000

SYSTEM = """You answer questions about an Amazon India seller's own business data (brands such as Mithila Foods, Howrah Foods), shown on three Portfolio sub-tabs: Profit, Repeat customers and Customer value.

RULES
- Use ONLY figures returned by the tools. Never estimate, recall or invent a number. If a tool returns null for a figure, say there is not enough data (it is a dash on screen, never zero).
- If you derive a figure (a difference, a share), say how, in a few words.
- Call as few tools as needed; ask for small limits. The tools default to the brand, window and category on the user's screen.
- Answer in the user's language (English, Hindi or Hinglish), briefly: a one-line answer first, then at most ~8 bullets or a small markdown table. No preamble.
- Money in rupees with Indian grouping (₹1,23,456). Percentages with one decimal.
- If a window is incomplete or unavailable, say so instead of answering from it.
- You cannot change anything (no decisions, bids, refreshes). If asked, say the owner does that on the tab or in Seller Central.

DEFINITIONS
Profit tab (from Amazon's economics data, per product; a multi-flavour parent is one row per flavour):
- Sales are ex-GST. Net = sales - refunds - Amazon fees (ex-GST) - ad spend (Sponsored Products + attributed Sponsored Brands). Net is BEFORE product cost (pre-COGS): a positive Net % is not profit.
- Refunds % + Amazon fees % + TACOS + Net % = 100% of sales.
- TACOS = ad spend / total sales. ACOS = ad cost / ad-attributed sales (Ads API); "no attributed sales" means spend with none attributed.
- Net ₹/kg = net / net weight sold. Returns % is by units.
- Verdicts: DEAD (no volume), KILL (high returns, or net < 0 with TACOS > 50%), SURGICAL (product earns but a size loses money), BEST BET / SCALE (net >= 25% and TACOS <= 30%, split on rating 4.0), MONITOR (everything else). Groups: Scale = BEST BET + SCALE; Maintain = MONITOR + SURGICAL; Kill or monitor = KILL + DEAD.
Repeat customers tab (FBA orders only; Easy Ship buyers cannot be seen):
- For window N (90/60/30): customers who first bought the product in a 30-day period, followed for N days. same_pct = share who bought it again on a later day. units_pct = share of the product's units bought by those repeaters, first order included (Brand Analytics' "repeat units"), so it reads higher than same_pct. came_from_pct = share of buyers who bought another of our products in the N days before; went_on_pct = bought another after.
- The brand or category total counts unique customers, so it is not the sum of rows. partial_fba_data = under 60% of units go through FBA, so the figures see only part of the customers.
Customer value tab (FBA only; "new" = first order since 1 Jan 2026):
- A row is the product that brought a customer in (most money in their first order). LTV N = what those customers paid us (ex-GST, after promotions, before product cost) in their first N days, per customer, over cohorts old enough. A product's LTV includes everything its customers went on to buy.
- CAC = ad spend x FBA unit share / new FBA customers, months with complete ad data only. LTV:CAC = 90-day LTV / CAC; 3x and over is healthy, under 1x loses money on ads. Payback = days until average spend covers CAC; 0 = the first order."""


def _midnight_utc() -> datetime:
    return datetime.combine(ist.today(), dtime()) - ist.IST_OFFSET


async def questions_today(db) -> int:
    return (await db.execute(select(func.count()).select_from(AssistantLog)
                             .where(AssistantLog.created_at >= _midnight_utc()))).scalar() or 0


def _context_note(context: dict) -> str:
    bits = [f"Today is {ist.today().isoformat()} (IST)."]
    tab = {"profit": "Profit", "repeat": "Repeat customers", "value": "Customer value"}.get(
        context.get("tab"), "")
    if tab:
        bits.append(f"The user is on the {tab} tab.")
    if context.get("brand"):
        bits.append(f"Brand on screen: {context['brand']}.")
    if context.get("start") and context.get("end"):
        bits.append(f"Profit window on screen: {context['start']} to {context['end']}.")
    if context.get("category"):
        bits.append(f"Category picked on screen: {context['category']}.")
    return " ".join(bits)


def _fit(result: dict) -> dict:
    """Keep a tool result under MAX_RESULT_CHARS by dropping trailing rows, and say so."""
    text = json.dumps(result, default=str, ensure_ascii=False)
    if len(text) <= MAX_RESULT_CHARS:
        return result
    for key in ("rows", "sizes", "cohorts", "categories"):
        while isinstance(result.get(key), list) and len(result[key]) > 1:
            result[key] = result[key][:-1]
            result["truncated"] = f"some {key} were left out to keep the answer small"
            if len(json.dumps(result, default=str, ensure_ascii=False)) <= MAX_RESULT_CHARS:
                return result
    return result


def source_label(result: dict) -> str | None:
    """What an answer's figures came from, in words the owner can check against the tab."""
    src = result.get("source")
    if not src:
        return None
    if src == "Profit":
        w = result.get("window") or []
        return f"Profit · {w[0]} → {w[1]}" if len(w) == 2 else "Profit"
    parts = [src, result.get("brand")]
    if result.get("follow_up_days"):
        parts.append(f"{result['follow_up_days']}-day window")
    if result.get("data_to"):
        parts.append(f"data to {result['data_to']}")
    return " · ".join(p for p in parts if p)


async def ask(question: str, history: list, context: dict, sources: tools.Sources,
              converse=None) -> dict:
    """Run the tool loop. Returns {answer, sources, tools, usage}. Raises BedrockError.

    `bedrock.converse` is looked up per call, not bound as a default, so the test suite's guard
    against a live call cannot be sidestepped by a default captured at import time."""
    converse = converse or bedrock.converse
    messages = []
    for turn in (history or [])[-HISTORY_TURNS:]:
        q, a = str(turn.get("q") or "")[:MAX_QUESTION_CHARS], str(turn.get("a") or "")[:3000]
        if q and a:
            messages += [{"role": "user", "content": [{"text": q}]},
                         {"role": "assistant", "content": [{"text": a}]}]
    messages.append({"role": "user", "content": [{"text": f"[{_context_note(context)}]\n\n{question}"}]})
    system = [{"text": SYSTEM}, {"cachePoint": {"type": "default"}}]
    spec = tools.SPECS + [{"cachePoint": {"type": "default"}}]
    usage = {"input": 0, "output": 0, "cache_read": 0}
    calls, labels = [], []
    for _ in range(MAX_ROUNDS):
        resp = await converse(system=system, messages=messages, tools=spec)
        u = resp.get("usage") or {}
        usage["input"] += int(u.get("inputTokens") or 0) + int(u.get("cacheWriteInputTokens") or 0)
        usage["output"] += int(u.get("outputTokens") or 0)
        usage["cache_read"] += int(u.get("cacheReadInputTokens") or 0)
        message = (resp.get("output") or {}).get("message") or {"role": "assistant", "content": []}
        messages.append(message)
        uses = [c["toolUse"] for c in message.get("content") or [] if "toolUse" in c]
        if resp.get("stopReason") != "tool_use" or not uses:
            text = "\n".join(c["text"] for c in message.get("content") or [] if "text" in c).strip()
            return {"answer": text or "I could not form an answer to that.",
                    "sources": labels, "tools": calls, "usage": usage}
        results = []
        for use in uses:
            calls.append({"name": use.get("name"), "input": use.get("input") or {}})
            try:
                out = await tools.run(use.get("name"), use.get("input") or {}, sources, context)
            except Exception as exc:          # a tool bug must become an answer, not a 500
                out = {"error": f"The tool failed: {exc}"}
            label = source_label(out)
            if label and label not in labels:
                labels.append(label)
            results.append({"toolResult": {"toolUseId": use["toolUseId"],
                                           "content": [{"json": _fit(out)}],
                                           "status": "error" if out.get("error") else "success"}})
        messages.append({"role": "user", "content": results})
    return {"answer": "That needed more look-ups than I allow for one question. Try a narrower one.",
            "sources": labels, "tools": calls, "usage": usage}


async def log(db, *, username, tab, question, result=None, error=None, started=None) -> None:
    result = result or {}
    usage = result.get("usage") or {}
    db.add(AssistantLog(
        username=username, tab=tab, question=question[:MAX_QUESTION_CHARS],
        answer=result.get("answer"), tools_json=json.dumps(result.get("tools") or [], default=str),
        input_tokens=usage.get("input", 0), output_tokens=usage.get("output", 0),
        cache_read_tokens=usage.get("cache_read", 0),
        latency_ms=int((time.monotonic() - started) * 1000) if started else 0, error=error))
    await db.commit()


def db_sources(db) -> tools.Sources:
    """The real payload builders, memoised for the length of one question."""
    from app.repeat import service as repeat_service, value_service
    from app.routers import portfolio as pf_router
    from app.shipment import repository as ship_repository

    memo: dict = {}

    async def profit(inp: dict):
        window, error = pf_router._requested_window(inp.get("start"), inp.get("end"), inp.get("days"))
        if error:
            return {"error": error}, {}
        inactive = bool(inp.get("include_inactive"))
        key = ("profit", tuple(window) if window else None, inactive)
        if key not in memo:
            data = await pf_router._dashboard(db, window, include_inactive=inactive)
            rows = await ship_repository.load_categories(db)
            memo[key] = (data, {r.product_key: r.priority for r in rows})
        return memo[key]

    async def repeat(brand):
        key = ("repeat", brand)
        if key not in memo:
            memo[key] = await repeat_service.build_payload(db, brand, ist.today())
        return memo[key]

    async def value(brand):
        key = ("value", brand)
        if key not in memo:
            memo[key] = await value_service.build_payload(db, brand, ist.today())
        return memo[key]

    return tools.Sources(profit, repeat, value)

