"""Bounded application-owned SMS context for a verified conversation actor."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from scheduling.domain.sms_ingress import SenderRole

WINDOW = timedelta(hours=24)
MAX_MESSAGES = 24
MAX_CHARACTERS = 12_000


@dataclass(frozen=True)
class HistoryMessage:
    provider_id: str
    role: str  # "assistant" or the verified sender's role
    at: datetime
    text: str
    invitation: bool = False


def bounded_history(messages: list[HistoryMessage], now: datetime,
                    actor: SenderRole) -> tuple[HistoryMessage, ...]:
    """Keep the latest exchange and latest invitation; omit older whole messages.

    A message is never shortened, because shortened text would misrepresent what
    the recipient saw. A single oversized text is omitted. The returned order is
    chronological, with provider ID breaking timestamp ties.
    """
    if now.tzinfo is None or actor not in (SenderRole.CLIENT, SenderRole.OWNER):
        raise ValueError("History needs an aware clock and verified actor")
    cutoff = now.astimezone(UTC) - WINDOW
    eligible = sorted((m for m in messages if m.text and m.at.tzinfo is not None
                       and cutoff <= m.at.astimezone(UTC) <= now.astimezone(UTC)
                       and m.role in ("assistant", actor.value)
                       and len(m.text) <= MAX_CHARACTERS),
                      key=lambda m: (m.at, m.provider_id))
    chosen: list[HistoryMessage] = []
    used = 0
    for message in reversed(eligible):
        if len(chosen) == MAX_MESSAGES:
            break
        if used + len(message.text) <= MAX_CHARACTERS:
            chosen.append(message)
            used += len(message.text)
    if actor == SenderRole.CLIENT:
        invitation = next((m for m in reversed(eligible) if m.invitation), None)
        if invitation is not None and invitation not in chosen:
            # Preserve the relevant invitation by removing oldest non-invitation
            # context. The latest messages remain preferred if it cannot fit.
            while chosen and (len(chosen) == MAX_MESSAGES
                              or used + len(invitation.text) > MAX_CHARACTERS):
                candidate = chosen[-1]
                if candidate == eligible[-1]:
                    break
                chosen.pop()
                used -= len(candidate.text)
            if len(chosen) < MAX_MESSAGES and used + len(invitation.text) <= MAX_CHARACTERS:
                chosen.append(invitation)
    return tuple(sorted(chosen, key=lambda m: (m.at, m.provider_id)))
