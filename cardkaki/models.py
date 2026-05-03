from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Rounding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["none", "floor_sgd_1", "floor_sgd_5"] = "none"
    unis_per_block: float | None = None


class Bonus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rate_mpd: float
    categories: list[str]
    excluded_categories: list[str] = Field(default_factory=list)
    applies_to_fcy: bool = False
    cap_sgd: float | None = None
    cap_period: Literal[
        "calendar_month", "statement_month", "calendar_quarter", "statement_quarter"
    ] | None = None
    min_spend_sgd: float | None = None
    min_spend_period: Literal[
        "calendar_month", "statement_month", "calendar_quarter", "statement_quarter"
    ] | None = None
    label: str | None = None


class Card(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    issuer: str
    network: Literal["visa", "mastercard", "amex"]
    base_rate_mpd: float = 0.4
    base_rate_mpd_fcy: float | None = None
    applies_base_to_fcy: bool = True
    bonus: list[Bonus] = Field(default_factory=list)
    rounding: Rounding = Field(default_factory=Rounding)
    pool: str | None = None
    tracks_by: Literal["posting_date", "transaction_date"] = "posting_date"
    fcy_fee: float = 0.0325
    notes: str | None = None


class ParsedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    merchant: str
    amount_sgd: float = Field(gt=0)
    is_fcy: bool = False


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: str
    card_name: str
    miles: int
    effective_mpd: float
    reasons: list[str] = Field(default_factory=list)
