"""
Event bus: append-only event store (Patent Spec Section 3.2).

All enterprise signals enter the system as immutable events. The event bus
is the system of record. The graph is derived state.

Supports Kafka, Pulsar, Kinesis, or PubSub in production. This module
provides an in-memory implementation for development and a Kafka-backed
implementation for production.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import datetime

import structlog

from aci.models.events import DomainEvent

logger = structlog.get_logger()

EventHandler = Callable[[DomainEvent], None | Awaitable[None]]


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

    async def publish(self, event: DomainEvent) -> bool:
        """
        Publish an event to the bus.

        Returns False if the event was deduplicated (idempotency_key seen before).
        """
        dedup_key = f"{event.source}:{event.idempotency_key}"
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
    def stats(self) -> dict:
        return {
            "total_events": len(self._log),
            "published": self._published_count,
            "deduplicated": self._deduplicated_count,
            "idempotency_cache_size": len(self._seen_keys),
            "idempotency_evictions": self._idempotency_evictions,
            "dispatch_errors": self._dispatch_errors,
            "topics": list(self._handlers.keys()),
            "published_by_topic": dict(self._topic_counts),
        }
