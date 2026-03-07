"""
GitHub source control + CI/CD connectors (PRD FR-102, FR-103a).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from aci.models.events import DomainEvent, EventType


class GitHubSCMConnector:
    """Transforms GitHub webhook payloads into canonical SCM events."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id

    def transform_webhook(
        self,
        *,
        event_name: str,
        payload: dict[str, Any],
        delivery_id: str = "",
    ) -> DomainEvent | None:
        normalized_event = event_name.strip().lower()
        repo = payload.get("repository", {}) if isinstance(payload.get("repository"), dict) else {}
        repo_full_name = str(repo.get("full_name", "")).strip()
        timestamp = self._parse_timestamp(
            payload.get("head_commit", {}).get("timestamp")
            if isinstance(payload.get("head_commit"), dict)
            else payload.get("merged_at")
        )

        stable_id = delivery_id.strip() or self._stable_hash(
            normalized_event,
            repo_full_name,
            payload,
        )
        if normalized_event == "push":
            after_sha = str(payload.get("after", "")).strip()
            before_sha = str(payload.get("before", "")).strip()
            pusher = payload.get("pusher", {}) if isinstance(payload.get("pusher"), dict) else {}
            return DomainEvent(
                event_type=EventType.CODE_COMMIT,
                subject_id=after_sha or repo_full_name,
                attributes={
                    "repository": repo_full_name,
                    "branch": str(payload.get("ref", "")).removeprefix("refs/heads/"),
                    "after_sha": after_sha,
                    "before_sha": before_sha,
                    "commit_count": int(payload.get("size", 0) or 0),
                    "pusher": str(pusher.get("name", "")),
                },
                event_time=timestamp,
                source="github-webhook",
                idempotency_key=f"github-push:{stable_id}",
                tenant_id=self.tenant_id,
            )

        if normalized_event == "pull_request":
            action = str(payload.get("action", "")).strip().lower()
            pr = (
                payload.get("pull_request", {})
                if isinstance(payload.get("pull_request"), dict)
                else {}
            )
            merged = bool(pr.get("merged", False))
            if action == "closed" and merged:
                author = pr.get("user", {}) if isinstance(pr.get("user"), dict) else {}
                return DomainEvent(
                    event_type=EventType.PR_MERGED,
                    subject_id=str(pr.get("id", "")).strip() or repo_full_name,
                    attributes={
                        "repository": repo_full_name,
                        "pr_number": int(pr.get("number", 0) or 0),
                        "merge_sha": str(pr.get("merge_commit_sha", "")),
                        "author": str(author.get("login", "")),
                        "base_branch": str(pr.get("base", {}).get("ref", "")),
                    },
                    event_time=self._parse_timestamp(pr.get("merged_at")),
                    source="github-webhook",
                    idempotency_key=f"github-pr-merge:{stable_id}",
                    tenant_id=self.tenant_id,
                )

        return None

    @staticmethod
    def _parse_timestamp(raw_time: object) -> datetime:
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


class GitHubActionsConnector:
    """Transforms GitHub Actions workflow/job webhooks into deployment events."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id

    def transform_workflow_job(
        self,
        payload: dict[str, Any],
        *,
        delivery_id: str = "",
    ) -> DomainEvent | None:
        action = str(payload.get("action", "")).strip().lower()
        if action != "completed":
            return None

        workflow_job = (
            payload.get("workflow_job", {}) if isinstance(payload.get("workflow_job"), dict) else {}
        )
        repo = payload.get("repository", {}) if isinstance(payload.get("repository"), dict) else {}
        repo_full_name = str(repo.get("full_name", "")).strip()
        run_id = str(workflow_job.get("run_id", "")).strip()
        if not repo_full_name and not run_id:
            return None

        labels = workflow_job.get("labels", [])
        normalized_labels = [str(label) for label in labels] if isinstance(labels, list) else []
        actor = payload.get("sender", {}) if isinstance(payload.get("sender"), dict) else {}
        event_time = GitHubSCMConnector._parse_timestamp(workflow_job.get("completed_at"))
        stable_id = delivery_id.strip() or GitHubSCMConnector._stable_hash(
            "workflow_job", repo_full_name, run_id, workflow_job
        )

        return DomainEvent(
            event_type=EventType.DEPLOYMENT,
            subject_id=run_id or repo_full_name,
            attributes={
                "service_name": (
                    repo_full_name.split("/")[-1] if repo_full_name else "github-actions"
                ),
                "repository": repo_full_name,
                "deploy_job_id": run_id,
                "deployer_identity": str(actor.get("login", "")),
                "target_environment": self._infer_environment(normalized_labels),
                "workflow_name": str(workflow_job.get("name", "")),
                "workflow_conclusion": str(workflow_job.get("conclusion", "")),
            },
            event_time=event_time,
            source="github-actions",
            idempotency_key=f"github-actions:{stable_id}",
            tenant_id=self.tenant_id,
        )

    @staticmethod
    def _infer_environment(labels: list[str]) -> str:
        lowered = [label.lower() for label in labels]
        if any("prod" in label for label in lowered):
            return "production"
        if any("stage" in label for label in lowered):
            return "staging"
        if any("dev" in label or "test" in label for label in lowered):
            return "development"
        return "unknown"
