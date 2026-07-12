from pydantic import BaseModel, Field, model_validator
from enums.trade import TradeDirection, ExitType, TradeMode, AmountType
from typing import Annotated
from typing import Literal, List, Optional


class TradingViewWebhookModel(BaseModel):
    epic: str
    direction: TradeDirection
    amount: Annotated[float, Field(gt=0)]
    amount_type: AmountType = AmountType.FIXED
    hook_name: str
    profit: Annotated[int, Field(ge=5)]
    loss: Annotated[int, Field(ge=5)]
    exit_criteria: List[ExitType]

    @model_validator(mode="after")
    def _validate_amount(self):
        # FIXED: at least 5 units of account currency.
        # PERCENT: a sensible slice of account value, within (0, 100].
        if self.amount_type == AmountType.FIXED and self.amount < 5:
            raise ValueError("amount must be >= 5 when amount_type is FIXED")
        if self.amount_type == AmountType.PERCENT and not 0 < self.amount <= 100:
            raise ValueError("amount must be within (0, 100] when amount_type is PERCENT")
        return self



class HookPayloadModel(BaseModel):
    hook_name: str
    direction: TradeDirection
    trade_amount: Annotated[float, Field(gt=0)]
    amount_type: AmountType = AmountType.FIXED
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