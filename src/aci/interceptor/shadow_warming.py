"""
Probabilistic shadow warming for cache coherence (Section 6.3).

Prevents the thundering herd problem when cache entries expire or a new
model version is released. On cache miss, the request proceeds without
optimization (fail-open) while a single background worker refreshes the
entry using probabilistic deduplication.

Stale-while-revalidate semantics: serve the last known index entry
while refresh is in progress.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from aci.config import InterceptorConfig

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = structlog.get_logger()


@dataclass
class RefreshState:
    """Tracks in-progress refresh operations to prevent thundering herd."""

    in_progress: set[str] = field(default_factory=set)
    last_refresh: dict[str, float] = field(default_factory=dict)
    min_refresh_interval_s: float = 5.0


class ShadowWarmer:
    """
    Probabilistic shadow warming for the attribution index.

    When a cache miss occurs:
    1. The request proceeds immediately (fail-open).
    2. With probability P (default 0.01), a background refresh is triggered.
    3. All other threads serve the stale entry (stale-while-revalidate).
    4. Only one refresh runs at a time per workload ID.
    """

    def __init__(self, config: InterceptorConfig | None = None) -> None:
        self.config = config or InterceptorConfig()
        self._state = RefreshState()
        self._refresh_count: int = 0
        self._suppressed_count: int = 0
        self._lock = threading.Lock()

    def should_trigger_refresh(self, workload_id: str) -> bool:
        """
        Determine whether this thread should trigger a background refresh.

        Uses probabilistic deduplication (Section 6.3):
        if random() < 0.01, trigger refresh. This ensures that out of
        10,000 concurrent misses, roughly 100 attempt refresh, but only
        one succeeds (the first to acquire the lock).
        """
        with self._lock:
            # Already refreshing this workload.
            if workload_id in self._state.in_progress:
                self._suppressed_count += 1
                return False

            # Minimum interval between refreshes for the same workload.
            now = time.monotonic()
            last = self._state.last_refresh.get(workload_id, 0.0)
            if now - last < self._state.min_refresh_interval_s:
                self._suppressed_count += 1
                return False

            # Probabilistic gate.
            return random.random() < self.config.shadow_warm_probability

    async def trigger_refresh(
        self,
        workload_id: str,
        refresh_fn: Callable[[str], Awaitable[None]],
    ) -> bool:
        """
        Execute a background refresh for a single workload.

        Only one refresh runs at a time per workload ID.
        Returns True if refresh completed, False if suppressed.
        """
        with self._lock:
            if workload_id in self._state.in_progress:
                return False
            self._state.in_progress.add(workload_id)

        try:
            await refresh_fn(workload_id)
            with self._lock:
                self._record_refresh_locked(workload_id, time.monotonic())
                self._refresh_count += 1
            logger.debug("shadow_warmer.refreshed", workload_id=workload_id)
            return True

        except Exception as exc:
            logger.warning(
                "shadow_warmer.refresh_failed",
                workload_id=workload_id,
                error=str(exc),
            )
            return False

        finally:
            with self._lock:
                self._state.in_progress.discard(workload_id)

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "refreshes_completed": self._refresh_count,
                "refreshes_suppressed": self._suppressed_count,
                "in_progress": len(self._state.in_progress),
            }

    def _record_refresh_locked(self, workload_id: str, refreshed_at: float) -> None:
        """Record refresh time while bounding memory growth for unique workload IDs."""
        self._state.last_refresh.pop(workload_id, None)
        self._state.last_refresh[workload_id] = refreshed_at

        max_entries = self.config.shadow_warm_max_tracked_workloads
        while len(self._state.last_refresh) > max_entries:
            oldest_workload = next(iter(self._state.last_refresh))
            self._state.last_refresh.pop(oldest_workload, None)
