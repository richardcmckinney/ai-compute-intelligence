from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from aiokafka.structs import OffsetAndMetadata, TopicPartition

from aci.core.event_bus import InMemoryEventBus, InMemoryIdempotencyStore, KafkaEventBus
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


@pytest.mark.asyncio
async def test_deduplication_is_scoped_by_tenant() -> None:
    bus = InMemoryEventBus(max_events=16)

    event_a = make_event("evt-tenant-a", "shared-key").model_copy(update={"tenant_id": "tenant-a"})
    event_b = make_event("evt-tenant-b", "shared-key").model_copy(update={"tenant_id": "tenant-b"})

    accepted_a = await bus.publish(event_a)
    accepted_b = await bus.publish(event_b)

    assert accepted_a is True
    assert accepted_b is True


class _DummyConsumer:
    def __init__(self) -> None:
        self.commits: list[dict[TopicPartition, OffsetAndMetadata]] = []

    async def commit(self, offsets: dict[TopicPartition, OffsetAndMetadata]) -> None:
        self.commits.append(offsets)


class _DummyProducer:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bytes, bytes | None]] = []

    async def send_and_wait(self, topic: str, value: bytes, key: bytes | None = None) -> None:
        self.messages.append((topic, value, key))


class _FailingDummyProducer:
    async def send_and_wait(
        self,
        topic: str,
        value: bytes,
        key: bytes | None = None,
    ) -> None:
        del topic, value, key
        raise RuntimeError("dlq unavailable")


@pytest.mark.asyncio
async def test_kafka_commit_uses_offset_and_metadata_type() -> None:
    bus = KafkaEventBus(
        bootstrap_servers="localhost:9092",
        topic="aci.events",
        dlq_topic="aci.events.dlq",
        consumer_group="aci-tests",
        idempotency_store=InMemoryIdempotencyStore(),
    )
    consumer = _DummyConsumer()
    bus._consumer = consumer

    topic_partition = TopicPartition("aci.events", 0)
    await bus._commit_offset(topic_partition, offset=41)

    assert len(consumer.commits) == 1
    committed = consumer.commits[0]
    metadata = committed[topic_partition]
    assert isinstance(metadata, OffsetAndMetadata)
    assert metadata.offset == 42


@pytest.mark.asyncio
async def test_kafka_handler_failures_go_to_dlq_and_still_commit() -> None:
    bus = KafkaEventBus(
        bootstrap_servers="localhost:9092",
        topic="aci.events",
        dlq_topic="aci.events.dlq",
        consumer_group="aci-tests",
        idempotency_store=InMemoryIdempotencyStore(),
    )
    consumer = _DummyConsumer()
    producer = _DummyProducer()
    bus._consumer = consumer
    bus._producer = producer

    def failing_handler(_event: DomainEvent) -> None:
        raise RuntimeError("boom")

    bus.subscribe(EventType.INFERENCE_REQUEST.value, failing_handler)

    event = make_event("evt-dlq", "key-dlq")
    topic_partition = TopicPartition("aci.events", 0)
    payload = event.model_dump_json().encode("utf-8")
    await bus._handle_message(topic_partition, offset=7, payload=payload)

    assert len(consumer.commits) == 1
    assert len(producer.messages) == 1
    topic, value, _key = producer.messages[0]
    assert topic == "aci.events.dlq"
    assert b"boom" in value


@pytest.mark.asyncio
async def test_kafka_dlq_publish_failures_still_commit_offsets() -> None:
    bus = KafkaEventBus(
        bootstrap_servers="localhost:9092",
        topic="aci.events",
        dlq_topic="aci.events.dlq",
        consumer_group="aci-tests",
        idempotency_store=InMemoryIdempotencyStore(),
    )
    consumer = _DummyConsumer()
    bus._consumer = consumer
    bus._producer = _FailingDummyProducer()

    def failing_handler(_event: DomainEvent) -> None:
        raise RuntimeError("boom")

    bus.subscribe(EventType.INFERENCE_REQUEST.value, failing_handler)

    event = make_event("evt-dlq-fail", "key-dlq-fail")
    topic_partition = TopicPartition("aci.events", 0)
    payload = event.model_dump_json().encode("utf-8")

    await bus._handle_message(topic_partition, offset=9, payload=payload)

    assert len(consumer.commits) == 1
    committed = consumer.commits[0][topic_partition]
    assert committed.offset == 10
