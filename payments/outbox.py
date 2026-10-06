import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from payments.broker import broker, dead_exchange, exchange
from payments.config import settings
from payments.db import session_factory
from payments.models import Outbox

log = logging.getLogger("payments.outbox")


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
