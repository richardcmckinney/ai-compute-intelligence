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
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Callable

import structlog

from aci.models.events import DomainEvent

logger = structlog.get_logger()

EventHandler = Callable[[DomainEvent], None]


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

        # Metrics.
        self._published_count: int = 0
        self._deduplicated_count: int = 0

    async def publish(self, event: DomainEvent) -> bool:
        """
        Publish an event to the bus.

        Returns False if the event was deduplicated (idempotency_key seen before).
        """
        # Idempotency check.
        dedup_key = f"{event.source}:{event.idempotency_key}"
        if dedup_key in self._seen_keys:
            self._deduplicated_count += 1
            return False

        self._seen_keys.add(dedup_key)
        self._log.append(event)
        self._published_count += 1

        # Dispatch to topic handlers.
        topic = event.event_type.value
        for handler in self._handlers.get(topic, []):
            try:
                handler(event)
            except Exception as exc:
                logger.error("event_bus.handler_error", topic=topic, error=str(exc))

        # Also dispatch to wildcard handlers.
        for handler in self._handlers.get("*", []):
            try:
                handler(event)
            except Exception as exc:
                logger.error("event_bus.wildcard_handler_error", error=str(exc))

        return True

    def subscribe(self, topic: str, handler: EventHandler) -> None:
        """Subscribe a handler to a topic. Use '*' for all events."""
        self._handlers[topic].append(handler)

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
            "topics": list(self._handlers.keys()),
        }
