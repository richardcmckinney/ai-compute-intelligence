"""
Domain events for the append-only event bus.

All enterprise signals enter the system as immutable events (Patent Spec Section 3.2).
The event bus is the system of record; the graph is derived state. Each event carries
an idempotency_key per source to prevent duplicate processing.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator


class EventType(StrEnum):
    """Canonical event types ingested from enterprise systems."""

    # Cloud billing and telemetry.
    INFERENCE_REQUEST = "inference.request"
    INFERENCE_RESPONSE = "inference.response"
    BILLING_LINE_ITEM = "billing.line_item"
    RESOURCE_CREATED = "resource.created"
    RESOURCE_DELETED = "resource.deleted"

    # CI/CD and source control.
    DEPLOYMENT = "cicd.deployment"
    CODE_COMMIT = "scm.commit"
    CODEOWNER_CHANGE = "scm.codeowner_change"
    PR_MERGED = "scm.pr_merged"

    # Identity and organization.
    IDENTITY_LOGIN = "identity.login"
    IDENTITY_GROUP_CHANGE = "identity.group_change"
    ORG_CHANGE = "org.hierarchy_change"
    TEAM_MEMBERSHIP = "org.team_membership"

    # Manual corrections and ground truth.
    ATTRIBUTION_CORRECTION = "correction.attribution"
    CHARGEBACK_DECISION = "correction.chargeback"
    FINOPS_REVIEW = "correction.finops_review"

    # System-internal events.
    ATTRIBUTION_COMPUTED = "system.attribution_computed"
    INDEX_MATERIALIZED = "system.index_materialized"
    CALIBRATION_UPDATED = "system.calibration_updated"

    # Interceptor shadow events (Section 6.3).
    # Emitted on cache miss or timeout, consumed by async reconciliation
    # to trigger graph resolution for unknown/stale workloads.
    SHADOW_INTERCEPT_MISS = "interceptor.shadow.miss"
    SHADOW_INTERCEPT_TIMEOUT = "interceptor.shadow.timeout"


class DomainEvent(BaseModel):
    """
    Immutable event on the append-only event bus.

    Schema from Patent Spec Section 3.2:
    { event_id, event_type, subject_id, attributes{}, event_time,
      ingest_time, source, idempotency_key }
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: EventType
    subject_id: str = Field(description="Primary entity this event concerns")
    attributes: dict[str, Any] = Field(default_factory=dict)
    event_time: datetime = Field(description="When the event occurred in the source system")
    ingest_time: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the event entered the platform",
    )
    source: str = Field(description="Originating system (e.g., 'aws-cloudwatch', 'okta')")
    idempotency_key: str = Field(
        description="Per-source key for at-least-once delivery deduplication"
    )
    tenant_id: str = Field(description="Customer tenant isolation boundary")
    schema_version: int = Field(default=1, description="Event schema version for evolution")

    model_config = {"frozen": True}

    MAX_ATTRIBUTES_PAYLOAD_BYTES: ClassVar[int] = 512 * 1024

    @field_validator("event_time", "ingest_time")
    @classmethod
    def _validate_tz_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime fields must be timezone-aware")
        return value

    @field_validator("attributes")
    @classmethod
    def _validate_attributes_payload_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(value, default=str, separators=(",", ":")).encode("utf-8")
        if len(encoded) > cls.MAX_ATTRIBUTES_PAYLOAD_BYTES:
            raise ValueError(f"attributes payload exceeds {cls.MAX_ATTRIBUTES_PAYLOAD_BYTES} bytes")
        return value


class InferenceEvent(BaseModel):
    """Structured attributes for an inference request/response event."""

    model: str
    provider: str
    cloud_resource_arn: str = ""
    region: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = Field(default=0.0, ge=0.0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    api_key_id: str = ""
    service_name: str = ""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class DeploymentEvent(BaseModel):
    """Structured attributes for a CI/CD deployment event."""

    service_name: str
    repository: str
    commit_sha: str = ""
    deployer_identity: str = ""
    deploy_tool: str = ""  # spinnaker, github-actions, jenkins, etc.
    deploy_job_id: str = ""
    target_environment: str = ""
    target_resource_arn: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BillingLineItem(BaseModel):
    """Structured attributes for a cloud billing line item."""

    cloud_provider: str  # aws, azure, gcp
    account_id: str
    service: str
    resource_arn: str = ""
    region: str = ""
    cost_usd: float = Field(ge=0.0)
    usage_quantity: float = Field(default=0.0, ge=0.0)
    usage_unit: str = ""
    tags: dict[str, str] = Field(default_factory=dict)
    billing_period_start: datetime | None = None
    billing_period_end: datetime | None = None


class OrgChangeEvent(BaseModel):
    """Structured attributes for organizational hierarchy changes."""

    person_id: str
    previous_team: str = ""
    new_team: str = ""
    previous_manager: str = ""
    new_manager: str = ""
    effective_date: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_system: str = "workday"
