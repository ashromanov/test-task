"""Read/idempotency profile or sustained end-to-end profile; only run on a test stack."""

import os
import uuid

from locust import FastHttpUser, constant_throughput, task


class PaymentsUser(FastHttpUser):
    # 20 users => at most 4 payments/s, within the emulator's ~4.6/s capacity at prefetch=16.
    wait_time = constant_throughput(0.2) if os.getenv("LOAD_PROFILE") == "pipeline" else lambda _: 0

    def on_start(self):
        self.key = str(uuid.uuid4())
        self.body = {
            "amount": "123.45",
            "currency": "RUB",
            "description": "Locust synthetic payment",
            "metadata": {"load_test": True},
            "webhook_url": f"http://receiver:8080/ok/{self.key}",
        }
        self.headers = {"X-API-Key": os.environ["API_KEY"], "Idempotency-Key": self.key}
        with self.client.post(
            "/api/v1/payments",
            json=self.body,
            headers=self.headers,
            name="POST create (setup)",
            catch_response=True,
        ) as response:
            if response.status_code != 202:
                response.failure(f"Expected 202, got {response.status_code}")
                return
            self.payment_id = response.json()["payment_id"]

    @task
    def payment(self):
        if os.getenv("LOAD_PROFILE") == "pipeline":
            self.headers["Idempotency-Key"] = str(uuid.uuid4())
            with self.client.post(
                "/api/v1/payments",
                json=self.body,
                headers=self.headers,
                name="POST create",
                catch_response=True,
            ) as response:
                if response.status_code != 202:
                    response.failure(f"Expected 202, got {response.status_code}")
        else:
            with self.client.post(
                "/api/v1/payments",
                json=self.body,
                headers=self.headers,
                name="POST idempotent replay",
                catch_response=True,
            ) as response:
                if response.status_code != 202:
                    response.failure(f"Expected 202, got {response.status_code}")
            self.client.get(
                f"/api/v1/payments/{self.payment_id}",
                headers=self.headers,
                name="GET payment",
            )
