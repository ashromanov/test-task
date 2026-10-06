from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from payments.db import session_factory
from payments.models import Outbox, Payment
from payments.schemas import PaymentCreate


class IdempotencyConflict(ValueError):
    pass


async def create_payment(body: PaymentCreate, idempotency_key: str) -> Payment:
    values = {
        "amount": body.amount,
        "currency": body.currency.value,
        "description": body.description,
        "metadata_json": body.metadata,
        "webhook_url": str(body.webhook_url),
    }
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
                raise IdempotencyConflict("Idempotency-Key payload mismatch")
        else:
            session.add(
                Outbox(
                    payment_id=payment.id,
                    payload={"payment_id": str(payment.id), "attempt": 1},
                    routing_key="payments.new",
                    attempt=1,
                )
            )
    return payment


async def get_payment(payment_id: UUID) -> Payment | None:
    async with session_factory() as session:
        return await session.get(Payment, payment_id)
