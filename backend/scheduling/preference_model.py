"""Lazy preference interpreter: the model key is read on first use, never at startup."""

from collections.abc import Callable
from threading import Lock

from scheduling.domain.scheduling_preferences import PreferenceInterpreter


class LazyPreferenceInterpreter:
    """Builds the real interpreter on the first review and caches only a success.

    A missing parameter, denied access, throttling or missing configuration raises from
    ``interpret_scheduling_preferences``; the owner API reports that as 503 for the review
    route alone, so every other route keeps working and a later review can retry.
    """

    def __init__(self, build: Callable[[], PreferenceInterpreter]) -> None:
        self._build = build
        self._built: PreferenceInterpreter | None = None
        self._lock = Lock()

    def interpret_scheduling_preferences(self, text: str, timezone: str) -> dict[str, object]:
        with self._lock:
            if self._built is None:
                self._built = self._build()
            interpreter = self._built
        return interpreter.interpret_scheduling_preferences(text, timezone)
