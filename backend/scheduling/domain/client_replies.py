"""Facts a client SMS draft may state after a validated scheduling action."""

import re
from dataclasses import dataclass


DATE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?|(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\s+)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2})\b", re.I)
TIME = re.compile(r"\b(?:\d{1,2}:\d{2}\s*(?:AM|PM)?|\d{1,2}\s*(?:AM|PM))\b", re.I)
REF = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b|\b[0-9a-f]{8}\b", re.I)
RELATIVE = re.compile(r"\b(?:today|tomorrow|tonight|next\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*)\b", re.I)
CONFIRMED = re.compile(r"\b(?:confirmed|booked|scheduled|reserved|locked\s+in)\b", re.I)
PENDING = re.compile(r"\b(?:pending|awaiting|waiting\s+for)\b.*\b(?:owner|approval)\b|\b(?:owner|approval)\b.*\b(?:pending|awaiting|required|needed)\b", re.I)
NOT_CHANGED = re.compile(r"\b(?:nothing|no\s+visit|no\s+request)\b.*\b(?:booked|cancelled|canceled|changed)\b", re.I)


@dataclass(frozen=True)
class ClientReplyResult:
    """Small, backend-owned result; fallback is the existing safe wording."""

    kind: str
    status: str
    fallback: str
    dates: tuple[str, ...] = ()
    times: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    reason: str | None = None

    @classmethod
    def from_safe_text(cls, kind: str, status: str, fallback: str,
                       reason: str | None = None) -> "ClientReplyResult":
        """Extract only tokens the backend already rendered from trusted state."""
        return cls(kind, status, fallback,
                   tuple(dict.fromkeys(match.group().lower() for match in DATE.finditer(fallback))),
                   tuple(dict.fromkeys(match.group().lower().replace(" ", "")
                                       for match in TIME.finditer(fallback))),
                   tuple(dict.fromkeys(match.group().lower() for match in REF.finditer(fallback))),
                   reason)


def valid_draft(draft: str, result: ClientReplyResult) -> bool:
    """Fail closed on unsupported facts or unsafe action claims."""
    if (not draft or len(draft) > 160 or not draft.isascii()
            or draft.count("?") > 1 or "\n" in draft):
        return False
    dates = {match.group().lower() for match in DATE.finditer(draft)}
    times = {match.group().lower().replace(" ", "") for match in TIME.finditer(draft)}
    refs = {match.group().lower() for match in REF.finditer(draft)}
    if (not dates.issubset(result.dates) or not times.issubset(result.times)
            or not refs.issubset(result.references)
            or not set(result.references).issubset(refs)):
        return False
    if {m.group().lower() for m in RELATIVE.finditer(draft)} - {
            m.group().lower() for m in RELATIVE.finditer(result.fallback)}:
        return False
    positive = re.sub(r"\b(?:nothing|no\s+(?:visit|request))\s+(?:was\s+)?(?:booked|confirmed|scheduled|reserved)\b",
                      "", draft, flags=re.I)
    if result.status != "confirmed" and CONFIRMED.search(positive):
        return False
    cancellation_claim = re.sub(r"\b(?:nothing|no\s+(?:visit|request))\s+(?:was\s+)?cancell?ed\b",
                                "", draft, flags=re.I)
    if result.kind != "cancelled" and re.search(r"\bcancell?ed\b", cancellation_claim, re.I):
        return False
    if result.status == "pending" and not PENDING.search(draft):
        return False
    if result.kind in {"failed", "request_failed", "expired", "nothing_changed"} and not NOT_CHANGED.search(draft):
        return False
    if result.kind == "cancelled" and not re.search(r"\bcancell?ed\b", draft, re.I):
        return False
    return True
