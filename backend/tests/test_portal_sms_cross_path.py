"""Portal and text paths share one calendar: each sees, blocks, and can end the other's visits."""

from datetime import datetime

from fastapi.testclient import TestClient
from test_client_changes import cancel, confirmed, free_starts, mine
from test_client_requests import BASE, NOW, OWNER, START, Repository, request, setup
from test_conversation_flow import ZONE, Script, ask

from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.conversation import ConversationOutcome, ConversationService
from scheduling.domain.holds import HoldService
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole

PHONES = {"a": "+15550100001", "b": "+15550100002"}
LOCAL = START.astimezone(ZONE)  # Tue Sep 29, 9:00 AM local.
DAY = LOCAL.date().isoformat()
BOOK = "Can you do Tuesday at 9?"
CANCEL = "I need to cancel Tuesday"


class Consent:
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return False

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        who = "a" if phone_e164 == PHONES["a"] else "b"
        return ConsentEvidence(business_id, f"client-{who}", "Synthetic Client", phone_e164,
                               NOW, "v1")


class Texts:
    """The real conversation service on the portal's repository and clock, with a fake model."""

    def __init__(self, repository: Repository, clock: list[datetime]) -> None:
        self.repository = repository
        self.clock = clock
        self.model = Script()
        self.model.replies[BOOK] = ask("request_booking", DAY, DAY, "09:00", "09:00")
        self.model.replies[CANCEL] = ask("cancel", target_date=DAY)
        self.service = ConversationService(
            repository, self.model, HoldService(repository),
            LifecycleService(repository, lambda: clock[0]), Consent(), lambda: clock[0],
            "+14155559999")
        self.count = 0

    def text(self, who: str, body: str) -> ConversationOutcome:
        self.count += 1
        return self.service.handle(InboundReceipt(
            "business-1", f"SM-{self.count}", PHONES[who], "+14155550000", body, self.clock[0],
            SenderRole.CLIENT, f"client-{who}", Keyword.OTHER, True))

    def book(self, who: str) -> ConversationOutcome:
        offer = self.text(who, BOOK)
        assert not offer.committed
        return self.text(who, "Yes please")


def both() -> tuple[TestClient, Repository, Texts]:
    api, repository, clock = setup()
    return api, repository, Texts(repository, clock)


def holds(repository: Repository) -> list[tuple[str, str]]:
    appointments = (repository.read_appointment(e.event_id)
                    for e in repository.read_calendar("business-1").events)
    return sorted((a.client_id, a.status.value) for a in appointments if a is not None)


def intents(repository: Repository) -> list[tuple[str, str]]:
    return [(i.recipient, i.template) for i in repository.list_outbox_intents()]


def test_text_hold_makes_an_overlapping_portal_request_conflict_without_a_second_hold() -> None:
    api, repository, texts = both()
    booked = texts.book("a")
    assert booked.committed and booked.appointment_id is not None
    revision = repository.read_revision("business-1")
    response = request(api, "b", "k1")
    assert response.status_code == 409  # type: ignore[attr-defined]
    detail = response.json()["detail"]  # type: ignore[attr-defined]
    assert detail["code"] == "SLOT_CONFLICT"
    alternatives = [datetime.fromisoformat(v) for v in detail["alternatives"]]
    assert alternatives and START not in alternatives
    assert holds(repository) == [("client-a", "PENDING_APPROVAL")]
    assert repository.read_revision("business-1") == revision
    # The same client's own text hold blocks the portal too, and the owner sees one request.
    assert request(api, "a", "k2").status_code == 409  # type: ignore[attr-defined]
    pending = api.get(f"{BASE}/requests", headers=OWNER).json()
    assert [p["appointment_id"] for p in pending] == [booked.appointment_id]
    assert START not in free_starts(api)


def test_portal_request_blocks_a_text_booking_for_the_same_time() -> None:
    api, repository, texts = both()
    created = request(api, "a", "k1")
    assert created.status_code == 200  # type: ignore[attr-defined]
    before = repository.read_revision("business-1")
    offer = texts.text("b", BOOK)
    assert not offer.committed
    assert "isn't open" in offer.text  # The text path offers other times instead.
    assert holds(repository) == [("client-a", "PENDING_APPROVAL")]
    # A bare yes has no offer to accept, so it cannot take the held time either.
    assert not texts.text("b", "Yes please").committed
    assert holds(repository) == [("client-a", "PENDING_APPROVAL")]
    assert repository.read_revision("business-1") == before


def test_text_offer_taken_by_the_portal_before_yes_writes_nothing() -> None:
    api, repository, texts = both()
    assert not texts.text("b", BOOK).committed  # Open at offer time.
    assert request(api, "a", "k1").status_code == 200  # type: ignore[attr-defined]
    late = texts.text("b", "Yes please")
    assert not late.committed
    assert holds(repository) == [("client-a", "PENDING_APPROVAL")]


def test_portal_confirmed_booking_is_cancelled_by_text() -> None:
    api, repository, texts = both()
    booking = confirmed(api)
    assert START not in free_starts(api)
    prompt = texts.text("a", CANCEL)
    assert not prompt.committed and "Reply YES" in prompt.text
    assert [b["status"] for b in mine(api)] == ["CONFIRMED"]  # Asking changes nothing.
    done = texts.text("a", "Yes")
    assert done.committed
    appointment = repository.read_appointment(booking["appointment_id"])
    assert appointment is not None and appointment.status == CalendarStatus.CANCELLED
    assert mine(api) == []
    assert START in free_starts(api)
    assert ("owner", "cancel") in intents(repository)
    # The freed time can be requested again from the portal.
    assert request(api, "b", "k2").status_code == 200  # type: ignore[attr-defined]


def test_text_confirmed_booking_is_cancelled_in_the_portal() -> None:
    api, repository, texts = both()
    booked = texts.book("a")
    assert booked.committed and booked.appointment_id is not None
    done = api.post(f"{BASE}/requests/{booked.appointment_id}/approve",
                    json={"expected_version": 1}, headers={**OWNER, "Idempotency-Key": "ap"})
    assert done.status_code == 200, done.text
    booking = mine(api)[0]
    assert booking["status"] == "CONFIRMED" and START not in free_starts(api)
    before = len(intents(repository))
    response = cancel(api, "a", booking)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"
    assert mine(api) == []
    assert START in free_starts(api)
    assert intents(repository)[before:] == [("client", "cancel"), ("owner", "cancel")]
    # The text path now finds nothing left to cancel.
    again = texts.text("a", CANCEL)
    assert not again.committed
    assert request(api, "b", "k3").status_code == 200  # type: ignore[attr-defined]


def test_other_clients_cannot_cancel_across_paths() -> None:
    api, repository, texts = both()
    booking = confirmed(api)
    assert not texts.text("b", CANCEL).committed
    assert not texts.text("b", "Yes").committed
    assert cancel(api, "b", booking).status_code == 404
    assert [b["status"] for b in mine(api)] == ["CONFIRMED"]
    assert holds(repository) == [("client-a", "CONFIRMED")]
