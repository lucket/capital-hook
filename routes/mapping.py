"""CRUD UI for the ticker-mapping tables (provider / markets / ticker / mapping).

Writes come in as JSON (htmx json-enc) and each handler returns the re-rendered
section fragment so the table refreshes in place. Ticker pickers in the mapping
form are datalists populated by searching the `ticker` table.
"""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.templating import Jinja2Templates

import ticker_map as tm

mapping = APIRouter()
templates = Jinja2Templates(directory="views")


def _frag(name: str, request: Request, **ctx):
    return templates.TemplateResponse(request, f"components/mapping/{name}", {"request": request, **ctx})


async def _sections_frag(request):
    """Re-render all four sections together (used by the env reload)."""
    return _frag(
        "sections.html", request,
        providers=await tm.list_providers(),
        markets=await tm.list_markets(),
        tickers=await tm.list_tickers(),
        mappings=await tm.list_mappings(),
    )


async def _providers_frag(request):
    return _frag("providers.html", request, providers=await tm.list_providers())


async def _markets_frag(request):
    return _frag("markets.html", request, markets=await tm.list_markets(), providers=await tm.list_providers())


async def _tickers_frag(request):
    return _frag("tickers.html", request, tickers=await tm.list_tickers(), providers=await tm.list_providers())


async def _mappings_frag(request):
    return _frag("mappings.html", request, mappings=await tm.list_mappings(), providers=await tm.list_providers())


@mapping.get("/mapping", tags=["Mapping"])
async def mapping_page(request: Request):
    from memory import settings
    return templates.TemplateResponse(request, "pages/mapping.html", {
        "request": request,
        "providers": await tm.list_providers(),
        "markets": await tm.list_markets(),
        "tickers": await tm.list_tickers(),
        "mappings": await tm.list_mappings(),
        "env_count": len(tm._env_index),
        "mode": settings.TRADE_MODE.value,
    })


@mapping.api_route("/mapping/reload-env", methods=["GET", "POST"], tags=["Mapping"])
async def reload_env(request: Request):
    """Re-read TICKER_CONFIG from the environment (and .env) without a restart.

    POST (htmx button) returns the re-rendered sections; GET (plain link)
    reloads and redirects back to the Mapping page.
    """
    await tm.load_env_mappings(reload_dotenv=True)
    if request.method == "GET":
        return RedirectResponse("/mapping", status_code=303)
    return await _sections_frag(request)


# --- providers ---
@mapping.post("/mapping/provider")
async def create_provider(request: Request):
    body = await request.json()
    pid = (body.get("id") or "").strip()
    if pid:
        await tm.add_provider(pid, (body.get("name") or "").strip())
    return await _providers_frag(request)


@mapping.post("/mapping/provider/delete")
async def remove_provider(request: Request):
    body = await request.json()
    await tm.delete_provider((body.get("id") or "").strip())
    return await _providers_frag(request)


# --- markets ---
@mapping.post("/mapping/market")
async def create_market(request: Request):
    body = await request.json()
    provider_id = (body.get("provider_id") or "").strip()
    market_id = (body.get("market_id") or "").strip()
    if provider_id and market_id:
        await tm.add_market(provider_id, market_id, (body.get("description") or "").strip() or None)
    return await _markets_frag(request)


@mapping.post("/mapping/market/delete")
async def remove_market(request: Request):
    body = await request.json()
    await tm.delete_market((body.get("provider_id") or "").strip(), (body.get("market_id") or "").strip())
    return await _markets_frag(request)


# --- tickers ---
@mapping.post("/mapping/ticker")
async def create_ticker(request: Request):
    body = await request.json()
    provider_id = (body.get("provider_id") or "").strip()
    ticker = (body.get("ticker") or "").strip()
    if provider_id and ticker:
        await tm.add_ticker(provider_id, ticker, (body.get("description") or "").strip() or None, (body.get("market_id") or "").strip() or None)
    return await _tickers_frag(request)


@mapping.post("/mapping/ticker/delete")
async def remove_ticker(request: Request):
    body = await request.json()
    await tm.delete_ticker((body.get("provider_id") or "").strip(), (body.get("ticker") or "").strip())
    return await _tickers_frag(request)


# --- mappings ---
@mapping.post("/mapping/link")
async def create_mapping(request: Request):
    body = await request.json()
    sp = (body.get("source_provider_id") or "").strip()
    st = (body.get("source_ticker") or "").strip()
    tp = (body.get("target_provider_id") or "").strip()
    tt = (body.get("target_ticker") or "").strip()
    if sp and st and tp and tt:
        await tm.add_mapping(sp, st, tp, tt)
    return await _mappings_frag(request)


@mapping.post("/mapping/link/delete")
async def remove_mapping(request: Request):
    body = await request.json()
    await tm.delete_mapping(
        (body.get("source_provider_id") or "").strip(),
        (body.get("source_ticker") or "").strip(),
        (body.get("target_provider_id") or "").strip(),
    )
    return await _mappings_frag(request)


# --- ticker search (datalist options for the mapping form) ---
@mapping.get("/mapping/search/tickers")
async def search_tickers(request: Request):
    qp = request.query_params
    provider_id = qp.get("provider_id") or qp.get("source_provider_id") or qp.get("target_provider_id")
    q = qp.get("q") or qp.get("source_ticker") or qp.get("target_ticker")
    tickers = await tm.list_tickers(provider_id=provider_id or None, q=q or None)
    return _frag("ticker_options.html", request, tickers=tickers)
