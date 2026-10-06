"""Local-only text simulator that shares the synthetic owner API's calendar.

Not for deployment. It is mounted only by ``scheduling.local_owner``. Fictional
texts go through the real ``ConversationService`` against the same in-memory
repository the owner web app uses, so a change from either side is visible to
the other. Nothing reaches Twilio, DynamoDB, or any cloud service except the
optional OpenAI interpreter when ``OPENAI_API_KEY`` is set.

Intake mirrors the signed Twilio ingress: only the owner number or an active,
phone-verified client with consent reaches the conversation service. Local
consent is implied by a verified profile, because the in-person consent record
lives in the SMS store, which this harness does not have. Notification texts are
rendered from committed outbox intents with the production templates and are
shown, never sent.
"""

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Annotated, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.concurrency import run_in_threadpool

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.adapters.sms_twilio import render_notification
from scheduling.domain.client_records import ACCESS_CODE_PATTERN
from scheduling.domain.conversation import (
    MAX_MESSAGE_LENGTH,
    ConversationService,
    MessageContext,
    MessageInterpreter,
    MessageProposal,
)
from scheduling.domain.conversation_state import InMemoryConversationStates
from scheduling.domain.holds import HoldService, OutboxIntent
from scheduling.domain.lifecycle import LifecycleService
from scheduling.domain.outbox import PermanentDeliveryFailure
from scheduling.domain.owner_reply_classification import (
    Confidence,
    OwnerReplyContext,
    OwnerReplyIntent,
    OwnerReplyProposal,
)
from scheduling.domain.sms_ingress import (
    HELP_WORDS,
    START_WORDS,
    STOP_WORDS,
    ConsentEvidence,
    InboundReceipt,
    Keyword,
    SenderRole,
)
from scheduling.owner_api import OwnerPrincipal, StrictModel

logger = logging.getLogger(__name__)
OWNER_PHONE = "+14155559999"
BUSINESS_PHONE = "+14155550000"
OWNER = "owner"
PHONE_IN_TEXT = re.compile(
    r"(?<!\w)(?:\+?1[\s.()-]?)?(?:\(?[2-9]\d{2}\)?[\s.()-]?)"
    r"[2-9]\d{2}[\s.()-]?\d{4}(?!\w)"
)


class LocalConsent:
    """Treat an active, verified profile as consented; nothing is ever opted out."""

    def __init__(self, repository: InMemoryCalendarRepository) -> None:
        self._repository = repository

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return False

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        profile = self._repository.read_verified_phone(business_id, phone_e164)
        if profile is None or profile.phone_verified_at is None:
            return None
        return ConsentEvidence(business_id, profile.client_id, profile.name,
                               phone_e164, profile.phone_verified_at, "local-only")


class OfflineInterpreter:
    """Without an OpenAI key, every non-command text gets the safe clarification."""

    def __init__(self) -> None:
        self.consulted = False

    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        self.consulted = True
        return MessageProposal("clarify", None, None, None, True)

    def classify_owner_reply(self, body: str, context: OwnerReplyContext) -> OwnerReplyProposal:
        self.consulted = True  # Offline, an owner's plain reply is only ever clarified.
        return OwnerReplyProposal(OwnerReplyIntent.UNCLEAR, None, Confidence.HIGH)


@dataclass(frozen=True)
class SimulatedText:
    sequence: int
    at: str
    party: str
    party_label: str
    direction: str  # "in" from the phone, "out" to the phone, "note" for the harness
    kind: str  # "text", "reply", "notification", or "note"
    body: str


class TextSimulator:
    """One reentrant lock serializes each exchange and capture, so the log stays in
    commit order and a notification is rendered right after its commit. Concurrent
    owner-app writes can still commit between a commit and its capture; the harness
    then shows current appointment details where production would skip a stale one.
    """

    def __init__(self, repository: InMemoryCalendarRepository,
                 interpreter: MessageInterpreter, business_id: str,
                 clock: Callable[[], datetime]) -> None:
        self._repository = repository
        self._business_id = business_id
        self._clock = clock
        self._offline = interpreter if isinstance(interpreter, OfflineInterpreter) else None
        self._service = ConversationService(
            repository, interpreter, HoldService(repository),
            LifecycleService(repository, clock), LocalConsent(repository), clock, OWNER_PHONE,
            InMemoryConversationStates())
        self._lock = threading.RLock()
        self._log: list[SimulatedText] = []
        # Seeded history predates the simulator; show only later notifications.
        self._seen = {intent.outbox_id for intent in repository.list_outbox_intents()}

    def parties(self) -> list[dict[str, Any]]:
        parties: list[dict[str, Any]] = [
            {"id": OWNER, "label": "Owner", "can_text": True}]
        for profile in self._repository.list_profiles(self._business_id):
            verified = self._repository.read_verified_phone(
                self._business_id, profile.phone_e164) is not None
            parties.append({"id": profile.client_id, "label": profile.name,
                            "can_text": verified})
        return parties

    def messages(self) -> list[dict[str, Any]]:
        with self._lock:
            self.capture_notifications()
            return [asdict(text) for text in self._log]

    def send(self, party: str, body: str) -> list[dict[str, Any]]:
        text = body.strip()
        if not text:
            raise ValueError("Type a message first.")
        if len(text) > MAX_MESSAGE_LENGTH:
            raise ValueError(f"Keep the message under {MAX_MESSAGE_LENGTH} characters.")
        if PHONE_IN_TEXT.search(text) or ACCESS_CODE_PATTERN.search(text):
            raise ValueError("Use fictional text without phone numbers or access codes.")
        with self._lock:
            if party == OWNER:
                phone, role, client_id, label = OWNER_PHONE, SenderRole.OWNER, None, "Owner"
            else:
                profile = self._repository.read_profile(self._business_id, party)
                if profile is None:
                    raise LookupError("Unknown client.")
                phone, label = profile.phone_e164, profile.name
                verified = self._repository.read_verified_phone(self._business_id, phone)
                role = SenderRole.CLIENT if verified is not None else SenderRole.UNKNOWN
                client_id = verified.client_id if verified is not None else None
            start = len(self._log)
            self._append(party, label, "in", "text", text)
            word = text.upper()
            if word in STOP_WORDS or word in HELP_WORDS or word in START_WORDS:
                self._append(party, label, "note", "note",
                             "Keyword texts are handled by Twilio Advanced Opt-Out and are "
                             "not simulated here. Nothing changed.")
            elif role == SenderRole.UNKNOWN:
                self._append(party, label, "note", "note",
                             "No reply. This number has no active, verified profile with "
                             "consent, so production ignores the text. Nothing changed.")
            else:
                receipt = InboundReceipt(
                    self._business_id, f"local-{uuid4()}", phone, BUSINESS_PHONE, text,
                    self._clock(), role, client_id, Keyword.OTHER, True)
                if self._offline is not None:
                    self._offline.consulted = False
                outcome = self._service.handle(receipt)
                if outcome.committed:
                    # Production texts only the notifications for a committed change
                    # (see ReceiptProcessor); the reply itself is never sent.
                    self._append(party, label, "note", "note",
                                 f"Not texted: {outcome.text} The notification "
                                 "below is what the phone receives.")
                else:
                    self._append(party, label, "out", "reply", outcome.text)
                if self._offline is not None and self._offline.consulted:
                    self._append(party, label, "note", "note",
                                 "Plain-language texts need OPENAI_API_KEY. Without it, use "
                                 "exact commands such as Book YYYY-MM-DD; replies to an "
                                 "offer, such as a number or YES, still work.")
            self.capture_notifications()
            return [asdict(entry) for entry in self._log[start:]]

    def capture_notifications(self) -> None:
        """Render each newly committed outbox intent once, as the sender would."""
        with self._lock:
            zone = ZoneInfo(self._repository.read_policy(self._business_id).timezone)
            for intent in self._repository.list_outbox_intents():
                if intent.outbox_id in self._seen:
                    continue
                self._seen.add(intent.outbox_id)
                self._capture(intent, zone)
            for outbox in self._repository.list_manual_invitation_outbox():
                if outbox.outbox_id in self._seen:
                    continue
                self._seen.add(outbox.outbox_id)
                stored = self._repository.read_invitation(outbox.business_id, outbox.entity_id)
                if stored is None or stored.intent.manual_message is None:
                    continue
                profile = self._repository.read_profile(outbox.business_id, stored.intent.client_id)
                if profile is not None:
                    self._append(profile.client_id, profile.name, "out", "notification",
                                 stored.intent.manual_message)

    def _capture(self, intent: OutboxIntent, zone: ZoneInfo) -> None:
        appointment = self._repository.read_appointment(intent.hold_id)
        profile = (self._repository.read_profile(self._business_id, appointment.client_id)
                   if appointment is not None else None)
        if intent.recipient == "client":
            if appointment is None or profile is None or not profile.active:
                self._append(OWNER, "Owner", "note", "note",
                             f"A client {intent.template} notification would not be sent "
                             "(CLIENT_UNAVAILABLE).")
                return
            party, label = profile.client_id, profile.name
        else:
            party, label = OWNER, "Owner"
        try:
            body = render_notification(intent.template, intent.recipient, appointment,
                                       profile, zone)
        except PermanentDeliveryFailure as failure:
            self._append(party, label, "note", "note",
                         f"A {intent.template} notification would not be sent "
                         f"({failure.code}).")
            return
        if (intent.recipient == "client" and profile is not None
                and self._repository.read_verified_phone(
                    self._business_id, profile.phone_e164) is None):
            self._append(party, label, "note", "note",
                         "Not sent: this client has no verified phone with consent. "
                         f"The text would have said: {body}")
            return
        self._append(party, label, "out", "notification", body)

    def _append(self, party: str, label: str, direction: str, kind: str, body: str) -> None:
        with self._lock:
            self._log.append(SimulatedText(
                len(self._log) + 1, self._clock().isoformat(), party, label,
                direction, kind, body))


class SendText(StrictModel):
    party: str
    body: str


def mount_text_simulator(app: FastAPI, simulator: TextSimulator,
                         verify_token: Callable[[str], OwnerPrincipal]) -> None:
    security = HTTPBearer(auto_error=False)

    def authorized(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    ) -> None:
        if credentials is None:
            raise HTTPException(status_code=401, detail="Local token required")
        try:
            verify_token(credentials.credentials)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="Invalid local token") from exc

    @app.middleware("http")
    async def capture_after_request(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return response
        # Owner web app changes commit outbox intents; render them at commit time.
        # A harness failure must not turn an already committed write into an error.
        try:
            # Off the event loop: a text in progress holds the simulator lock.
            await run_in_threadpool(simulator.capture_notifications)
        except Exception:
            logger.exception("Local text simulator could not render a notification")
        return response

    @app.get("/local/texts", response_class=HTMLResponse, include_in_schema=False)
    def page() -> HTMLResponse:
        return HTMLResponse(PAGE, headers={
            "Content-Security-Policy": ("default-src 'none'; script-src 'unsafe-inline'; "
                                        "style-src 'unsafe-inline'; connect-src 'self'; "
                                        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
            "Cache-Control": "no-store",
        })

    @app.get("/local/texts/state", include_in_schema=False,
             dependencies=[Depends(authorized)])
    def state() -> dict[str, Any]:
        return {"parties": simulator.parties(), "messages": simulator.messages()}

    @app.post("/local/texts", include_in_schema=False, dependencies=[Depends(authorized)])
    def send(request: SendText) -> dict[str, Any]:
        try:
            return {"messages": simulator.send(request.party, request.body)}
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Local text simulator</title>
<style>
  body { font: 15px/1.4 system-ui, sans-serif; margin: 0; background: #f4f4f5; color: #18181b; }
  main { max-width: 760px; margin: 0 auto; padding: 16px; }
  .banner { background: #fef3c7; border: 1px solid #f59e0b; padding: 8px 12px;
            border-radius: 6px; font-size: 13px; }
  form { display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }
  input, select, button { font: inherit; padding: 6px 10px; }
  #body { flex: 1 1 240px; }
  #log { display: flex; flex-direction: column; gap: 6px; }
  .msg { max-width: 80%; padding: 8px 12px; border-radius: 12px; white-space: pre-wrap; }
  .in { align-self: flex-end; background: #2563eb; color: white; }
  .out { align-self: flex-start; background: white; border: 1px solid #d4d4d8; }
  .notification { border-color: #16a34a; }
  .note { align-self: center; background: none; color: #52525b; font-style: italic;
          font-size: 13px; max-width: 95%; }
  .meta { font-size: 11px; opacity: .75; margin-bottom: 2px; }
  #error { color: #b91c1c; }
  .hint { font-size: 13px; color: #52525b; }
</style>
</head>
<body>
<main>
  <h1>Local text simulator</h1>
  <p class="banner">Fictional data only. Nothing is sent by SMS. This page shares the
  calendar with the owner web app on port 3000 and resets when the backend stops.</p>
  <form id="signin">
    <input id="token" type="password" placeholder="LOCAL_OWNER_TOKEN" autocomplete="off" required>
    <button>Connect</button>
  </form>
  <p id="error" role="alert"></p>
  <section id="chat" hidden>
    <form id="send">
      <label>Text as <select id="party"></select></label>
      <input id="body" maxlength="1000" autocomplete="off"
             placeholder="e.g. Do you have anything tomorrow morning?">
      <button id="send-button">Send</button>
    </form>
    <p class="hint">Client: ask in plain words, such as <em>Do you have availability
    tomorrow?</em>, then reply with an offered number or time, or <em>yes</em>.
    <em>I can't make Thursday</em> asks before cancelling. Owner: <em>yes</em> or
    <em>decline</em> acts when exactly one request is pending. Exact commands still work:
    <code>Book YYYY-MM-DD</code>, <code>Cancel REF</code>, <code>Approve REF</code>.
    Plain language needs <code>OPENAI_API_KEY</code>.
    Green-bordered messages are notifications that would be texted. Grey notes are
    not texted; after a change, only its notifications are.</p>
    <div id="log" aria-live="polite"></div>
  </section>
</main>
<script>
let token = null;
const $ = (id) => document.getElementById(id);
const alert_ = (text) => { $("error").textContent = text; };
async function call(path, options = {}) {
  const response = await fetch(path, { ...options, headers: {
    "Authorization": "Bearer " + token, "Content-Type": "application/json" } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || response.statusText);
  return data;
}
function render(messages) {
  const log = $("log");
  log.replaceChildren(...messages.map((m) => {
    const div = document.createElement("div");
    div.className = "msg " + m.direction + " " + m.kind;
    const meta = document.createElement("div");
    meta.className = "meta";
    const who = m.direction === "in" ? m.party_label + " \\u2192 business"
      : m.direction === "out" ? "business \\u2192 " + m.party_label : m.party_label;
    meta.textContent = who + " \\u00b7 " + new Date(m.at).toLocaleTimeString();
    const text = document.createElement("div");
    text.textContent = m.body;
    div.append(meta, text);
    return div;
  }));
  log.lastElementChild?.scrollIntoView({ block: "end" });
}
async function refresh() {
  const state = await call("/local/texts/state");
  const select = $("party"), current = select.value;
  select.replaceChildren(...state.parties.map((p) => {
    const option = document.createElement("option");
    option.value = p.id;
    option.textContent = p.label + (p.can_text ? "" : " (not verified)");
    return option;
  }));
  if (current) select.value = current;
  if (state.messages.length !== $("log").children.length) render(state.messages);
}
$("signin").addEventListener("submit", async (event) => {
  event.preventDefault();
  token = $("token").value.trim();
  try {
    await refresh();
    $("signin").hidden = true; $("chat").hidden = false; $("error").textContent = "";
    setInterval(() => refresh().catch(() => {}), 3000);
  } catch (error) { token = null; alert_(error.message); }
});
let sending = false;
function setSending(on) {
  // One text at a time: a reply that waits on the model takes a few seconds.
  sending = on;
  for (const id of ["party", "body", "send-button"]) $(id).disabled = on;
  $("send-button").textContent = on ? "Sending..." : "Send";
}
$("send").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (sending) return;
  $("error").textContent = "";
  setSending(true);
  try {
    await call("/local/texts", { method: "POST",
      body: JSON.stringify({ party: $("party").value, body: $("body").value }) });
    $("body").value = "";
    await refresh();
  } catch (error) { $("error").textContent = error.message; }
  finally { setSending(false); $("body").focus(); }
});
</script>
</body>
</html>
"""
