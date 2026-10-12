"""Client and owner texts name the saved business and service, never "cleaning" (#348).

Every case uses a synthetic non-cleaning business so a leftover hard-coded word fails.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_booking_invitation_selection import BUSINESS, RUN, client, enable, setup
from test_conversation_flow import TOMORROW, Harness, ask

from scheduling.adapters.openai_messages import INSTRUCTIONS, model_input
from scheduling.adapters.sms_twilio import TwilioSmsSender, render_notification
from scheduling.domain.appointments import Appointment
from scheduling.domain.booking_invitations import InvitationPromoter
from scheduling.domain.booking_outreach import (
    OutreachSettings,
    approved_invitation_message,
    invitation_message,
)
from scheduling.domain.business_profile import BusinessProfile, BusinessProfileRecord, visit_phrase
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile
from scheduling.domain.conversation import MessageContext
from scheduling.domain.outbox import PermanentDeliveryFailure
from scheduling.domain.sms_ingress import SenderRole

NAME = "Example Pet Care"
SERVICE = "Dog grooming"
ZONE = ZoneInfo("America/Los_Angeles")
START = datetime(2026, 10, 1, 16, tzinfo=UTC)
PROFILE = BusinessProfile(NAME, "A synthetic pet business.", "help@example.invalid", SERVICE,
                          "A synthetic grooming visit.", 60)


def grooming() -> Harness:
    chat = Harness()
    chat.store.set_business_profile_for_test("pilot", 120, NAME, SERVICE)
    return chat


def unsaved() -> Harness:
    chat = Harness()
    chat.store._profiles["pilot"] = BusinessProfileRecord(None, 0)
    return chat


def test_offers_name_the_saved_service() -> None:
    chat = grooming()
    chat.model.replies["Can you do tomorrow at 1pm?"] = ask(
        "request_booking", TOMORROW, TOMORROW, "13:00", "13:00")
    single = chat.text("Can you do tomorrow at 1pm?")
    assert "is open for your 2-hour Dog grooming visit. Reply YES" in single.text
    chat.model.replies["Anything tomorrow?"] = ask("availability", "2026-09-30", "2026-10-02")
    several = chat.text("Anything tomorrow?")
    assert "Open times for your 2-hour Dog grooming visit:" in several.text
    assert "cleaning" not in (single.text + several.text).lower()


def test_calendar_replies_name_the_saved_service() -> None:
    chat = grooming()
    chat.model.replies["Booking Friday?"] = ask("clarify_booking", "2026-10-02")
    choice = chat.text("Booking Friday?")
    assert "request a new Dog grooming visit?" in choice.text
    chat.model.replies["Do I have anything?"] = ask("calendar_question")
    empty = chat.text("Do I have anything?")
    assert "To request a new Dog grooming visit, tell me what day works." in empty.text
    assert "cleaning" not in (choice.text + empty.text).lower()


def test_fallback_prompts_use_the_business_name_not_cleaning() -> None:
    chat = grooming()
    assert chat.text("hmm").text == (
        "What day would you like to book your Dog grooming visit? You can say something like "
        "'tomorrow morning' or 'Friday afternoon'.")
    chat.model.replies["Who are you?"] = ask("unsupported")
    assert f"a visit with {NAME}." in chat.text("Who are you?").text


def test_booking_is_blocked_and_wording_is_neutral_until_the_profile_is_saved() -> None:
    chat = unsaved()
    chat.model.replies["Anything tomorrow?"] = ask("availability", TOMORROW)
    blocked = chat.text("Anything tomorrow?")
    assert blocked.text == "Please contact the owner to schedule this visit."
    assert chat.calendar() == ()
    assert chat.text("hmm").text == "Please contact the owner to schedule this visit."
    chat.model.replies["Booking Friday?"] = ask("clarify_booking", "2026-10-02")
    assert "request a new visit?" in chat.text("Booking Friday?").text
    assert all("cleaning" not in context_text(context).lower()
               for context in chat.model.contexts)


def context_text(context: MessageContext) -> str:
    return model_input("hello", context)


def test_model_context_carries_only_the_saved_names() -> None:
    chat = grooming()
    chat.text("hmm")
    context = chat.model.contexts[-1]
    assert (context.business_name, context.service_name) == (NAME, SERVICE)
    text = model_input("hello", context)
    assert f"Business: {NAME}\nService: {SERVICE}\n" in text
    bare = model_input("hello", MessageContext(SenderRole.CLIENT, START.date(), "UTC", ()))
    assert "Business: not saved\nService: not saved\n" in bare
    assert "clean" not in INSTRUCTIONS.lower()


def appointment() -> Appointment:
    return Appointment("visit-12345678", "pilot", "client-1", START, START.replace(hour=17),
                       CalendarStatus.PENDING_APPROVAL, START.replace(hour=18), 60, 0, 1)


def person() -> ClientProfile:
    return ClientProfile("pilot", "client-1", "Avery Example", "+14155550101", "1 Test Street",
                         True, 1, START, START, START)


@pytest.mark.parametrize(("template", "recipient", "expected"), [
    ("hold-request", "owner", "New Dog grooming visit request from Avery Example: "),
    ("hold-pending", "client", "Your Dog grooming visit request for Thu Oct 1 at 9:00 AM"),
    ("approve", "client", ("Dog grooming visit confirmed: Thu Oct 1 at 9:00 AM. Ref visit-12. "
                           f"Reply to the {NAME} number for help.")),
    ("decline", "client", "Dog grooming visit declined: Thu Oct 1 at 9:00 AM. Ref visit-12. "),
])
def test_notifications_name_the_saved_service(template: str, recipient: str,
                                              expected: str) -> None:
    body = render_notification(template, recipient, appointment(), person(), ZONE,
                               business=PROFILE)
    assert body.startswith(expected)
    assert "cleaning" not in body.lower()


def test_notifications_without_a_saved_profile_stay_neutral() -> None:
    body = render_notification("approve", "client", appointment(), person(), ZONE)
    assert body.startswith("Visit confirmed: ")
    assert body.endswith("Reply to the business number for help.")
    assert "cleaning" not in render_notification(
        "hold-pending", "client", appointment(), person(), ZONE).lower()


def test_invitation_text_names_the_saved_business_and_service() -> None:
    assert approved_invitation_message(2, PROFILE) == (
        f"Smart Scheduling Assistant: Would you like to book a visit for {SERVICE} with {NAME} in the next two weeks? "
        "Reply with a day and time that works for you, or STOP to opt out.")
    assert invitation_message(OutreachSettings(message="Custom text."), None) == "Custom text."
    legacy = ("Smart Scheduling Assistant: Would you like to book a cleaning visit in the next "
              "two weeks? Reply with a day and time that works for you, or STOP to opt out.")
    assert invitation_message(
        OutreachSettings(lookahead_weeks=2, message=legacy), PROFILE) == approved_invitation_message(
            2, PROFILE)
    with pytest.raises(ValueError, match="Save the business profile"):
        approved_invitation_message(1, None)


class Messages:
    def __init__(self) -> None:
        self.bodies: list[str] = []

    def create(self, **kwargs: str) -> SimpleNamespace:
        self.bodies.append(kwargs["body"])
        return SimpleNamespace(sid="SM-synthetic-invitation")


def queued_invitation(saved: bool) -> tuple[TwilioSmsSender, Messages, object]:
    records, consent, selector = setup()
    client(records, consent, 501)
    enable(records)
    if saved:
        records.set_business_profile_for_test(BUSINESS, 120, NAME, SERVICE)
    else:
        records._profiles[BUSINESS] = BusinessProfileRecord(None, 0)
    assert selector.run(BUSINESS, RUN).queued == 1
    assert InvitationPromoter(records, consent).run(BUSINESS, RUN) == {"promoted": 1}
    messages = Messages()
    sender = TwilioSmsSender(messages, records, consent, BUSINESS, "+14155550000",
                             "+14155559999", clock=lambda: RUN,
                             invitation_send_enabled=lambda: True)
    return sender, messages, next(iter(records._invitation_outbox.values()))


def test_sender_uses_the_saved_names_in_the_default_invitation() -> None:
    sender, messages, outbox = queued_invitation(True)
    assert sender.deliver(outbox) == "SM-synthetic-invitation"  # type: ignore[arg-type]
    assert messages.bodies == [(
        f"Smart Scheduling Assistant: Would you like to book a visit for {SERVICE} with {NAME} in the next week? "
        "Reply with a day and time that works for you, or STOP to opt out.")]


def test_default_invitation_is_not_sent_until_the_profile_is_saved() -> None:
    sender, messages, outbox = queued_invitation(False)
    with pytest.raises(PermanentDeliveryFailure, match="INVITATION_PROFILE_MISSING"):
        sender.deliver(outbox)  # type: ignore[arg-type]
    assert messages.bodies == []


def test_a_failed_profile_read_leaves_the_invitation_retryable() -> None:
    sender, messages, outbox = queued_invitation(True)
    records = sender._records  # type: ignore[attr-defined]
    real = records.read_business_profile

    def broken(business_id: str) -> BusinessProfileRecord:
        raise OSError("synthetic transient read failure")

    records.read_business_profile = broken
    with pytest.raises(OSError):
        sender.deliver(outbox)  # type: ignore[arg-type]
    stored = records.read_invitation(outbox.business_id, outbox.entity_id)
    assert stored is not None and stored.state == "OUTBOX"
    records.read_business_profile = real
    assert sender.deliver(outbox) == "SM-synthetic-invitation"  # type: ignore[arg-type]
    assert len(messages.bodies) == 1


def test_visit_phrase_does_not_repeat_visit() -> None:
    from dataclasses import replace
    assert visit_phrase(PROFILE) == f"{SERVICE} visit"
    assert visit_phrase(replace(PROFILE, service_name="Home visit")) == "Home visit"
    assert visit_phrase(None) == "visit"
