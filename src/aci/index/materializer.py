"""
Attribution index materializer (Patent Spec Section 3.1, Phase 4).

The attribution index is a synthesized control artifact derived from probabilistic
reconciliation. It is NOT a cache of raw source data. Its structure is intentionally
constrained to support deterministic, low-latency, constant-time O(1) decision-time
enforcement.

Materialization is incremental (update changed entries) with full rebuild on backfill.
Old entries are not overwritten; they are superseded with version metadata.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import UTC, datetime
from threading import RLock

import structlog
from redis import Redis
from redis.exceptions import RedisError

from aci.config import PlatformConfig
from aci.models.attribution import AttributionIndexEntry, AttributionResult

logger = structlog.get_logger()


class AttributionIndexStore:
    """
    In-memory attribution index with O(1) lookup.

    In production, backed by Redis with local process cache for sub-ms access.
    The interceptor reads ONLY from this store; never from the graph.
    """

    def __init__(
        self,
        max_entries: int = 250_000,
        redis_url: str | None = None,
        redis_prefix: str = "aci:index",
    ) -> None:
        # Primary index: workload_id -> AttributionIndexEntry.
        self._index: OrderedDict[str, AttributionIndexEntry] = OrderedDict()

        # Version tracking for cache coherence.
        self._versions: dict[str, int] = {}
        self._max_entries = max_entries

        # Metrics.
        self._hit_count: int = 0
        self._miss_count: int = 0
        self._materialization_count: int = 0
        self._eviction_count: int = 0
        self._durable_hits: int = 0
        self._durable_misses: int = 0
        self._durable_writes: int = 0
        self._durable_errors: int = 0

        # Single-process synchronization for mixed async/sync callers.
        self._lock = RLock()

        self._redis_prefix = redis_prefix
        self._redis: Redis | None = None
        if redis_url:
            try:
                self._redis = Redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_timeout=0.01,
                    socket_connect_timeout=0.01,
                )
            except Exception as exc:
                logger.warning("index.redis_unavailable", error=str(exc))

    def lookup(self, workload_id: str) -> AttributionIndexEntry | None:
        """
        Constant-time O(1) lookup by workload identifier.

        This is the ONLY method called on the decision-time critical path.
        It must complete in < 5ms under all conditions.
        """
        with self._lock:
            entry = self._index.get(workload_id)
            if entry is not None:
                self._hit_count += 1
                # LRU bump: recently read entries stay hot in memory.
                self._index.move_to_end(workload_id)
                return entry

            self._miss_count += 1

        # Durable fallback: load evicted entries from Redis in production.
        if self._redis is None:
            return None

        durable_entry = self._load_durable_entry(workload_id)
        if durable_entry is None:
            return None

        with self._lock:
            self._durable_hits += 1
            self._insert_local_locked(durable_entry)
        return durable_entry

    def lookup_batch(self, workload_ids: list[str]) -> dict[str, AttributionIndexEntry | None]:
        """Batch lookup for multiple workload IDs."""
        return {wid: self.lookup(wid) for wid in workload_ids}

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._index)

    @property
    def hit_rate(self) -> float:
        with self._lock:
            total = self._hit_count + self._miss_count
            return self._hit_count / total if total > 0 else 0.0

    @property
    def stats(self) -> dict:
        with self._lock:
            total = self._hit_count + self._miss_count
            hit_rate = self._hit_count / total if total > 0 else 0.0
            return {
                "size": len(self._index),
                "max_entries": self._max_entries,
                "hits": self._hit_count,
                "misses": self._miss_count,
                "hit_rate": round(hit_rate, 4),
                "materializations": self._materialization_count,
                "evictions": self._eviction_count,
                "durable_backend_enabled": self._redis is not None,
                "durable_hits": self._durable_hits,
                "durable_misses": self._durable_misses,
                "durable_writes": self._durable_writes,
                "durable_errors": self._durable_errors,
            }

    def durable_backend_healthy(self) -> bool:
        """Fast readiness probe for the optional durable Redis backend."""
        if self._redis is None:
            return False
        try:
            return bool(self._redis.ping())
        except RedisError:
            with self._lock:
                self._durable_errors += 1
            return False

    def materialize(self, entry: AttributionIndexEntry) -> None:
        """
        Upsert an entry into the index.

        Entries are versioned. Old entries are superseded, not overwritten,
        preserving the ability to reconstruct historical state.
        """
        with self._lock:
            current_version = self._versions.get(entry.workload_id, 0)
            new_version = current_version + 1

            entry_with_version = entry.model_copy(
                update={
                    "version": new_version,
                    "materialized_at": datetime.now(UTC),
                }
            )

            self._insert_local_locked(entry_with_version)
            self._materialization_count += 1
            self._versions[entry.workload_id] = new_version

        self._write_durable_entry(entry_with_version)

        logger.debug(
            "index.materialized",
            workload_id=entry.workload_id,
            version=new_version,
            confidence=entry.confidence,
        )

    def evict(self, workload_id: str) -> bool:
        """Remove an entry from the index (e.g., resource deleted)."""
        removed = False
        with self._lock:
            if workload_id in self._index:
                del self._index[workload_id]
                self._versions.pop(workload_id, None)
                removed = True

        if self._redis is not None:
            try:
                self._redis.delete(self._redis_key(workload_id))
            except RedisError:
                with self._lock:
                    self._durable_errors += 1

        return removed

    def clear(self) -> None:
        """Full index clear for rebuild."""
        with self._lock:
            self._index.clear()
            self._versions.clear()

    def _insert_local_locked(self, entry: AttributionIndexEntry) -> None:
        self._index[entry.workload_id] = entry
        self._index.move_to_end(entry.workload_id)
        self._versions[entry.workload_id] = entry.version

        while len(self._index) > self._max_entries:
            evicted_workload_id, _ = self._index.popitem(last=False)
            self._versions.pop(evicted_workload_id, None)
            self._eviction_count += 1

    def _redis_key(self, workload_id: str) -> str:
        return f"{self._redis_prefix}:{workload_id}"

    def _load_durable_entry(self, workload_id: str) -> AttributionIndexEntry | None:
        if self._redis is None:
            return None
        try:
            raw = self._redis.get(self._redis_key(workload_id))
            if raw is None:
                with self._lock:
                    self._durable_misses += 1
                return None
            return AttributionIndexEntry.model_validate_json(raw)
        except (RedisError, ValueError):
            with self._lock:
                self._durable_errors += 1
            return None

    def _write_durable_entry(self, entry: AttributionIndexEntry) -> None:
        if self._redis is None:
            return
        try:
            payload = json.dumps(entry.model_dump(mode="json"))
            self._redis.set(self._redis_key(entry.workload_id), payload)
            with self._lock:
                self._durable_writes += 1
        except RedisError:
            with self._lock:
                self._durable_errors += 1


class IndexMaterializer:
    """
    Transforms attribution results into index entries for decision-time serving.

    Phase 4 of the data flow (Section 3.3): The index builder emits updated
    attribution index rows to the serving cache. Incremental updates; full
    rebuild on backfill.
    """

    def __init__(
        self,
        store: AttributionIndexStore,
        config: PlatformConfig | None = None,
    ) -> None:
        self.store = store
        self.config = config or PlatformConfig()

    def materialize_attribution(
        self,
        result: AttributionResult,
        policies: dict | None = None,
    ) -> AttributionIndexEntry:
        """
        Transform a full AttributionResult into a compact index entry.

        This collapses the full attribution path, explanation, and policy
        context into a flat structure optimized for O(1) lookup.
        """
        # Determine confidence tier.
        conf = result.combined_confidence
        if conf >= self.config.confidence.chargeback_threshold:
            tier = "chargeback_ready"
        elif conf >= self.config.confidence.provisional_threshold:
            tier = "provisional"
        else:
            tier = "estimated"

        # Extract primary attribution target from path or explanation.
        team_id = ""
        team_name = ""
        cost_center_id = ""
        person_id = ""
        repository = ""

        if result.explanation:
            team_id = result.explanation.target_entity
            team_name = result.explanation.target_entity

        # Extract method chain.
        method_used = result.explanation.method_used if result.explanation else "unknown"

        # Build the compact index entry.
        entry = AttributionIndexEntry(
            workload_id=result.workload_id,
            team_id=team_id,
            team_name=team_name,
            cost_center_id=cost_center_id,
            person_id=person_id,
            repository=repository,
            confidence=conf,
            confidence_tier=tier,
            method_used=method_used,
            source_event_ids=[],
            time_bucket=self._compute_time_bucket(),
        )

        # Apply pre-evaluated policies if provided.
        if policies:
            entry = self._apply_policy_context(entry, policies)

        # Write to the serving index.
        self.store.materialize(entry)

        return entry

    def full_rebuild(self, results: list[AttributionResult]) -> int:
        """
        Full index rebuild from a list of attribution results.

        Used during backfill when late-arriving events or corrected identity
        mappings require recomputation (Section 3.2).
        """
        self.store.clear()
        count = 0
        for result in results:
            self.materialize_attribution(result)
            count += 1

        logger.info("index.full_rebuild", entries=count)
        return count

    @staticmethod
    def _compute_time_bucket() -> str:
        """Compute discrete time bucket for deterministic reconstruction."""
        now = datetime.now(UTC)
        # 15-minute buckets for reasonable granularity.
        bucket_min = (now.minute // 15) * 15
        return now.strftime(f"%Y-%m-%dT%H:{bucket_min:02d}:00Z")

    @staticmethod
    def _apply_policy_context(
        entry: AttributionIndexEntry,
        policies: dict,
    ) -> AttributionIndexEntry:
        """Pre-evaluate and embed policy constraints into the index entry."""
        updates: dict = {}

        if "model_allowlist" in policies:
            updates["model_allowlist"] = policies["model_allowlist"]
        if "budget_remaining_usd" in policies:
            updates["budget_remaining_usd"] = policies["budget_remaining_usd"]
        if "budget_limit_usd" in policies:
            updates["budget_limit_usd"] = policies["budget_limit_usd"]
        if "token_budget_output" in policies:
            updates["token_budget_output"] = policies["token_budget_output"]
        if "equivalence_class_id" in policies:
            updates["equivalence_class_id"] = policies["equivalence_class_id"]
            updates["approved_alternatives"] = policies.get("approved_alternatives", [])

        return entry.model_copy(update=updates) if updates else entry
