"""
AWS ingestion connector: OTel-native passive ingestion (Section 9.1).

Connects to existing log pipelines (CloudWatch Logs, Datadog forwarders,
cloud billing APIs) and emits canonical DomainEvents to the event bus.
No bespoke host-level agents required.

Integration contract defines: minimum required fields, optional enrichment,
how they are obtained without code changes, and fallback when missing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from aci.models.events import (
    BillingLineItem,
    DomainEvent,
    EventType,
    InferenceEvent,
)

logger = structlog.get_logger()


class AWSBillingConnector:
    """
    Ingests AWS Cost and Usage Reports (CUR) and transforms them into
    canonical BillingLineItem events.

    Integration contract:
    - Required: account_id, service, cost_usd, usage_period.
    - Optional: resource_arn, tags, region.
    - Fallback: when resource_arn is missing, fall back to service-level attribution.
    """

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id

    def transform_cur_record(self, record: dict[str, Any]) -> DomainEvent:
        """Transform a single AWS CUR record into a DomainEvent."""
        billing = BillingLineItem(
            cloud_provider="aws",
            account_id=record.get("lineItem/UsageAccountId", ""),
            service=record.get("lineItem/ProductCode", ""),
            resource_arn=record.get("lineItem/ResourceId", ""),
            region=record.get("product/region", ""),
            cost_usd=float(record.get("lineItem/UnblendedCost", 0)),
            usage_quantity=float(record.get("lineItem/UsageAmount", 0)),
            usage_unit=record.get("pricing/unit", ""),
            tags=self._extract_tags(record),
        )

        return DomainEvent(
            event_type=EventType.BILLING_LINE_ITEM,
            subject_id=billing.resource_arn or billing.account_id,
            attributes=billing.model_dump(),
            event_time=datetime.now(UTC),
            source="aws-cur",
            idempotency_key=f"aws-cur:{record.get('identity/LineItemId', '')}",
            tenant_id=self.tenant_id,
        )

    @staticmethod
    def _extract_tags(record: dict[str, Any]) -> dict[str, str]:
        """Extract resource tags from CUR record."""
        tags: dict[str, str] = {}
        for key, value in record.items():
            if key.startswith("resourceTags/user:") and value:
                tag_name = key.replace("resourceTags/user:", "")
                tags[tag_name] = str(value)
        return tags


class AWSCloudTrailConnector:
    """
    Ingests CloudTrail events for identity and deployment correlation.

    Provides temporal signals for R2 reconciliation and identity signals
    for R1 direct matching.
    """

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id

    def transform_event(self, trail_event: dict[str, Any]) -> DomainEvent | None:
        """Transform a CloudTrail event into a DomainEvent."""
        event_name = trail_event.get("eventName", "")

        # Map relevant CloudTrail events to our event types.
        if event_name in ("CreateEndpoint", "UpdateEndpoint", "CreateFunction"):
            return self._transform_resource_event(trail_event)
        elif event_name in ("AssumeRole", "ConsoleLogin"):
            return self._transform_identity_event(trail_event)

        return None

    def _transform_resource_event(self, trail_event: dict[str, Any]) -> DomainEvent:
        identity = trail_event.get("userIdentity", {})
        return DomainEvent(
            event_type=EventType.RESOURCE_CREATED,
            subject_id=trail_event.get("responseElements", {}).get("endpointArn", ""),
            attributes={
                "event_name": trail_event.get("eventName"),
                "identity_arn": identity.get("arn", ""),
                "identity_type": identity.get("type", ""),
                "source_ip": trail_event.get("sourceIPAddress", ""),
                "region": trail_event.get("awsRegion", ""),
            },
            event_time=datetime.fromisoformat(
                trail_event.get("eventTime", datetime.now(UTC).isoformat())
            ),
            source="aws-cloudtrail",
            idempotency_key=f"cloudtrail:{trail_event.get('eventID', '')}",
            tenant_id=self.tenant_id,
        )

    def _transform_identity_event(self, trail_event: dict[str, Any]) -> DomainEvent:
        identity = trail_event.get("userIdentity", {})
        return DomainEvent(
            event_type=EventType.IDENTITY_LOGIN,
            subject_id=identity.get("arn", ""),
            attributes={
                "principal_id": identity.get("principalId", ""),
                "account_id": identity.get("accountId", ""),
                "user_name": identity.get("userName", ""),
                "session_context": trail_event.get("requestParameters", {}),
            },
            event_time=datetime.fromisoformat(
                trail_event.get("eventTime", datetime.now(UTC).isoformat())
            ),
            source="aws-cloudtrail",
            idempotency_key=f"cloudtrail:{trail_event.get('eventID', '')}",
            tenant_id=self.tenant_id,
        )


class BedrockTelemetryConnector:
    """
    Ingests Amazon Bedrock model invocation telemetry.

    Captures inference events including model ID, token counts, latency,
    and cost from Bedrock's built-in logging.
    """

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id

    def transform_invocation(self, log_entry: dict[str, Any]) -> DomainEvent:
        """Transform a Bedrock invocation log into an InferenceEvent."""
        inference = InferenceEvent(
            model=log_entry.get("modelId", ""),
            provider="aws-bedrock",
            cloud_resource_arn=log_entry.get("modelArn", ""),
            region=log_entry.get("region", ""),
            input_tokens=log_entry.get("inputTokenCount", 0),
            output_tokens=log_entry.get("outputTokenCount", 0),
            latency_ms=log_entry.get("invocationLatency", 0),
            cost_usd=self._estimate_cost(log_entry),
            request_id=log_entry.get("requestId", ""),
        )

        return DomainEvent(
            event_type=EventType.INFERENCE_REQUEST,
            subject_id=inference.request_id,
            attributes=inference.model_dump(),
            event_time=datetime.fromisoformat(
                log_entry.get("timestamp", datetime.now(UTC).isoformat())
            ),
            source="aws-bedrock",
            idempotency_key=f"bedrock:{inference.request_id}",
            tenant_id=self.tenant_id,
        )

    @staticmethod
    def _estimate_cost(log_entry: dict[str, Any]) -> float:
        """Estimate cost from token counts using known Bedrock pricing."""
        # Simplified pricing lookup. In production, this uses a pricing table
        # updated from the AWS Pricing API.
        input_tokens = float(log_entry.get("inputTokenCount", 0))
        output_tokens = float(log_entry.get("outputTokenCount", 0))
        model_id = log_entry.get("modelId", "")

        # Example rates (per 1K tokens).
        rates: dict[str, tuple[float, float]] = {
            "anthropic.claude-3-5-sonnet": (0.003, 0.015),
            "anthropic.claude-3-haiku": (0.00025, 0.00125),
            "amazon.titan-text-express": (0.0002, 0.0006),
        }

        input_rate, output_rate = rates.get(str(model_id), (0.001, 0.002))
        return (input_tokens * input_rate + output_tokens * output_rate) / 1000
