"""Trusted SMS ingress boundary; no scheduling command is inferred here."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from scheduling.domain.client_records import ACCESS_CODE_PATTERN, ClientProfile

if TYPE_CHECKING:
    from scheduling.domain.sms_status import SmsDeliveryStatus

PHONE = re.compile(r"^\+[1-9][0-9]{1,14}$")
STOP_WORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "END", "QUIT", "REVOKE", "OPTOUT"})
HELP_WORDS = frozenset({"HELP", "INFO"})
START_WORDS = frozenset({"START", "UNSTOP"})


class SmsCommandInterrupted(Exception):
    """An SMS scheduling transaction was cancelled; the old text must not replay."""


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
    """Trusted owner action; no automated enrollment text is sent."""
    if agreed_at.tzinfo is None or not participant_name.strip() or not script_version.strip():
        raise ValueError("Consent needs a name, script version and aware timestamp")
    phone = normalize_phone(phone_e164)
    profile = clients.read_profile(business_id, client_id)
    if profile is None or not profile.active or profile.phone_e164 != phone:
        raise ValueError("Consent phone must match an active client profile")
    evidence = ConsentEvidence(business_id, client_id, participant_name.strip(),
                               phone, agreed_at.astimezone(UTC), script_version.strip())
    store.put_consent(evidence)
    return evidence


class SmsIngressService:
    def __init__(self, store: SmsIngressStore, clients: VerifiedClientLookup,
                 business_id: str, business_number: str, owner_number: str) -> None:
        if not business_id:
            raise ValueError("Business ID is required")
        self._store = store
        self._clients = clients
        self._business_id = business_id
        self._business_number = normalize_phone(business_number)
        self._owner_number = normalize_phone(owner_number)

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
        inserted = self._store.put_received(receipt)
        return receipt, not inserted

    def record_in_person_consent(self, client_id: str, phone_e164: str,
                                 participant_name: str, script_version: str,
                                 agreed_at: datetime) -> ConsentEvidence:
        return record_in_person_consent(self._store, self._clients, self._business_id,
                                        client_id, phone_e164, participant_name,
                                        script_version, agreed_at)
