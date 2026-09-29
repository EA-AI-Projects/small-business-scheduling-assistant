"""Fictional local conversation loop; no Twilio, DynamoDB, or live scheduling data."""

import os
import re
from datetime import UTC, datetime
from uuid import uuid4

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.adapters.openai_messages import OpenAIMessageInterpreter
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ACCESS_CODE_PATTERN, ClientProfile, HomeSize
from scheduling.domain.conversation import ConversationService
from scheduling.domain.conversation_state import InMemoryConversationStates
from scheduling.domain.holds import HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole

BUSINESS = "local-synthetic"
CLIENT_PHONE = "+14155550101"
OWNER_PHONE = "+14155559999"
BUSINESS_PHONE = "+14155550000"
PHONE_IN_TEXT = re.compile(
    r"(?<!\w)(?:\+?1[\s.()-]?)?(?:\(?[2-9]\d{2}\)?[\s.()-]?)"
    r"[2-9]\d{2}[\s.()-]?\d{4}(?!\w)"
)


class SyntheticConsent:
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return False

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        if business_id != BUSINESS or phone_e164 != CLIENT_PHONE:
            return None
        return ConsentEvidence(BUSINESS, "client-1", "Synthetic Client",
                               CLIENT_PHONE, datetime.now(UTC), "local-only")


def main() -> int:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("Set OPENAI_API_KEY in the process environment before starting.")
        return 2
    store = InMemoryCalendarRepository()
    now = datetime.now(UTC)
    store.save_profile(ClientProfile(
        BUSINESS, "client-1", "Synthetic Client", CLIENT_PHONE,
        "123 Fictional Street", HomeSize.MEDIUM, 120, True, 1,
        now, now, now), 0, None)
    service = ConversationService(
        store, OpenAIMessageInterpreter(key), HoldService(store),
        LifecycleService(store, lambda: datetime.now(UTC)),
        SyntheticConsent(), lambda: datetime.now(UTC), OWNER_PHONE,
        InMemoryConversationStates())
    actor = SenderRole.CLIENT
    print("Local synthetic scheduling conversation. No live texts or cloud writes.")
    print("Use fictional text only. /client, /owner, /calendar, /quit are available.")
    while True:
        try:
            body = input(f"{actor.value}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if body == "/quit":
            return 0
        if body in ("/client", "/owner"):
            actor = SenderRole(body[1:])
            continue
        if body == "/calendar":
            events = store.read_calendar(BUSINESS).events
            for event in events:
                if event.status in (CalendarStatus.CONFIRMED, CalendarStatus.PENDING_APPROVAL):
                    print(f"{event.event_id[:8]} {event.status.value} {event.start_at.isoformat()}")
            if not events:
                print("No visits or requests yet.")
            continue
        if not body:
            continue
        if len(body) > 1000 or ACCESS_CODE_PATTERN.search(body) or PHONE_IN_TEXT.search(body):
            print("Use a short fictional message without phone numbers or access codes.")
            continue
        received = InboundReceipt(
            BUSINESS, f"local-{uuid4()}",
            OWNER_PHONE if actor == SenderRole.OWNER else CLIENT_PHONE,
            BUSINESS_PHONE, body, datetime.now(UTC), actor,
            None if actor == SenderRole.OWNER else "client-1", Keyword.OTHER, True)
        result = service.handle(received)
        print(result.text)


if __name__ == "__main__":
    raise SystemExit(main())
