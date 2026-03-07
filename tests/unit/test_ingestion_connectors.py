from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aci.ingestion.connectors.aws import AWSBillingConnector, AWSCloudTrailConnector
from aci.ingestion.connectors.github import GitHubActionsConnector, GitHubSCMConnector
from aci.ingestion.connectors.okta import OktaIdentityConnector
from aci.models.events import EventType


def test_aws_cur_requires_account_and_service() -> None:
    connector = AWSBillingConnector(tenant_id="tenant-a")
    with pytest.raises(ValueError, match="missing required"):
        connector.transform_cur_record(
            {
                "lineItem/UsageAccountId": "",
                "lineItem/ProductCode": "",
                "lineItem/UnblendedCost": "1.2",
            }
        )


def test_aws_cur_fallback_idempotency_when_line_item_id_missing() -> None:
    connector = AWSBillingConnector(tenant_id="tenant-a")
    event = connector.transform_cur_record(
        {
            "lineItem/UsageAccountId": "123456789012",
            "lineItem/ProductCode": "AmazonBedrock",
            "lineItem/UnblendedCost": "4.50",
            "lineItem/UsageStartDate": "2026-03-01T00:00:00Z",
            "lineItem/UsageEndDate": "2026-03-01T01:00:00Z",
        }
    )
    assert event.idempotency_key.startswith("aws-cur-fallback:")
    assert event.event_type == EventType.BILLING_LINE_ITEM


def test_aws_cloudtrail_session_context_is_allowlisted() -> None:
    connector = AWSCloudTrailConnector(tenant_id="tenant-a")
    event = connector.transform_event(
        {
            "eventName": "AssumeRole",
            "eventTime": "2026-03-04T01:02:03Z",
            "eventID": "evt-123",
            "userIdentity": {
                "arn": "arn:aws:iam::123456789012:user/alice",
                "principalId": "AIDAEXAMPLE",
                "accountId": "123456789012",
                "userName": "alice",
            },
            "requestParameters": {
                "roleArn": "arn:aws:iam::123456789012:role/analytics",
                "roleSessionName": "alice-session",
                "password": "must-not-pass-through",
            },
        }
    )

    assert event is not None
    assert event.event_type == EventType.IDENTITY_LOGIN
    assert event.attributes["session_context"]["roleArn"].endswith("role/analytics")
    assert "password" not in event.attributes["session_context"]


def test_okta_system_log_login_and_group_change() -> None:
    connector = OktaIdentityConnector(tenant_id="tenant-a")

    login_event = connector.transform_system_log_event(
        {
            "eventType": "user.session.start",
            "uuid": "okta-evt-1",
            "published": "2026-03-05T02:04:06Z",
            "actor": {"id": "00u1", "alternateId": "alice@example.com", "displayName": "Alice"},
            "client": {"ipAddress": "203.0.113.7"},
        }
    )
    assert login_event is not None
    assert login_event.event_type == EventType.IDENTITY_LOGIN
    assert login_event.idempotency_key == "okta:okta-evt-1"

    group_event = connector.transform_system_log_event(
        {
            "eventType": "group.user_membership.add",
            "uuid": "okta-evt-2",
            "published": "2026-03-05T02:05:06Z",
            "actor": {"id": "00u1", "alternateId": "alice@example.com"},
            "target": [{"id": "00g1", "displayName": "ml-platform"}],
        }
    )
    assert group_event is not None
    assert group_event.event_type == EventType.IDENTITY_GROUP_CHANGE
    assert group_event.attributes["operation"] == "add"


def test_okta_scim_user_sync_event() -> None:
    connector = OktaIdentityConnector(tenant_id="tenant-a")
    event = connector.transform_scim_user(
        {
            "id": "00u2",
            "userName": "bob@example.com",
            "active": True,
            "groups": [{"value": "00g1", "display": "ml-platform"}],
        }
    )
    assert event is not None
    assert event.event_type == EventType.IDENTITY_GROUP_CHANGE
    assert event.attributes["operation"] == "sync"
    assert event.attributes["groups"][0]["group_id"] == "00g1"


def test_github_push_and_pr_merge_transformations() -> None:
    connector = GitHubSCMConnector(tenant_id="tenant-a")

    push_event = connector.transform_webhook(
        event_name="push",
        delivery_id="gh-delivery-1",
        payload={
            "ref": "refs/heads/main",
            "after": "abcd1234",
            "before": "0000ffff",
            "size": 2,
            "pusher": {"name": "alice"},
            "repository": {"full_name": "acme/aci"},
            "head_commit": {"timestamp": "2026-03-05T03:00:00Z"},
        },
    )
    assert push_event is not None
    assert push_event.event_type == EventType.CODE_COMMIT
    assert push_event.idempotency_key == "github-push:gh-delivery-1"

    pr_event = connector.transform_webhook(
        event_name="pull_request",
        delivery_id="gh-delivery-2",
        payload={
            "action": "closed",
            "repository": {"full_name": "acme/aci"},
            "pull_request": {
                "id": 44,
                "number": 44,
                "merged": True,
                "merge_commit_sha": "ff00aa11",
                "merged_at": "2026-03-05T03:10:00Z",
                "user": {"login": "alice"},
                "base": {"ref": "main"},
            },
        },
    )
    assert pr_event is not None
    assert pr_event.event_type == EventType.PR_MERGED
    assert pr_event.attributes["pr_number"] == 44


def test_github_actions_workflow_job_to_deployment_event() -> None:
    connector = GitHubActionsConnector(tenant_id="tenant-a")
    event = connector.transform_workflow_job(
        delivery_id="gha-delivery-1",
        payload={
            "action": "completed",
            "sender": {"login": "release-bot"},
            "repository": {"full_name": "acme/aci"},
            "workflow_job": {
                "run_id": 98765,
                "name": "deploy-prod",
                "labels": ["ubuntu-latest", "prod"],
                "conclusion": "success",
                "completed_at": "2026-03-05T03:20:00Z",
            },
        },
    )
    assert event is not None
    assert event.event_type == EventType.DEPLOYMENT
    assert event.attributes["target_environment"] == "production"
    assert event.event_time == datetime(2026, 3, 5, 3, 20, tzinfo=UTC)
