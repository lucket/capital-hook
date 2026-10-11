"""Enumerations shared by the client, the stream and applications built on them."""
from enum import Enum


class TradeDirection(Enum):
    SELL = "SELL"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"


class TradeMode(Enum):
    """Which Capital.com environment a client talks to."""
    DEMO = "DEMO"
    LIVE = "LIVE"


class WorkingOrderType(Enum):
    """Kind of working order: LIMIT (better price than the market) or STOP (worse price, i.e. breakout)."""
    LIMIT = "LIMIT"
    STOP = "STOP"


class TradeInstrument(Enum):
    """Capital.com `instrumentType` of a market."""
    CRYPTOCURRENCIES = "CRYPTOCURRENCIES"
    SHARES = "SHARES"
    INDICES = "INDICES"
    CURRENCIES = "CURRENCIES"
    COMMODITIES = "COMMODITIES"
    UNKNOWN = ""
    UNKONWN = ""  # alias kept for the original (misspelled) member name

    @classmethod
    def _missing_(cls, value):
        # instrument types we do not model explicitly (bonds, options, ...)
        return cls.UNKNOWN
