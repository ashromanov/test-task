import asyncio
import ipaddress
import socket
from typing import Any

import aiohttp
from aiohttp.abc import AbstractResolver
from yarl import URL

from payments.config import settings

allowed_hosts = frozenset(
    host.strip().lower() for host in settings.webhook_allowed_hosts.split(",") if host.strip()
)


def public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class PublicResolver(AbstractResolver):
    """Validate the addresses actually passed to the connector, preventing DNS rebinding."""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        if host.lower() not in allowed_hosts and any(not public_ip(a[4][0]) for a in addresses):
            raise OSError("Webhook must resolve only to public IP addresses")
        return [
            {
                "hostname": host,
                "host": address[0],
                "port": port,
                "family": kind,
                "proto": proto,
                "flags": socket.AI_NUMERICHOST,
            }
            for kind, _, proto, _, address in addresses
        ]

    async def close(self):
        pass


def client() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            resolver=PublicResolver(), limit=settings.consumer_prefetch, ttl_dns_cache=60
        ),
        timeout=aiohttp.ClientTimeout(total=settings.webhook_timeout),
        trust_env=False,
    )


async def deliver(http: aiohttp.ClientSession, url: str, payload: dict[str, Any]) -> None:
    parsed = URL(url)
    host = parsed.host or ""
    # aiohttp bypasses resolvers for literal IPs, so check those separately.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not public_ip(str(literal)):
        if host.lower() not in allowed_hosts:
            raise ValueError("Webhook IP must be public")
    async with http.post(
        url,
        json=payload,
        headers={"Idempotency-Key": payload["event_id"]},
        allow_redirects=False,
    ) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"Webhook returned HTTP {response.status}")
        # No need to download an arbitrary response body from the receiver.
