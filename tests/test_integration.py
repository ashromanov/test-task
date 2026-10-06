"""Real PostgreSQL/RabbitMQ checks: run with compose.yaml + compose.test.yaml."""
# ruff: noqa: E402 -- configure local integration URLs before importing the application.

import asyncio
import json
import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import aio_pika
import httpx

environment = dict(
    line.split("=", 1)
    for line in Path(".env").read_text().splitlines()
    if line and not line.startswith("#")
)
os.environ["API_KEY"] = "local-development-key"
os.environ["DATABASE_URL"] = (
    f"postgresql+asyncpg://payments:{environment['POSTGRES_PASSWORD']}@127.0.0.1:15432/payments"
)
os.environ["RABBITMQ_URL"] = f"amqp://payments:{environment['RABBITMQ_PASSWORD']}@127.0.0.1:15673/"

from sqlalchemy import func, select

from payments import worker
from payments.broker import broker
from payments.config import settings
from payments.db import Outbox, Payment, engine, session_factory
from payments.schemas import PaymentEvent

COMPOSE = ["docker", "compose", "-f", "compose.yaml", "-f", "compose.test.yaml"]
BODY = {
    "amount": "150.50",
    "currency": "RUB",
    "description": "Integration test",
    "metadata": {"order": "test"},
}


async def compose(*args):
    proc = await asyncio.create_subprocess_exec(
        *COMPOSE, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    output, _ = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(output.decode())


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            base_url="http://127.0.0.1:18000",
            headers={"X-API-Key": "local-development-key"},
            timeout=10,
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await engine.dispose()

    async def create(self, scenario="ok", key=None, **change):
        path = f"/{scenario}/{uuid.uuid4()}"
        body = BODY | {"webhook_url": f"http://receiver:8080{path}"} | change
        response = await self.client.post(
            "/api/v1/payments",
            headers={"Idempotency-Key": key or str(uuid.uuid4())},
            json=body,
        )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["payment_id"], path, body

    async def wait_for(self, check, deadline_seconds=40):
        deadline = asyncio.get_running_loop().time() + deadline_seconds
        while asyncio.get_running_loop().time() < deadline:
            result = await check()
            if result:
                return result
            await asyncio.sleep(0.2)
        self.fail("Timed out waiting for pipeline")

    async def payment(self, payment_id):
        async with session_factory() as session:
            return await session.get(Payment, uuid.UUID(payment_id))

    async def receiver(self, path):
        response = await self.client.get("http://127.0.0.1:18081/")
        response.raise_for_status()
        return response.json().get(path, [])

    async def delivered(self, payment_id):
        payment = await self.payment(payment_id)
        return payment if payment.webhook_delivered_at else None

    async def test_concurrent_idempotency(self):
        key = str(uuid.uuid4())
        body = BODY | {"webhook_url": f"http://receiver:8080/ok/{key}"}
        responses = await asyncio.gather(
            *[
                self.client.post("/api/v1/payments", headers={"Idempotency-Key": key}, json=body)
                for _ in range(20)
            ]
        )
        self.assertTrue(all(r.status_code == 202 for r in responses))
        ids = {r.json()["payment_id"] for r in responses}
        self.assertEqual(len(ids), 1)
        payment_id = ids.pop()
        async with session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Outbox)
                .where(Outbox.payment_id == uuid.UUID(payment_id))
            )
            self.assertEqual(count, 1)
        conflict = await self.client.post(
            "/api/v1/payments",
            headers={"Idempotency-Key": key},
            json=body | {"amount": "151.50"},
        )
        self.assertEqual(conflict.status_code, 409)
        detail = await self.client.get(f"/api/v1/payments/{payment_id}")
        self.assertEqual(detail.json()["amount"], "150.50")
        self.assertEqual(
            (await self.client.get(f"/api/v1/payments/{uuid.uuid4()}")).status_code, 404
        )

    async def test_webhook_retry_and_deduplication(self):
        payment_id, path, _ = await self.create("flaky")
        completed = await self.wait_for(lambda: self.delivered(payment_id))
        self.assertEqual(completed.completed_attempts, 3)
        callbacks = await self.receiver(path)
        self.assertEqual(len(callbacks), 3)
        self.assertEqual(len({r["payload"]["event_id"] for r in callbacks}), 1)
        self.assertEqual(len({r["payload"]["processed_at"] for r in callbacks}), 1)
        self.assertGreaterEqual(callbacks[1]["at"] - callbacks[0]["at"], 0.9)
        self.assertGreaterEqual(callbacks[2]["at"] - callbacks[1]["at"], 1.9)
        connection = await aio_pika.connect_robust(settings.rabbitmq_url)
        async with connection:
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            exchange = await channel.get_exchange("payments")
            await exchange.publish(
                aio_pika.Message(json.dumps({"payment_id": payment_id, "attempt": 1}).encode()),
                routing_key="payments.new",
                mandatory=True,
            )
        await asyncio.sleep(1)
        self.assertEqual(len(await self.receiver(path)), 3)

    async def test_exhausted_webhook_reaches_dlq(self):
        payment_id, path, _ = await self.create("fail")

        async def exhausted():
            payment = await self.payment(payment_id)
            return payment if payment.completed_attempts == 3 else None

        completed = await self.wait_for(exhausted)
        self.assertIn(completed.status, ["succeeded", "failed"])
        self.assertIsNone(completed.webhook_delivered_at)
        self.assertEqual(len(await self.receiver(path)), 3)
        connection = await aio_pika.connect_robust(settings.rabbitmq_url)
        async with connection:
            channel = await connection.channel()
            queue = await channel.get_queue("payments.dlq")

            async def find():
                message = await queue.get(fail=False)
                if message is None:
                    return None
                payload = json.loads(message.body)
                await message.ack()
                return payload if payload.get("payment_id") == payment_id else None

            dead = await self.wait_for(find)
            self.assertEqual(dead["attempt"], 4)
            self.assertIn("503", dead["error"])

    async def test_outbox_survives_broker_outage(self):
        await compose("stop", "rabbitmq")
        try:
            payment_id, _, _ = await self.create()
            await asyncio.sleep(2)
            async with session_factory() as session:
                event = await session.scalar(
                    select(Outbox).where(Outbox.payment_id == uuid.UUID(payment_id))
                )
                self.assertIsNone(event.published_at)
        finally:
            await compose("start", "rabbitmq")
        await self.wait_for(lambda: self.delivered(payment_id), deadline_seconds=60)

    async def test_restart_during_processing(self):
        payment_id, path, _ = await self.create()

        async def published():
            async with session_factory() as session:
                event = await session.scalar(
                    select(Outbox).where(Outbox.payment_id == uuid.UUID(payment_id))
                )
                return event.published_at

        await self.wait_for(published)
        self.assertEqual((await self.payment(payment_id)).status, "pending")
        await compose("kill", "-s", "SIGKILL", "consumer")
        await compose("up", "-d", "consumer")
        await self.wait_for(lambda: self.delivered(payment_id))
        self.assertEqual(len(await self.receiver(path)), 1)

    async def test_database_outage_preserves_work(self):
        payment_id, _, _ = await self.create()
        await compose("stop", "postgres")
        try:
            response = await self.client.post(
                "/api/v1/payments",
                headers={"Idempotency-Key": str(uuid.uuid4())},
                json=BODY | {"webhook_url": "https://example.com/hook"},
            )
            self.assertGreaterEqual(response.status_code, 500)
        finally:
            await compose("up", "-d", "--wait", "postgres")
        await self.wait_for(lambda: self.delivered(payment_id))

    async def test_unroutable_event_is_not_marked_published(self):
        await compose("stop", "consumer")
        try:
            payment_id, _, _ = await self.create()
            async with session_factory.begin() as session:
                event = await session.scalar(
                    select(Outbox).where(Outbox.payment_id == uuid.UUID(payment_id))
                )
                event.routing_key = "does.not.exist"
            await broker.connect()
            try:
                with self.assertRaises(aio_pika.exceptions.DeliveryError):
                    await worker.publish_batch()
            finally:
                await broker.stop()
            async with session_factory.begin() as session:
                event = await session.scalar(
                    select(Outbox).where(Outbox.payment_id == uuid.UUID(payment_id))
                )
                self.assertIsNone(event.published_at)
                event.routing_key = "payments.new"
        finally:
            await compose("start", "consumer")
        await self.wait_for(lambda: self.delivered(payment_id))

    async def test_processing_branches_and_duplicate_inflight(self):
        await compose("stop", "consumer")
        try:
            for result in ["succeeded", "failed"]:
                payment_id, _, _ = await self.create()
                event = PaymentEvent(payment_id=payment_id, attempt=1)
                with (
                    patch.object(worker, "simulate_gateway", return_value=result) as gateway,
                    patch.object(worker.webhook, "deliver", return_value=None) as delivery,
                ):
                    await asyncio.gather(worker.process(event), worker.process(event))
                    self.assertEqual(gateway.call_count, 1)
                    self.assertEqual(delivery.call_count, 1)
                self.assertEqual((await self.payment(payment_id)).status, result)
            payment_id, _, _ = await self.create()
            with patch.object(
                worker, "simulate_gateway", side_effect=RuntimeError("Gateway error")
            ):
                for attempt in range(1, 4):
                    await worker.process(PaymentEvent(payment_id=payment_id, attempt=attempt))
            payment = await self.payment(payment_id)
            self.assertEqual(payment.completed_attempts, 3)
            self.assertEqual(payment.status, "failed")
            async with session_factory() as session:
                self.assertEqual(
                    await session.scalar(
                        select(Outbox.routing_key).where(
                            Outbox.payment_id == uuid.UUID(payment_id), Outbox.attempt == 4
                        )
                    ),
                    "payments.dlq",
                )
        finally:
            await compose("start", "consumer")


if __name__ == "__main__":
    unittest.main()
