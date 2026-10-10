"""POST /portfolio/ask: the Portfolio "Ask" panel. Read-only; see app/assistant."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import permissions
from app.assistant import bedrock, service
from app.config import get_settings
from app.database import get_db
from app.routers.auth import get_current_username, require_area

router = APIRouter(prefix="/portfolio/ask", tags=["assistant"])
logger = logging.getLogger(__name__)

TABS = {"profit", "repeat", "value"}
CONTEXT_KEYS = ("brand", "start", "end", "category")


@router.post("")
async def ask(request: Request, body: dict = Body(...), db: AsyncSession = Depends(get_db),
              grant=Depends(require_area(permissions.PORTFOLIO))):
    settings = get_settings()
    if not settings.assistant_configured:
        return JSONResponse({"error": "The assistant is not configured on this server "
                                      "(AWS_BEARER_TOKEN_BEDROCK is not set)."}, status_code=503)
    question = str(body.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "Type a question first."}, status_code=400)
    if len(question) > service.MAX_QUESTION_CHARS:
        return JSONResponse({"error": f"Keep the question under {service.MAX_QUESTION_CHARS} "
                                      "characters."}, status_code=400)
    if await service.questions_today(db) >= settings.assistant_daily_limit:
        return JSONResponse({"error": f"Today's limit of {settings.assistant_daily_limit} questions "
                                      "is used up. It resets at midnight IST."}, status_code=429)
    raw = body.get("context") if isinstance(body.get("context"), dict) else {}
    context = {k: str(raw[k])[:80] for k in CONTEXT_KEYS if raw.get(k)}
    tab = raw.get("tab") if raw.get("tab") in TABS else None
    if tab:
        context["tab"] = tab
    history = body.get("history") if isinstance(body.get("history"), list) else []
    history = [h for h in history if isinstance(h, dict)]

    username = get_current_username(request)
    started = time.monotonic()
    try:
        result = await service.ask(question, history, context, service.db_sources(db))
    except bedrock.BedrockError as exc:
        logger.warning("assistant: %s", exc)
        await service.log(db, username=username, tab=tab, question=question, error=str(exc),
                          started=started)
        return JSONResponse({"error": "The assistant could not answer just now. " + str(exc)},
                            status_code=502)
    await service.log(db, username=username, tab=tab, question=question, result=result,
                      started=started)
    return {"answer": result["answer"], "sources": result["sources"], "usage": result["usage"]}
