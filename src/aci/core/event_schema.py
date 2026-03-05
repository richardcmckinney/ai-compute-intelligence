"""
Strict event schema validation for ingestion and broker publication.

Production safety requirement: events must conform to explicit schemas
before entering the append-only bus. This prevents malformed payloads from
silently propagating through reconciliation and materialization.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
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


class _ResourceLifecyclePayload(_StrictBaseModel):
    resource_arn: str
    cloud_provider: str = ""
    service: str = ""
    region: str = ""
    tags: dict[str, str] = Field(default_factory=dict)
    timestamp: datetime | None = None


class _ScmCommitPayload(_StrictBaseModel):
    repository: str
    commit_sha: str = ""
    author_identity: str = ""
    branch: str = ""
    message: str = ""
    timestamp: datetime | None = None


class _CodeownerChangePayload(_StrictBaseModel):
    repository: str
    changed_path: str = ""
    previous_owner: str = ""
    new_owner: str = ""
    actor_identity: str = ""
    timestamp: datetime | None = None


class _PrMergedPayload(_StrictBaseModel):
    repository: str
    pr_number: int = 0
    merged_by: str = ""
    author_identity: str = ""
    branch: str = ""
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


class _IdentityLoginPayload(_StrictBaseModel):
    person_id: str
    auth_provider: str = ""
    ip_address: str = ""
    succeeded: bool = True
    timestamp: datetime | None = None


class _IdentityGroupChangePayload(_StrictBaseModel):
    person_id: str
    group_id: str
    action: str = "added"
    source_system: str = ""
    timestamp: datetime | None = None


class _TeamMembershipPayload(_StrictBaseModel):
    person_id: str
    team_id: str
    action: str = "added"
    source_system: str = ""
    effective_date: datetime | None = None


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


class _ChargebackDecisionPayload(_StrictBaseModel):
    attribution_id: str
    disposition: str = ""
    notes: str = ""
    decided_by: str = ""
    timestamp: datetime | None = None


class _FinopsReviewPayload(_StrictBaseModel):
    attribution_id: str
    reviewer: str = ""
    decision: str = ""
    notes: str = ""
    timestamp: datetime | None = None


class _AttributionComputedPayload(_StrictBaseModel):
    workload_id: str
    team_id: str = ""
    confidence: float = 0.0
    method_used: str = ""
    timestamp: datetime | None = None


class _IndexMaterializedPayload(_StrictBaseModel):
    workload_id: str
    index_version: int = 1
    source_event_ids: list[str] = Field(default_factory=list)
    timestamp: datetime | None = None


class _CalibrationUpdatedPayload(_StrictBaseModel):
    method: str
    sample_count: int = 0
    timestamp: datetime | None = None


EVENT_PAYLOAD_SCHEMAS: dict[EventType, type[BaseModel]] = {
    EventType.INFERENCE_REQUEST: _InferencePayload,
    EventType.INFERENCE_RESPONSE: _InferencePayload,
    EventType.RESOURCE_CREATED: _ResourceLifecyclePayload,
    EventType.RESOURCE_DELETED: _ResourceLifecyclePayload,
    EventType.DEPLOYMENT: _DeploymentPayload,
    EventType.BILLING_LINE_ITEM: _BillingLineItemPayload,
    EventType.CODE_COMMIT: _ScmCommitPayload,
    EventType.CODEOWNER_CHANGE: _CodeownerChangePayload,
    EventType.PR_MERGED: _PrMergedPayload,
    EventType.IDENTITY_LOGIN: _IdentityLoginPayload,
    EventType.IDENTITY_GROUP_CHANGE: _IdentityGroupChangePayload,
    EventType.ORG_CHANGE: _OrgChangePayload,
    EventType.TEAM_MEMBERSHIP: _TeamMembershipPayload,
    EventType.SHADOW_INTERCEPT_MISS: _ShadowInterceptPayload,
    EventType.SHADOW_INTERCEPT_TIMEOUT: _ShadowInterceptPayload,
    EventType.ATTRIBUTION_CORRECTION: _AttributionCorrectionPayload,
    EventType.CHARGEBACK_DECISION: _ChargebackDecisionPayload,
    EventType.FINOPS_REVIEW: _FinopsReviewPayload,
    EventType.ATTRIBUTION_COMPUTED: _AttributionComputedPayload,
    EventType.INDEX_MATERIALIZED: _IndexMaterializedPayload,
    EventType.CALIBRATION_UPDATED: _CalibrationUpdatedPayload,
}


def validate_event_attributes(
    event_type: EventType | str,
    attributes: dict[str, Any],
) -> dict[str, Any]:
    """Validate event attributes against the strict schema for the event type."""
    resolved_type: EventType | None
    if isinstance(event_type, EventType):
        resolved_type = event_type
    else:
        try:
            resolved_type = EventType(str(event_type))
        except ValueError:
            resolved_type = None

    event_type_value = event_type.value if isinstance(event_type, EventType) else str(event_type)
    schema_model = EVENT_PAYLOAD_SCHEMAS.get(resolved_type) if resolved_type else None
    if schema_model is None:
        raise EventSchemaValidationError(
            f"No schema registered for event_type='{event_type_value}'"
        )

    try:
        validated = schema_model.model_validate(attributes)
    except ValidationError as exc:
        raise EventSchemaValidationError(
            f"Invalid payload for event_type='{event_type_value}': {exc.errors()}"
        ) from exc

    return validated.model_dump()


def validate_domain_event(event: DomainEvent) -> None:
    """Validate the payload of a fully-formed DomainEvent."""
    if event.schema_version != 1:
        raise EventSchemaValidationError(
            f"Unsupported schema_version={event.schema_version}; expected version=1"
        )
    validate_event_attributes(event.event_type, event.attributes)
