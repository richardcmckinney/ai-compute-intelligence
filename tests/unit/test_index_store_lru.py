from __future__ import annotations

from aci.index.materializer import AttributionIndexStore
from aci.models.attribution import AttributionIndexEntry


def make_entry(workload_id: str) -> AttributionIndexEntry:
    return AttributionIndexEntry(
        workload_id=workload_id,
        team_id="team-x",
        team_name="Team X",
        cost_center_id="CC-1",
        confidence=0.9,
        confidence_tier="chargeback_ready",
        method_used="R1",
    )


def test_lru_eviction_keeps_store_bounded() -> None:
    store = AttributionIndexStore(max_entries=2)

    store.materialize(make_entry("a"))
    store.materialize(make_entry("b"))
    store.materialize(make_entry("c"))

    assert store.size == 2
    assert store.lookup("a") is None
    assert store.lookup("b") is not None
    assert store.lookup("c") is not None
    assert store.stats["evictions"] == 1


def test_lookup_bumps_lru_order() -> None:
    store = AttributionIndexStore(max_entries=2)

    store.materialize(make_entry("a"))
    store.materialize(make_entry("b"))

    # Bump "a" to most recently used.
    assert store.lookup("a") is not None

    # Adding "c" should evict "b" now.
    store.materialize(make_entry("c"))

    assert store.lookup("a") is not None
    assert store.lookup("b") is None
    assert store.lookup("c") is not None
