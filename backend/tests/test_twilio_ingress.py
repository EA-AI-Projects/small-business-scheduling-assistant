"""Signed synthetic Twilio callbacks cannot bypass sender and consent gates."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from starlette.datastructures import FormData
from twilio.request_validator import RequestValidator

from scheduling.domain.client_records import ClientProfile, HomeSize
from scheduling.domain.sms_ingress import (
    ConsentEvidence,
    InboundReceipt,
    Keyword,
    SmsIngressService,
    SmsReceiptErased,
    normalize_phone,
)
from scheduling.domain.sms_status import SmsDeliveryStatus
from scheduling.twilio_webhooks import create_twilio_ingress_app

NOW = datetime(2026, 9, 28, 17, tzinfo=UTC)
URL = "https://sms.example.test/webhooks/sms/inbound"
STATUS_URL = "https://sms.example.test/webhooks/sms/status"
TOKEN = "synthetic-auth-token"


class MemoryStore:
    def __init__(self) -> None:
        self.receipts: dict[str, InboundReceipt] = {}
        self.consent: dict[str, ConsentEvidence] = {}
        self.opted_out: dict[str, datetime] = {}
        self.statuses: dict[str, SmsDeliveryStatus] = {}
        self.erased_ids: set[str] = set()

    def put_received(self, receipt: InboundReceipt) -> bool:
        if receipt.provider_id in self.erased_ids:
            raise SmsReceiptErased
        if receipt.provider_id in self.receipts:
            return False
        self.receipts[receipt.provider_id] = receipt
        if receipt.keyword == Keyword.STOP:
            self.opted_out[receipt.sender] = receipt.received_at
        if receipt.keyword == Keyword.START:
            consent = self.consent.get(receipt.sender)
            if consent is not None and consent.agreed_at > self.opted_out.get(receipt.sender, NOW):
                self.opted_out.pop(receipt.sender, None)
        return True

    def read_received(self, business_id: str, provider_id: str) -> InboundReceipt | None:
        receipt = self.receipts.get(provider_id)
        return receipt if receipt is not None and receipt.business_id == business_id else None

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return phone_e164 in self.opted_out

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        return self.consent.get(phone_e164)

    def put_consent(self, evidence: ConsentEvidence) -> None:
        self.consent[evidence.phone_e164] = evidence

    def put_consent_verifying_phone(self, evidence: ConsentEvidence,
                                    verified: ClientProfile,
                                    welcome: object = None) -> None:
        self.put_consent(evidence)

    def put_status(self, status: SmsDeliveryStatus) -> None:
        self.statuses[status.provider_id] = status


class Clients:
    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None:
        return self.read_verified_phone(business_id, "+14155550101") if client_id == "client-1" else None

    def read_verified_phone(self, business_id: str, phone_e164: str) -> ClientProfile | None:
        if phone_e164 != "+14155550101":
            return None
        return ClientProfile(business_id, "client-1", "Synthetic Client", phone_e164,
                             "123 Test Street", HomeSize.SMALL, 60, True, 1,
                             NOW, NOW, NOW)


def setup() -> tuple[TestClient, SmsIngressService, MemoryStore]:
    store = MemoryStore()
    service = SmsIngressService(store, Clients(), "pilot", "+14155550000", "+14155559999")
    return (TestClient(create_twilio_ingress_app(service, TOKEN, URL, lambda: NOW,
                                                status_url=STATUS_URL, status_store=store,
                                                business_id="pilot")),
            service, store)


def send(client: TestClient, values: dict[str, str], signature: str | None = None) -> int:
    form = FormData(values)
    signed = signature or RequestValidator(TOKEN).compute_signature(URL, form)
    return client.post("/webhooks/sms/inbound", data=values,
                       headers={"X-Twilio-Signature": signed}).status_code


def inbound(sid: str, sender: str = "+14155550101", body: str = "Tuesday at 9") -> dict[str, str]:
    return {"MessageSid": sid, "From": sender, "To": "+14155550000", "Body": body}


def test_signature_then_dedup_and_verified_consent_gate() -> None:
    client, service, store = setup()
    values = inbound("SM-1")
    assert send(client, values, "bad-signature") == 403
    assert store.receipts == {}
    assert send(client, values) == 204
    assert store.receipts["SM-1"].authorized_for_commands is False
    assert send(client, values) == 204
    assert len(store.receipts) == 1

    evidence = service.record_in_person_consent("client-1", "+1 (415) 555-0101",
                                                "Synthetic Client", "pilot-v1", NOW)
    assert evidence.method == "in_person"
    assert evidence.script_version == "pilot-v1"
    assert send(client, inbound("SM-2")) == 204
    assert store.receipts["SM-2"].authorized_for_commands is True
    assert send(client, inbound("SM-3", "+14155550102")) == 204
    assert "SM-3" not in store.receipts  # Unknown numbers are never persisted.
    assert send(client, inbound("SM-4", "+14155559999")) == 204
    assert store.receipts["SM-4"].role.value == "owner"
    assert send(client, inbound("SM-code", body="door code 1234")) == 204
    assert store.receipts["SM-code"].body is None


def test_erased_provider_retry_is_acknowledged_without_recreating_a_receipt() -> None:
    client, service, store = setup()
    service.record_in_person_consent("client-1", "+14155550101", "Synthetic Client",
                                     "pilot-v1", NOW)
    store.erased_ids.add("SM-erased")
    assert send(client, inbound("SM-erased")) == 204
    assert "SM-erased" not in store.receipts
    receipt, duplicate = service.receive(inbound("SM-erased"), NOW)
    assert duplicate is True
    assert receipt.authorized_for_commands is False


def test_stop_help_and_start_never_run_scheduling_command() -> None:
    client, service, store = setup()
    service.record_in_person_consent("client-1", "+14155550101", "Synthetic Client",
                                     "pilot-v1", NOW)
    assert send(client, {**inbound("SM-stop", body="stop"), "OptOutType": "STOP"}) == 204
    assert store.receipts["SM-stop"].body is None
    assert store.receipts["SM-stop"].authorized_for_commands is False
    assert "+14155550101" in store.opted_out
    assert send(client, inbound("SM-after")) == 204
    assert store.receipts["SM-after"].authorized_for_commands is False
    assert send(client, {**inbound("SM-help", body="help"), "OptOutType": "HELP"}) == 204
    assert store.receipts["SM-help"].keyword == Keyword.HELP
    assert send(client, {**inbound("SM-start", body="start"), "OptOutType": "START"}) == 204
    assert store.receipts["SM-start"].authorized_for_commands is False
    assert "+14155550101" in store.opted_out  # START is not in-person re-consent.
    service.record_in_person_consent("client-1", "+14155550101", "Synthetic Client",
                                     "pilot-v1", NOW + timedelta(seconds=1))
    assert "+14155550101" in store.opted_out  # Provider START is still needed.
    assert send(client, {**inbound("SM-start-again", body="start"), "OptOutType": "START"}) == 204
    assert "+14155550101" not in store.opted_out


def test_provider_start_tag_is_honoured_only_for_reserved_opt_in_words() -> None:
    client, service, store = setup()
    service.record_in_person_consent("client-1", "+14155550101", "Synthetic Client",
                                     "pilot-v1", NOW)
    for sid, body in (("SM-yes", "Yes"), ("SM-sub", "SUBSCRIBE")):
        assert send(client, {**inbound(sid, body=body), "OptOutType": "START"}) == 204
        assert store.receipts[sid].keyword == Keyword.OTHER
        assert store.receipts[sid].body == body
        assert store.receipts[sid].authorized_for_commands is True
    assert send(client, {**inbound("SM-owner", "+14155559999", "yes"),
                         "OptOutType": "START"}) == 204
    assert store.receipts["SM-owner"].keyword == Keyword.OTHER
    assert store.receipts["SM-owner"].authorized_for_commands is True
    for sid, body in (("SM-start", " start "), ("SM-unstop", "unstop")):
        assert send(client, {**inbound(sid, body=body), "OptOutType": "START"}) == 204
        assert store.receipts[sid].keyword == Keyword.START
        assert store.receipts[sid].body is None
    # Without a provider tag, UNSTOP is also an opt-in keyword, never a command.
    assert send(client, inbound("SM-bare-unstop", body="UNSTOP")) == 204
    assert store.receipts["SM-bare-unstop"].keyword == Keyword.START
    assert store.receipts["SM-bare-unstop"].body is None


def test_provider_stop_and_help_tags_stay_authoritative() -> None:
    client, service, store = setup()
    service.record_in_person_consent("client-1", "+14155550101", "Synthetic Client",
                                     "pilot-v1", NOW)
    assert send(client, {**inbound("SM-yes-stop", body="yes"), "OptOutType": "STOP"}) == 204
    assert store.receipts["SM-yes-stop"].keyword == Keyword.STOP
    assert store.receipts["SM-yes-stop"].body is None
    assert "+14155550101" in store.opted_out
    assert send(client, {**inbound("SM-yes-help", body="yes"), "OptOutType": "HELP"}) == 204
    assert store.receipts["SM-yes-help"].keyword == Keyword.HELP
    assert store.receipts["SM-yes-help"].body is None


def test_start_tagged_yes_never_reenables_an_opted_out_sender() -> None:
    client, service, store = setup()
    service.record_in_person_consent("client-1", "+14155550101", "Synthetic Client",
                                     "pilot-v1", NOW)
    assert send(client, {**inbound("SM-stop", body="stop"), "OptOutType": "STOP"}) == 204
    service.record_in_person_consent("client-1", "+14155550101", "Synthetic Client",
                                     "pilot-v2", NOW + timedelta(seconds=1))
    assert send(client, {**inbound("SM-yes", body="yes"), "OptOutType": "START"}) == 204
    receipt = store.receipts["SM-yes"]
    assert receipt.keyword == Keyword.OTHER
    assert receipt.body is None
    assert receipt.authorized_for_commands is False
    assert "+14155550101" in store.opted_out
    assert send(client, {**inbound("SM-unstop", body="unstop"), "OptOutType": "START"}) == 204
    assert store.receipts["SM-unstop"].keyword == Keyword.START
    assert "+14155550101" not in store.opted_out


def test_malformed_or_wrong_recipient_is_never_recorded() -> None:
    client, _, store = setup()
    assert send(client, {**inbound("SM-wrong"), "To": "+14155550001"}) == 422
    assert send(client, inbound("SM-local", "4155550101")) == 422
    assert client.post("/webhooks/sms/inbound", data=inbound("SM-unsigned")).status_code == 403
    assert store.receipts == {}
    assert normalize_phone("+1 (415) 555-0101") == "+14155550101"


def test_status_callback_requires_signature_and_records_provider_result() -> None:
    client, _, store = setup()
    fields = {"MessageSid": "SM-status", "MessageStatus": "undelivered",
              "To": "+14155550101", "ErrorCode": "30007"}
    path = "/webhooks/sms/status?outbox_id=notice-1"
    assert client.post(path, data=fields,
                       headers={"X-Twilio-Signature": "bad"}).status_code == 403
    signature = RequestValidator(TOKEN).compute_signature(
        STATUS_URL + "?outbox_id=notice-1", FormData(fields))
    assert client.post(path, data=fields,
                       headers={"X-Twilio-Signature": signature}).status_code == 204
    assert store.statuses["SM-status"].status == "undelivered"
    assert store.statuses["SM-status"].outbox_id == "notice-1"
    assert store.statuses["SM-status"].error_code == "30007"


def test_authorized_receipt_handoff_retries_duplicates_without_queueing_unknowns() -> None:
    _, service, store = setup()
    service.record_in_person_consent("client-1", "+14155550101", "Synthetic Client",
                                     "pilot-v1", NOW)

    class Queue:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.fail = True

        def enqueue(self, business_id: str, provider_id: str) -> None:
            self.calls.append((business_id, provider_id))
            if self.fail:
                raise RuntimeError("synthetic handoff failure")

    queue = Queue()
    with TestClient(create_twilio_ingress_app(service, TOKEN, URL, lambda: NOW,
                                              receipt_queue=queue)) as handoff_client:
        assert send(handoff_client, inbound("SM-queued")) == 503
        assert "SM-queued" in store.receipts
        queue.fail = False
        assert send(handoff_client, inbound("SM-queued")) == 204
        assert queue.calls == [("pilot", "SM-queued"), ("pilot", "SM-queued")]
        assert send(handoff_client, inbound("SM-unknown", "+14155550102")) == 204
        assert send(handoff_client, inbound("SM-stop", body="STOP")) == 204
        assert queue.calls == [("pilot", "SM-queued"), ("pilot", "SM-queued")]
        assert len(store.receipts) == 2
