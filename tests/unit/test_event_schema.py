from __future__ import annotations

from typing import cast

import pytest

from aci.core.event_schema import (
    EVENT_PAYLOAD_SCHEMAS,
    EventSchemaValidationError,
    validate_event_attributes,
)
from aci.models.events import EventType


def test_all_event_types_have_registered_schema_models() -> None:
    missing = sorted(
        event_type.value
        for event_type in EventType
        if event_type not in EVENT_PAYLOAD_SCHEMAS
    )
    assert missing == []


def test_unknown_event_type_is_rejected() -> None:
    with pytest.raises(EventSchemaValidationError, match="No schema registered"):
        validate_event_attributes(cast("EventType", "unsupported.event"), {})


def test_resource_lifecycle_payload_requires_resource_arn() -> None:
    with pytest.raises(EventSchemaValidationError):
        validate_event_attributes(
            EventType.RESOURCE_CREATED,
            {"cloud_provider": "aws"},
        )

    payload = validate_event_attributes(
        EventType.RESOURCE_CREATED,
        {
            "resource_arn": "arn:aws:sagemaker:us-east-1:123:endpoint/example",
            "cloud_provider": "aws",
        },
    )
    assert payload["resource_arn"].startswith("arn:")
