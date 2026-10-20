"""Client-specific scheduling preferences in SMS offers (#383), with a scripted model.

Boundary: the owner API (review, ambiguous-name selection, activation, clearing), the client
portal availability route, and the SMS conversation service with its tool loop, all over one
in-memory repository and calendar. A scripted model stands in for the model's tool choices
and text. It proves that the verified sender's client id (from the receipt) selects the
rules through the portal's ``recommended_starts``, and that acceptance, revalidation,
approval, expiry and replay are unchanged; it does not prove how the real model words an
offer or interprets owner text (see ``backend/evals``, none run live). No live model, SMS or
AWS is used.
"""

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_client_scheduling_preferences import Scripted
from test_client_tool_loop import Tools, chat_with
from test_conversation_flow import NOW, THURSDAY, ZONE, Harness, ask

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.client_api import add_client_session_route
from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.client_records import ClientProfile
from scheduling.domain.conversation import ConversationOutcome
from scheduling.domain.lifecycle import Action, ActorRole, AppointmentCommand, LifecycleService
from scheduling.domain.owner_policy import OwnerPolicyService
from scheduling.domain.sms_ingress import ConsentEvidence, InboundReceipt, Keyword, SenderRole
from scheduling.identity_links import IdentityLink, LinkRole, LinkState
from scheduling.owner_api import OwnerPrincipal, create_owner_app
from tests.preference_scenarios import CLIENT_ONLY, OVERRIDE, SCENARIOS, Scenario

WED, THU, FRI = "2026-09-30", "2026-10-01", "2026-10-02"
PHONES = {"client-1": "+14155550101", "client-2": "+14155550102", "client-3": "+14155550103"}
NAMES = {"client-1": "Client Alder", "client-2": "Client Birch", "client-3": "Client Cedar"}
OTHER_PHONE = "+14155550109"  # a client of a second business whose id is also client-1
BASE = "/v1/owner/businesses/pilot/scheduling-preferences"
OWNER = {"Authorization": "Bearer owner"}
STATIC_REPLY = "Here are the open times. The owner approves every request."


def get(tools: Tools, first: str = WED, last: str = FRI, **extra: Any) -> dict[str, Any]:
    return tools("get_availability", {"from": first, "to": last, "time_from": None,
                                      "time_to": None, **extra})


class Consents:
    """Every synthetic sender here has consented, as themselves and in their own business."""

    def __init__(self, repo: InMemoryCalendarRepository) -> None:
        self._repo = repo

    def is_opted_out(self, business_id: str, phone_e164: str) -> bool:
        return False

    def read_consent(self, business_id: str, phone_e164: str) -> ConsentEvidence | None:
        if phone_e164 == OTHER_PHONE:
            return ConsentEvidence(business_id, "client-1", "Other Household", phone_e164, NOW, "v1")
        client = next((c for c, phone in PHONES.items() if phone == phone_e164), None)
        if client is None:
            return None
        return ConsentEvidence(business_id, client, NAMES[client], phone_e164, NOW, "v1")


def profile(client: str, name: str, version: int = 1) -> ClientProfile:
    return ClientProfile("pilot", client, name, PHONES[client], "1 Test Street", True, version,
                         NOW, NOW, NOW)


class World:
    """Three synthetic clients, one calendar, the owner API and the portal, and the SMS service."""

    def __init__(self, plan: Any = None, deterministic: bool = False) -> None:
        self.seen: list[dict[str, Any]] = []

        def default(tools: Tools) -> str:
            self.seen.append(get(tools))
            return STATIC_REPLY

        if deterministic:
            # No tool-loop model: the backend's own deterministic offer path runs.
            self.chat = Harness()
            self.model: Any = self.chat.model
        else:
            self.chat, self.model = chat_with(plan or default)
        self.repo: InMemoryCalendarRepository = self.chat.store
        self.chat.service._consent = Consents(self.repo)  # type: ignore[attr-defined]
        self.repo.save_profile(profile("client-1", NAMES["client-1"], 2), 1, PHONES["client-1"])
        for client in ("client-2", "client-3"):
            self.repo.save_profile(profile(client, NAMES[client]), 0, None)
        OwnerPolicyService(self.repo, lambda: self.chat.now).seed("pilot", "owner", "seed")
        self.interpreter = Scripted()
        app = create_owner_app(self.repo, lambda _token: OwnerPrincipal("owner", "pilot"),
                               lambda: self.chat.now, preference_interpreter=self.interpreter)
        links = {f"token-{n}": IdentityLink("sub", LinkRole.CLIENT, "pilot", f"client-{n}",
                                            LinkState.ACTIVE, 1, NOW, "admin", "test")
                 for n in (1, 2, 3)}
        add_client_session_route(app, lambda token: links[token], self.repo,
                                 lambda: self.chat.now)
        self.api = TestClient(app)
        self.count = 0

    # -- owner and portal --------------------------------------------------

    def review(self, text: str, selections: list[dict[str, str]] | None = None) -> dict[str, Any]:
        response = self.api.post(f"{BASE}/review", headers=OWNER, json={
            "text": text, "client_selections": selections or []})
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def version(self) -> int:
        return int(self.api.get(BASE, headers=OWNER).json()["version"])

    def activate(self, reviewed: dict[str, Any], key: str = "activate") -> None:
        response = self.api.put(BASE, headers={**OWNER, "Idempotency-Key": key}, json={
            "expected_version": self.version(), "review_token": reviewed["review_token"]})
        assert response.status_code == 200, response.text

    def adopt(self, scenario: Scenario, key: str = "activate") -> None:
        self.interpreter.answers[scenario.text] = scenario.answer
        reviewed = self.review(scenario.text)
        assert reviewed["ready"] is True, reviewed
        self.activate(reviewed, key)

    def clear(self, key: str = "clear") -> None:
        response = self.api.request("DELETE", BASE, headers={**OWNER, "Idempotency-Key": key},
                                    json={"expected_version": self.version()})
        assert response.status_code == 200, response.text

    def portal(self, client: str, *days: str) -> set[datetime]:
        recommended: set[datetime] = set()
        for day in days:
            response = self.api.get(f"/v1/client/availability?day={day}",
                                    headers={"Authorization": f"Bearer token-{client[-1]}"})
            assert response.status_code == 200, response.text
            recommended |= {datetime.fromisoformat(v)
                            for v in response.json()["recommended_starts_at"]}
        return recommended

    # -- SMS ---------------------------------------------------------------

    def sms(self, client: str, body: str, business: str = "pilot") -> ConversationOutcome:
        self.count += 1
        return self.chat.service.handle(InboundReceipt(
            business, f"SM-c{self.count}", PHONES[client], "+14155550000", body, self.chat.now,
            SenderRole.CLIENT, client, Keyword.OTHER, True))

    def offer(self, client: str, body: str = "what do you have this week?") -> list[datetime]:
        self.sms(client, body)
        return self.stored(client)

    def stored(self, client: str, business: str = "pilot") -> list[datetime]:
        state = self.chat.states.read_state(business, PHONES[client])
        assert state is not None
        return list(state.options)


def hour_set(starts: list[datetime], day: str) -> set[int]:
    return {s.astimezone(ZONE).hour for s in starts if s.astimezone(ZONE).date().isoformat() == day}


def test_two_clients_over_one_calendar_get_different_offers_matching_the_portal() -> None:
    world = World()
    world.adopt(OVERRIDE)
    alder, birch = world.offer("client-1"), world.offer("client-2")
    assert alder != birch
    for client, offer in (("client-1", alder), ("client-2", birch)):
        # Preferred starts fill the offer, and the portal recommends exactly those starts.
        assert len(offer) == 5 and set(offer) <= world.portal(client, WED, THU, FRI)
    assert hour_set(alder, THU) <= {13, 14, 15}  # the client exception replaces Thursday mornings
    assert hour_set(birch, THU) <= {8, 9, 10, 11}  # business-wide guidance for the other client
    # Neither tool result nor reply carries names, owner text or any preference marking.
    for result in world.seen:
        dumped = str(result).lower()
        for leaked in ("alder", "birch", "cedar", "prefer", "recommend", "afternoon"):
            assert leaked not in dumped


def test_a_client_with_no_rule_gets_business_wide_guidance_then_ordinary_offers() -> None:
    world = World()
    world.adopt(OVERRIDE)
    cedar = world.offer("client-3")  # no client rule: business-wide weekday mornings
    assert cedar and set(cedar) <= world.portal("client-3", WED, THU, FRI)
    ordinary = World()
    plain = ordinary.offer("client-3")
    world.clear()
    assert world.offer("client-3") == plain  # nothing configured: ordinary offers


def test_client_only_guidance_leaves_other_clients_with_the_ordinary_offer() -> None:
    world = World()
    ordinary = World().offer("client-1")
    world.adopt(CLIENT_ONLY)
    assert world.offer("client-1") == ordinary  # Alder has no rule and there is no general one
    birch = world.offer("client-2")
    assert set(birch) <= world.portal("client-2", WED, THU, FRI) and len(birch) == 5
    assert hour_set(birch, WED) <= {8, 9, 10, 11} and birch != ordinary


def test_renaming_the_profile_keeps_its_preference_behavior() -> None:
    world = World()
    world.adopt(OVERRIDE)
    before = world.offer("client-1")
    world.repo.save_profile(profile("client-1", "Renamed Household", 3), 2, PHONES["client-1"])
    assert world.offer("client-1") == before


def window_world(first: str, last: str, **window: str) -> World:
    return World(lambda tools: (get(tools, first, last, **window), STATIC_REPLY)[1])


def test_an_explicit_window_is_honored_and_a_client_rule_replaces_not_adds() -> None:
    # Alder's Thursday-afternoon rule replaces Thursday mornings, so a Thursday-only morning
    # window holds nothing preferred: the window is still honored and the offer is ordinary.
    ordinary = window_world(THU, THU, time_from="08:00", time_to="10:00").offer("client-2")
    world = window_world(THU, THU, time_from="08:00", time_to="10:00")
    world.adopt(OVERRIDE)
    options = world.offer("client-1")
    assert options and all(8 <= s.astimezone(ZONE).hour <= 10 for s in options)
    assert options == window_world(THU, THU, time_from="08:00", time_to="10:00").offer("client-1")
    assert ordinary  # Birch, with business-wide morning guidance, is offered the same window
    # A window holding both: only the replacing afternoon rule is preferred for Alder.
    both = window_world(THU, THU, time_from="08:00", time_to="15:00")
    both.adopt(OVERRIDE)
    mixed = both.offer("client-1")
    assert len(mixed) == 5 and hour_set(mixed, THU) <= {13, 14, 15}
    assert hour_set(both.offer("client-2"), THU) <= {8, 9, 10, 11}


def request(tools: Tools, pick: datetime) -> dict[str, Any]:
    local = pick.astimezone(ZONE)
    return tools("request_booking", {"date": local.date().isoformat(),
                                     "time": local.strftime("%H:%M")})


def book(world: World, client: str, start: datetime) -> None:
    """Request exactly this start through an exact-time offer and an acceptance."""
    local = start.astimezone(ZONE)
    day, clock = local.date().isoformat(), local.strftime("%H:%M")
    world.model.plan = lambda tools: (
        get(tools, day, day, time_from=clock, time_to=clock), STATIC_REPLY)[1]
    world.sms(client, "that time?")
    results: list[dict[str, Any]] = []
    world.model.plan = lambda tools: (results.append(request(tools, start)), (
        f"Requested (ref {results[0].get('ref')}). It is pending owner approval, "
        "not confirmed yet."))[1]
    assert world.sms(client, "yes").committed


def test_activating_or_clearing_affects_next_offer_while_a_prior_offer_still_accepts() -> None:
    results: list[dict[str, Any]] = []
    picks: list[datetime] = []

    def plan(tools: Tools) -> str:
        if picks:
            results.append(request(tools, picks[0]))
            return ("Requested (ref " + str(results[-1].get("ref")) + "). It is pending owner "
                    "approval, not confirmed yet.")
        get(tools)
        return STATIC_REPLY

    world = World(plan)
    ordinary = world.offer("client-1")
    world.adopt(OVERRIDE)  # activation after the offer was sent
    assert world.stored("client-1") == ordinary  # the stored offer is untouched
    picks.append(ordinary[0])
    booked = world.sms("client-1", "the first one")
    assert booked.committed and results[0]["requested"] is True
    assert world.chat.status(booked.appointment_id or "") == CalendarStatus.PENDING_APPROVAL
    picks.clear()
    preferred = world.offer("client-1")
    assert preferred != ordinary and set(preferred) <= world.portal("client-1", WED, THU, FRI)
    world.clear()  # clearing after an offer: it still accepts, the next offer is ordinary again
    picks.append(preferred[0])
    assert world.sms("client-1", "the first one").committed
    picks.clear()
    after_clear = world.offer("client-1")  # read afresh: nothing is configured any more
    assert after_clear and world.portal("client-1", WED, THU, FRI) == set()
    assert len(world.chat.calendar()) == 2
    assert all(status == CalendarStatus.PENDING_APPROVAL.value
               for _, status, _ in world.chat.calendar())
    # The same two pending requests in a world that never had preferences: the same offer.
    reference = World()
    for start in (ordinary[0], preferred[0]):
        book(reference, "client-1", start)
    reference.model.plan = lambda tools: (get(tools), STATIC_REPLY)[1]
    assert reference.offer("client-1") == after_clear


def test_the_deterministic_offer_path_uses_the_senders_client_rules() -> None:
    world = World(deterministic=True)  # no tool loop: ConversationService._offer runs
    world.adopt(OVERRIDE)
    world.model.replies["anything this week?"] = ask("availability", WED, FRI)
    alder = world.sms("client-1", "anything this week?")
    alder_options = world.stored("client-1")
    birch = world.sms("client-2", "anything this week?")
    birch_options = world.stored("client-2")
    assert alder_options != birch_options and not alder.committed and not birch.committed
    assert set(alder_options) <= world.portal("client-1", WED, THU, FRI)
    assert set(birch_options) <= world.portal("client-2", WED, THU, FRI)
    assert hour_set(alder_options, THU) <= {13, 14, 15}
    assert hour_set(birch_options, THU) <= {8, 9, 10, 11}
    assert "Alder" not in birch.text and "afternoon" not in alder.text.lower()
    assert world.chat.calendar() == ()  # an offer books nothing


def test_the_deterministic_reschedule_offer_uses_the_senders_client_rules() -> None:
    world = World(deterministic=True)
    world.adopt(OVERRIDE)
    visit = world.chat.hold(THURSDAY, "visit", confirm=True)
    world.model.replies["move my visit"] = ask("reschedule", THU, THU, target_date=THU)
    world.sms("client-1", "move my visit")
    options = world.stored("client-1")
    assert options and set(options) <= world.portal("client-1", THU)
    assert hour_set(options, THU) <= {13, 14, 15}  # Alder's own Thursday rule
    assert world.chat.status(visit) == CalendarStatus.CONFIRMED
    assert [status for _, status, _ in world.chat.calendar()] == [CalendarStatus.CONFIRMED.value]


def move_plan(seen: list[dict[str, Any]]) -> Any:
    def plan(tools: Tools) -> str:
        ref = tools("get_my_bookings", {"from": None, "to": None})["bookings"][0]["ref"]
        seen.append(get(tools, THU, FRI, ref=ref))
        return ("To move your visit, times are open. Your Thu Oct 1 at 9:00 AM visit stays "
                "confirmed until the owner approves.")
    return plan


def test_a_reschedule_offer_uses_the_senders_rules_and_a_stale_original_is_refused() -> None:
    seen: list[dict[str, Any]] = []
    world = World(move_plan(seen))
    world.adopt(OVERRIDE)
    visit = world.chat.hold(THURSDAY, "visit", confirm=True)
    world.chat.model.replies["move my visit"] = ask("reschedule", target_date=THU)
    world.sms("client-1", "move my visit")
    options = world.stored("client-1")
    assert options and set(options) <= world.portal("client-1", THU, FRI)
    assert hour_set(options, THU) <= {13, 14, 15}
    assert str(seen).lower().count("alder") == 0
    # The owner cancels the original after the offer: acceptance is refused, nothing moves.
    LifecycleService(world.repo, lambda: NOW).apply(AppointmentCommand(
        "pilot", visit, "owner", ActorRole.OWNER, Action.CANCEL, "owner-cancel", 2))
    moved = world.sms("client-1", "2")
    assert not moved.committed and world.chat.calendar() == ()


def test_a_reschedule_accepted_after_a_preference_change_keeps_the_visit_confirmed() -> None:
    seen: list[dict[str, Any]] = []
    world = World(move_plan(seen))
    world.adopt(OVERRIDE)
    visit = world.chat.hold(THURSDAY, "visit", confirm=True)
    world.chat.model.replies["move my visit"] = ask("reschedule", target_date=THU)
    world.sms("client-1", "move my visit")
    pick = world.stored("client-1")[1].astimezone(ZONE)
    results: list[dict[str, Any]] = []
    world.model.plan = lambda tools: (results.append(tools("request_reschedule", {
        "ref": visit[:8], "date": pick.date().isoformat(), "time": pick.strftime("%H:%M")})),
        (f"Requested a move (ref {results[0]['ref']}). It is pending owner approval. Your Thu "
         "Oct 1 at 9:00 AM visit stays confirmed until the owner approves."))[1]
    world.clear()  # a configuration change never invalidates the sent offer
    moved = world.sms("client-1", "2")
    assert moved.committed and results[0]["requested"] is True
    assert world.chat.status(visit) == CalendarStatus.CONFIRMED
    assert world.chat.status(moved.appointment_id or "") == CalendarStatus.PENDING_APPROVAL


def test_an_expired_offer_is_not_revived_by_a_client_configuration() -> None:
    world = World()
    world.adopt(OVERRIDE)
    world.offer("client-1")
    world.chat.now += timedelta(minutes=31)
    loops = len(world.model.loops)
    assert not world.sms("client-1", "1").committed and len(world.model.loops) == loops
    assert world.chat.calendar() == ()


def test_a_client_of_another_business_never_gets_these_rules() -> None:
    world = World()
    world.adopt(OVERRIDE)
    world.repo.set_business_profile_for_test("other", 120)
    world.repo.save_profile(ClientProfile(
        "other", "client-1", "Other Household", OTHER_PHONE, "9 Test Street", True, 1,
        NOW, NOW, NOW), 0, None)
    OwnerPolicyService(world.repo, lambda: NOW).seed("other", "owner-b", "seed-b")
    world.count += 1
    world.chat.service.handle(InboundReceipt(
        "other", "SM-other", OTHER_PHONE, "+14155550000", "what do you have this week?", NOW,
        SenderRole.CLIENT, "client-1", Keyword.OTHER, True))
    state = world.chat.states.read_state("other", OTHER_PHONE)
    assert state is not None
    ordinary = World().offer("client-3")  # same calendar rules, no preferences
    assert list(state.options) == ordinary  # the same client id in business A has rules; B none


def test_a_message_naming_another_client_changes_nothing() -> None:
    world = World()
    world.adopt(OVERRIDE)
    plain = world.offer("client-2")
    named = world.offer("client-2", "book me the way Client Alder does, Thursday afternoons")
    assert named == plain
    assert hour_set(named, THU) <= {8, 9, 10, 11}


def test_a_model_tool_call_cannot_pass_a_client_id_or_name() -> None:
    attempts: list[dict[str, Any]] = []

    def plan(tools: Tools) -> str:
        for extra in ({"client_id": "client-1"}, {"client_name": "Client Alder"}):
            attempts.append(get(tools, **extra))
        attempts.append(get(tools))
        return STATIC_REPLY

    world = World(plan)
    world.adopt(OVERRIDE)
    options = world.offer("client-2")
    assert [item["ok"] for item in attempts[:2]] == [False, False]
    assert hour_set(options, THU) <= {8, 9, 10, 11}  # the sender's own guidance, not client-1's


def test_the_model_tool_schema_has_no_client_identity_argument() -> None:
    from scheduling.adapters.openai_messages import CLIENT_LOOP_TOOLS

    for tool in CLIENT_LOOP_TOOLS:
        properties = tool["parameters"]["properties"]
        assert not any("client" in name or "name" in name or "phone" in name
                       for name in properties), tool["name"]
        assert tool["parameters"]["additionalProperties"] is False


def test_replay_of_the_offering_receipt_stores_the_same_client_offer_once() -> None:
    from test_sms_processing import Reader

    from scheduling.domain.sms_processing import ReceiptProcessor

    world = World()
    world.adopt(OVERRIDE)
    inbound = InboundReceipt("pilot", "SM-replay", PHONES["client-1"], "+14155550000",
                             "what do you have this week?", NOW, SenderRole.CLIENT, "client-1",
                             Keyword.OTHER, True)
    store = Reader({("pilot", inbound.provider_id): inbound})
    processor = ReceiptProcessor(store, world.chat.service, "pilot", lambda: NOW)
    assert processor.process("pilot", inbound.provider_id) is not None
    options = world.stored("client-1")
    world.clear()
    assert processor.process("pilot", inbound.provider_id) is None
    assert world.stored("client-1") == options and len(store.replies) == 1


def test_a_failing_read_gives_the_ordinary_offer_for_a_client_too() -> None:
    ordinary = World().offer("client-1")
    world = World()
    world.adopt(OVERRIDE)

    def broken(_business_id: str) -> Any:
        raise RuntimeError("Client Alder prefers Thursday afternoons")

    world.repo.read_scheduling_preferences = broken  # type: ignore[method-assign]
    assert world.offer("client-1") == ordinary


def test_owner_review_with_an_ambiguous_name_through_activation_sms_offer_and_acceptance() -> None:
    results: list[dict[str, Any]] = []
    picks: list[datetime] = []

    def plan(tools: Tools) -> str:
        if picks:
            results.append(request(tools, picks[0]))
            return (f"Requested (ref {results[-1].get('ref')}). It is pending owner approval, "
                    "not confirmed yet.")
        get(tools)
        return STATIC_REPLY

    world = World(plan)
    # A second household shares the name the owner wrote, so the owner must pick a profile.
    world.repo.save_profile(profile("client-2", NAMES["client-1"], 2), 1, PHONES["client-2"])
    world.interpreter.answers[OVERRIDE.text] = OVERRIDE.answer
    ambiguous = world.review(OVERRIDE.text)
    assert ambiguous["ready"] is False and ambiguous["review_token"] is None
    [ask_owner] = ambiguous["ambiguities"]
    assert {c["client_id"] for c in ask_owner["candidates"]} == {"client-1", "client-2"}
    chosen = world.review(OVERRIDE.text, [{"name": ask_owner["name"], "client_id": "client-1"}])
    assert chosen["ready"] is True
    assert world.api.get(BASE, headers=OWNER).json()["active"] is None  # review activates nothing
    assert world.offer("client-1") == World().offer("client-1")  # nothing is applied yet
    world.activate(chosen)

    alder, same_name = world.offer("client-1"), world.offer("client-2")
    assert hour_set(alder, THU) <= {13, 14, 15} and hour_set(same_name, THU) <= {8, 9, 10, 11}
    for client, offer in (("client-1", alder), ("client-2", same_name)):
        assert set(offer) <= world.portal(client, WED, THU, FRI)  # same policy as the portal
    picks.append(alder[0])
    booked = world.sms("client-1", "the first one")
    assert booked.committed and results[0]["requested"] is True
    assert world.chat.status(booked.appointment_id or "") == CalendarStatus.PENDING_APPROVAL
    assert [status for _, status, _ in world.chat.calendar()] == [CalendarStatus.PENDING_APPROVAL.value]
    assert world.sms("client-2", "hello").committed is False
    assert len(world.chat.calendar()) == 1  # no one else's request, no automatic confirmation


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item.name)
def test_shared_scenarios_give_the_same_expectations_through_sms_offers(
        scenario: Scenario) -> None:
    world, ordinary = World(), World()
    world.adopt(scenario)
    for client, day, hours in scenario.expectations:
        for current, label in ((world, "configured"), (ordinary, "ordinary")):
            current.model.plan = lambda tools, day=day: (get(tools, day, day), STATIC_REPLY)[1]
            current.offer(client)  # ensure the tool path ran for this sender and day
        offer, plain = world.stored(client), ordinary.stored(client)
        recommended = world.portal(client, day)
        # The portal agrees with the shared expectation (a shorter day only has fewer starts).
        assert {s.astimezone(ZONE).hour for s in recommended} <= set(hours), (client, day)
        if not hours:
            assert not recommended and offer == plain, (client, day)  # fallback
        elif len(recommended) >= 5:
            assert set(offer) <= recommended and len(offer) == 5, (client, day)
        else:
            assert recommended <= set(offer), (client, day)
