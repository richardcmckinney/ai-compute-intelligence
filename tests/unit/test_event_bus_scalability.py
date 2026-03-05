from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from aci.core.event_bus import InMemoryEventBus
from aci.models.events import DomainEvent, EventType


def make_event(event_id: str, key: str) -> DomainEvent:
    return DomainEvent(
        event_id=event_id,
        event_type=EventType.INFERENCE_REQUEST,
        subject_id=f"subject-{event_id}",
        attributes={
            "request_id": event_id,
            "model": "gpt-4o-mini",
            "provider": "openai",
        },
        event_time=datetime.now(UTC),
        source="unit-test",
        idempotency_key=key,
        tenant_id="test",
    )


@pytest.mark.asyncio
async def test_publish_dispatches_sync_and_async_handlers() -> None:
    bus = InMemoryEventBus(max_events=16)

    seen_sync: list[str] = []
    seen_async: list[str] = []

    def sync_handler(event: DomainEvent) -> None:
        seen_sync.append(event.event_id)

    async def async_handler(event: DomainEvent) -> None:
        seen_async.append(event.event_id)

    bus.subscribe(EventType.INFERENCE_REQUEST.value, sync_handler)
    bus.subscribe(EventType.INFERENCE_REQUEST.value, async_handler)

    published = await bus.publish(make_event("evt-1", "key-1"))

    assert published is True
    assert seen_sync == ["evt-1"]
    assert seen_async == ["evt-1"]


@pytest.mark.asyncio
async def test_idempotency_cache_is_bounded_and_evicts_oldest_keys() -> None:
    bus = InMemoryEventBus(max_events=2)  # max dedup keys = 4

    for idx in range(5):
        accepted = await bus.publish(make_event(f"evt-{idx}", f"key-{idx}"))
        assert accepted is True

    # Oldest key should be evicted from dedup cache, so this is accepted again.
    republished_oldest = await bus.publish(make_event("evt-old", "key-0"))
    assert republished_oldest is True

    stats = bus.stats
    assert cast("int", stats["idempotency_evictions"]) >= 1
    assert cast("int", stats["idempotency_cache_size"]) <= 4


@pytest.mark.asyncio
async def test_publish_batch_reports_counts() -> None:
    bus = InMemoryEventBus(max_events=16)

    events = [
        make_event("evt-1", "k1"),
        make_event("evt-2", "k2"),
        make_event("evt-3", "k2"),  # duplicate key
    ]

    result = await bus.publish_batch(events)

    assert result["published"] == 2
    assert result["deduplicated"] == 1
