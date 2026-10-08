"""Bounded client SMS drafting from backend-owned scheduling facts."""

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


def valid_draft(draft: str, result: ClientReplyResult) -> bool:
    """Check delivery size and explicit facts; wording follows the model instructions."""
    size = gsm_septets(draft)
    if size is None or size > 160 or not draft.strip():
        return False
    dates = {match.group().lower() for match in DATE.finditer(draft)}
    times = {match.group().lower().replace(" ", "") for match in TIME.finditer(draft)}
    refs = {match.group().lower() for match in REF.finditer(draft)}
    return (dates == {fact.date.lower() for fact in result.facts}
            and times == {fact.time.lower().replace(" ", "") for fact in result.facts}
            and refs == {ref.lower() for ref in result.references})
