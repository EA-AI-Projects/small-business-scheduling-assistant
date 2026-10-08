"""Bounded SMS drafting from backend-owned scheduling facts (client replies, owner answers)."""

import re
from dataclasses import dataclass

GSM_EXTENSION = frozenset("^{}\\[~]|")
GSM_BASIC = frozenset(
    " @£$¥èéùìòÇØøÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ!\"#%&'()*+,-./"
    "0123456789:;<=>?ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
DATE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\s+)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,?\s+\d{4})?)\b", re.IGNORECASE)
TIME = re.compile(r"\b(?:\d{1,2}:\d{2}\s*(?:AM|PM)?|\d{1,2}\s*(?:AM|PM))\b", re.IGNORECASE)
REF = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b|\b[0-9a-f]{8}\b", re.IGNORECASE)


def gsm_septets(text: str, line_breaks: bool = False) -> int | None:
    """Return encoded length for printable GSM-7, or None for unsupported text.

    Client drafts are one line; owner pages (``line_breaks``) are multi-line.
    """
    total = 0
    for char in text:
        if char in GSM_BASIC or (line_breaks and char == "\n"):
            total += 1
        elif char in GSM_EXTENSION:
            total += 2
        else:
            return None
    return total


@dataclass(frozen=True)
class ClientReplyFact:
    """An authorized local date, optional time/reference, and state."""

    date: str
    time: str | None
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
    # Fixed backend text appended verbatim after an owner draft (#285): exact commands, paging
    # footers, and the open-offer reminder are never left to the model. Client results use none.
    suffix: str = ""

    @property
    def references(self) -> tuple[str, ...]:
        return tuple(fact.reference for fact in self.facts if fact.reference is not None)


def _explicit(draft: str) -> tuple[set[str], set[str], set[str]]:
    return ({match.group().lower() for match in DATE.finditer(draft)},
            {match.group().lower().replace(" ", "") for match in TIME.finditer(draft)},
            {match.group().lower() for match in REF.finditer(draft)})


def _authorized(result: ClientReplyResult) -> tuple[set[str], set[str], set[str]]:
    return ({fact.date.lower() for fact in result.facts if fact.date},
            {fact.time.lower().replace(" ", "") for fact in result.facts
             if fact.time is not None},
            {ref.lower() for ref in result.references})


def valid_draft(draft: str, result: ClientReplyResult) -> bool:
    """Check delivery size and explicit facts; wording follows the model instructions."""
    size = gsm_septets(draft)
    if size is None or size > 160 or not draft.strip():
        return False
    return _explicit(draft) == _authorized(result)


# Owner answers may use the three-segment cap the owner thread already uses for calendar pages.
OWNER_REPLY_LIMIT = 480
DRAFTABLE_OWNER_KINDS = frozenset({"owner_calendar", "owner_requests", "owner_how_to"})
YEAR = re.compile(r",?\s+\d{4}$")
# A draft may not claim a finished action or invent a prompt the backend did not ask.
FORBIDDEN_OWNER_CLAIMS = re.compile(
    r"\b(?:approved|declined|cancell?ed|booked)\b"
    r"|\b(?:was|were|been|i|we|i've|we've)\s+sent\b|\bsent\s+(?:it|the|that|your)\b"
    r"|\breply\s+(?:yes|no|y|n|more)\b", re.IGNORECASE)


def facts_from_text(text: str, status: str = "listed") -> tuple[ClientReplyFact, ...]:
    """The explicit dates, times, and references of backend-built text, as authorized facts."""
    dates, times, refs = (list(dict.fromkeys(
        match.group() for match in pattern.finditer(text)))
        for pattern in (DATE, TIME, REF))
    return (tuple(ClientReplyFact(day, None, status) for day in dates)
            + tuple(ClientReplyFact("", clock, status) for clock in times)
            + tuple(ClientReplyFact("", None, status, ref) for ref in refs))


def valid_owner_draft(draft: str, result: ClientReplyResult) -> bool:
    """Check an owner draft that the backend will follow with ``result.suffix`` unchanged.

    Dates are compared without the year (the backend writes it once in a range header) but
    with the weekday. The draft plus the fixed suffix stays within the owner reply cap.
    """
    size, fixed = gsm_septets(draft, True), gsm_septets(result.suffix, True)
    if (result.kind not in DRAFTABLE_OWNER_KINDS or size is None or fixed is None
            or not draft.strip() or size + fixed > OWNER_REPLY_LIMIT
            or FORBIDDEN_OWNER_CLAIMS.search(draft)):
        return False
    dates, times, refs = _explicit(draft)
    allowed_dates, allowed_times, allowed_refs = _authorized(result)
    return ({YEAR.sub("", day) for day in dates} == {YEAR.sub("", day) for day in allowed_dates}
            and times == allowed_times and refs == allowed_refs)
