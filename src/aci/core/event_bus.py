"""
Event bus: append-only event store (Patent Spec Section 3.2).

All enterprise signals enter the system as immutable events. The event bus
is the system of record. The graph is derived state.

This module provides:
- InMemoryEventBus (development/testing)
- KafkaEventBus (production durable broker with DLQ + lag metrics)
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import structlog
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import OffsetAndMetadata
from redis.asyncio import Redis as AsyncRedis

from aci.core.event_schema import validate_domain_event
from aci.models.events import DomainEvent

if TYPE_CHECKING:
    from aiokafka.structs import TopicPartition

logger = structlog.get_logger()

EventHandler = Callable[[DomainEvent], None | Awaitable[None]]


class AsyncIdempotencyStore(Protocol):
    async def seen_or_add(self, key: str) -> bool: ...

    async def close(self) -> None: ...


class InMemoryIdempotencyStore:
    """Bounded in-memory idempotency store for local development."""

    def __init__(self, max_keys: int = 1_000_000) -> None:
        self._seen_keys: set[str] = set()
        self._order: deque[str] = deque()
        self._max_keys = max_keys
        self._lock = asyncio.Lock()
        self.evictions = 0

    async def seen_or_add(self, key: str) -> bool:
        async with self._lock:
            if key in self._seen_keys:
                return True

            self._seen_keys.add(key)
            self._order.append(key)
            if len(self._order) > self._max_keys:
                evicted = self._order.popleft()
                self._seen_keys.discard(evicted)
                self.evictions += 1
            return False

    async def close(self) -> None:
        return None


class RedisIdempotencyStore:
    """Redis-backed idempotency key store with TTL for durable deduplication."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        key_prefix: str = "aci:idempotency",
    ) -> None:
        self._redis = AsyncRedis.from_url(redis_url, decode_responses=True)
        self._ttl_seconds = ttl_seconds
        self._key_prefix = key_prefix

    async def seen_or_add(self, key: str) -> bool:
        redis_key = f"{self._key_prefix}:{key}"
        inserted = await self._redis.set(redis_key, "1", ex=self._ttl_seconds, nx=True)
        return inserted is None

    async def close(self) -> None:
        await self._redis.aclose()


class InMemoryEventBus:
    """
    In-memory event bus for development and testing.

    Provides the same interface as the production Kafka-backed bus,
    with at-least-once delivery semantics and idempotency deduplication.
    """

    def __init__(self, max_events: int = 1_000_000) -> None:
        # Append-only event log (system of record).
        self._log: deque[DomainEvent] = deque(maxlen=max_events)

        # Topic-based subscription.
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._handler_lock = threading.Lock()

        # Idempotency deduplication.
        self._seen_keys: set[str] = set()
        self._idempotency_order: deque[str] = deque()
        self._max_idempotency_keys: int = max_events * 2

        # Metrics.
        self._published_count: int = 0
        self._deduplicated_count: int = 0
        self._idempotency_evictions: int = 0
        self._topic_counts: dict[str, int] = defaultdict(int)
        self._dispatch_errors: int = 0

        # Concurrency controls.
        self._state_lock = asyncio.Lock()
        self._dispatch_semaphore = asyncio.Semaphore(64)

    async def start(self) -> None:
        """No-op for interface parity with durable bus backends."""
        return None

    async def stop(self) -> None:
        """No-op for interface parity with durable bus backends."""
        return None

    @property
    def is_started(self) -> bool:
        """In-memory backend is always available once instantiated."""
        return True

    async def publish(self, event: DomainEvent) -> bool:
        """
        Publish an event to the bus.

        Returns False if the event was deduplicated (idempotency_key seen before).
        """
        validate_domain_event(event)

        dedup_key = f"{event.tenant_id}:{event.source}:{event.idempotency_key}"
        topic = event.event_type.value

        async with self._state_lock:
            if dedup_key in self._seen_keys:
                self._deduplicated_count += 1
                return False

            self._seen_keys.add(dedup_key)
            self._idempotency_order.append(dedup_key)

            if len(self._idempotency_order) > self._max_idempotency_keys:
                evicted_key = self._idempotency_order.popleft()
                self._seen_keys.discard(evicted_key)
                self._idempotency_evictions += 1

            self._log.append(event)
            self._published_count += 1
            self._topic_counts[topic] += 1

        with self._handler_lock:
            handlers = [
                *(self._handlers.get(topic, [])),
                *(self._handlers.get("*", [])),
            ]
        if not handlers:
            return True

        await asyncio.gather(
            *(self._dispatch_handler(topic, handler, event) for handler in handlers)
        )
        return True

    def subscribe(self, topic: str, handler: EventHandler) -> None:
        """Subscribe a handler to a topic. Use '*' for all events."""
        with self._handler_lock:
            self._handlers[topic].append(handler)

    async def publish_batch(self, events: list[DomainEvent]) -> dict[str, int]:
        """Publish a batch of events and return publish/dedup counts."""
        published = 0
        deduplicated = 0
        for event in events:
            accepted = await self.publish(event)
            if accepted:
                published += 1
            else:
                deduplicated += 1
        return {"published": published, "deduplicated": deduplicated}

    async def _dispatch_handler(
        self,
        topic: str,
        handler: EventHandler,
        event: DomainEvent,
    ) -> None:
        """Invoke handlers safely, supporting both sync and async callbacks."""
        async with self._dispatch_semaphore:
            try:
                maybe_result = handler(event)
                if inspect.isawaitable(maybe_result):
                    await maybe_result
            except Exception as exc:
                self._dispatch_errors += 1
                logger.error(
                    "event_bus.handler_error",
                    topic=topic,
                    error=str(exc),
                )

    def replay(
        self,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        event_types: list[str] | None = None,
    ) -> list[DomainEvent]:
        """
        Replay events from the log (Section 3.2: backfill mechanism).

        Used when late-arriving events, corrected identity mappings, or new
        attribution logic versions require recomputation.
        """
        results: list[DomainEvent] = []
        for event in self._log:
            if from_time and event.event_time < from_time:
                continue
            if to_time and event.event_time > to_time:
                continue
            if event_types and event.event_type.value not in event_types:
                continue
            results.append(event)
        return results

    @property
    def stats(self) -> dict[str, object]:
        with self._handler_lock:
            topics = list(self._handlers.keys())

        return {
            "backend": "memory",
            "started": True,
            "total_events": len(self._log),
            "published": self._published_count,
            "deduplicated": self._deduplicated_count,
            "idempotency_cache_size": len(self._seen_keys),
            "idempotency_evictions": self._idempotency_evictions,
            "dispatch_errors": self._dispatch_errors,
            "topics": topics,
            "published_by_topic": dict(self._topic_counts),
        }


class KafkaEventBus:
    """Durable Kafka-backed event bus with DLQ and consumer lag metrics."""

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        topic: str,
        dlq_topic: str,
        consumer_group: str,
        idempotency_store: AsyncIdempotencyStore,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._topic = topic
        self._dlq_topic = dlq_topic
        self._consumer_group = consumer_group
        self._idempotency_store = idempotency_store

        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._handler_lock = threading.Lock()
        self._dispatch_semaphore = asyncio.Semaphore(64)

        self._producer: AIOKafkaProducer | None = None
        self._consumer: AIOKafkaConsumer | None = None
        self._consumer_task: asyncio.Task[None] | None = None

        self._started = False
        self._published_count = 0
        self._deduplicated_count = 0
        self._dispatch_errors = 0
        self._dlq_count = 0
        self._consumer_lag_by_partition: dict[str, int] = {}

    async def start(self) -> None:
        if self._started:
            return

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap_servers,
            acks="all",
            enable_idempotence=True,
            linger_ms=5,
        )
        await self._producer.start()

        self._consumer = AIOKafkaConsumer(
            self._topic,
            bootstrap_servers=self._bootstrap_servers,
            group_id=self._consumer_group,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await self._consumer.start()

        self._consumer_task = asyncio.create_task(self._consume_loop())
        self._started = True
        logger.info(
            "event_bus.kafka_started",
            topic=self._topic,
            dlq_topic=self._dlq_topic,
            consumer_group=self._consumer_group,
        )

    async def stop(self) -> None:
        if not self._started:
            return

        if self._consumer_task:
            self._consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None

        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None

        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

        await self._idempotency_store.close()
        self._started = False

    @property
    def is_started(self) -> bool:
        """Whether Kafka producer/consumer loops are initialized."""
        return self._started

    def subscribe(self, topic: str, handler: EventHandler) -> None:
        with self._handler_lock:
            self._handlers[topic].append(handler)

    async def publish(self, event: DomainEvent) -> bool:
        validate_domain_event(event)

        if not self._started:
            await self.start()

        assert self._producer is not None

        dedup_key = f"{event.tenant_id}:{event.source}:{event.idempotency_key}"
        already_seen = await self._idempotency_store.seen_or_add(dedup_key)
        if already_seen:
            self._deduplicated_count += 1
            return False

        payload = event.model_dump_json().encode("utf-8")
        await self._producer.send_and_wait(
            self._topic,
            value=payload,
            key=dedup_key.encode("utf-8"),
        )
        self._published_count += 1
        return True

    async def publish_batch(self, events: list[DomainEvent]) -> dict[str, int]:
        published = 0
        deduplicated = 0
        for event in events:
            accepted = await self.publish(event)
            if accepted:
                published += 1
            else:
                deduplicated += 1
        return {"published": published, "deduplicated": deduplicated}

    async def _consume_loop(self) -> None:
        assert self._consumer is not None

        while True:
            batches = await self._consumer.getmany(timeout_ms=500, max_records=256)
            for topic_partition, messages in batches.items():
                for message in messages:
                    await self._handle_message(topic_partition, message.offset, message.value)

    async def _handle_message(
        self,
        topic_partition: TopicPartition,
        offset: int,
        payload: bytes,
    ) -> None:
        assert self._consumer is not None

        try:
            event = DomainEvent.model_validate_json(payload)
            validate_domain_event(event)
            await self._dispatch_event(event)
            await self._commit_offset(topic_partition, offset)
            self._update_partition_lag(topic_partition, offset)
        except Exception as exc:
            self._dispatch_errors += 1
            await self._publish_dlq(payload, str(exc), topic_partition, offset)
            await self._commit_offset(topic_partition, offset)

    async def _commit_offset(self, topic_partition: TopicPartition, offset: int) -> None:
        """Commit a processed offset with explicit metadata type."""
        assert self._consumer is not None
        await self._consumer.commit({topic_partition: OffsetAndMetadata(offset + 1, "")})

    async def _dispatch_event(self, event: DomainEvent) -> None:
        topic = event.event_type.value
        with self._handler_lock:
            handlers = [
                *(self._handlers.get(topic, [])),
                *(self._handlers.get("*", [])),
            ]
        if not handlers:
            return

        results = await asyncio.gather(
            *(self._dispatch_handler(handler, event) for handler in handlers),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if not errors:
            return

        for exc in errors:
            self._dispatch_errors += 1
            logger.error("event_bus.kafka_handler_error", topic=topic, error=str(exc))

        first = errors[0]
        if len(errors) == 1:
            raise first
        raise RuntimeError(f"{len(errors)} handlers failed for topic '{topic}'") from first

    async def _dispatch_handler(
        self,
        handler: EventHandler,
        event: DomainEvent,
    ) -> None:
        async with self._dispatch_semaphore:
            maybe_result = handler(event)
            if inspect.isawaitable(maybe_result):
                await maybe_result

    async def _publish_dlq(
        self,
        payload: bytes,
        error: str,
        topic_partition: TopicPartition,
        offset: int,
    ) -> None:
        if self._producer is None:
            return

        dlq_envelope = {
            "error": error,
            "topic": topic_partition.topic,
            "partition": topic_partition.partition,
            "offset": offset,
            "failed_at": datetime.now(UTC).isoformat(),
            "payload": payload.decode("utf-8", errors="replace"),
        }
        await self._producer.send_and_wait(
            self._dlq_topic,
            value=json.dumps(dlq_envelope).encode("utf-8"),
            key=f"{topic_partition.topic}:{topic_partition.partition}:{offset}".encode(),
        )
        self._dlq_count += 1

    def _update_partition_lag(self, topic_partition: TopicPartition, offset: int) -> None:
        if self._consumer is None:
            return

        highwater = self._consumer.highwater(topic_partition)
        if highwater is None:
            return

        lag = max(0, highwater - (offset + 1))
        key = f"{topic_partition.topic}:{topic_partition.partition}"
        self._consumer_lag_by_partition[key] = lag

    def replay(
        self,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        event_types: list[str] | None = None,
    ) -> list[DomainEvent]:
        # Kafka replay is performed via offset/time seek in dedicated jobs.
        # This runtime bus API exposes ingestion/dispatch only.
        del from_time, to_time, event_types
        return []

    @property
    def stats(self) -> dict[str, object]:
        with self._handler_lock:
            topics = list(self._handlers.keys())

        return {
            "backend": "kafka",
            "started": self._started,
            "published": self._published_count,
            "deduplicated": self._deduplicated_count,
            "dispatch_errors": self._dispatch_errors,
            "dlq_count": self._dlq_count,
            "consumer_group": self._consumer_group,
            "topic": self._topic,
            "dlq_topic": self._dlq_topic,
            "consumer_lag_by_partition": dict(self._consumer_lag_by_partition),
            "consumer_lag_total": sum(self._consumer_lag_by_partition.values()),
            "topics": topics,
        }
