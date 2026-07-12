from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from auth import (
    COOKIE_NAME,
    SESSION_TTL,
    create_session_token,
    is_authenticated,
    verify_password,
)
from logger import Logger

auth = APIRouter()
templates = Jinja2Templates(directory="views")


@auth.get("/login", tags=["Auth"])
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "pages/login.html", {"request": request, "error": None})


@auth.post("/login", tags=["Auth"])
async def login_submit(request: Request):
    # Parse the urlencoded body directly to avoid a python-multipart dependency.
    body = (await request.body()).decode("utf-8", errors="ignore")
    password = parse_qs(body).get("password", [""])[0]
    if not verify_password(password):
        client_ip = request.client.host if request.client else "unknown"
        await Logger.app_log(title="LOGIN_FAIL", message=f"Failed login attempt from {client_ip}")
        return templates.TemplateResponse(request, 
            "pages/login.html",
            {"request": request, "error": "Invalid password"},
            status_code=401,
        )

    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        COOKIE_NAME,
        create_session_token(),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
    )
    return response


@auth.get("/logout", tags=["Auth"])
@auth.post("/logout", tags=["Auth"])
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response
