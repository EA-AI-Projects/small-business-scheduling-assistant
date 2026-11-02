"""One authorized owner SMS's bounded access to shared client reminder preferences."""

import re
from collections.abc import Callable
from datetime import datetime, time
from typing import Any, Protocol

from scheduling.domain.client_records import (
    ClientProfile,
    ClientRecordRepository,
    ClientRecordService,
    RecordConflict,
    RecordNotFound,
    ReminderReceipt,
)
from scheduling.domain.client_tools import reminder_facts
from scheduling.domain.reminder_text import acknowledgment_only, unclear_time
from scheduling.domain.reminders import ReminderDeliveryRepository, reminder_preference_view
from scheduling.domain.scheduling_preferences import ClientReference, candidate_profiles, is_exact
from scheduling.domain.sms_ingress import InboundReceipt, SmsIngressStore

NAMES = frozenset({"get_client_reminder_preference", "set_client_reminder_preference"})
SCOPE = "Only reminder settings were handled; other requests in this text were not acted on."


class Repository(ClientRecordRepository, ReminderDeliveryRepository, Protocol):
    pass


class OwnerReminderTools:
    def __init__(self, repository: Repository, consent: SmsIngressStore,
                 receipt: InboundReceipt, clock: Callable[[], datetime], timezone: str) -> None:
        self.repository = repository
        self.consent = consent
        self.receipt = receipt
        self.clock = clock
        self.timezone = timezone
        self.resolved: dict[str, ClientProfile] = {}
        self.attempted = False
        self.reply: str | None = None

    def _receipt_text(self, profile: ClientProfile, saved: ReminderReceipt,
                      replay: bool = False) -> str:
        state = "on" if saved.enabled else "off"
        hour = saved.local_time.strftime("%I:%M %p").lstrip("0") if saved.local_time else "10:00 AM"
        days = saved.days_before or 1
        # Phone endings remain GSM-safe even with long/non-GSM display names and ids.
        text = (f"Client phone ending {profile.phone_e164[-4:]}: reminders {state}; saved timing {days} "
                f"day{'s' if days != 1 else ''} before at {hour} {self.timezone}.")
        if replay:
            return "Original saved result: " + text + " Later edits may have changed it. " + SCOPE
        try:
            view = reminder_preference_view(profile, self.consent, self.repository, self.clock())
            if saved.enabled and view.blockers:
                text += " Sending is currently blocked."
            elif saved.enabled and any(row.status == "past_time" for row in view.upcoming_reminders):
                text += " An upcoming visit's reminder time has passed; it will be skipped (no catch-up)."
        except Exception:  # noqa: BLE001 - the save still stands if explanation is unavailable
            text += " Current delivery eligibility could not be checked."
        return text + " Delivery is not guaranteed. " + SCOPE

    def replay(self) -> str | None:
        """Replay before asking the model, including after a later edit on another surface."""
        for profile in self.repository.list_profiles(self.receipt.business_id):
            saved = self.repository.read_reminder_preference_receipt(
                self.receipt.business_id, profile.client_id, self.receipt.provider_id)
            if saved is not None:
                return self._receipt_text(profile, saved, replay=True)
        return None

    def _refuse(self, error: str, text: str) -> dict[str, Any]:
        self.reply = f"{text} Reminder settings did not change. {SCOPE}"
        return {"ok": False, "error": error}

    def __call__(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "get_client_reminder_preference":
            try:
                return self._read(args)
            except Exception:  # noqa: BLE001 - a read failure must not replace a later write receipt
                return {"ok": False, "error": "unavailable"}
        try:
            if self.attempted:
                return {"ok": False, "error": "action_already_attempted"}
            self.attempted = True
            return self._write(args)
        except (RecordConflict, RecordNotFound):
            return self._refuse("stale_version", "The client profile changed or is unavailable. Please ask again.")
        except ValueError:
            return self._refuse("invalid_arguments", "Use 1 to 7 days before and a time from 8 AM to 6 PM.")
        except Exception:  # noqa: BLE001 - unknown commit outcomes must never claim success
            self.reply = f"I couldn't verify the saved reminder settings. Please check the client profile. {SCOPE}"
            return {"ok": False, "error": "unavailable"}

    def _read(self, args: dict[str, Any]) -> dict[str, Any]:
        if (set(args) != {"client_name", "client_id"} or not isinstance(args["client_name"], str)
                or not args["client_name"].strip()
                or (args["client_id"] is not None and not isinstance(args["client_id"], str))):
            return {"ok": False, "error": "invalid_arguments"}
        profiles = {p.client_id: p for p in self.repository.list_profiles(self.receipt.business_id)
                    if p.active}
        references = tuple(ClientReference(p.client_id, p.name, p.phone_e164[-4:])
                           for p in profiles.values())
        candidates = candidate_profiles(args["client_name"], references)
        selected = args["client_id"]
        sure = len(candidates) == 1 and is_exact(args["client_name"], candidates[0])
        if not sure:
            # The model cannot invent a disambiguating pick. The current owner text must
            # name that reference or uniquely identify its phone ending.
            body = self.receipt.body or ""
            explicit = [p for p in candidates if p.client_id == selected and (
                re.search(rf"(?<![\w-]){re.escape(p.client_id)}(?![\w-])", body)
                or (len(p.phone_last4) == 4
                and re.search(rf"(?<!\w){re.escape(p.phone_last4)}(?!\w)", body)
                and sum(c.phone_last4 == p.phone_last4 for c in candidates) == 1))]
            if len(explicit) != 1:
                return {"ok": False, "error": "client_needs_clarification",
                        "candidates": [{"client_id": p.client_id, "name": p.name,
                                        "phone_last4": p.phone_last4} for p in candidates[:5]],
                        "guidance": "Ask for a full name or unique phone ending/reference. Do not write yet."}
            chosen = explicit[0]
        else:
            chosen = candidates[0]
            if selected is not None and selected != chosen.client_id:
                return {"ok": False, "error": "invalid_client_selection"}
        profile = profiles[chosen.client_id]
        facts = reminder_facts(reminder_preference_view(
            profile, self.consent, self.repository, self.clock()))
        self.resolved[profile.client_id] = profile
        return {**facts, "client_id": profile.client_id, "client_name": profile.name,
                "version": profile.version}

    def _write(self, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"client_id", "version", "enabled", "days_before", "local_time"}:
            return self._refuse("invalid_arguments", "Please state the client and reminder change explicitly.")
        client_id, version = args["client_id"], args["version"]
        enabled, days, raw_time = args["enabled"], args["days_before"], args["local_time"]
        if (not isinstance(client_id, str) or type(version) is not int
                or (enabled is not None and type(enabled) is not bool)
                or (days is not None and type(days) is not int)
                or (raw_time is not None and (not isinstance(raw_time, str)
                    or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw_time) is None))
                or (enabled is None and days is None and raw_time is None)):
            return self._refuse("invalid_arguments", "Please state the client and reminder change explicitly.")
        profile = self.resolved.get(client_id)
        if profile is None or profile.version != version:
            return self._refuse("client_not_resolved", "Please identify the client and read the current settings first.")
        clock = time.fromisoformat(raw_time) if raw_time is not None else None
        body = self.receipt.body or ""
        if acknowledgment_only(body):
            return self._refuse("explicit_choice_required", "Please state the client's expressed reminder choice.")
        # Identity digits are not clock hours ("phone ending 0501 ... at 9 AM").
        time_body = re.sub(re.escape(profile.name), " ", body, flags=re.IGNORECASE)
        time_body = time_body.replace(profile.client_id, " ")
        time_body = re.sub(
            rf"\b(?:phone\s+)?(?:ending|ends in|last four|last 4)\s+{re.escape(profile.phone_e164[-4:])}\b",
            " ", time_body, flags=re.IGNORECASE)
        if clock is not None and unclear_time(time_body, clock):
            return self._refuse("ambiguous_time", "Please specify a time with AM or PM.")
        saved = ClientRecordService(self.repository).apply_owner_reminder_request_from_receipt(
            self.receipt.business_id, client_id, self.receipt.provider_id, version,
            enabled, self.clock(), days_before=days, local_time=clock)
        updated = self.repository.read_profile(self.receipt.business_id, client_id)
        self.reply = self._receipt_text(updated or profile, saved)
        return {"ok": True, "reminder_preference_saved": True, "enabled": saved.enabled,
                "days_before": saved.days_before,
                "local_time": saved.local_time.strftime("%H:%M") if saved.local_time else None,
                "timezone": self.timezone}
