"""Provider delivery statuses, separate from appointment truth."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from scheduling.domain.sms_ingress import normalize_phone

STATUS_RANK = {
    "accepted": 0, "scheduled": 0, "queued": 1, "sending": 1,
    "sent": 2, "delivered": 3, "undelivered": 3, "failed": 3,
}


@dataclass(frozen=True)
class SmsDeliveryStatus:
    business_id: str
    outbox_id: str
    provider_id: str
    status: str
    recipient: str
    observed_at: datetime
    error_code: str | None = None

    def __post_init__(self) -> None:
        if (not self.business_id or not self.outbox_id or not self.provider_id
                or self.status not in STATUS_RANK):
            raise ValueError("Invalid delivery status identity")
        normalize_phone(self.recipient)
        if self.observed_at.tzinfo is None:
            raise ValueError("Delivery status needs an aware timestamp")
        if self.error_code is not None and (
            not self.error_code.isdigit() or len(self.error_code) > 10
        ):
            raise ValueError("Invalid provider error code")


class SmsStatusStore(Protocol):
    def put_status(self, status: SmsDeliveryStatus) -> None: ...
