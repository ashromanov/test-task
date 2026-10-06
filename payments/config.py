import os

from pydantic import BaseModel, Field


class Settings(BaseModel):
    database_url: str = (
        "postgresql+asyncpg://payments:local-postgres-password@localhost:5432/payments"
    )
    rabbitmq_url: str = "amqp://payments:local-rabbitmq-password@localhost:5672/"
    api_key: str = Field(min_length=16)
    demo_mode: bool = False
    consumer_prefetch: int = Field(default=16, ge=1, le=256)
    db_pool_size: int = Field(default=5, ge=1, le=64)
    outbox_batch_size: int = Field(default=100, ge=1, le=1000)
    outbox_poll_interval: float = Field(default=0.25, gt=0, le=2)
    webhook_timeout: float = Field(default=5, gt=0, le=30)
    webhook_allowed_hosts: str = ""


settings = Settings.model_validate(
    {name: os.environ[name.upper()] for name in Settings.model_fields if name.upper() in os.environ}
)
