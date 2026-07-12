from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from routes.api import api
from routes.auth import auth
from routes.webhook import webhook
from routes.view import view
from memory import memory, settings
from service.capital_api import get_account_preferences, update_markets, update_auth_header
from auth import is_authenticated
from job import jobs


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown lifecycle."""
    from database import migrate_db

    # --- startup ---
    await migrate_db()  # migrate DB
    await settings.sync_trade_mode()

    # update market data
    await update_auth_header()
    await update_markets()
    print(f"{len(memory.epics):,} market data updated")

    # prefetch preference data
    preferences = await get_account_preferences()
    memory.preferences = preferences

    # initialize jobs
    await jobs.run()

    # resume trades
    await jobs.resume_trades()

    yield

    # --- shutdown ---
    # close DB connection
    if settings.DB_CONNECTION:
        await settings.DB_CONNECTION.close()
        settings.DB_CONNECTION = None

    # close HTTP session
    await settings.session.aclose()


app = FastAPI(
    title=settings.APP_TITLE,
    lifespan=lifespan,
)


# Paths reachable without a session. The webhook stays open because it is
# authenticated separately by the TradingView IP whitelist, and TradingView
# cannot present a session cookie.
def _is_public(path: str) -> bool:
    if path in ("/login", "/logout"):
        return True
    return path.startswith("/assets/") or path.startswith("/webhook")


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """Require a valid session for every route except the public ones."""
    path = request.url.path
    if _is_public(path) or is_authenticated(request):
        return await call_next(request)

    # htmx request (polling/actions): trigger a client-side redirect to login.
    if request.headers.get("hx-request"):
        response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
        response.headers["HX-Redirect"] = "/login"
        return response

    # Plain browser navigation: redirect to the login page.
    if request.method == "GET" and not path.startswith("/api"):
        return RedirectResponse("/login", status_code=302)

    # Programmatic/API access without a session.
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


app.include_router(auth, tags=["Auth"])
app.include_router(view, tags=["View"])
app.include_router(api, prefix="/api", tags=["API"])
app.include_router(webhook, prefix="/webhook", tags=["Webhook"])


app.mount("/assets", StaticFiles(directory="assets"), name="assets")
