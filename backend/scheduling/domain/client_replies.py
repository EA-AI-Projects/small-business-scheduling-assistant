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
# A draft may not claim a finished action (existing check, deliberately not widened) ...
FORBIDDEN_OWNER_CLAIMS = re.compile(
    r"\b(?:approved|declined|cancell?ed|booked)\b"
    r"|\b(?:was|were|been|i|we|i've|we've)\s+sent\b|\bsent\s+(?:it|the|that|your)\b",
    re.IGNORECASE)
# ... nor write its own reply prompt: an open offer's YES is honored by the counteroffer
# handler, and the backend's fixed suffix already carries every instruction the owner needs.
_ANSWER = r"(?:yes|no|y|n|more)"
OWNER_PROMPTS = re.compile(
    rf"\b(?:reply|respond|answer|text|type|say|send)\b[^.\n]{{0,24}}?\b{_ANSWER}\b"
    rf"|[\"'\u201c\u2018]\s*{_ANSWER}\s*[\"'\u201d\u2019]"
    r"|\b(?:reply|respond|text|send)\b[^.\n]{0,24}?\b(?:approve|decline)\b"
    r"|\b(?:approve|decline)\s+[0-9a-f]{8}\b", re.IGNORECASE)
SEGMENTS = re.compile(r"[\n;]|(?<=[.!?])\s+")


def facts_from_text(text: str, status: str = "listed") -> tuple[ClientReplyFact, ...]:
    """The explicit dates, times, and references of backend-built text, as authorized facts."""
    dates, times, refs = (list(dict.fromkeys(
        match.group() for match in pattern.finditer(text)))
        for pattern in (DATE, TIME, REF))
    return (tuple(ClientReplyFact(day, None, status) for day in dates)
            + tuple(ClientReplyFact("", clock, status) for clock in times)
            + tuple(ClientReplyFact("", None, status, ref) for ref in refs))


def _core(day: str) -> str:
    return YEAR.sub("", day.lower())


def _dates_ok(drafted: set[str], allowed: set[str]) -> bool:
    """Every drafted date is a backend date (an explicit year must be the backend's own),
    and every backend date appears (the year of a range header is optional)."""
    return (all(day in allowed if YEAR.search(day) else _core(day) in {_core(a) for a in allowed}
                for day in drafted)
            and {_core(day) for day in allowed} == {_core(day) for day in drafted})


def _entries(backend: str) -> list[tuple[set[str], set[str], str]]:
    """Each backend line or clause that names a request reference: its dates, times, ref."""
    found = []
    for part in SEGMENTS.split(backend):
        dates, times, refs = _explicit(part)
        if len(refs) == 1:
            found.append((dates, times, next(iter(refs))))
    return found


def valid_owner_draft(draft: str, result: ClientReplyResult) -> bool:
    """Basic check of a free-wording owner draft; the backend then appends ``result.suffix``.

    Like the client check: one GSM-7 budget (the draft plus the suffix within 480), every
    explicit date, time, and reference is exactly one of the backend's and every backend one
    appears. Per entry: a draft sentence, line, or clause naming a reference may carry only
    the dates and times of the backend entries it names, and each entry's reference appears
    with its own date and time, so two requests cannot swap. Prose is not otherwise checked
    (owner decision on #285, 2026-10-08).
    """
    size, fixed = gsm_septets(draft, True), gsm_septets(result.suffix, True)
    if (result.kind not in DRAFTABLE_OWNER_KINDS or size is None or fixed is None
            or not draft.strip() or size + fixed > OWNER_REPLY_LIMIT
            or FORBIDDEN_OWNER_CLAIMS.search(draft) or OWNER_PROMPTS.search(draft)):
        return False
    dates, times, refs = _explicit(draft)
    allowed_dates, allowed_times, allowed_refs = _authorized(result)
    if not (_dates_ok(dates, allowed_dates) and times == allowed_times
            and refs == allowed_refs):
        return False
    entries = _entries(result.detail or "")
    segments = [_explicit(part) for part in SEGMENTS.split(draft)]
    for entry_dates, entry_times, ref in entries:
        cores = {_core(day) for day in entry_dates}
        if not any(ref in seg_refs and cores <= {_core(day) for day in seg_dates}
                   and entry_times <= seg_times
                   for seg_dates, seg_times, seg_refs in segments):
            return False
    owned = {ref: (entry_dates, entry_times) for entry_dates, entry_times, ref in entries}
    for seg_dates, seg_times, seg_refs in segments:
        if seg_refs:
            ok_dates = {_core(day) for ref in seg_refs for day in owned.get(ref, (set(), set()))[0]}
            ok_times = {clock for ref in seg_refs for clock in owned.get(ref, (set(), set()))[1]}
            if ({_core(day) for day in seg_dates} - ok_dates or seg_times - ok_times):
                return False
    return True
