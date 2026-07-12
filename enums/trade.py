from enum import Enum


class TradeDirection(Enum):
    SELL = "SELL"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    
    
class ExitType(Enum):
    TP = "TP"
    SL = "SL"
    USER = "USER"
    STRATEGY = "STRATEGY"
    EOD_CLOSE = "EOD_CLOSE"
    EOW_CLOSE = "EOW_CLOSE"
    RECALIBRATE = "RECALIBRATE"
    
class TradeMode(Enum):
    DEMO = "DEMO"
    LIVE = "LIVE"

class AmountType(Enum):
    FIXED = "FIXED"      # `amount` is a fixed cash figure in the account currency
    PERCENT = "PERCENT"  # `amount` is a percentage of total account value

class TradeInstrument(Enum):
    CRYPTOCURRENCIES = "CRYPTOCURRENCIES"
    SHARES = "SHARES"
    INDICES = "INDICES"
    CURRENCIES = "CURRENCIES"
    COMMODITIES = "COMMODITIES"
    UNKONWN = ""