import hmac
import time
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import APIKeyHeader
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from payments.config import settings
from payments.db import Outbox, Payment, engine, session_factory
from payments.schemas import PaymentAccepted, PaymentCreate, PaymentDetail

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def authenticate(key: Annotated[str | None, Depends(api_key_header)]) -> None:
    if key is None or not hmac.compare_digest(key.encode(), settings.api_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid API key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(
    title="Async Payments",
    description=(
        "Асинхронная обработка платежей: transactional outbox, RabbitMQ, retry и webhook. "
        "Это эмулятор: реальные списания не выполняются. "
        "Для запросов нажмите **Authorize** и введите X-API-Key."
    ),
    version="1.0.0",
    lifespan=lifespan,
    redoc_url=None,
    swagger_ui_parameters={"persistAuthorization": True},
)

# ponytail: one API process; use a shared limiter if the public demo gains replicas.
tokens = 20.0
last_refill = time.monotonic()
MAX_BODY = 64 * 1024


class RequestLimits:
    """Bound request bodies before JSON decoding; limit creation in the public demo."""

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        global tokens, last_refill
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.application(scope, receive, send)
        if settings.demo_mode:
            now = time.monotonic()
            tokens = min(20.0, tokens + (now - last_refill) * 5)
            last_refill = now
            if tokens < 1:
                return await self.respond(send, 429, b'{"detail":"Rate limit exceeded"}')
            tokens -= 1
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY:
                return await self.respond(send, 413, b'{"detail":"Request body too large"}')
            if not message.get("more_body", False):
                break
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.application(scope, replay, send)

    @staticmethod
    async def respond(send, status: int, body: bytes):
        headers = [(b"content-type", b"application/json")]
        if status == 429:
            headers.append((b"retry-after", b"1"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(RequestLimits)


@app.get("/health", dependencies=[Depends(authenticate)], tags=["Operations"])
async def health():
    async with session_factory() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post(
    "/api/v1/payments",
    status_code=202,
    response_model=PaymentAccepted,
    dependencies=[Depends(authenticate)],
    tags=["Payments"],
    responses={401: {"description": "Invalid API key"}, 409: {"description": "Key conflict"}},
)
async def create_payment(
    body: PaymentCreate,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
):
    if not idempotency_key.strip():
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank")
    values = body.db_values()
    async with session_factory.begin() as session:
        payment = (
            await session.execute(
                insert(Payment)
                .values(**values, idempotency_key=idempotency_key)
                .on_conflict_do_nothing(index_elements=[Payment.idempotency_key])
                .returning(Payment)
            )
        ).scalar_one_or_none()
        if payment is None:
            payment = (
                await session.execute(
                    select(Payment).where(Payment.idempotency_key == idempotency_key)
                )
            ).scalar_one()
            if any(getattr(payment, key) != value for key, value in values.items()):
                raise HTTPException(status_code=409, detail="Idempotency-Key payload mismatch")
        else:
            session.add(
                Outbox(
                    payment_id=payment.id,
                    payload={"payment_id": str(payment.id), "attempt": 1},
                    routing_key="payments.new",
                    attempt=1,
                )
            )
        result = PaymentAccepted(
            payment_id=payment.id, status=payment.status, created_at=payment.created_at
        )
    return result


@app.get(
    "/api/v1/payments/{payment_id}",
    response_model=PaymentDetail,
    dependencies=[Depends(authenticate)],
    tags=["Payments"],
    responses={401: {"description": "Invalid API key"}, 404: {"description": "Not found"}},
)
async def get_payment(payment_id: UUID):
    async with session_factory() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        return PaymentDetail(
            payment_id=payment.id,
            status=payment.status,
            created_at=payment.created_at,
            processed_at=payment.processed_at,
            webhook_delivered_at=payment.webhook_delivered_at,
            idempotency_key=payment.idempotency_key,
            amount=payment.amount,
            currency=payment.currency,
            description=payment.description,
            metadata=payment.metadata_json,
            webhook_url=payment.webhook_url,
        )
