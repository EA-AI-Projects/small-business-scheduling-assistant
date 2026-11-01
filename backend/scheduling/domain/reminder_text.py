"""Deterministic checks of the inbound text behind a reminder write (#402).

These are guards, not interpretation: the model still reads the request. They stop a write
whose message carries no reminder content, and a timing write whose message never made the
hour unambiguous. Range-based guesses (21:00 can only be morning) never count.
"""
import re

_NUMBER_WORDS = ("one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve")
_NOT_AN_HOUR = r"(?:days?|reminders?|texts?|messages?|times?|hours?|minutes?|weeks?)"
_DAY_QUANTITY = re.compile(
    rf"\b(?:\d+|{_NUMBER_WORDS})\s*(?:calendar\s+)?days?\b|\b(?:a|the)\s+day\b")
_MERIDIEM = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)(?![a-z])")
_TWENTY_FOUR = re.compile(r"\b(?:0\d|1[3-9]|2[0-3]):[0-5]\d\b")
_FIXED_WORDS = re.compile(r"\b(?:noon|midnight)\b")
_DAY_PART = re.compile(r"\b(?:morning|afternoon|evening|night|tonight)\b")
_HOUR = re.compile(
    rf"\b\d{{1,2}}(?:[:.]\d{{2}})?\b|\b(?:{_NUMBER_WORDS})\b(?!\s+{_NOT_AN_HOUR})")
_CONTENT = re.compile(
    rf"\d|\b(?:{_NUMBER_WORDS}|noon|midnight|am|pm|a\.m\.|p\.m\.|morning|afternoon|evening|"
    r"night|on|off|stop|enable|disable|turn|remind\w*|reminders?|days?|before|earlier|"
    r"later|text|texts|notify|unsubscribe|opt)\b")


def has_reminder_content(body: str) -> bool:
    """A write needs words or numbers about reminders; 'go ahead', 'k' or an emoji have none."""
    return _CONTENT.search(body.lower()) is not None


def ambiguous_hour(body: str) -> bool:
    """True when the text names an hour without AM/PM, a day part, noon or a 24-hour form.

    Day quantities ("two days before") are not hours. A text with no hour at all (for
    example 'morning' answering 'AM or PM?') is not ambiguous.
    """
    text = _DAY_QUANTITY.sub(" ", body.lower())
    text = _MERIDIEM.sub(" ", text)
    text = _TWENTY_FOUR.sub(" ", text)
    text = _FIXED_WORDS.sub(" ", text)
    if _DAY_PART.search(text) is not None:
        return False
    return _HOUR.search(text) is not None
