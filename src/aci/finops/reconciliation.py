"""Bifurcated synthetic vs reconciled cost tracking and drift reporting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock


@dataclass
class CostRecord:
    """Synthetic and reconciled cost record for a single request."""

    request_id: str
    service_name: str
    provider: str
    model: str
    synthetic_cost_usd: float
    synthetic_recorded_at: datetime
    reconciled_cost_usd: float | None = None
    reconciled_recorded_at: datetime | None = None
    reconciliation_source: str = ""

    @property
    def is_reconciled(self) -> bool:
        return self.reconciled_cost_usd is not None

    @property
    def drift_usd(self) -> float | None:
        if self.reconciled_cost_usd is None:
            return None
        return self.reconciled_cost_usd - self.synthetic_cost_usd

    @property
    def drift_pct(self) -> float | None:
        drift = self.drift_usd
        if drift is None:
            return None
        if self.synthetic_cost_usd == 0:
            return 0.0 if drift == 0 else 100.0
        return (drift / self.synthetic_cost_usd) * 100.0


@dataclass(frozen=True)
class CostDriftSummary:
    """Aggregated drift metrics for provider/service grouping."""

    group: str
    total_records: int
    reconciled_records: int
    unresolved_records: int
    synthetic_cost_usd: float
    reconciled_cost_usd: float
    absolute_drift_usd: float
    drift_pct: float


class ReconciliationLedger:
    """Thread-safe in-memory ledger for synthetic and reconciled request costs."""

    def __init__(self) -> None:
        self._records: dict[str, CostRecord] = {}
        self._lock = Lock()

    def upsert_synthetic(
        self,
        *,
        request_id: str,
        service_name: str,
        provider: str,
        model: str,
        synthetic_cost_usd: float,
        recorded_at: datetime | None = None,
    ) -> CostRecord:
        """Insert or update synthetic cost estimate for a request."""
        timestamp = _aware(recorded_at) if recorded_at else datetime.now(UTC)
        normalized_provider = provider.strip().lower()
        with self._lock:
            current = self._records.get(request_id)
            if current is None:
                record = CostRecord(
                    request_id=request_id,
                    service_name=service_name.strip(),
                    provider=normalized_provider,
                    model=model.strip(),
                    synthetic_cost_usd=synthetic_cost_usd,
                    synthetic_recorded_at=timestamp,
                )
                self._records[request_id] = record
                return record

            current.service_name = service_name.strip()
            current.provider = normalized_provider
            current.model = model.strip()
            current.synthetic_cost_usd = synthetic_cost_usd
            current.synthetic_recorded_at = timestamp
            return current

    def reconcile(
        self,
        *,
        request_id: str,
        reconciled_cost_usd: float,
        source: str = "billing_api",
        recorded_at: datetime | None = None,
    ) -> CostRecord:
        """Attach billing-truth reconciled cost to an existing request record."""
        timestamp = _aware(recorded_at) if recorded_at else datetime.now(UTC)
        with self._lock:
            record = self._records.get(request_id)
            if record is None:
                msg = f"request_id={request_id!r} not found in synthetic ledger"
                raise ValueError(msg)
            record.reconciled_cost_usd = reconciled_cost_usd
            record.reconciled_recorded_at = timestamp
            record.reconciliation_source = source.strip()
            return record

    def get(self, request_id: str) -> CostRecord | None:
        """Return one record if present."""
        with self._lock:
            record = self._records.get(request_id)
        return record

    def list_records(self) -> list[CostRecord]:
        """Return all records in insertion order."""
        with self._lock:
            return list(self._records.values())

    def clear(self) -> None:
        """Clear all records (used by deterministic tests/demo resets)."""
        with self._lock:
            self._records.clear()

    def summarize_drift(self, group_by: str = "provider") -> list[CostDriftSummary]:
        """Return aggregated drift metrics grouped by provider or service."""
        if group_by not in {"provider", "service"}:
            msg = "group_by must be one of {'provider', 'service'}"
            raise ValueError(msg)

        with self._lock:
            records = list(self._records.values())

        buckets: dict[str, list[CostRecord]] = {}
        for record in records:
            key = record.provider if group_by == "provider" else record.service_name
            buckets.setdefault(key, []).append(record)

        summaries: list[CostDriftSummary] = []
        for key, items in buckets.items():
            total = len(items)
            reconciled = [item for item in items if item.is_reconciled]
            unresolved = total - len(reconciled)
            synth_total = sum(item.synthetic_cost_usd for item in items)
            reconciled_total = sum(item.reconciled_cost_usd or 0.0 for item in reconciled)
            absolute_drift = abs(reconciled_total - synth_total)
            drift_pct = 0.0
            if synth_total > 0:
                drift_pct = ((reconciled_total - synth_total) / synth_total) * 100.0

            summaries.append(
                CostDriftSummary(
                    group=key,
                    total_records=total,
                    reconciled_records=len(reconciled),
                    unresolved_records=unresolved,
                    synthetic_cost_usd=synth_total,
                    reconciled_cost_usd=reconciled_total,
                    absolute_drift_usd=absolute_drift,
                    drift_pct=drift_pct,
                )
            )

        summaries.sort(key=lambda summary: summary.absolute_drift_usd, reverse=True)
        return summaries


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
