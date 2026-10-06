import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import AliasChoices, AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


class Currency(StrEnum):
    RUB = "RUB"
    USD = "USD"
    EUR = "EUR"


class Status(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]
    currency: Currency
    description: Annotated[str, Field(min_length=1, max_length=1024)]
    metadata: dict[str, Any] = Field(default_factory=dict)
    webhook_url: Annotated[AnyHttpUrl, Field(max_length=2048)]

    @field_validator("metadata")
    @classmethod
    def json_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        json.dumps(value, allow_nan=False)
        return value

    @field_validator("webhook_url")
    @classmethod
    def no_credentials(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username is not None or value.password is not None or value.fragment:
            raise ValueError("Webhook URL must not contain credentials or a fragment")
        return value


class PaymentAccepted(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_id: UUID = Field(validation_alias=AliasChoices("payment_id", "id"))
    status: Status
    created_at: datetime


class PaymentDetail(PaymentAccepted):
    amount: Decimal
    currency: Currency
    description: str
    metadata: dict[str, Any] = Field(validation_alias=AliasChoices("metadata_json", "metadata"))
    idempotency_key: str
    webhook_url: str
    processed_at: datetime | None
    webhook_delivered_at: datetime | None


class PaymentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payment_id: UUID
    attempt: Annotated[int, Field(ge=1, le=3)]
