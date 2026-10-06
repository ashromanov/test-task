import asyncio
import os
import socket
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("API_KEY", "local-development-key")

import httpx
from pydantic import ValidationError

from payments import api, webhook, worker
from payments.schemas import PaymentCreate

BODY = {
    "amount": "99.99",
    "currency": "RUB",
    "description": "Тестовый платёж",
    "metadata": {"order_id": "order-1"},
    "webhook_url": "https://example.com/webhook",
}


class UnitTests(unittest.IsolatedAsyncioTestCase):
    def test_validation(self):
        self.assertEqual(str(PaymentCreate(**BODY).amount), "99.99")
        for change in [
            {"amount": "0"},
            {"amount": "-1"},
            {"amount": "NaN"},
            {"amount": "1.001"},
            {"amount": "10000000000000000"},
            {"currency": "GBP"},
            {"webhook_url": "file:///etc/passwd"},
            {"webhook_url": "http://user:pass@example.com"},
            {"metadata": {"value": float("inf")}},
            {"extra": True},
        ]:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                PaymentCreate(**(BODY | change))

    async def test_auth_docs_and_body_limit(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app), base_url="http://test"
        ) as client:
            self.assertEqual((await client.get("/docs")).status_code, 200)
            self.assertEqual((await client.get("/openapi.json")).status_code, 200)
            for key in [None, "wrong", "ключ".encode()]:
                headers = {"X-API-Key": key} if key else {}
                self.assertEqual((await client.get("/health", headers=headers)).status_code, 401)
            headers = {"X-API-Key": api.settings.api_key}
            self.assertEqual(
                (await client.post("/api/v1/payments", headers=headers, json=BODY)).status_code,
                422,
            )
            self.assertEqual(
                (
                    await client.post(
                        "/api/v1/payments", headers=headers, content=b"x" * (api.MAX_BODY + 1)
                    )
                ).status_code,
                413,
            )

    async def test_demo_rate_limit(self):
        api.tokens = 1
        api.last_refill = 100
        with (
            patch.object(api.settings, "demo_mode", True),
            patch.object(api.time, "monotonic", return_value=100),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api.app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/payments", json={})
                response = await client.post("/api/v1/payments", json={})
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.headers["Retry-After"], "1")

    async def test_gateway_both_results(self):
        for value, expected in [(0.89, "succeeded"), (0.9, "failed")]:
            with (
                patch.object(worker.random, "random", return_value=value),
                patch.object(worker.asyncio, "sleep", new=AsyncMock()) as delay,
            ):
                self.assertEqual(await worker.simulate_gateway(), expected)
                self.assertTrue(2 <= delay.call_args.args[0] <= 5)

    async def test_webhook_network_boundaries(self):
        for address in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"]:
            self.assertFalse(webhook.public_ip(address))
        self.assertTrue(webhook.public_ip("1.1.1.1"))
        result = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with patch.object(
            asyncio.get_running_loop(), "getaddrinfo", new=AsyncMock(return_value=result)
        ):
            with self.assertRaises(OSError):
                await webhook.PublicResolver().resolve("rebind.example", 80)
        async with webhook.client() as client:
            with self.assertRaises(ValueError):
                await webhook.deliver(client, "http://127.0.0.1:1/", {"event_id": "test"})

    async def test_webhook_does_not_follow_redirects(self):
        from aiohttp import web

        async def redirect(request):
            raise web.HTTPFound("http://169.254.169.254/latest/meta-data/")

        application = web.Application()
        application.router.add_post("/", redirect)
        runner = web.AppRunner(application)
        await runner.setup()
        server = await asyncio.get_running_loop().create_server(runner.server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with patch.object(webhook, "allowed_hosts", frozenset({"127.0.0.1"})):
                async with webhook.client() as client:
                    with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                        await webhook.deliver(
                            client, f"http://127.0.0.1:{port}/", {"event_id": "test"}
                        )
        finally:
            server.close()
            await server.wait_closed()
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
