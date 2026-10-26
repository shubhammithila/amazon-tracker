"""Keyword growth's relevance check: Claude reads the product page and keeps only close matches.

Asked for as *"they should be very relevant and close to the product detail page. for eg sattu should not
have just atta as a target keyword… amazon suggests a lot of random keywords, no need to add them all or
suggest all. only the relevant ones."* The owner chose Claude over word-matching rules.

**One Converse call per ad group** (its products' pages + the candidates not judged before), on the same
Bedrock setup as the Portfolio "Ask" panel. The answer comes through ONE tool, `record_verdicts`, so it is
structured rather than prose. Verdicts are stored per (product key, kind, term) and reused for
`VERDICT_DAYS`, so asking again is instant and free.

- **A term the model does not answer for is REJECTED** ("not judged"), never kept by default.
- **A failure raises `JudgeError`** and the caller shows nothing for that ad group: an unfiltered list is
  exactly what the owner asked not to see.
- **Daily cap** `GROWTH_DAILY_LIMIT` (60) Finds per IST day, counted in `assistant_log` (tab `growth`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import ist
from app.models import AssistantLog, GrowthVerdict

logger = logging.getLogger(__name__)

VERDICT_DAYS = 30
CHUNK = 150
MAX_IDEAS = 15
LOG_TAB = "growth"

SYSTEM_PROMPT = """You judge advertising targets for an Amazon India seller of traditional Indian foods
(sattu, roasted chana, flours, rice, seeds, makhana and similar). You are given the product page(s) an ad
group advertises and a list of candidate {kind}s. Decide for EACH candidate whether to KEEP it.

For keywords, KEEP only a phrase a shopper would type when looking for THIS product:
- its name and close variants (e.g. "sattu", "chana sattu", "bihari sattu", "sattu powder", "sattu drink");
- its form, use or benefit when the product word is in the phrase ("sattu for weight loss");
- a pack size or weight the ad group actually sells.
REJECT:
- a single generic category word or phrase without the product ("atta", "flour", "healthy snacks");
- a different product, even a related one ("besan" for sattu, "roasted peanuts" for roasted chana);
- any competitor or other brand name ("tata", "rajdhani", "24 mantra"), even next to the product word;
- a pack size or weight the ad group does not sell; gibberish; another language's script the page lacks.

For competitor products (ASINs, given with their titles), KEEP only another brand's listing of the SAME
kind of product (another brand's chana sattu for a chana sattu ad); REJECT different products, bundles of
unrelated items, and our own brand (MITHILA FOODS, Howrah Foods).

Give every candidate a reason of at most 12 words. {ideas_rule}
Answer ONLY by calling the record_verdicts tool once, with every candidate."""

IDEAS_RULE = (f"Also propose up to {MAX_IDEAS} NEW keywords taken from the page itself that obey the KEEP "
              "rules and are not already in the candidate list, each with a reason.")


class JudgeError(RuntimeError):
    """The relevance check could not run; nothing is offered."""


def product_key(keys: Sequence[str]) -> str:
    """The cache key of what an ad group advertises: its one product key, else a hash of the set."""
    unique = sorted({str(k) for k in keys if k})
    if len(unique) == 1 and len(unique[0]) <= 20:
        return unique[0]
    return "set-" + hashlib.sha1("|".join(unique).encode("utf-8")).hexdigest()[:16]


def _tool(kind: str) -> dict:
    props = {"verdicts": {"type": "array", "items": {"type": "object", "properties": {
        "term": {"type": "string"}, "keep": {"type": "boolean"}, "reason": {"type": "string"}},
        "required": ["term", "keep", "reason"]}}}
    if kind == "keyword":
        props["ideas"] = {"type": "array", "items": {"type": "object", "properties": {
            "term": {"type": "string"}, "reason": {"type": "string"}}, "required": ["term", "reason"]}}
    return {"toolSpec": {"name": "record_verdicts", "description": "Record a keep/reject verdict for "
                         "every candidate.", "inputSchema": {"json": {"type": "object", "properties": props,
                                                                     "required": ["verdicts"]}}}}


def parse_answer(response: Mapping) -> dict:
    """The `record_verdicts` input from a Converse response, or `JudgeError`."""
    message = ((response or {}).get("output") or {}).get("message") or {}
    for part in message.get("content") or []:
        use = part.get("toolUse")
        if use and use.get("name") == "record_verdicts":
            return use.get("input") or {}
    raise JudgeError("The relevance check gave no verdicts.")


async def used_today(db: AsyncSession) -> int:
    start = datetime.fromisoformat(ist.today().isoformat()) - timedelta(hours=5, minutes=30)
    return int((await db.execute(select(func.count(AssistantLog.id)).where(
        AssistantLog.tab == LOG_TAB, AssistantLog.created_at >= start))).scalar() or 0)


async def load_verdicts(db: AsyncSession, key: str, kind: str, terms: Sequence[str],
                        now: datetime | None = None) -> dict[str, tuple[bool, str]]:
    now = now or datetime.utcnow()
    if not terms:
        return {}
    rows = (await db.execute(select(GrowthVerdict).where(
        GrowthVerdict.product_key == key, GrowthVerdict.kind == kind,
        GrowthVerdict.term.in_(list(terms))))).scalars().all()
    return {r.term: (bool(r.keep), r.reason or "") for r in rows
            if r.judged_at and now - r.judged_at < timedelta(days=VERDICT_DAYS)}


async def _store(db: AsyncSession, key: str, kind: str, verdicts: Mapping[str, tuple[bool, str]],
                 now: datetime) -> None:
    if not verdicts:
        return
    existing = {r.term: r for r in (await db.execute(select(GrowthVerdict).where(
        GrowthVerdict.product_key == key, GrowthVerdict.kind == kind,
        GrowthVerdict.term.in_(list(verdicts))))).scalars().all()}
    for term, (keep, reason) in verdicts.items():
        row = existing.get(term) or GrowthVerdict(product_key=key, kind=kind, term=term)
        row.keep, row.reason, row.judged_at = bool(keep), (reason or "")[:300], now
        if term not in existing:
            db.add(row)
    await db.commit()


async def judge(db: AsyncSession, *, key: str, kind: str, page_text: str, candidates: Sequence[Mapping],
                group_label: str = "", converse=None, limit: int | None = None,
                username: str | None = None) -> dict:
    """`{"verdicts": {term: (keep, reason)}, "ideas": [{term, reason}], "judged": n, "cached": n}`.

    ``candidates`` are `{"term", "title"?}` (a competitor ASIN carries its title). Raises `JudgeError`."""
    from app.assistant import bedrock
    from app.config import get_settings
    from app.ads import growth

    converse = converse or bedrock.converse
    now = datetime.utcnow()
    terms = [c["term"] for c in candidates]
    cached = await load_verdicts(db, key, kind, terms, now)
    todo = [c for c in candidates if c["term"] not in cached]
    out = dict(cached)
    # Ideas from an earlier Find are KEPT verdicts on terms no source offered; they come back from the
    # cache rather than from another call.
    ideas: list[dict] = []
    if kind == growth.KIND_KEYWORD:
        known = set(terms)
        for row in (await db.execute(select(GrowthVerdict).where(
                GrowthVerdict.product_key == key, GrowthVerdict.kind == kind,
                GrowthVerdict.keep.is_(True)))).scalars().all():
            if row.term not in known and row.judged_at and now - row.judged_at < timedelta(days=VERDICT_DAYS):
                ideas.append({"term": row.term, "reason": row.reason or ""})
                out[row.term] = (True, row.reason or "")
    if not todo:
        return {"verdicts": out, "ideas": ideas, "judged": 0, "cached": len(cached)}
    if ideas:
        # Earlier ideas are reused; no new ones are asked for on top of them.
        kind_for_ideas = None
    else:
        kind_for_ideas = growth.KIND_KEYWORD if kind == growth.KIND_KEYWORD else None

    settings = get_settings()
    cap = limit if limit is not None else int(getattr(settings, "growth_daily_limit", 60) or 60)
    if await used_today(db) >= cap:
        raise JudgeError(f"Today's {cap} relevance checks are used up; cached verdicts still show.")
    if not settings.bedrock_api_key:
        raise JudgeError("The relevance check is not configured (no Bedrock key).")

    word = "competitor product" if kind == growth.KIND_PRODUCT else "keyword"
    system = [{"text": SYSTEM_PROMPT.format(kind=word, ideas_rule=IDEAS_RULE if kind == growth.KIND_KEYWORD
                                            else "")}, {"cachePoint": {"type": "default"}}]
    chunks = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)] or [[]]
    usage = {"input": 0, "output": 0, "cache_read": 0}
    started = time.monotonic()
    fresh: dict[str, tuple[bool, str]] = {}
    error = None
    try:
        for index, chunk in enumerate(chunks):
            lines = [f"- {c['term']}" + (f" — {c['title']}" if c.get("title") else "") for c in chunk]
            want_ideas = kind_for_ideas is not None and index == 0
            prompt = (f"Ad group: {group_label}\n\nPRODUCT PAGE(S):\n{page_text}\n\n"
                      f"CANDIDATE {word.upper()}S ({len(chunk)}):\n" + ("\n".join(lines) or "(none)")
                      + ("\n\nAlso give your ideas." if want_ideas else "\n\nNo ideas this time."))
            try:
                response = await converse(system=system, messages=[{"role": "user", "content": [{"text": prompt}]}],
                                          tools=[_tool(kind if want_ideas else "product")], max_tokens=6000)
            except bedrock.BedrockError as exc:
                raise JudgeError(f"The relevance check failed: {exc}") from exc
            u = response.get("usage") or {}
            usage["input"] += int(u.get("inputTokens") or 0) + int(u.get("cacheWriteInputTokens") or 0)
            usage["output"] += int(u.get("outputTokens") or 0)
            usage["cache_read"] += int(u.get("cacheReadInputTokens") or 0)
            answer = parse_answer(response)
            said = {growth.norm(v.get("term")): (bool(v.get("keep")), str(v.get("reason") or "")[:300])
                    for v in answer.get("verdicts") or [] if v.get("term")}
            for c in chunk:
                fresh[c["term"]] = said.get(c["term"], (False, "not judged"))
            if want_ideas:
                known = set(terms) | {i["term"] for i in ideas}
                for idea in (answer.get("ideas") or [])[:MAX_IDEAS]:
                    term = growth.norm(idea.get("term"))
                    if term and term not in known and not growth.is_asin(term):
                        known.add(term)
                        ideas.append({"term": term, "reason": str(idea.get("reason") or "")[:300]})
                        fresh[term] = (True, str(idea.get("reason") or "from the page")[:300])
    except JudgeError as exc:
        error = str(exc)
        raise
    finally:
        db.add(AssistantLog(username=username, tab=LOG_TAB,
                            question=f"growth {kind} {group_label}"[:2000],
                            answer=None if error else f"{sum(1 for k, _ in fresh.values() if k)} kept of {len(fresh)}",
                            tools_json=json.dumps({"key": key, "candidates": len(todo)}),
                            input_tokens=usage["input"], output_tokens=usage["output"],
                            cache_read_tokens=usage["cache_read"],
                            latency_ms=int((time.monotonic() - started) * 1000), error=error))
        await db.commit()
    await _store(db, key, kind, fresh, now)
    out.update(fresh)
    return {"verdicts": out, "ideas": ideas, "judged": len(todo), "cached": len(cached)}
