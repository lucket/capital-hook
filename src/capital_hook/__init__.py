"""capital_hook - async client and live price streaming for Capital.com.

    from capital_hook import CapitalClient, PriceStream, TradeMode, TradeDirection

    client = CapitalClient(identity, password, api_key, mode=TradeMode.DEMO)
    await client.login()
    ref = await client.open_position("GOLD", 1.0, TradeDirection.BUY)
    deal_id = await client.find_deal_id("GOLD", 1.0, TradeDirection.BUY)
    await client.close_position(deal_id)

See ``capital_hook.hours`` for opening-hours helpers and ``capital_hook.testing``
for an in-memory fake of the API to test applications against.
"""
from .client import DEMO_HOST, LIVE_HOST, STREAM_URL, CapitalClient
from .enums import TradeDirection, TradeInstrument, TradeMode
from .errors import APIError, AuthError, CapitalError, StreamError
from .hours import is_closing_for_day, is_closing_for_week, is_market_closed
from .streaming import PriceStream, Quote

__version__ = "0.2.0"

__all__ = [
    "CapitalClient", "PriceStream", "Quote",
    "TradeDirection", "TradeInstrument", "TradeMode",
    "CapitalError", "AuthError", "APIError", "StreamError",
    "is_market_closed", "is_closing_for_day", "is_closing_for_week",
    "LIVE_HOST", "DEMO_HOST", "STREAM_URL", "__version__",
]
