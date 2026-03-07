"""
Okta identity connector (PRD FR-101a).

Supports transformation of:
- System Log events (session login, group membership changes)
- SCIM provisioning payloads for user/group membership updates
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from aci.models.events import DomainEvent, EventType


class OktaIdentityConnector:
    """Transforms Okta System Log + SCIM payloads into canonical domain events."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id

    def transform_system_log_event(self, event: dict[str, Any]) -> DomainEvent | None:
        """Transform one Okta System Log event."""
        event_type = str(event.get("eventType", "")).strip()
        actor = event.get("actor", {}) if isinstance(event.get("actor"), dict) else {}
        targets = event.get("target", [])
        target = targets[0] if isinstance(targets, list) and targets else {}
        target_profile = (
            target.get("detailEntry", {}) if isinstance(target.get("detailEntry"), dict) else {}
        )

        event_time = self._parse_event_time(event.get("published"))
        event_id = str(event.get("uuid", "")).strip()
        idempotency_key = (
            f"okta:{event_id}"
            if event_id
            else f"okta-fallback:{self._stable_hash(event_type, event_time.isoformat(), event)}"
        )

        if event_type in {"user.session.start", "user.authentication.sso"}:
            subject_id = str(actor.get("id") or actor.get("alternateId") or "").strip()
            if not subject_id:
                return None
            return DomainEvent(
                event_type=EventType.IDENTITY_LOGIN,
                subject_id=subject_id,
                attributes={
                    "actor_id": str(actor.get("id", "")),
                    "actor_login": str(actor.get("alternateId", "")),
                    "actor_display_name": str(actor.get("displayName", "")),
                    "ip_address": str(event.get("client", {}).get("ipAddress", "")),
                    "event_type": event_type,
                },
                event_time=event_time,
                source="okta-system-log",
                idempotency_key=idempotency_key,
                tenant_id=self.tenant_id,
            )

        if event_type in {"group.user_membership.add", "group.user_membership.remove"}:
            user_id = str(actor.get("id") or actor.get("alternateId") or "").strip()
            group_id = str(target.get("id", "")).strip()
            group_name = str(target.get("displayName", "") or target_profile.get("groupName", ""))
            if not user_id or not group_id:
                return None
            return DomainEvent(
                event_type=EventType.IDENTITY_GROUP_CHANGE,
                subject_id=user_id,
                attributes={
                    "group_id": group_id,
                    "group_name": group_name,
                    "operation": "add" if event_type.endswith(".add") else "remove",
                    "event_type": event_type,
                },
                event_time=event_time,
                source="okta-system-log",
                idempotency_key=idempotency_key,
                tenant_id=self.tenant_id,
            )

        return None

    def transform_scim_user(self, payload: dict[str, Any]) -> DomainEvent | None:
        """Transform one SCIM user payload into canonical identity-group event."""
        user_id = str(payload.get("id", "")).strip()
        if not user_id:
            return None

        groups = payload.get("groups", [])
        normalized_groups = []
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                normalized_groups.append(
                    {
                        "group_id": str(group.get("value", "")),
                        "group_name": str(group.get("display", "")),
                    }
                )

        username = str(payload.get("userName", "")).strip()
        active = bool(payload.get("active", True))
        key_fingerprint = self._stable_hash(
            "okta-scim",
            user_id,
            normalized_groups,
            username,
            active,
        )
        return DomainEvent(
            event_type=EventType.IDENTITY_GROUP_CHANGE,
            subject_id=user_id,
            attributes={
                "username": username,
                "active": active,
                "groups": normalized_groups,
                "operation": "sync",
            },
            event_time=datetime.now(UTC),
            source="okta-scim",
            idempotency_key=f"okta-scim:{key_fingerprint}",
            tenant_id=self.tenant_id,
        )

    @staticmethod
    def _parse_event_time(raw_time: object) -> datetime:
        if isinstance(raw_time, datetime):
            if raw_time.tzinfo is None or raw_time.utcoffset() is None:
                return raw_time.replace(tzinfo=UTC)
            return raw_time.astimezone(UTC)

        if not raw_time:
            return datetime.now(UTC)

        value = str(raw_time).strip()
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(UTC)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _stable_hash(*parts: object) -> str:
        payload = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":")).encode(
            "utf-8"
        )
        return hashlib.sha256(payload).hexdigest()[:24]
