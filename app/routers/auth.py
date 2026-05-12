from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer

from app.config import get_settings

router = APIRouter()
settings = get_settings()
templates = Jinja2Templates(directory="templates")
serializer = URLSafeTimedSerializer(settings.secret_key)

SESSION_COOKIE = "session_token"
SESSION_MAX_AGE = 86400 * 7


def get_current_user(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        serializer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except Exception:
        return False


def require_auth(request: Request):
    if not get_current_user(request):
        raise RedirectException()
    return True


class RedirectException(Exception):
    pass


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login(request: Request, password: str = Form(...)):
    if password == settings.app_password:
        token = serializer.dumps({"authenticated": True})
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax"
        )
        return response
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Invalid password"}
    )


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
