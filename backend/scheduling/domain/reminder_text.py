"""Deterministic checks of the inbound text behind a reminder write (#402).

These are guards, not interpretation: the model still reads the request. They stop a write
whose message only acknowledges something, and a time write whose current message leaves the
hour ambiguous or disagrees with the time the model supplied. They do not understand other
languages' time words, and they cannot catch a model-supplied time when the current message
names no time at all; the acknowledgment states the saved time so the client can correct it.
"""
import re
from datetime import time

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_W = "|".join(_WORDS)
_H = rf"\d{{1,2}}(?:[:.]\d{{2}})?|{_W}"
_START = r"(?<![\w:.])"
_DAY_QUANTITY = re.compile(rf"\b(?:\d+|{_W})\s*(?:calendar\s+)?days?\b|\b(?:a|the)\s+day\b")
_MERIDIEM = re.compile(rf"{_START}({_H})\s*([ap])\.?m\b\.?")
_TWENTY_FOUR = re.compile(rf"{_START}(0\d|1[3-9]|2[0-3]):([0-5]\d)(?!\d)")
_NOON = re.compile(rf"{_START}(?:12\s+)?noon\b")
_PART = r"morning|afternoon|evening|night"
_HOUR_PART = re.compile(
    rf"{_START}({_H})\s*(?:o'?clock\s*)?(?:in the |at |on the |this )?({_PART})\b")
_PART_HOUR = re.compile(rf"\b({_PART})\s*(?:at |around )?({_H})(?![\d:.])")
_DAY_PART = re.compile(rf"\b({_PART})\b")
_DIGITS = re.compile(rf"{_START}\d{{1,4}}(?:[:.]\d{{2}})?(?!\d)")
_WORD_HOUR = re.compile(
    rf"\b(?:{_W})\b(?!\s+(?:days?|reminders?|texts?|messages?|times?|hours?|minutes?|weeks?))")
_ACKNOWLEDGMENT = frozenset({
    "yes", "yeah", "yep", "yup", "y", "ok", "okay", "k", "sure", "si", "sí", "oui", "please",
    "pls", "do", "it", "go", "ahead", "absolutely", "definitely", "certainly", "thanks",
    "thank", "you", "sounds", "good", "great", "fine", "no", "nope", "nah", "n"})


def acknowledgment_only(body: str) -> bool:
    """True for texts with no letters or digits (an emoji) or only acknowledgment words."""
    if not any(char.isalnum() for char in body):
        return True
    if any(char.isdigit() for char in body):
        return False
    return all(word in _ACKNOWLEDGMENT for word in re.findall(r"[^\W\d_]+", body.lower()))


def _clock(token: str) -> tuple[int, int]:
    if token in _WORDS:
        return _WORDS[token], 0
    hour, *minute = re.split(r"[:.]", token)
    return int(hour), int(minute[0]) if minute else 0


def unclear_time(body: str, value: time) -> bool:
    """True when the current message cannot back the model's ``value``.

    Ambiguous: an hour with no AM/PM, noon, 24-hour form or adjacent day part ("9", "at 9",
    "9ish", "900", "nine", "9:30", "I work in the morning, remind me at 9"). Contradictory:
    a clear time or day part in the text that differs from ``value``. Day quantities ("two
    days") are not hours; a text with no hour at all ("morning") is not ambiguous.
    """
    text = _DAY_QUANTITY.sub(" ", body.lower())
    clear: list[tuple[int, int]] = []

    def take_meridiem(match: re.Match[str]) -> str:
        hour, minute = _clock(match[1])
        clear.append((hour % 12 + (12 if match[2] == "p" else 0), minute))
        return " "

    def take_part(match: re.Match[str], hour_group: int, part_group: int) -> str:
        hour, minute = _clock(match[hour_group])
        clear.append((hour % 12 + (12 if match[part_group] != "morning" else 0), minute))
        return " "

    text = _MERIDIEM.sub(take_meridiem, text)
    for match in _TWENTY_FOUR.finditer(text):
        clear.append((int(match[1]), int(match[2])))
    text = _TWENTY_FOUR.sub(" ", text)
    if _NOON.search(text):
        clear.append((12, 0))
        text = _NOON.sub(" ", text)
    text = _HOUR_PART.sub(lambda m: take_part(m, 1, 2), text)
    text = _PART_HOUR.sub(lambda m: take_part(m, 2, 1), text)
    parts = set(_DAY_PART.findall(text))
    if _DIGITS.search(text) or (not clear and _WORD_HOUR.search(text)):
        return True
    if clear:
        return (value.hour, value.minute) not in clear
    if len(parts) == 1:
        return (value < time(12)) != (parts == {"morning"})
    return False
