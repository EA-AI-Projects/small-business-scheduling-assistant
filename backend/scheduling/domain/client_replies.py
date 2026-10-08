"""Bounded client SMS drafting from backend-owned scheduling facts."""

import re
from dataclasses import dataclass
from itertools import pairwise

GSM_EXTENSION = frozenset("^{}\\[~]|")
GSM_BASIC = frozenset(
    " @£$¥èéùìòÇØøÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ!\"#%&'()*+,-./"
    "0123456789:;<=>?ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
DATE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\s+)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2})\b", re.IGNORECASE)
TIME = re.compile(r"\b(?:\d{1,2}:\d{2}\s*(?:AM|PM)?|\d{1,2}\s*(?:AM|PM))\b", re.IGNORECASE)
REF = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b|\b[0-9a-f]{8}\b", re.IGNORECASE)
RELATIVE = re.compile(r"\b(?:today|tomorrow|tonight|next\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*)\b", re.IGNORECASE)
CONFIRMED = re.compile(r"\b(?:confirmed|booked|scheduled|reserved|approved|accepted|locked\s+in)\b", re.IGNORECASE)
PENDING = re.compile(r"\b(?:pending|awaiting|waiting\s+for|needs?|requires?)\b.{0,35}\b(?:owner|approval)\b|\bowner approval\b.{0,20}\b(?:needed|required|pending)\b", re.IGNORECASE)
CANCELLED = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
NEGATIVE = re.compile(r"\b(?:no|not|never|cannot|can't|wasn't|isn't|didn't|unavailable|closed|rejected|declined)\b", re.IGNORECASE)
NO_VISITS = re.compile(r"\b(?:no|zero|none)\s+(?:upcoming\s+)?(?:visits|appointments|bookings|requests)\b", re.IGNORECASE)
NO_CHANGE = re.compile(r"\b(?:nothing|no\s+(?:visit|request))\b.{0,35}\b(?:booked|cancelled|canceled|changed)\b", re.IGNORECASE)


def gsm_septets(text: str) -> int | None:
    """Return encoded length for printable GSM-7, or None for unsupported text."""
    total = 0
    for char in text:
        if char in GSM_BASIC:
            total += 1
        elif char in GSM_EXTENSION:
            total += 2
        else:
            return None
    return total


@dataclass(frozen=True)
class ClientReplyFact:
    """One inseparable local time, reference, and state from an authorized result."""

    date: str
    time: str
    status: str
    reference: str | None = None


@dataclass(frozen=True)
class ClientReplyResult:
    kind: str
    status: str
    fallback: str
    facts: tuple[ClientReplyFact, ...] = ()
    reason: str | None = None
    detail: str | None = None

    @property
    def references(self) -> tuple[str, ...]:
        return tuple(fact.reference for fact in self.facts if fact.reference is not None)


def _fact_segments(draft: str, facts: tuple[ClientReplyFact, ...]) -> tuple[str, ...] | None:
    """Locate each complete local date-time pair and its following reference/status."""
    found: list[tuple[int, int, ClientReplyFact]] = []
    for fact in facts:
        pair = re.search(rf"\b{re.escape(fact.date)}\s+at\s+{re.escape(fact.time)}\b",
                         draft, re.IGNORECASE)
        if pair is None or re.search(rf"\b{re.escape(fact.date)}\s+at\s+{re.escape(fact.time)}\b",
                                     draft[pair.end():], re.IGNORECASE):
            return None
        found.append((pair.start(), pair.end(), fact))
    found.sort(key=lambda item: item[0])
    if any(first[1] > second[0] for first, second in pairwise(found)):
        return None
    segments: list[str] = []
    for index, (_, start, fact) in enumerate(found):
        end = found[index + 1][0] if index + 1 < len(found) else len(draft)
        segment = draft[start:end]
        if fact.reference is not None and not re.search(
                rf"\b{re.escape(fact.reference)}\b", segment, re.IGNORECASE):
            return None
        segments.append(segment)
    return tuple(segments)


def valid_draft(draft: str, result: ClientReplyResult) -> bool:
    """Reject observed false claims; unrecognized prose falls back conservatively."""
    size = gsm_septets(draft)
    if size is None or size > 160 or draft.count("?") > 1 or not draft.strip():
        return False
    if any(fact.status not in {"pending owner approval", "confirmed", "cancelled",
                               "open", "unavailable"} for fact in result.facts):
        return False
    dates = {match.group().lower() for match in DATE.finditer(draft)}
    times = {match.group().lower().replace(" ", "") for match in TIME.finditer(draft)}
    refs = {match.group().lower() for match in REF.finditer(draft)}
    if (dates != {fact.date.lower() for fact in result.facts}
            or times != {fact.time.lower().replace(" ", "") for fact in result.facts}
            or refs != {ref.lower() for ref in result.references}
            or RELATIVE.search(draft)):
        return False
    segments = _fact_segments(draft, result.facts)
    if segments is None:
        return False
    for fact, segment in zip(sorted(result.facts, key=lambda item: draft.lower().find(
            f"{item.date} at {item.time}".lower())), segments):
        if fact.status == "pending owner approval":
            checked = re.sub(r"\bnot\s+confirmed(?:\s+yet)?\b", "", segment,
                             flags=re.IGNORECASE)
            if (not PENDING.search(segment) or CONFIRMED.search(checked)
                    or CANCELLED.search(checked) or NEGATIVE.search(checked)):
                return False
        elif fact.status == "confirmed":
            truthful_status = (re.search(r"\bconfirmed\b", segment, re.IGNORECASE)
                                or (result.kind == "cancel_kept" and re.search(
                                    r"\b(?:kept|keep|stays(?:\s+booked)?)\b", draft,
                                    re.IGNORECASE)))
            checked = (re.sub(r"\bnothing was cancell?ed\b", "", segment,
                              flags=re.IGNORECASE) if result.kind == "cancel_kept"
                       else segment)
            if (not truthful_status
                    or NEGATIVE.search(checked) or PENDING.search(checked)
                    or CANCELLED.search(checked)):
                return False
        elif fact.status == "cancelled":
            claim = CANCELLED.search(segment) or (result.kind == "cancelled"
                                                 and len(result.facts) == 1
                                                 and CANCELLED.search(draft))
            if not claim or NEGATIVE.search(segment):
                return False
        elif fact.status == "open":
            if not re.search(r"\b(?:open|available|can offer)\b", segment,
                             re.IGNORECASE) or NEGATIVE.search(segment):
                return False
        elif fact.status == "unavailable" and (
                not re.search(r"\b(?:unavailable|no longer open|taken)\b", segment,
                              re.IGNORECASE) or re.search(r"\b(?:is|now) open\b", segment,
                                                        re.IGNORECASE)):
            return False
    lead = draft[:min((draft.lower().find(f"{fact.date} at {fact.time}".lower())
                       for fact in result.facts), default=len(draft))]
    if result.kind == "offer_made":
        return (not NEGATIVE.search(lead) and not CONFIRMED.search(draft)
                and not CANCELLED.search(draft) and not NO_VISITS.search(draft))
    if result.kind in {"request_created", "move_requested"}:
        without_pending_note = re.sub(r"\bnot\s+confirmed(?:\s+yet)?\b", "", draft,
                                      flags=re.IGNORECASE)
        return (not re.search(r"\b(?:rejected|declined|cancelled|canceled|failed)\b", draft,
                              re.IGNORECASE) and not NEGATIVE.search(lead)
                and not CONFIRMED.search(lead)
                and (result.kind != "move_requested" or not re.search(
                    r"\b(?:booked|approved|accepted|reserved|scheduled|locked\s+in)\b",
                    draft, re.IGNORECASE))
                and (not CONFIRMED.search(without_pending_note)
                     if not any(fact.status == "confirmed" for fact in result.facts) else True)
                and bool(PENDING.search(draft)))
    if result.kind == "cancelled":
        contrary = re.search(
            r"\b(?:kept|keep|active|stays|remains|scheduled|confirmed|pending|booked)\b",
            draft, re.IGNORECASE)
        return ("?" not in draft and bool(CANCELLED.search(draft))
                and not NEGATIVE.search(lead) and not contrary)
    if result.kind == "calendar_list":
        if CONFIRMED.search(lead) or PENDING.search(lead) or CANCELLED.search(lead):
            return False
        return not NO_VISITS.search(draft) if result.facts else bool(NO_VISITS.search(draft))
    if result.kind in {"request_failed", "expired", "nothing_changed"}:
        remainder = NO_CHANGE.sub("", draft)
        return (bool(NO_CHANGE.search(draft)) and not CONFIRMED.search(remainder)
                and not CANCELLED.search(remainder))
    if result.kind == "cancel_kept":
        remainder = re.sub(r"\bnothing was cancell?ed\b", "", draft,
                           flags=re.IGNORECASE)
        return bool(re.search(r"\b(?:kept|keep|stays)\b", draft, re.IGNORECASE)
                    and re.search(r"\bnothing was cancell?ed\b", draft, re.IGNORECASE)
                    and not NEGATIVE.search(lead)
                    and not CANCELLED.search(remainder))
    if result.kind == "cancel_question":
        return draft.count("?") == 1 and not re.search(r"\b(?:was|is) cancell?ed\b", draft,
                                                       re.IGNORECASE)
    return (result.kind == "clarification" and draft.count("?") == 1
            and not CONFIRMED.search(draft) and not CANCELLED.search(draft)
            and not PENDING.search(draft))
