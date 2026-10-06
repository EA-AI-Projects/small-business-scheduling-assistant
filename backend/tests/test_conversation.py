"""Model proposals cannot bypass the trusted scheduling services."""

import json
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest

from evals.scheduling_messages import CASES
from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.adapters.openai_messages import OpenAIMessageInterpreter
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile, HomeSize
from scheduling.domain.conversation import (
    ConversationService,
    MessageContext,
    MessageProposal,
)
from scheduling.domain.holds import CreateHold, HoldService
from scheduling.domain.lifecycle import (
    Action,
    ActorRole,
    AppointmentCommand,
    LifecycleService,
)
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole

NOW = datetime(2026, 9, 29, 17, tzinfo=UTC)
START = datetime(2026, 10, 1, 16, tzinfo=UTC)


class Interpreter:
    def __init__(self, proposal: MessageProposal) -> None:
        self.proposal = proposal
        self.calls: list[MessageContext] = []

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        self.calls.append(context)
        return self.proposal


class Consent:
    def __init__(self) -> None:
        self.opted_out = False

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return self.opted_out

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        return ConsentEvidence(business_id, "client-1", "Synthetic Client",
                               phone_e164, NOW, "pilot-v1")


def receipt(body: str, *, role: SenderRole = SenderRole.CLIENT,
            authorized: bool = True, provider_id: str = "SM-1",
            client_id: str | None = "client-1") -> InboundReceipt:
    return InboundReceipt("pilot", provider_id,
                          "+14155559999" if role == SenderRole.OWNER else "+14155550101",
                          "+14155550000", body, NOW, role,
                          None if role == SenderRole.OWNER else client_id,
                          Keyword.OTHER, authorized)


def setup(proposal: MessageProposal) -> tuple[
    ConversationService, InMemoryCalendarRepository, Interpreter, Consent,
]:
    store = InMemoryCalendarRepository()
    store.save_profile(ClientProfile(
        "pilot", "client-1", "Synthetic Client", "+14155550101", "123 Test Street",
        HomeSize.MEDIUM, 120, True, 1, NOW, NOW, NOW), 0, None)
    interpreter = Interpreter(proposal)
    consent = Consent()
    return (ConversationService(store, interpreter, HoldService(store),
                                LifecycleService(store, lambda: NOW), consent, lambda: NOW,
                                "+14155559999"),
            store, interpreter, consent)


def pending(store: InMemoryCalendarRepository) -> str:
    return HoldService(store).create(CreateHold(
        "pilot", "client-1", "client-1", "seed-hold", START, 120), NOW).hold_id


def second_pending(store: InMemoryCalendarRepository) -> str:
    return HoldService(store).create(CreateHold(
        "pilot", "client-1", "client-1", "seed-second", START + timedelta(hours=3), 120),
        NOW).hold_id


class NoConsent(Consent):
    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        return None


class OtherClientConsent(Consent):
    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        return ConsentEvidence(business_id, "client-2", "Other Client", phone_e164, NOW, "pilot-v1")


class TimedOut(Interpreter):
    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        self.calls.append(context)
        raise TimeoutError("synthetic model timeout")

    def classify_owner_reply(self, body: str, context: object) -> object:
        self.calls.append(context)  # type: ignore[arg-type]
        raise TimeoutError("synthetic model timeout")


def calendar_state(store: InMemoryCalendarRepository) -> tuple[tuple[str, str, int], ...]:
    appointments = (store.read_appointment(event.event_id)
                    for event in store.read_calendar("pilot").events)
    return tuple(sorted((appointment.appointment_id, appointment.status.value,
                         appointment.version)
                        for appointment in appointments if appointment is not None))


def test_unauthorized_or_keyword_message_never_reaches_model_or_schedule() -> None:
    service, store, model, _ = setup(MessageProposal("request_booking", None,
                                                      "2026-10-01 09:00", None, False))
    result = service.handle(receipt("Book 2026-10-01 09:00", authorized=False))
    assert not result.committed
    assert not model.calls
    assert store.read_calendar("pilot").events == ()


def test_long_model_question_returns_normal_clarification_without_calendar_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = {
        "intent": "clarify", "request_reference": None, "date_text": None,
        "date_from": None, "date_to": None, "time_from": None, "time_to": None,
        "target_date": None, "owner_decision": None, "needs_clarification": True,
        "question": "Which date would you prefer? " * 8, "statuses": None, "view": None,
    }
    monkeypatch.setattr("scheduling.adapters.openai_messages.urlopen",
                        lambda *_args, **_kwargs: BytesIO(json.dumps({"output": [{
                            "type": "function_call", "name": "propose_message",
                            "arguments": json.dumps(arguments),
                        }]}).encode()))
    _, store, _, consent = setup(MessageProposal("clarify", None, None, None, True))
    service = ConversationService(store, OpenAIMessageInterpreter("synthetic-key"),
                                  HoldService(store), LifecycleService(store, lambda: NOW),
                                  consent, lambda: NOW, "+14155559999")
    result = service.handle(receipt("I would like to book an appointment for this week"))
    assert result.text != "I couldn't understand that message. Please try again later."
    assert "What day" in result.text
    assert not result.committed
    assert store.read_calendar("pilot").events == ()


def test_current_opt_out_blocks_a_previously_authorized_receipt() -> None:
    service, store, model, consent = setup(MessageProposal(
        "request_booking", None, "2026-10-01 09:00", None, False))
    consent.opted_out = True
    result = service.handle(receipt("Book 2026-10-01 09:00"))
    assert not result.committed
    assert not model.calls
    assert store.read_calendar("pilot").events == ()


def test_late_processing_uses_current_clock_not_old_receipt_time() -> None:
    _, store, model, consent = setup(MessageProposal(
        "request_booking", None, "2026-10-01 09:00", None, False))
    late = NOW + timedelta(days=3)
    service = ConversationService(store, model, HoldService(store),
                                  LifecycleService(store, lambda: late), consent, lambda: late,
                                  "+14155559999")
    result = service.handle(receipt("Book 2026-10-01 09:00"))
    assert not result.committed
    assert store.read_calendar("pilot").events == ()


def test_owner_yes_with_two_pending_cannot_approve_even_if_model_selects_target() -> None:
    service, store, model, _ = setup(MessageProposal("owner_decision", None,
                                                      None, "approve", False))
    hold_id = pending(store)
    other_id = second_pending(store)
    model.proposal = MessageProposal("owner_decision", hold_id[:8], None, "approve", False)
    result = service.handle(receipt("Yes", role=SenderRole.OWNER))
    assert not result.committed
    assert "2 requests are pending" in result.text
    assert hold_id[:8] in result.text and other_id[:8] in result.text
    assert not model.calls
    assert store.read_appointment(hold_id).status == CalendarStatus.PENDING_APPROVAL  # type: ignore[union-attr]


def test_old_owner_number_cannot_approve_a_persisted_receipt_after_rotation() -> None:
    _, store, model, consent = setup(MessageProposal(
        "owner_decision", None, None, "approve", False))
    hold_id = pending(store)
    service = ConversationService(store, model, HoldService(store),
                                  LifecycleService(store, lambda: NOW), consent,
                                  lambda: NOW, "+14155558888")
    result = service.handle(receipt(f"Approve {hold_id[:8]}", role=SenderRole.OWNER))
    assert not result.committed
    assert not model.calls
    assert store.read_appointment(hold_id).status == CalendarStatus.PENDING_APPROVAL  # type: ignore[union-attr]


def test_full_request_id_is_accepted_for_owner_approval() -> None:
    service, store, model, _ = setup(MessageProposal("clarify", None, None, None, True))
    hold_id = pending(store)
    result = service.handle(receipt(f"Approve {hold_id}", role=SenderRole.OWNER))
    assert result.committed
    assert not model.calls
    assert store.read_appointment(hold_id).status == CalendarStatus.CONFIRMED  # type: ignore[union-attr]


def test_client_cannot_cancel_another_clients_visit_or_inferred_target() -> None:
    service, store, model, _ = setup(MessageProposal("cancel", None, None, None, False))
    hold_id = pending(store)
    LifecycleService(store, lambda: NOW).apply(AppointmentCommand(
        "pilot", hold_id, "owner", ActorRole.OWNER, Action.APPROVE, "approve-seed", 1))
    model.proposal = MessageProposal("cancel", hold_id[:8], None, None, False)
    assert not service.handle(receipt("Cancel my appointment", client_id="client-2")).committed
    assert not service.handle(receipt(f"Visit {hold_id[:8]}")).committed
    assert store.read_appointment(hold_id).status == CalendarStatus.CONFIRMED  # type: ignore[union-attr]


def test_full_appointment_id_is_accepted_for_cancellation() -> None:
    service, store, model, _ = setup(MessageProposal("clarify", None, None, None, True))
    hold_id = pending(store)
    LifecycleService(store, lambda: NOW).apply(AppointmentCommand(
        "pilot", hold_id, "owner", ActorRole.OWNER, Action.APPROVE, "approve-seed", 1))
    result = service.handle(receipt(f"Cancel {hold_id}", provider_id="SM-cancel-full"))
    assert result.committed
    assert not model.calls


def test_model_invented_date_cannot_create_hold() -> None:
    service, store, _, _ = setup(MessageProposal("request_booking", None,
                                                  "2026-10-01 09:00", None, False))
    result = service.handle(receipt("Book next Thursday morning"))
    assert not result.committed
    assert store.read_calendar("pilot").events == ()


def test_model_cannot_turn_negation_or_qualified_time_into_booking() -> None:
    service, store, model, _ = setup(MessageProposal(
        "request_booking", None, "2026-10-01 09:00", None, False))
    for body in ("Don't book 2026-10-01 09:00",
                 "Book 2026-10-01 09:00 UTC",
                 "Book 2026-10-01 09:00+00:00",
                 "Book 2026-10-01 09:00 or 2026-10-02",
                 "Book 2026-10-01 09:00 or 10:00"):
        assert not service.handle(receipt(body)).committed
    assert len(model.calls) == 5
    assert store.read_calendar("pilot").events == ()


def test_oversized_message_with_late_correction_is_rejected_before_model() -> None:
    service, store, model, _ = setup(MessageProposal(
        "request_booking", None, "2026-10-01 09:00", None, False))
    body = "Book 2026-10-01 09:00 " + "x" * 1000 + " actually don't book"
    result = service.handle(receipt(body))
    assert not result.committed
    assert not model.calls
    assert store.read_calendar("pilot").events == ()


def test_exact_booking_works_when_model_would_clarify() -> None:
    service, _, model, _ = setup(MessageProposal("clarify", None, None, None, True))
    result = service.handle(receipt("Book 2026-10-01 09:00"))
    assert result.committed
    assert not model.calls


def test_multiple_dates_and_ambiguous_local_time_cannot_create_hold() -> None:
    service, store, model, _ = setup(MessageProposal("request_booking", None,
                                                      "2026-10-01 09:00", None, False))
    result = service.handle(receipt("Book 2026-10-01 09:00 or 2026-10-02 09:00"))
    assert not result.committed
    model.proposal = MessageProposal("request_booking", None, "2026-11-01 01:30", None, False)
    assert not service.handle(receipt("Book 2026-11-01 01:30")).committed
    assert store.read_calendar("pilot").events == ()


def test_model_cannot_turn_negated_reschedule_into_replacement() -> None:
    service, store, model, _ = setup(MessageProposal("clarify", None, None, None, True))
    original_id = pending(store)
    LifecycleService(store, lambda: NOW).apply(AppointmentCommand(
        "pilot", original_id, "owner", ActorRole.OWNER, Action.APPROVE,
        "approve-seed", 1))
    model.proposal = MessageProposal("reschedule", original_id[:8],
                                     "2026-10-02 09:00", None, False)
    assert not service.handle(receipt(
        f"Don't reschedule {original_id[:8]} to 2026-10-02 09:00")).committed
    assert store.read_appointment(original_id).status == CalendarStatus.CONFIRMED  # type: ignore[union-attr]
    assert len(store.read_calendar("pilot").events) == 1


def test_exact_reschedule_works_when_model_would_clarify() -> None:
    service, store, model, _ = setup(MessageProposal("clarify", None, None, None, True))
    original_id = pending(store)
    LifecycleService(store, lambda: NOW).apply(AppointmentCommand(
        "pilot", original_id, "owner", ActorRole.OWNER, Action.APPROVE,
        "approve-seed", 1))
    result = service.handle(receipt(
        f"Reschedule {original_id[:8]} to 2026-10-02 09:00"))
    assert result.committed
    assert not model.calls
    assert store.read_appointment(original_id).status == CalendarStatus.CONFIRMED  # type: ignore[union-attr]


def test_reschedule_suggestions_exclude_the_original_visit() -> None:
    service, store, model, _ = setup(MessageProposal("reschedule", None,
                                                      "2026-10-01", None, False))
    original_id = pending(store)
    LifecycleService(store, lambda: NOW).apply(AppointmentCommand(
        "pilot", original_id, "owner", ActorRole.OWNER, Action.APPROVE,
        "approve-seed", 1))
    model.proposal = MessageProposal("reschedule", original_id[:8],
                                     "2026-10-01", None, False)
    result = service.handle(receipt(f"Reschedule {original_id[:8]} to 2026-10-01"))
    assert not result.committed
    assert "To move your Thu Oct 1 at 9:00 AM visit" in result.text
    assert "1) 8:00 AM" in result.text
    assert "stays booked" in result.text


def test_ambiguous_reschedule_asks_for_same_replacement_reference() -> None:
    service, store, model, _ = setup(MessageProposal("clarify", None, None, None, True))
    original_id = pending(store)
    LifecycleService(store, lambda: NOW).apply(AppointmentCommand(
        "pilot", original_id, "owner", ActorRole.OWNER, Action.APPROVE,
        "approve-seed", 1))
    outcome = service.handle(receipt(f"Reschedule {original_id[:8]} to next Thursday"))
    assert not outcome.committed
    assert f"RESCHEDULE {original_id[:8]} to YYYY-MM-DD" in outcome.text
    assert "BOOK " not in outcome.text
    assert model.calls
    assert store.read_appointment(original_id).status == CalendarStatus.CONFIRMED  # type: ignore[union-attr]
    prefixed = service.handle(receipt(
        f"Can you reschedule {original_id[:8]} to next Thursday?", provider_id="SM-prefixed"))
    assert not prefixed.committed
    assert f"RESCHEDULE {original_id[:8]} to YYYY-MM-DD" in prefixed.text
    assert "BOOK " not in prefixed.text


def test_reschedule_clarification_does_not_offer_pending_request_as_target() -> None:
    service, store, _, _ = setup(MessageProposal("clarify", None, None, None, True))
    pending_id = pending(store)
    outcome = service.handle(receipt(f"Could you reschedule {pending_id[:8]} to next week?"))
    assert not outcome.committed
    assert "needs a confirmed visit" in outcome.text
    assert f"RESCHEDULE {pending_id[:8]}" not in outcome.text


def test_client_without_owner_verified_profile_is_refused_without_model_or_write() -> None:
    booking = MessageProposal("request_booking", None, "2026-10-01 09:00", None, False)
    service, store, model, _ = setup(booking)
    assert not service.handle(receipt("Book 2026-10-01 09:00",
                                      client_id="client-unknown")).committed
    unverified = InMemoryCalendarRepository()
    unverified.save_profile(ClientProfile(
        "pilot", "client-1", "Synthetic Client", "+14155550101", "123 Test Street",
        HomeSize.MEDIUM, 120, True, 1, NOW, NOW), 0, None)
    inactive = InMemoryCalendarRepository()
    inactive.save_profile(ClientProfile(
        "pilot", "client-1", "Synthetic Client", "+14155550101", "123 Test Street",
        HomeSize.MEDIUM, 120, False, 1, NOW, NOW, NOW), 0, None)
    other_phone = InMemoryCalendarRepository()
    other_phone.save_profile(ClientProfile(
        "pilot", "client-1", "Synthetic Client", "+14155550102", "123 Test Street",
        HomeSize.MEDIUM, 120, True, 1, NOW, NOW, NOW), 0, None)
    variants = [(unverified, Consent()), (inactive, Consent()), (store, NoConsent()),
                (store, OtherClientConsent()), (other_phone, Consent())]
    for repository, consent in variants:
        variant = ConversationService(repository, model, HoldService(repository),
                                      LifecycleService(repository, lambda: NOW), consent,
                                      lambda: NOW, "+14155559999")
        result = variant.handle(receipt("Book 2026-10-01 09:00"))
        assert not result.committed
        assert "verified client profile" in result.text
        assert repository.read_calendar("pilot").events == ()
    assert not model.calls
    assert store.read_calendar("pilot").events == ()


def test_booking_without_complete_date_and_time_asks_for_it_and_writes_nothing() -> None:
    service, store, model, _ = setup(MessageProposal("clarify", None, None, None, True))
    result = service.handle(receipt("Can I get a cleaning next week?"))
    assert not result.committed
    assert "What day" in result.text
    model.proposal = MessageProposal("request_booking", None, None, None, False)
    result = service.handle(receipt("I'd like to book a visit"))
    assert not result.committed
    assert "What day" in result.text
    assert store.read_calendar("pilot").events == ()


def test_model_timeout_gives_safe_retry_and_writes_nothing() -> None:
    _, store, _, consent = setup(MessageProposal("clarify", None, None, None, True))
    model = TimedOut(MessageProposal("clarify", None, None, None, True))
    service = ConversationService(store, model, HoldService(store),
                                  LifecycleService(store, lambda: NOW), consent,
                                  lambda: NOW, "+14155559999")
    hold_id = pending(store)
    before = calendar_state(store)
    for message, expected in (
            (receipt("Can you come Friday?"), "try again later"),
            (receipt("Looks fine to me", role=SenderRole.OWNER, provider_id="SM-owner"),
             "couldn't tell what you meant")):
        result = service.handle(message)
        assert not result.committed
        assert expected in result.text
    assert len(model.calls) == 2
    assert calendar_state(store) == before
    assert store.read_appointment(hold_id).status == CalendarStatus.PENDING_APPROVAL  # type: ignore[union-attr]


def test_issue_24_synthetic_cases_cannot_change_calendar_even_with_unsafe_model_output() -> None:
    service, store, model, _ = setup(MessageProposal("clarify", None, None, None, True))
    pending_id = pending(store)
    confirmed_id = HoldService(store).create(CreateHold(
        "pilot", "client-1", "client-1", "seed-confirmed", START + timedelta(days=1), 120),
        NOW).hold_id
    LifecycleService(store, lambda: NOW).apply(AppointmentCommand(
        "pilot", confirmed_id, "owner", ActorRole.OWNER, Action.APPROVE, "approve-seed", 1))
    second_pending(store)  # The owner cases describe two pending requests.
    before = calendar_state(store)
    unsafe = (
        MessageProposal("request_booking", None, "2026-10-05 09:00", None, False),
        MessageProposal("owner_decision", pending_id[:8], None, "approve", False),
        MessageProposal("owner_decision", pending_id[:8], None, "decline", False),
        MessageProposal("cancel", confirmed_id[:8], None, None, False),
        MessageProposal("reschedule", confirmed_id[:8], "2026-10-05 09:00", None, False),
        MessageProposal("clarify", None, None, None, True),
    )
    for number, case in enumerate(CASES):
        role = SenderRole.OWNER if case.actor == "owner" else SenderRole.CLIENT
        for variant, proposal in enumerate(unsafe):
            model.proposal = proposal
            result = service.handle(receipt(case.message, role=role,
                                            provider_id=f"SM-eval-{number}-{variant}"))
            assert not result.committed, (case.name, proposal)
    assert calendar_state(store) == before


def test_issue_24_cases_with_real_references_do_not_let_the_model_pick_one() -> None:
    service, store, model, _ = setup(MessageProposal("clarify", None, None, None, True))
    first = pending(store)
    second = HoldService(store).create(CreateHold(
        "pilot", "client-1", "client-1", "seed-second", START + timedelta(days=1), 120),
        NOW).hold_id
    before = calendar_state(store)
    refs = {"a101a101": first[:8], "b202b202": second[:8]}
    for case in CASES:
        if case.actor != "owner" or case.name == "explicit-owner-reference":
            continue
        body = case.message
        for placeholder, reference in refs.items():
            body = body.replace(placeholder, reference)
        for decision in ("approve", "decline"):
            model.proposal = MessageProposal("owner_decision", first[:8], None, decision, False)
            result = service.handle(receipt(body, role=SenderRole.OWNER,
                                            provider_id=f"SM-real-{case.name}-{decision}"))
            assert not result.committed, (case.name, decision)
    assert calendar_state(store) == before
    explicit = next(case for case in CASES if case.name == "explicit-owner-reference")
    result = service.handle(receipt(explicit.message.replace("a101a101", first[:8]),
                                    role=SenderRole.OWNER, provider_id="SM-real-explicit"))
    assert result.committed
    assert store.read_appointment(first).status == CalendarStatus.CONFIRMED  # type: ignore[union-attr]
