"""Model framing for read-only owner replies (#285).

The owner acts on exact commands and references taken from these texts, so the model never
writes a fact. It may write one short opening sentence; the backend's own text (entries,
counts, dates, times, names, references, and the exact instructions) follows it unchanged.
The opening is accepted only if it carries no fact or instruction at all.
"""

import re
from itertools import pairwise

from scheduling.domain.client_replies import gsm_septets

DRAFTABLE_OWNER_KINDS = frozenset({"owner_calendar", "owner_requests", "owner_how_to"})
MAX_FRAMING_LENGTH = 100
OWNER_REPLY_LIMIT = 480  # Three GSM-7 segments, the owner thread's cap for these replies.
OPENERS = frozenset({
    "here", "here's", "sure", "okay", "ok", "certainly", "absolutely", "alright", "happy",
    "glad", "got", "thanks", "thank", "i", "i'm", "i've", "i'll", "let", "this", "that",
    "below", "hi", "hello", "right", "checked", "of"})
ALLOWED_CHARACTERS = re.compile(r"[A-Za-z ,.!?'-]+")
# Anything that names a day, time, count, status, action, prompt, command, or capability.
FORBIDDEN = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b"
    r"|\b(?:today|tomorrow|tonight|yesterday|next|last|week|weekend|month|year|morning|"
    r"afternoon|evening|noon|midnight|later|soon|now|already|earlier)\b"
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|zero|none|"
    r"no|nothing|any|all|every|both|first|second|third|only)\b"
    r"|\b(?:yes|y|n|more|approv\w*|declin\w*|confirm\w*|cancel\w*|accept\w*|"
    r"reject\w*|deny|denied|book\w*|sen[dt]\w*|text\w*|messag\w*|offer\w*|schedul\w*|"
    r"free|open\w*|availab\w*|pending|unavailab\w*|block\w*|set|done|went|queue\w*|"
    r"reply|respond\w*|answer\w*|type|say|command|ref\w*|can|could|will|would|able|also|"
    r"should|must|need|please|resched\w*|move\w*|change\w*|add\w*|remind\w*|sending)\b",
    re.IGNORECASE)


def framed(text: str, fallback: str) -> str | None:
    """The reply with ``text`` as its opening and ``fallback`` after it, or None.

    ``fallback`` is the backend's complete existing reply and is returned inside the result
    byte for byte, exactly once. ``None`` means keep the existing text.
    """
    opening = text.strip()
    words = opening.split(" ")
    if (not opening or len(opening) > MAX_FRAMING_LENGTH or gsm_septets(opening) is None
            or not ALLOWED_CHARACTERS.fullmatch(opening)
            or words[0].strip(",.!?").lower() not in OPENERS
            or any(word[:1].isupper() and word not in ("I", "I'm", "I've", "I'll")
                   and not previous.endswith((".", "!", "?"))  # A new sentence's first word.
                   for previous, word in pairwise(words))
            or FORBIDDEN.search(opening)):
        return None
    reply = f"{opening}\n{fallback}"
    return reply if len(fallback) <= OWNER_REPLY_LIMIT - 1 - len(opening) else None
