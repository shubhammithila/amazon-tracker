import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.database import engine, Base
from app.routers import auth, scrape, products, keywords, ws, invoice, churn
from app.routers.auth import get_current_user, RedirectException
from app.scheduler import setup_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created")
    setup_scheduler()
    yield
    from app.scheduler import scheduler
    scheduler.shutdown(wait=False)
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


@app.exception_handler(RedirectException)
async def auth_redirect_handler(request: Request, exc: RedirectException):
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not get_current_user(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "index.html")


@app.get("/history-page", response_class=HTMLResponse)
async def history_page(request: Request):
    if not get_current_user(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "history.html")


@app.get("/keywords-page", response_class=HTMLResponse)
async def keywords_page(request: Request):
    if not get_current_user(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "keywords.html")


@app.get("/invoice-page", response_class=HTMLResponse)
async def invoice_page(request: Request):
    if not get_current_user(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "invoice.html")


@app.get("/churn-page", response_class=HTMLResponse)
async def churn_page(request: Request):
    if not get_current_user(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "churn.html")


@app.get("/health")
async def health():
    return {"status": "ok"}
