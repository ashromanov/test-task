from faststream.rabbit import (
    Channel,
    ExchangeType,
    QueueType,
    RabbitBroker,
    RabbitExchange,
    RabbitQueue,
)

from payments.config import settings

broker = RabbitBroker(
    settings.rabbitmq_url,
    default_channel=Channel(
        prefetch_count=settings.consumer_prefetch,
        publisher_confirms=True,
        on_return_raises=True,
    ),
    graceful_timeout=15,
)
exchange = RabbitExchange("payments", type=ExchangeType.DIRECT, durable=True)
dead_exchange = RabbitExchange("payments.dlx", type=ExchangeType.DIRECT, durable=True)


def queue(name: str, *, delay_ms: int | None = None) -> RabbitQueue:
    arguments = {"x-quorum-initial-group-size": 1, "x-delivery-limit": -1}
    if name != "payments.dlq":
        arguments.update(
            {
                "x-dead-letter-exchange": "payments" if delay_ms else "payments.dlx",
                "x-dead-letter-routing-key": "payments.new" if delay_ms else "payments.dlq",
                "x-dead-letter-strategy": "at-least-once",
                "x-overflow": "reject-publish",
            }
        )
    if delay_ms is not None:
        arguments["x-message-ttl"] = delay_ms
    return RabbitQueue(name, queue_type=QueueType.QUORUM, durable=True, arguments=arguments)


new_queue = queue("payments.new")
retry_queues = [
    queue("payments.retry.1s", delay_ms=1000),
    queue("payments.retry.2s", delay_ms=2000),
]
dead_queue = queue("payments.dlq")


async def declare_topology() -> None:
    live = await broker.declare_exchange(exchange)
    dead = await broker.declare_exchange(dead_exchange)
    for spec in [new_queue, *retry_queues, dead_queue]:
        declared = await broker.declare_queue(spec)
        await declared.bind(live, routing_key=spec.name)
        if spec == dead_queue:
            await declared.bind(dead, routing_key=spec.name)
