"""
Strict event schema validation for ingestion and broker publication.

Production safety requirement: events must conform to explicit schemas
before entering the append-only bus. This prevents malformed payloads from
silently propagating through reconciliation and materialization.
"""

from __future__ import annotations

from datetime import datetime

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aci.models.events import DomainEvent, EventType


class EventSchemaValidationError(ValueError):
    """Raised when a domain event payload fails schema validation."""


class _StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _InferencePayload(_StrictBaseModel):
    model: str
    provider: str
    cloud_resource_arn: str = ""
    region: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    api_key_id: str = ""
    service_name: str = ""
    request_id: str = ""


class _DeploymentPayload(_StrictBaseModel):
    service_name: str
    repository: str
    commit_sha: str = ""
    deployer_identity: str = ""
    deploy_tool: str = ""
    deploy_job_id: str = ""
    target_environment: str = ""
    target_resource_arn: str = ""
    timestamp: datetime | None = None


class _BillingLineItemPayload(_StrictBaseModel):
    cloud_provider: str
    account_id: str
    service: str
    resource_arn: str = ""
    region: str = ""
    cost_usd: float
    usage_quantity: float = 0.0
    usage_unit: str = ""
    tags: dict[str, str] = Field(default_factory=dict)
    billing_period_start: datetime | None = None
    billing_period_end: datetime | None = None


class _OrgChangePayload(_StrictBaseModel):
    person_id: str
    previous_team: str = ""
    new_team: str = ""
    previous_manager: str = ""
    new_manager: str = ""
    effective_date: datetime | None = None
    source_system: str = "workday"


class _ShadowInterceptPayload(_StrictBaseModel):
    request_id: str
    workload_id: str
    elapsed_ms: float
    interceptor_mode: str


class _AttributionCorrectionPayload(_StrictBaseModel):
    attribution_id: str
    true_team_id: str
    true_cost_center_id: str
    predicted_team_id: str = ""
    predicted_confidence: float = 0.0
    method_used: str = ""
    was_correct: bool = False


EVENT_PAYLOAD_SCHEMAS: dict[EventType, type[BaseModel]] = {
    EventType.INFERENCE_REQUEST: _InferencePayload,
    EventType.INFERENCE_RESPONSE: _InferencePayload,
    EventType.DEPLOYMENT: _DeploymentPayload,
    EventType.BILLING_LINE_ITEM: _BillingLineItemPayload,
    EventType.ORG_CHANGE: _OrgChangePayload,
    EventType.SHADOW_INTERCEPT_MISS: _ShadowInterceptPayload,
    EventType.SHADOW_INTERCEPT_TIMEOUT: _ShadowInterceptPayload,
    EventType.ATTRIBUTION_CORRECTION: _AttributionCorrectionPayload,
}


def validate_event_attributes(
    event_type: EventType,
    attributes: dict[str, Any],
) -> dict[str, Any]:
    """Validate event attributes against the strict schema for the event type."""
    schema_model = EVENT_PAYLOAD_SCHEMAS.get(event_type)
    if schema_model is None:
        return attributes

    try:
        validated = schema_model.model_validate(attributes)
    except ValidationError as exc:
        raise EventSchemaValidationError(
            f"Invalid payload for event_type='{event_type.value}': {exc.errors()}"
        ) from exc

    return validated.model_dump()


def validate_domain_event(event: DomainEvent) -> None:
    """Validate the payload of a fully-formed DomainEvent."""
    if event.schema_version != 1:
        raise EventSchemaValidationError(
            f"Unsupported schema_version={event.schema_version}; expected version=1"
        )
    validate_event_attributes(event.event_type, event.attributes)
