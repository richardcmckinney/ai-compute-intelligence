"""Versioned model-pricing catalog and deterministic cost estimation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock


@dataclass(frozen=True)
class PricingRule:
    """Provider/model pricing rule effective from a timestamp."""

    provider: str
    model: str
    effective_from: datetime
    input_usd_per_1k_tokens: float
    output_usd_per_1k_tokens: float
    cached_input_usd_per_1k_tokens: float | None = None
    request_base_usd: float = 0.0


@dataclass(frozen=True)
class PricingUsage:
    """Normalized token usage payload for a pricing estimate."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    request_count: int = 1
    effective_at: datetime | None = None


@dataclass(frozen=True)
class CostEstimate:
    """Detailed cost estimate with token-level decomposition."""

    provider: str
    model: str
    rule_effective_from: datetime
    request_count: int
    input_cost_usd: float
    output_cost_usd: float
    cached_input_cost_usd: float
    request_base_cost_usd: float
    total_cost_usd: float


class PricingCatalog:
    """In-memory versioned pricing table with deterministic rule selection."""

    def __init__(self) -> None:
        self._rules: list[PricingRule] = []
        self._lock = Lock()

    def register_rule(self, rule: PricingRule) -> None:
        """Register a pricing rule and keep the catalog sorted by effective time."""
        normalized = PricingRule(
            provider=rule.provider.strip().lower(),
            model=rule.model.strip(),
            effective_from=_aware(rule.effective_from),
            input_usd_per_1k_tokens=rule.input_usd_per_1k_tokens,
            output_usd_per_1k_tokens=rule.output_usd_per_1k_tokens,
            cached_input_usd_per_1k_tokens=rule.cached_input_usd_per_1k_tokens,
            request_base_usd=rule.request_base_usd,
        )
        with self._lock:
            self._rules.append(normalized)
            self._rules.sort(
                key=lambda item: (
                    item.provider,
                    item.model,
                    item.effective_from,
                )
            )

    def list_rules(
        self,
        provider: str | None = None,
        model: str | None = None,
    ) -> list[PricingRule]:
        """List pricing rules, optionally filtered by provider/model."""
        provider_filter = provider.strip().lower() if provider else None
        model_filter = model.strip() if model else None
        with self._lock:
            filtered = [
                rule
                for rule in self._rules
                if (provider_filter is None or rule.provider == provider_filter)
                and (model_filter is None or rule.model == model_filter)
            ]
        return list(filtered)

    def estimate(self, usage: PricingUsage) -> CostEstimate:
        """Estimate request cost from usage and provider/model versioned pricing."""
        if usage.request_count <= 0:
            msg = "request_count must be > 0"
            raise ValueError(msg)

        provider = usage.provider.strip().lower()
        model = usage.model.strip()
        effective_at = _aware(usage.effective_at) if usage.effective_at else datetime.now(UTC)
        rule = self._resolve_rule(provider, model, effective_at)

        if usage.input_tokens < 0 or usage.output_tokens < 0 or usage.cache_read_input_tokens < 0:
            msg = "token counts must be non-negative"
            raise ValueError(msg)

        cache_read = min(usage.cache_read_input_tokens, usage.input_tokens)
        billable_input = max(usage.input_tokens - cache_read, 0)
        cache_rate = (
            rule.cached_input_usd_per_1k_tokens
            if rule.cached_input_usd_per_1k_tokens is not None
            else rule.input_usd_per_1k_tokens
        )

        request_multiplier = float(usage.request_count)
        input_cost = (billable_input / 1000.0) * rule.input_usd_per_1k_tokens * request_multiplier
        cached_input_cost = (cache_read / 1000.0) * cache_rate * request_multiplier
        output_cost = (
            (usage.output_tokens / 1000.0) * rule.output_usd_per_1k_tokens * request_multiplier
        )
        base_cost = rule.request_base_usd * request_multiplier
        total_cost = input_cost + cached_input_cost + output_cost + base_cost

        return CostEstimate(
            provider=provider,
            model=model,
            rule_effective_from=rule.effective_from,
            request_count=usage.request_count,
            input_cost_usd=input_cost,
            output_cost_usd=output_cost,
            cached_input_cost_usd=cached_input_cost,
            request_base_cost_usd=base_cost,
            total_cost_usd=total_cost,
        )

    def _resolve_rule(self, provider: str, model: str, effective_at: datetime) -> PricingRule:
        with self._lock:
            candidates = [
                rule
                for rule in self._rules
                if rule.provider == provider
                and rule.model == model
                and rule.effective_from <= effective_at
            ]
        if not candidates:
            msg = f"no pricing rule available for provider={provider!r} model={model!r}"
            raise ValueError(msg)
        return max(candidates, key=lambda rule: rule.effective_from)

    @property
    def snapshot_id(self) -> str:
        """
        Deterministic pricing snapshot identifier for audit/headers (FR-110/ES 9.2).
        """
        with self._lock:
            rows = [
                {
                    "provider": rule.provider,
                    "model": rule.model,
                    "effective_from": rule.effective_from.isoformat(),
                    "input": rule.input_usd_per_1k_tokens,
                    "output": rule.output_usd_per_1k_tokens,
                    "cached_input": rule.cached_input_usd_per_1k_tokens,
                    "request_base": rule.request_base_usd,
                }
                for rule in self._rules
            ]

        payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()[:12]
        return f"pricing-{digest}"

    @classmethod
    def with_default_rules(cls) -> PricingCatalog:
        """Factory with sensible defaults for demo and local validation."""
        catalog = cls()
        anchor = datetime(2026, 1, 1, tzinfo=UTC)
        catalog.register_rule(
            PricingRule(
                provider="openai",
                model="gpt-4o",
                effective_from=anchor,
                input_usd_per_1k_tokens=0.0050,
                output_usd_per_1k_tokens=0.0150,
            )
        )
        catalog.register_rule(
            PricingRule(
                provider="openai",
                model="gpt-4o-mini",
                effective_from=anchor,
                input_usd_per_1k_tokens=0.00015,
                output_usd_per_1k_tokens=0.00060,
            )
        )
        catalog.register_rule(
            PricingRule(
                provider="google",
                model="gemini-2.0-flash",
                effective_from=anchor,
                input_usd_per_1k_tokens=0.0030,
                output_usd_per_1k_tokens=0.0150,
                cached_input_usd_per_1k_tokens=0.00030,
            )
        )
        catalog.register_rule(
            PricingRule(
                provider="google",
                model="gemini-1.5-flash",
                effective_from=anchor,
                input_usd_per_1k_tokens=0.00025,
                output_usd_per_1k_tokens=0.00125,
                cached_input_usd_per_1k_tokens=0.00003,
            )
        )
        catalog.register_rule(
            PricingRule(
                provider="aws-bedrock",
                model="amazon.nova-pro-v1:0",
                effective_from=anchor,
                input_usd_per_1k_tokens=0.0030,
                output_usd_per_1k_tokens=0.0150,
            )
        )
        return catalog


def _aware(value: datetime) -> datetime:
    """Normalize datetimes to timezone-aware UTC values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
