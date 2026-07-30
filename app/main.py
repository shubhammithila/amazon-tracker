import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.database import engine, Base
from app.routers import auth, scrape, products, keywords, ws, invoice, churn, projections, shipment
from app.routers.auth import (
    ForbiddenException,
    RedirectException,
    require_admin,
    require_ops_or_admin,
)
from app.scheduler import setup_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # create_all() only ever CREATEs missing tables — it never ALTERs an
    # existing one. Relying on it silently skipped new columns (products.use_by),
    # which 500'd the whole /products router. Alembic owns the schema now;
    # run `alembic upgrade head` before starting. This call is kept only so a
    # brand-new empty database still boots, and it is a no-op once migrated.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database ready (run 'alembic upgrade head' to apply migrations)")
    setup_scheduler()
    try:
        yield
    finally:
        from app.scheduler import scheduler

        # setup_scheduler() returns early when SCHEDULER_ENABLED is false, so the
        # scheduler may never have started. Calling shutdown() on a scheduler
        # that was never started raises AttributeError on its unset event loop —
        # which aborted the whole shutdown path and skipped engine.dispose()
        # below, leaking connections on every exit.
        if scheduler.running:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                logger.exception("Scheduler shutdown failed")

        await engine.dispose()


app = FastAPI(title="Amazon Tracker v2", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.include_router(auth.router)
app.include_router(scrape.router)
app.include_router(products.router)
app.include_router(keywords.router)
app.include_router(ws.router)
app.include_router(invoice.router)
app.include_router(churn.router)
app.include_router(projections.router)
app.include_router(shipment.router)


@app.exception_handler(RedirectException)
async def auth_redirect_handler(request: Request, exc: RedirectException):
    return RedirectResponse(url="/login", status_code=303)


@app.exception_handler(ForbiddenException)
async def forbidden_handler(request: Request, exc: ForbiddenException):
    """403, deliberately not a redirect to /login.

    Bouncing an authenticated ops user to the login page would look like their
    session had expired, and they would just log in again and loop. A 403 says
    'you are signed in, this part is not yours'.
    """
    return JSONResponse({"error": "Admin only"}, status_code=403)


# Page routes. Each passes `active` so templates/nav.html can highlight the
# current tab; the nav lives in that one partial because it used to be
# copy-pasted into every template and drifted (projections.html had no Shipment
# link, so opening Projections made the Shipment tab vanish).
#
# Auth is a dependency rather than an inline check so the role rule lives in one
# place. The five admin pages take require_admin — plan editing, verification
# and GST invoicing are owner-only. Only /ops-page admits the ops role.
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, role: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "index.html", {"active": "dashboard"})


@app.get("/invoice-page", response_class=HTMLResponse)
async def invoice_page(request: Request, role: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "invoice.html", {"active": "invoice"})


@app.get("/churn-page", response_class=HTMLResponse)
async def churn_page(request: Request, role: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "churn.html", {"active": "churn"})


@app.get("/projections-page", response_class=HTMLResponse)
async def projections_page(request: Request, role: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "projections.html", {"active": "projections"})


@app.get("/shipment-page", response_class=HTMLResponse)
async def shipment_page(request: Request, role: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "shipment.html", {"active": "shipment"})


@app.get("/ops-page", response_class=HTMLResponse)
async def ops_page(request: Request, role: str = Depends(require_ops_or_admin)):
    """The operations employee's only screen: record what was packed today.

    Admin passes too — the owner supervises packing, and being unable to see the
    screen he is verifying would be absurd. `role` is handed to the template so
    it can show admin-only affordances without a second round trip.
    """
    return templates.TemplateResponse(request, "ops.html", {"active": "ops", "role": role})


@app.get("/health")
async def health():
    return {"status": "ok"}
