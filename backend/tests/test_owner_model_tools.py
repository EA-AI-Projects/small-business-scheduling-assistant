"""Model-selected counteroffer tools at the owner conversation boundary (#300).

The fake model chooses tool arguments; stored state decides what can be sent.
"""

from datetime import timedelta

import pytest
from test_owner_counteroffer import (
    ASK,
    OWNER,
    THURSDAY_9AM,
    World,
    make_world,
)

from scheduling.domain.calendar import CalendarStatus
from scheduling.domain.owner_counteroffer import OfferState


@pytest.fixture(name="world")
def _world_fixture() -> World:
    return make_world()


def test_model_can_select_named_request_among_several(world: World) -> None:
    first = world.pending(key="a")
    second = world.pending(THURSDAY_9AM + timedelta(days=1), "client-2", "b")
    world.model.ref_override = second[:8]
    world.model.date = "2026-10-02"
    prompt = world.say(ASK)
    active = world.store.read_active("pilot", OWNER)
    assert active is not None and active.request_id == second
    assert "Blake" in prompt.text and not world.store.outbox
    assert world.status(first) == world.status(second) == (
        CalendarStatus.PENDING_APPROVAL, 1)


def test_model_draft_text_is_stored_verbatim_and_delivered(world: World) -> None:
    world.pending()
    world.model.client_text = "Hi Avery, 2 PM Thursday is open. Reply YES to request that time."
    prompt = world.say(ASK)
    active = world.store.read_active("pilot", OWNER)
    assert active is not None and active.text == world.model.client_text
    assert world.model.client_text in prompt.text
    world.say("YES")
    world.deliver()
    assert world.messages.calls[0]["body"] == world.model.client_text


def test_model_cancel_is_read_only_for_request(world: World) -> None:
    request = world.pending()
    world.say(ASK)
    answer = world.say("Cancel offer")
    assert "cancelled" in answer.text
    assert world.store.read_active("pilot", OWNER).state == OfferState.DISCARDED  # type: ignore[union-attr]
    assert not world.store.outbox
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)


def test_invalid_client_sms_is_refused_without_a_draft(world: World) -> None:
    request = world.pending()
    world.model.client_text = "Avery 😀"
    answer = world.say(ASK)
    assert "did not prepare" in answer.text and "Nothing was sent" in answer.text
    assert world.store.read_active("pilot", OWNER) is None
    assert world.status(request) == (CalendarStatus.PENDING_APPROVAL, 1)
