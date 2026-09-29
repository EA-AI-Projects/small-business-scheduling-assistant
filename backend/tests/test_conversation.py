"""Model proposals cannot bypass the trusted scheduling services."""

from datetime import UTC, datetime, timedelta

from scheduling.adapters.memory import InMemoryCalendarRepository
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
                                LifecycleService(store, lambda: NOW), consent, lambda: NOW),
            store, interpreter, consent)


def pending(store: InMemoryCalendarRepository) -> str:
    return HoldService(store).create(CreateHold(
        "pilot", "client-1", "client-1", "seed-hold", START, 120), NOW).hold_id


def test_unauthorized_or_keyword_message_never_reaches_model_or_schedule() -> None:
    service, store, model, _ = setup(MessageProposal("request_booking", None,
                                                      "2026-10-01 09:00", None, False))
    result = service.handle(receipt("Book 2026-10-01 09:00", authorized=False))
    assert not result.committed
    assert not model.calls
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
                                  LifecycleService(store, lambda: late), consent, lambda: late)
    result = service.handle(receipt("Book 2026-10-01 09:00"))
    assert not result.committed
    assert store.read_calendar("pilot").events == ()


def test_ambiguous_owner_reply_cannot_approve_even_if_model_selects_target() -> None:
    service, store, model, _ = setup(MessageProposal("owner_decision", None,
                                                      None, "approve", False))
    hold_id = pending(store)
    model.proposal = MessageProposal("owner_decision", hold_id[:8], None, "approve", False)
    result = service.handle(receipt("Yes", role=SenderRole.OWNER))
    assert not result.committed
    assert store.read_appointment(hold_id).status == CalendarStatus.PENDING_APPROVAL  # type: ignore[union-attr]


def test_exact_owner_approval_uses_lifecycle_and_is_not_repeatable() -> None:
    service, store, model, _ = setup(MessageProposal("owner_decision", None,
                                                      None, "approve", False))
    hold_id = pending(store)
    model.proposal = MessageProposal("owner_decision", hold_id[:8], None, "approve", False)
    message = receipt(f"Approve {hold_id[:8]}", role=SenderRole.OWNER)
    result = service.handle(message)
    assert result.committed
    assert result.appointment_id == hold_id
    assert not model.calls  # Exact authorized commands do not depend on model behavior.
    assert store.read_appointment(hold_id).status == CalendarStatus.CONFIRMED  # type: ignore[union-attr]
    assert not service.handle(message).committed


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


def test_exact_client_cancellation_uses_lifecycle() -> None:
    service, store, model, _ = setup(MessageProposal("cancel", None, None, None, False))
    hold_id = pending(store)
    LifecycleService(store, lambda: NOW).apply(AppointmentCommand(
        "pilot", hold_id, "owner", ActorRole.OWNER, Action.APPROVE, "approve-seed", 1))
    model.proposal = MessageProposal("cancel", hold_id[:8], None, None, False)
    assert not service.handle(receipt(f"Don't cancel {hold_id[:8]}")).committed
    result = service.handle(receipt(f"Cancel {hold_id[:8]}", provider_id="SM-cancel"))
    assert result.committed
    assert store.read_appointment(hold_id).status == CalendarStatus.CANCELLED  # type: ignore[union-attr]


def test_full_appointment_id_is_accepted_for_cancellation() -> None:
    service, store, model, _ = setup(MessageProposal("clarify", None, None, None, True))
    hold_id = pending(store)
    LifecycleService(store, lambda: NOW).apply(AppointmentCommand(
        "pilot", hold_id, "owner", ActorRole.OWNER, Action.APPROVE, "approve-seed", 1))
    result = service.handle(receipt(f"Cancel {hold_id}", provider_id="SM-cancel-full"))
    assert result.committed
    assert not model.calls


def test_exact_client_booking_creates_pending_hold_and_day_query_only_suggests() -> None:
    service, store, model, _ = setup(MessageProposal("request_booking", None,
                                                      "2026-10-01", None, False))
    suggestions = service.handle(receipt("Book 2026-10-01"))
    assert not suggestions.committed
    assert "09:00" in suggestions.text
    assert store.read_calendar("pilot").events == ()
    model.proposal = MessageProposal("request_booking", None, "2026-10-01 09:00", None, False)
    result = service.handle(receipt("Book 2026-10-01 09:00", provider_id="SM-2"))
    assert result.committed
    assert "pending owner approval" in result.text
    assert result.appointment_id is not None
    assert store.read_appointment(result.appointment_id).status == CalendarStatus.PENDING_APPROVAL  # type: ignore[union-attr]


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


def test_reschedule_keeps_original_confirmed_until_owner_approval() -> None:
    service, store, model, _ = setup(MessageProposal("reschedule", None,
                                                      "2026-10-02 09:00", None, False))
    original_id = pending(store)
    LifecycleService(store, lambda: NOW).apply(AppointmentCommand(
        "pilot", original_id, "owner", ActorRole.OWNER, Action.APPROVE,
        "approve-seed", 1))
    model.proposal = MessageProposal("reschedule", original_id[:8],
                                     "2026-10-02 09:00", None, False)
    result = service.handle(receipt(
        f"Reschedule {original_id[:8]} to 2026-10-02 09:00", provider_id="SM-replace"))
    assert result.committed
    assert result.appointment_id is not None
    assert store.read_appointment(original_id).status == CalendarStatus.CONFIRMED  # type: ignore[union-attr]
    assert store.read_appointment(result.appointment_id).status == CalendarStatus.PENDING_APPROVAL  # type: ignore[union-attr]
    assert "remains confirmed" in result.text


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
    assert "09:00" in result.text
