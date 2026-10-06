"""Measure only the isolated test project; writes Locust CSV/HTML and system evidence."""

import argparse
import csv
import json
import os
import platform
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

COMPOSE = ["docker", "compose", "-f", "compose.yaml", "-f", "compose.test.yaml"]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def measure(output: Path, duration: str):
    output.mkdir(parents=True, exist_ok=True)
    # Only the disposable test project is reset; production volumes are never touched.
    run(*COMPOSE, "down", "-v", stdout=subprocess.DEVNULL)
    started = time.perf_counter()
    run(*COMPOSE, "up", "-d", "--wait", stdout=subprocess.DEVNULL)
    cold_seconds = time.perf_counter() - started
    started = time.perf_counter()
    run(*COMPOSE, "up", "-d", "--wait", stdout=subprocess.DEVNULL)
    unchanged_seconds = time.perf_counter() - started
    environment = os.environ | {"API_KEY": "local-development-key"}
    for profile in ["http", "pipeline"]:
        env = environment | {"LOAD_PROFILE": profile}
        with (output / f"{profile}.log").open("w") as log:
            run(
                "uv",
                "run",
                "--group",
                "load",
                "locust",
                "-f",
                "load/locustfile.py",
                "--headless",
                "--host",
                "http://127.0.0.1:18000",
                "-u",
                "20",
                "-r",
                "5",
                "-t",
                duration,
                "--only-summary",
                "--stop-timeout",
                "10",
                "--csv",
                str(output / profile),
                "--html",
                str(output / f"{profile}.html"),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
    # Allow the last in-flight payments to finish with the normal 2–5 s gateway delay.
    time.sleep(10)
    query = (
        "SELECT count(*) AS payments, "
        "count(*) FILTER (WHERE webhook_delivered_at IS NOT NULL) AS delivered, "
        "count(*) FILTER (WHERE status='pending') AS pending, "
        "count(*) FILTER (WHERE status='succeeded') AS succeeded, "
        "count(*) FILTER (WHERE status='failed') AS failed FROM payments "
        "WHERE metadata @> '{\"load_test\": true}'::jsonb;"
    )
    result = run(
        *COMPOSE,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "payments",
        "-d",
        "payments",
        "--csv",
        "-c",
        query,
        capture_output=True,
    )
    (output / "pipeline_database.csv").write_text(result.stdout)
    containers = run(*COMPOSE, "ps", "-q", capture_output=True).stdout.split()
    stats = run(
        "docker",
        "stats",
        "--no-stream",
        "--format",
        "{{json .}}",
        *containers,
        capture_output=True,
    )
    (output / "containers.jsonl").write_text(stats.stdout)
    evidence = {
        "measured_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "cpu": run("lscpu", capture_output=True).stdout,
        "memory": run("free", "-m", capture_output=True).stdout,
        "cold_start_images_cached_seconds": cold_seconds,
        "unchanged_compose_up_seconds": unchanged_seconds,
        "duration_per_profile": duration,
        "users": 20,
        "spawn_rate": 5,
        "consumer_prefetch": 16,
        "gateway_delay_seconds": [2, 5],
        "api_url": "http://127.0.0.1:18000",
        "demo_rate_limit": False,
        "commit": run("git", "rev-parse", "HEAD", capture_output=True).stdout.strip(),
    }
    (output / "environment.json").write_text(json.dumps(evidence, indent=2))
    totals = next(csv.DictReader(result.stdout.splitlines()))
    if int(totals["pending"]):
        raise RuntimeError("Benchmark ended with pending payments: inspect pipeline_database.csv")
    print(json.dumps(totals))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("performance/local"))
    parser.add_argument("--duration", default="60s")
    args = parser.parse_args()
    measure(args.output, args.duration)
