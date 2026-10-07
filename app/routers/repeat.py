"""Portfolio -> Repeat customers. Reads stored rows; only the explicit Refresh calls Amazon."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import ist, permissions
from app.database import get_db
from app.repeat import refresh, service
from app.routers.auth import require_area

router = APIRouter(prefix="/portfolio/repeat", tags=["repeat"])


@router.get("")
async def repeat(brand: str | None = None, db: AsyncSession = Depends(get_db),
                 grant=Depends(require_area(permissions.PORTFOLIO))):
    return await service.build_payload(db, brand, ist.today())


@router.post("/refresh")
async def start_refresh(grant=Depends(require_area(permissions.PORTFOLIO))):
    if refresh.STATE["running"]:
        return JSONResponse({"error": "A repeat-customer refresh is already running."},
                            status_code=409)
    asyncio.create_task(refresh.run_incremental())
    return {"started": True}


@router.get("/refresh-status")
async def refresh_status(grant=Depends(require_area(permissions.PORTFOLIO))):
    return refresh.STATE
