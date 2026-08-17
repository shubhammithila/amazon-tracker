import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlalchemy import inspect as sa_inspect

from app.config import get_settings
from app.database import engine, Base
from app import permissions
from app.routers import (
    admin_users, auth, churn, invoice, keywords, product_prices, products,
    projections, scrape, shipment, ws,
)
from app.routers.auth import (
    ForbiddenException,
    RedirectException,
    require_admin_grant,
    require_area,
    require_packing,
)
from app.scheduler import setup_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # create_all() only ever CREATEs missing tables — it never ALTERs an existing one.
    # Relying on it silently skipped new columns (products.use_by), which 500'd the whole
    # /products router. Alembic owns the schema; run `alembic upgrade head` before start.
    #
    # **It now runs only on a genuinely EMPTY database, and that restriction fixes a real
    # production failure.** On a populated database it would create any table the new
    # models define but the schema lacks — at the models' FINAL shape, skipping every
    # migration in between. Alembic's recorded revision stays where it was, so the next
    # `upgrade head` tries to CREATE tables that already exist and dies with "table
    # product_categories already exists". That is exactly what happened on the EC2 box:
    # two deploys failed and rolled back, and the stamp had to be repaired by hand.
    #
    # Restricting it to an empty database removes the divergence entirely: either Alembic
    # built the schema, or there is no schema yet.
    async with engine.begin() as conn:
        existing = await conn.run_sync(
            lambda sync_conn: sa_inspect(sync_conn).get_table_names()
        )
        if not existing:
            await conn.run_sync(Base.metadata.create_all)
            logger.info("Empty database — schema created. Run 'alembic stamp head' next.")
        else:
            logger.info(
                "Database has %d tables; the schema belongs to Alembic "
                "(run 'alembic upgrade head' if a migration is pending)",
                len(existing),
            )
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
app.include_router(admin_users.router)
app.include_router(product_prices.router)


@app.exception_handler(RedirectException)
async def auth_redirect_handler(request: Request, exc: RedirectException):
    return RedirectResponse(url="/login", status_code=303)


@app.exception_handler(ForbiddenException)
async def forbidden_handler(request: Request, exc: ForbiddenException):
    """403, deliberately not a redirect to /login.

    Bouncing an authenticated user to the login page would look like their session had
    expired, and they would just log in again and loop. A 403 says 'you are signed in,
    this part is not yours'.

    The message no longer says "Admin only". With per-area permissions that is usually
    the wrong remedy: the accounts user refused the Projections tab does not need to
    become an administrator, they need that one area granted. Naming the wrong fix sends
    them to ask for the wrong thing.
    """
    return JSONResponse(
        {
            "error": "You do not have access to this section. Ask the owner to grant it "
            "from the Users screen."
        },
        status_code=403,
    )


# Page routes. Each passes `active` so templates/nav.html can highlight the
# current tab; the nav lives in that one partial because it used to be
# copy-pasted into every template and drifted (projections.html had no Shipment
# link, so opening Projections made the Shipment tab vanish).
#
# Auth is a dependency rather than an inline check so the rule lives in one place.
#
# Each page is gated on its AREA rather than on a role. That is what lets the owner
# grant the accounts person Invoice without also handing over Projections, which the
# two shared passwords could not express. `require_area` re-reads the grant from the
# database on every request, so revoking access takes effect immediately instead of
# whenever a week-long session cookie expires.
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, grant=Depends(require_area(permissions.DASHBOARD))):
    return templates.TemplateResponse(
        request, "index.html", {"active": "dashboard", "grant": grant}
    )


@app.get("/invoice-page", response_class=HTMLResponse)
async def invoice_page(request: Request, grant=Depends(require_area(permissions.INVOICE))):
    return templates.TemplateResponse(
        request, "invoice.html", {"active": "invoice", "grant": grant}
    )


@app.get("/churn-page", response_class=HTMLResponse)
async def churn_page(request: Request, grant=Depends(require_area(permissions.PORTFOLIO))):
    return templates.TemplateResponse(
        request, "churn.html", {"active": "churn", "grant": grant}
    )


@app.get("/projections-page", response_class=HTMLResponse)
async def projections_page(
    request: Request, grant=Depends(require_area(permissions.PROJECTIONS))
):
    return templates.TemplateResponse(
        request, "projections.html", {"active": "projections", "grant": grant}
    )


@app.get("/shipment-page", response_class=HTMLResponse)
async def shipment_page(
    request: Request, grant=Depends(require_area(permissions.SHIPMENT))
):
    return templates.TemplateResponse(
        request, "shipment.html", {"active": "shipment", "grant": grant}
    )


@app.get("/ops-page", response_class=HTMLResponse)
async def ops_page(request: Request, role: str = Depends(require_packing)):
    """The operations employee's only screen: record what was packed today.

    `require_packing`, not `require_ops_or_admin`. The difference was a real bug caught on
    production: `require_ops_or_admin` reads only the cookie, so a named account that had
    been DISABLED still opened this page for up to a week, while every other page cut it
    off immediately.

    `require_packing` re-checks a named account against the database and leaves
    shared-password sessions on the cookie alone — so OPS_PASSWORD still works even if the
    users table is missing, which is why the API routes keep the looser guard.

    `role` is handed to the template so it can show admin-only affordances without a
    second round trip.
    """
    return templates.TemplateResponse(request, "ops.html", {"active": "ops", "role": role})


@app.get("/pricing-page", response_class=HTMLResponse)
async def pricing_page(request: Request, grant=Depends(require_admin_grant)):
    """Purchase rate, HSN and GST per product. Administrators only.

    Admin rather than an area grant: a purchase rate is the cost side of the business, and
    the Accounts preset deliberately withholds purchase costs for the same reason.

    Takes the Grant so nav.html can render its own tab — passing None is what left the
    Users page reachable only by typing the URL.
    """
    return templates.TemplateResponse(
        request, "pricing.html", {"active": "pricing", "grant": grant}
    )


@app.get("/users-page", response_class=HTMLResponse)
async def users_page(request: Request, grant=Depends(require_admin_grant)):
    """Create logins and decide what each person may see. Administrators only.

    Takes the Grant rather than just the username so nav.html can render its own tab —
    passing None left this page as the one screen with no Users link, reachable only by
    typing the URL.
    """
    return templates.TemplateResponse(
        request, "users.html", {"active": "users", "grant": grant}
    )


@app.get("/no-access", response_class=HTMLResponse)
async def no_access(request: Request):
    """Where a signed-in user with no areas at all lands.

    Reachable only if the owner created an account and granted nothing. A 403 loop or a
    redirect to /login would look like a broken account; this says what happened and who
    to ask.
    """
    return templates.TemplateResponse(request, "no_access.html", {"active": ""})


@app.get("/health")
async def health():
    return {"status": "ok"}
