"""Trusted SMS ingress boundary; no scheduling command is inferred here."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from scheduling.domain.client_records import ACCESS_CODE_PATTERN, ClientProfile
from scheduling.domain.outbox import DeliveryState, OutboxRecord

if TYPE_CHECKING:
    from scheduling.domain.sms_status import SmsDeliveryStatus

PHONE = re.compile(r"^\+[1-9][0-9]{1,14}$")
STOP_WORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "END", "QUIT", "REVOKE", "OPTOUT"})
HELP_WORDS = frozenset({"HELP", "INFO"})
START_WORDS = frozenset({"START", "UNSTOP"})


class SmsCommandInterrupted(Exception):
    """An SMS scheduling transaction was cancelled; the old text must not replay."""


class SmsReceiptErased(Exception):
    """A provider retry belongs to a receipt removed by client deletion."""


def normalize_phone(value: str) -> str:
    """Accept an international number, without guessing a country from local digits."""
    phone = "+" + re.sub(r"[ ().-]", "", value[1:]) if value.startswith("+") else value
    if not PHONE.fullmatch(phone):
        raise ValueError("SMS sender or recipient must be an E.164 number")
    return phone


class SenderRole(StrEnum):
    OWNER = "owner"
    CLIENT = "client"
    UNKNOWN = "unknown"


class Keyword(StrEnum):
    STOP = "STOP"
    HELP = "HELP"
    START = "START"
    OTHER = "OTHER"


@dataclass(frozen=True)
class InboundReceipt:
    business_id: str
    provider_id: str
    sender: str
    recipient: str
    body: str | None
    received_at: datetime
    role: SenderRole
    client_id: str | None
    keyword: Keyword
    authorized_for_commands: bool


@dataclass(frozen=True)
class ConsentEvidence:
    business_id: str
    client_id: str
    participant_name: str
    phone_e164: str
    agreed_at: datetime
    script_version: str
    method: str = "in_person"


WELCOME_TEMPLATE = "welcome"
# Must match docs/sms-consent/index.html and doc/A2P_REGISTRATION.md.
WELCOME_TEXT = (
    "Smart Scheduling Assistant: You're enrolled for appointment scheduling texts. "
    "Message frequency varies. Message and data rates may apply. "
    "Reply HELP for help or STOP to opt out."
)


def welcome_outbox_id(client_id: str) -> str:
    return f"welcome#{client_id}"


class SmsIngressStore(Protocol):
    def put_received(self, receipt: InboundReceipt) -> bool: ...
    def read_received(self, business_id: str, provider_id: str) -> InboundReceipt | None: ...
    def read_reply_text(self, business_id: str, provider_id: str) -> str | None: ...
    def claim_processing(self, receipt: InboundReceipt, token: str,
                         now: datetime, lease_until: datetime) -> bool: ...
    def mark_processed(self, receipt: InboundReceipt, token: str, now: datetime) -> None: ...
    def put_reply(self, receipt: InboundReceipt, text: str,
                  token: str, now: datetime) -> bool: ...
    def record_outbound(self, business_id: str, phone_e164: str,
                        provider_id: str, sent_at: datetime) -> None: ...
    def is_opted_out(self, business_id: str, phone_e164: str) -> bool: ...
    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None: ...
    def put_consent(self, evidence: ConsentEvidence) -> None: ...
    def put_consent_verifying_phone(self, evidence: ConsentEvidence,
                                    verified: ClientProfile,
                                    welcome: OutboxRecord | None = None) -> None:
        """Write consent and the verified profile together, or raise RecordConflict.

        ``verified`` is the profile at version N+1; the write requires version N
        and the same phone to still be stored. ``welcome``, when given, is stored
        as a pending outbox record in the same atomic write, unless a record with
        that outbox ID already exists, in which case it is silently dropped.
        """
        ...
    def list_delivery_failures(self, business_id: str) -> tuple[SmsDeliveryStatus, ...]: ...


class VerifiedClientLookup(Protocol):
    def read_verified_phone(self, business_id: str, phone_e164: str) -> ClientProfile | None: ...
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None: ...


class SmsReceiptQueue(Protocol):
    def enqueue(self, business_id: str, provider_id: str) -> None: ...


def record_in_person_consent(store: SmsIngressStore, clients: VerifiedClientLookup,
                             business_id: str, client_id: str, phone_e164: str,
                             participant_name: str, script_version: str,
                             agreed_at: datetime) -> ConsentEvidence:
    """Trusted owner action; a first enrollment queues one welcome text.

    The welcome text is queued in the same atomic write only when the profile
    phone was never verified, and at most once per client (deterministic outbox
    ID). Re-recording consent, including after STOP/START, queues nothing. The
    sender still enforces consent, opt-out and verified phone at delivery.

    The owner and client meet in person and the owner reads the number back, so
    this also marks the profile phone verified in the same atomic write as the
    consent records. Nothing proves possession of the phone. A concurrent profile
    change raises ``RecordConflict`` and records nothing.
    """
    if agreed_at.tzinfo is None or not participant_name.strip() or not script_version.strip():
        raise ValueError("Consent needs a name, script version and aware timestamp")
    phone = normalize_phone(phone_e164)
    profile = clients.read_profile(business_id, client_id)
    if profile is None or not profile.active or profile.phone_e164 != phone:
        raise ValueError("Consent phone must match an active client profile")
    evidence = ConsentEvidence(business_id, client_id, participant_name.strip(),
                               phone, agreed_at.astimezone(UTC), script_version.strip())
    verified = replace(profile, version=profile.version + 1,
                       updated_at=evidence.agreed_at, phone_verified_at=evidence.agreed_at)
    welcome = None
    if profile.phone_verified_at is None:
        welcome = OutboxRecord(business_id, welcome_outbox_id(client_id), client_id, "client",
                               WELCOME_TEMPLATE, verified.version, DeliveryState.PENDING,
                               evidence.agreed_at, evidence.agreed_at, evidence.agreed_at)
    store.put_consent_verifying_phone(evidence, verified, welcome)
    return evidence


class SmsIngressService:
    def __init__(self, store: SmsIngressStore, clients: VerifiedClientLookup,
                 business_id: str, business_number: str, owner_number: str,
                 message_created_at: Callable[[str], datetime]) -> None:
        if not business_id:
            raise ValueError("Business ID is required")
        self._store = store
        self._clients = clients
        self._business_id = business_id
        self._business_number = normalize_phone(business_number)
        self._owner_number = normalize_phone(owner_number)
        self._message_created_at = message_created_at

    def receive(self, values: dict[str, str], now: datetime) -> tuple[InboundReceipt, bool]:
        """Deduplicate a verified provider message before a future conversation worker sees it."""
        if now.tzinfo is None:
            raise ValueError("Receipt time must be timezone-aware")
        provider_id = values.get("MessageSid", "")
        if not provider_id or len(provider_id) > 128:
            raise ValueError("Twilio MessageSid is required")
        sender = normalize_phone(values.get("From", ""))
        recipient = normalize_phone(values.get("To", ""))
        if recipient != self._business_number:
            raise ValueError("Message was sent to another business number")
        body = values.get("Body", "")
        if len(body) > 4000:
            raise ValueError("SMS body exceeds the processing limit")
        provider_keyword = values.get("OptOutType", "").upper()
        if provider_keyword and provider_keyword not in {"STOP", "HELP", "START"}:
            raise ValueError("Unsupported provider opt-out type")
        word = body.strip().upper()
        # STOP and HELP provider tags are authoritative. Twilio also tags any
        # configured opt-in keyword (such as YES or SUBSCRIBE) as START, which
        # would swallow our "Reply YES" confirmations, so honour START only for
        # Twilio's reserved opt-in words and otherwise classify the body ourselves.
        if provider_keyword == "START" and word not in START_WORDS:
            provider_keyword = ""
        keyword = (Keyword(provider_keyword) if provider_keyword else
                   Keyword.STOP if word in STOP_WORDS else
                   Keyword.HELP if word in HELP_WORDS else
                   Keyword.START if word in START_WORDS else Keyword.OTHER)
        profile = (None if sender == self._owner_number else
                   self._clients.read_verified_phone(self._business_id, sender))
        role = (SenderRole.OWNER if sender == self._owner_number else
                SenderRole.CLIENT if profile else SenderRole.UNKNOWN)
        consent = self._store.read_consent(self._business_id, sender)
        authorized = (role == SenderRole.OWNER or
                      role == SenderRole.CLIENT and profile is not None and consent is not None and
                      consent.client_id == profile.client_id and
                      not self._store.is_opted_out(self._business_id, sender))
        # Keywords are never passed to a scheduling command. Advanced Opt-Out
        # sends the provider's own STOP/HELP response; do not send a duplicate.
        safe_body = (body if authorized and keyword == Keyword.OTHER
                     and not ACCESS_CODE_PATTERN.search(body) else None)
        receipt = InboundReceipt(
            self._business_id, provider_id, sender, recipient,
            safe_body, now.astimezone(UTC), role,
            profile.client_id if profile else None, keyword,
            safe_body is not None,
        )
        if role == SenderRole.UNKNOWN:
            return receipt, True
        if role == SenderRole.CLIENT and consent is not None and profile is not None:
            # Webhook arrival time can be much later than message creation.
            # A retry of a pre-consent message must never enter a new profile.
            created_at = self._message_created_at(provider_id)
            if created_at.tzinfo is None:
                raise RuntimeError("Provider message timestamp is unavailable")
            if created_at.astimezone(UTC) <= max(consent.agreed_at, profile.created_at):
                return replace(receipt, body=None, authorized_for_commands=False), True
        try:
            inserted = self._store.put_received(receipt)
        except SmsReceiptErased:
            return replace(receipt, body=None, authorized_for_commands=False), True
        return receipt, not inserted

    def record_in_person_consent(self, client_id: str, phone_e164: str,
                                 participant_name: str, script_version: str,
                                 agreed_at: datetime) -> ConsentEvidence:
        return record_in_person_consent(self._store, self._clients, self._business_id,
                                        client_id, phone_e164, participant_name,
                                        script_version, agreed_at)
