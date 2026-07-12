from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from model import TradingViewWebhookModel
from memory import memory, settings
from logger import Logger

webhook = APIRouter()

@webhook.post("/trading-view")
async def tradingview_webhook_route(data: TradingViewWebhookModel, request: Request, background_task: BackgroundTasks):

    # validate whitelisted Tradingview IP Address using the transport-level peer
    # address only: X-Forwarded-For is client-controlled and spoofable. Behind a
    # reverse proxy, run uvicorn with --proxy-headers --forwarded-allow-ips so
    # request.client reflects the real client.
    client_ip = request.client.host if request.client else None
    if str(client_ip) not in settings.TRADINGVIEW_IP_ADDRESS:
        await Logger.app_log(title="TradingView_Webhook_Error", message=f"IP {client_ip} not whitelisted")
        return JSONResponse(status_code=403, content={"message": "IP not whitelisted"})
    
    # resolve the executing epic. `epic` (if given) is used directly — original
    # behavior. Otherwise map `ticker` from its source provider to the executing
    # provider's epic via the ticker mapping tables.
    epic = data.epic.strip() if data.epic else None
    if not epic:
        from ticker_map import resolve_epic
        source = (data.source or settings.DEFAULT_SOURCE_PROVIDER)
        epic = await resolve_epic(source_provider_id=source, source_ticker=data.ticker, target_provider_id=settings.EXECUTING_PROVIDER)
        if not epic:
            await Logger.app_log(title="TradingView_Webhook_Error", message=f"No mapping for {source}:{data.ticker} -> {settings.EXECUTING_PROVIDER}")
            return JSONResponse(status_code=400, content={"message": "No ticker mapping"})

    # validate capital.com epic
    if epic not in memory.epics:
        await Logger.app_log(title="TradingView_Webhook_Error", message=f"Epic {epic} not available")
        return JSONResponse(status_code=400, content={"message": "Invalid epic"})

    #
    if memory.get_trading_view_hooked_trade_side(epic, data.hook_name) == data.direction: # check if the trade is already executed on trade side
        await Logger.app_log(title="TradingView_Webhook_Error", message=f"Trade already executed for {epic} {data.direction.value}")
    else:
        from hook_trade import HookedTradeExecution
        hooked_trade = HookedTradeExecution(epic=epic, trade_amount=data.amount, profit=data.profit, loss=data.loss, hook_name=data.hook_name, trade_direction=data.direction, exit_criteria=data.exit_criteria, amount_type=data.amount_type)
        background_task.add_task(hooked_trade.execute_trade)

        memory.update_trading_view_hooked_trades(epic=epic, direction=data.direction, hook_name=data.hook_name) # update the hooked trades in settings
        await Logger.app_log(title="TradingView_Webhook", message=f"Webhook received from {client_ip} for {epic} {data.direction.value} trade")