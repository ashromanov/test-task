import asyncio
import contextlib
import logging
import random
import signal
from datetime import UTC, datetime
from weakref import WeakValueDictionary

from faststream import AckPolicy
from faststream.rabbit import RabbitMessage
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from payments import webhook
from payments.broker import (
    broker,
    dead_exchange,
    declare_topology,
    exchange,
    new_queue,
    retry_queues,
)
from payments.config import settings
from payments.db import Outbox, Payment, engine, session_factory
from payments.schemas import PaymentEvent

log = logging.getLogger("payments.worker")
locks: WeakValueDictionary = WeakValueDictionary()
http = None


async def simulate_gateway() -> str:
    await asyncio.sleep(random.uniform(2, 5))
    return "succeeded" if random.random() < 0.9 else "failed"


def notification(payment: Payment) -> dict:
    return {
        "event_id": str(payment.id),
        "event_type": "payment.processed",
        "payment_id": str(payment.id),
        "status": payment.status,
        "amount": format(payment.amount, ".2f"),
        "currency": payment.currency,
        "description": payment.description,
        "metadata": payment.metadata_json,
        "created_at": payment.created_at.isoformat(),
        "processed_at": payment.processed_at.isoformat(),
    }


async def process(event: PaymentEvent) -> None:
    # ponytail: one consumer process; distributed ownership is needed before adding replicas.
    lock = locks.setdefault(event.payment_id, asyncio.Lock())
    async with lock:
        async with session_factory() as session:
            payment = await session.get(Payment, event.payment_id)
        if payment is None:
            raise ValueError("Payment does not exist")
        if payment.webhook_delivered_at or event.attempt <= payment.completed_attempts:
            return
        try:
            if payment.status == "pending":
                result = await simulate_gateway()
                async with session_factory.begin() as session:
                    payment = await session.get(Payment, event.payment_id, with_for_update=True)
                    if payment.status == "pending":
                        payment.status = result
                        payment.processed_at = datetime.now(UTC)
                log.info("payment_processed payment_id=%s status=%s", payment.id, payment.status)
            await webhook.deliver(http, payment.webhook_url, notification(payment))
        except SQLAlchemyError:
            raise
        except Exception as exc:
            error = str(exc)[:512]
        else:
            error = None
        async with session_factory.begin() as session:
            payment = await session.get(Payment, event.payment_id, with_for_update=True)
            payment.completed_attempts = event.attempt
            payment.last_error = error
            if error is None:
                payment.webhook_delivered_at = datetime.now(UTC)
            else:
                if event.attempt == 3 and payment.status == "pending":
                    payment.status = "failed"
                    payment.processed_at = datetime.now(UTC)
                next_attempt = event.attempt + 1
                route = (
                    retry_queues[event.attempt - 1].name if event.attempt < 3 else "payments.dlq"
                )
                session.add(
                    Outbox(
                        payment_id=payment.id,
                        attempt=next_attempt,
                        routing_key=route,
                        payload={
                            "payment_id": str(payment.id),
                            "attempt": next_attempt,
                            **({"error": error} if next_attempt == 4 else {}),
                        },
                    )
                )
        log.info(
            "webhook_attempt payment_id=%s attempt=%s delivered=%s error=%s",
            event.payment_id,
            event.attempt,
            error is None,
            error,
        )


@broker.subscriber(new_queue, exchange, ack_policy=AckPolicy.MANUAL, no_reply=True)
async def handle(message: RabbitMessage) -> None:
    try:
        event = PaymentEvent.model_validate_json(message.body)
        await process(event)
    except SQLAlchemyError:
        # Database outages are infrastructure failures; don't discard unpaid work.
        log.exception("database_unavailable")
        await asyncio.sleep(2)
        await message.nack(requeue=True)
    except (ValidationError, ValueError) as exc:
        # Malformed/unknown events cannot be stored against a payment. Confirm before ACK.
        attempt = message.headers.get("poison-attempt", 1)
        if not isinstance(attempt, int) or not 1 <= attempt <= 3:
            attempt = 1
        target = retry_queues[attempt - 1].name if attempt < 3 else "payments.dlq"
        try:
            await broker.publish(
                message.body,
                exchange=exchange if attempt < 3 else dead_exchange,
                routing_key=target,
                persist=True,
                mandatory=True,
                timeout=5,
                message_id=message.message_id,
                headers={"poison-attempt": attempt + 1, "error": str(exc)[:512]},
            )
        except Exception:
            log.exception("poison_publish_failed")
            await asyncio.sleep(2)
            await message.nack(requeue=True)
        else:
            await message.ack()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("consumer_error")
        await asyncio.sleep(2)
        await message.nack(requeue=True)
    else:
        await message.ack()


async def publish_batch() -> int:
    async with session_factory.begin() as session:
        events = (
            await session.scalars(
                select(Outbox)
                .where(Outbox.published_at.is_(None))
                .order_by(Outbox.created_at, Outbox.id)
                .limit(settings.outbox_batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for event in events:
            await broker.publish(
                event.payload,
                exchange=exchange if event.routing_key != "payments.dlq" else dead_exchange,
                routing_key=event.routing_key,
                persist=True,
                mandatory=True,
                timeout=5,
                message_id=str(event.id),
            )
            event.published_at = datetime.now(UTC)
        return len(events)


async def publish_outbox(stop: asyncio.Event) -> None:
    idle = settings.outbox_poll_interval
    while not stop.is_set():
        try:
            count = await publish_batch()
        except Exception:
            log.exception("outbox_publish_failed")
            count = 0
        idle = settings.outbox_poll_interval if count else min(idle * 2, 2)
        if count == settings.outbox_batch_size:
            continue
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=idle)


async def main() -> None:
    global http
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        async with webhook.client() as http:
            await broker.connect()
            await declare_topology()
            await broker.start()
            publisher = asyncio.create_task(publish_outbox(stop))
            log.info("consumer_ready prefetch=%s", settings.consumer_prefetch)
            try:
                await stop.wait()
            finally:
                await broker.stop()
                publisher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await publisher
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import uvloop

    uvloop.run(main())
