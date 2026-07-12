from pydantic import BaseModel, Field
from enums.trade import TradeDirection, ExitType, TradeMode
from typing import Annotated
from typing import Literal, List, Optional


class TradingViewWebhookModel(BaseModel):
    epic: str
    direction: TradeDirection
    amount: Annotated[int, Field(ge=5)]
    hook_name: str
    profit: Annotated[int, Field(ge=5)]
    loss: Annotated[int, Field(ge=5)]
    exit_criteria: List[ExitType]



class HookPayloadModel(BaseModel):
    hook_name: str
    direction: TradeDirection
    trade_amount: Annotated[float, Field(gt=10)]
    stop_loss: Annotated[float, Field(gt=10)]
    take_profit: Annotated[float, Field(gt=10)]
    take_profit_exit: Optional[Literal['on']] = None
    stop_loss_exit: Optional[Literal['on']] = None
    strategy_exit: Optional[Literal['on']] = None
    end_of_day_close_exit: Optional[Literal['on']] = None
    end_of_week_close_exit: Optional[Literal['on']] = None


class TradeModeModel(BaseModel):
    mode: TradeMode



class PositionsModel(BaseModel):
    id: str
    epic: str
    size: float
    hook_name: str
    direction: TradeDirection
    entry_price: float
    entry_date: str
    exit_criteria: List[ExitType]
    profit_price: float
    loss_price: float
    mode: TradeMode