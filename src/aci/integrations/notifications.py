"""Local-friendly notification delivery hub for Slack/email/webhooks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from urllib.error import URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class NotificationMessage:
    """Notification payload independent of channel transport."""

    event_type: str
    title: str
    detail: str
    severity: str = "info"
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NotificationDelivery:
    """One delivery attempt result."""

    delivery_id: str
    channel: str
    target: str
    status: str
    message: str
    sent_at: datetime


class NotificationHub:
    """Dispatch notifications to Slack/email/webhooks with simulate/live modes."""

    def __init__(self, live_network: bool = False) -> None:
        self.live_network = live_network
        self._deliveries: list[NotificationDelivery] = []
        self._lock = Lock()

    def send(
        self,
        *,
        message: NotificationMessage,
        channels: list[str],
        slack_webhook_url: str = "",
        webhook_url: str = "",
        email_to: list[str] | None = None,
    ) -> list[NotificationDelivery]:
        """Dispatch one message across the selected channels."""
        dispatched: list[NotificationDelivery] = []
        email_targets = email_to or []
        normalized_channels = [channel.strip().lower() for channel in channels]
        timestamp = datetime.now(UTC)

        for channel in normalized_channels:
            if channel == "slack":
                target = slack_webhook_url.strip() or "slack://simulated"
                dispatched.append(
                    self._deliver(
                        channel="slack",
                        target=target,
                        body=self._slack_body(message),
                        timestamp=timestamp,
                    )
                )
            elif channel == "webhook":
                target = webhook_url.strip() or "webhook://simulated"
                dispatched.append(
                    self._deliver(
                        channel="webhook",
                        target=target,
                        body=self._webhook_body(message),
                        timestamp=timestamp,
                    )
                )
            elif channel == "email":
                if not email_targets:
                    dispatched.append(
                        self._record(
                            channel="email",
                            target="email://missing-recipient",
                            status="failed",
                            message="email_to must contain at least one recipient",
                            timestamp=timestamp,
                        )
                    )
                    continue
                for recipient in email_targets:
                    dispatched.append(
                        self._record(
                            channel="email",
                            target=recipient,
                            status="simulated",
                            message=(
                                f"[{message.severity.upper()}] "
                                f"{message.title}: {message.detail}"
                            ),
                            timestamp=timestamp,
                        )
                    )

        return dispatched

    def list_deliveries(self, limit: int = 100) -> list[NotificationDelivery]:
        """Return most recent delivery records."""
        with self._lock:
            if limit <= 0:
                return []
            return list(self._deliveries[-limit:])

    def clear(self) -> None:
        """Clear delivery history."""
        with self._lock:
            self._deliveries.clear()

    def _deliver(
        self,
        *,
        channel: str,
        target: str,
        body: dict[str, object],
        timestamp: datetime,
    ) -> NotificationDelivery:
        if not self.live_network or not target.startswith("http"):
            return self._record(
                channel=channel,
                target=target,
                status="simulated",
                message="delivery simulated",
                timestamp=timestamp,
            )

        payload = json.dumps(body).encode("utf-8")
        request = Request(
            target,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=2) as response:  # nosec B310: configurable webhook target
                status = "sent" if 200 <= response.status < 300 else "failed"
                return self._record(
                    channel=channel,
                    target=target,
                    status=status,
                    message=f"http_status={response.status}",
                    timestamp=timestamp,
                )
        except URLError as exc:
            return self._record(
                channel=channel,
                target=target,
                status="failed",
                message=str(exc.reason),
                timestamp=timestamp,
            )

    def _record(
        self,
        *,
        channel: str,
        target: str,
        status: str,
        message: str,
        timestamp: datetime,
    ) -> NotificationDelivery:
        delivery = NotificationDelivery(
            delivery_id=f"delivery-{int(timestamp.timestamp() * 1000)}-{len(self._deliveries) + 1}",
            channel=channel,
            target=target,
            status=status,
            message=message,
            sent_at=timestamp,
        )
        with self._lock:
            self._deliveries.append(delivery)
            if len(self._deliveries) > 1000:
                self._deliveries = self._deliveries[-1000:]
        return delivery

    @staticmethod
    def _slack_body(message: NotificationMessage) -> dict[str, object]:
        return {
            "text": f"[{message.severity.upper()}] {message.title}\n{message.detail}",
            "metadata": message.metadata,
            "event_type": message.event_type,
        }

    @staticmethod
    def _webhook_body(message: NotificationMessage) -> dict[str, object]:
        return {
            "event_type": message.event_type,
            "severity": message.severity,
            "title": message.title,
            "detail": message.detail,
            "metadata": message.metadata,
            "sent_at": datetime.now(UTC).isoformat(),
        }
