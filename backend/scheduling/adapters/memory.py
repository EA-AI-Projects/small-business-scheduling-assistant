"""Deterministic local calendar adapter for synthetic tests and development."""

from collections import defaultdict
from datetime import datetime
from threading import RLock

from scheduling.domain.appointments import Appointment, ReplacementGuard
from scheduling.domain.availability import AvailabilityPolicy, pilot_policy
from scheduling.domain.booking_invitations import (
    INVITATION_TEMPLATE,
    InvitationIntent,
    StoredInvitation,
    invitation_outbox_id,
)
from scheduling.domain.booking_outreach import OutreachRecord, OutreachSettings
from scheduling.domain.calendar import CalendarEvent, CalendarSnapshot, CalendarStatus
from scheduling.domain.client_records import ClientNote, ClientProfile, RecordConflict
from scheduling.domain.holds import (
    CreateHold,
    HoldCommit,
    IdempotencyKeyReused,
    IdempotencyRecord,
    OutboxIntent,
    RevisionConflict,
)
from scheduling.domain.lifecycle import AppointmentCommand, TransitionCommit, TransitionRecord
from scheduling.domain.outbox import DeliveryState, OutboxRecord
from scheduling.domain.owner_calendar import (
    OwnerCalendarCommand,
    OwnerCalendarCommit,
    OwnerCalendarReplay,
    UnavailableBlock,
)
from scheduling.domain.owner_policy import PolicyCommand, PolicyCommit, PolicyRecord, PolicyReplay


class InMemoryCalendarRepository:
    def __init__(self) -> None:
        self._lock = RLock()
        self._events: dict[str, tuple[CalendarEvent, ...]] = defaultdict(tuple)
        self._revisions: dict[str, int] = defaultdict(int)
        self._policies: dict[str, AvailabilityPolicy] = {}
        self._policy_versions: dict[str, int] = {}
        self._policy_replays: dict[tuple[str, str, str, str], PolicyReplay] = {}
        self._outreach: dict[str, OutreachRecord] = {}
        self._outreach_replays: dict[tuple[str, str, str], tuple[OutreachSettings, int, OutreachRecord]] = {}
        self._invitation_intents: dict[str, InvitationIntent] = {}
        self._invitation_states: dict[str, str] = {}
        self._invitation_provider_ids: dict[str, str] = {}
        self._invitation_outbox: dict[str, OutboxRecord] = {}
        self._invitation_pending: dict[tuple[str, str], str] = {}
        self._invitation_last_selected: dict[tuple[str, str], datetime] = {}
        self._invitation_last_sent: dict[tuple[str, str], datetime] = {}
        self._idempotency: dict[tuple[str, str, str, str], IdempotencyRecord] = {}
        self._transition_idempotency: dict[tuple[str, str, str, str], TransitionRecord] = {}
        self._holds: dict[str, HoldCommit] = {}
        self._appointments: dict[str, Appointment] = {}
        self._blocks: dict[tuple[str, str], UnavailableBlock] = {}
        self._owner_calendar_replays: dict[
            tuple[str, str, str, str], OwnerCalendarReplay
        ] = {}
        self._replacement_guards: dict[tuple[str, str], ReplacementGuard] = {}
        self._outbox: dict[str, object] = {}
        self._audit: dict[str, object] = {}
        self._clients: dict[tuple[str, str], ClientProfile] = {}
        self._client_phones: dict[tuple[str, str], str] = {}
        self._client_notes: dict[tuple[str, str, str], ClientNote] = {}

    def read_outreach(self, business_id: str) -> OutreachRecord:
        with self._lock:
            return self._outreach.get(business_id, OutreachRecord(OutreachSettings(), 0))

    def save_outreach(self, business_id: str, actor_id: str, key: str,
                      expected_version: int, settings: OutreachSettings) -> OutreachRecord:
        with self._lock:
            identity = (business_id, actor_id, key)
            previous = self._outreach_replays.get(identity)
            if previous is not None:
                old_settings, old_version, result = previous
                if old_settings != settings or old_version != expected_version:
                    raise IdempotencyKeyReused("Key already used for another outreach edit")
                return result
            current = self.read_outreach(business_id)
            if current.version != expected_version:
                raise RevisionConflict("Outreach settings version changed")
            result = OutreachRecord(settings, expected_version + 1)
            self._outreach[business_id] = result
            self._outreach_replays[identity] = (settings, expected_version, result)
            self._audit[f"outreach#{business_id}#{actor_id}#{key}"] = result
            return result

    def read_confirmed_for_client(self, business_id: str, client_id: str,
                                  run_at: datetime, end_at: datetime) -> tuple[int, tuple[Appointment, ...]]:
        with self._lock:
            matches = tuple(appointment for appointment in self._appointments.values()
                if appointment.business_id == business_id and appointment.client_id == client_id
                and appointment.status == CalendarStatus.CONFIRMED
                and appointment.start_at <= end_at and appointment.end_at > run_at)
            return self._revisions[business_id], matches

    def reserve_invitation(self, intent: InvitationIntent, settings_version: int,
                           calendar_revision: int, profile: ClientProfile) -> bool:
        with self._lock:
            guard = (intent.business_id, intent.client_id)
            if intent.manual_message is not None:
                if (intent.intent_id in self._invitation_intents
                        or self._revisions[intent.business_id] != calendar_revision
                        or self.read_profile(intent.business_id, intent.client_id) != profile):
                    return False
                outbox_id = invitation_outbox_id(intent.intent_id)
                self._invitation_intents[intent.intent_id] = intent
                self._invitation_states[intent.intent_id] = "OUTBOX"
                self._invitation_outbox[outbox_id] = OutboxRecord(
                    intent.business_id, outbox_id, intent.intent_id, "client",
                    INVITATION_TEMPLATE, 0, DeliveryState.PENDING,
                    intent.run_at, intent.run_at, intent.run_at)
                return True
            if (self.read_outreach(intent.business_id).version != settings_version
                    or not self.read_outreach(intent.business_id).settings.enabled
                    or self._revisions[intent.business_id] != calendar_revision
                    or self.read_profile(intent.business_id, intent.client_id) != profile
                    or guard in self._invitation_pending
                    or (guard in self._invitation_last_selected and
                        self._invitation_last_selected[guard] >= intent.run_at)
                    or (guard in self._invitation_last_sent and
                        self._invitation_last_sent[guard] > intent.repeat_cutoff_at)):
                return False
            self._invitation_intents[intent.intent_id] = intent
            self._invitation_states[intent.intent_id] = "QUEUED"
            self._invitation_pending[guard] = intent.intent_id
            self._invitation_last_selected[guard] = intent.run_at
            return True

    def read_invitation(self, business_id: str, intent_id: str) -> StoredInvitation | None:
        with self._lock:
            intent = self._invitation_intents.get(intent_id)
            if intent is None or intent.business_id != business_id:
                return None
            return StoredInvitation(intent, self._invitation_states[intent_id],
                                    self._invitation_provider_ids.get(intent_id))

    def list_manual_invitation_outbox(self) -> tuple[OutboxRecord, ...]:
        with self._lock:
            return tuple(record for record in self._invitation_outbox.values()
                         if self._invitation_intents[record.entity_id].manual_message is not None)

    def read_last_invitation_sent(self, business_id: str, client_id: str) -> datetime | None:
        with self._lock:
            return self._invitation_last_sent.get((business_id, client_id))

    def queued_invitations(self, business_id: str, limit: int = 100) -> tuple[StoredInvitation, ...]:
        with self._lock:
            return tuple(record for intent_id in self._invitation_intents
                         if (record := self.read_invitation(business_id, intent_id)) is not None
                         and record.state == "QUEUED")[:limit]

    def promote_invitation(self, intent: InvitationIntent,
                           settings_version: int, now: datetime) -> bool:
        with self._lock:
            if (self.read_outreach(intent.business_id).version != settings_version
                    or not self.read_outreach(intent.business_id).settings.enabled
                    or self._invitation_states.get(intent.intent_id) != "QUEUED"):
                return False
            outbox_id = invitation_outbox_id(intent.intent_id)
            self._invitation_outbox[outbox_id] = OutboxRecord(
                intent.business_id, outbox_id, intent.intent_id, "client",
                INVITATION_TEMPLATE, 0, DeliveryState.PENDING, now, now, now)
            self._invitation_states[intent.intent_id] = "OUTBOX"
            return True

    def claim_invitation_handoff(self, intent: InvitationIntent) -> bool:
        with self._lock:
            if self._invitation_states.get(intent.intent_id) != "OUTBOX":
                return False
            self._invitation_states[intent.intent_id] = "SENDING"
            return True

    def complete_invitation(self, intent: InvitationIntent,
                            handed_off_at: datetime | None,
                            provider_id: str | None = None) -> bool:
        if handed_off_at is not None and (
            handed_off_at.tzinfo is None or handed_off_at < intent.run_at
        ):
            raise ValueError("Handoff instant must be aware and after the run")
        if (handed_off_at is None) != (provider_id is None):
            raise ValueError("Successful handoff requires a provider ID")
        with self._lock:
            guard = (intent.business_id, intent.client_id)
            if ((intent.manual_message is None and
                    self._invitation_pending.get(guard) != intent.intent_id)
                    or self._invitation_states.get(intent.intent_id) not in (
                        ("SENDING",) if provider_id else ("QUEUED", "OUTBOX", "SENDING"))):
                return False
            if intent.manual_message is None:
                del self._invitation_pending[guard]
            self._invitation_states[intent.intent_id] = (
                "SENT" if handed_off_at is not None else "SUPPRESSED")
            if handed_off_at is not None and intent.manual_message is None:
                self._invitation_last_sent[guard] = handed_off_at
            if handed_off_at is not None:
                assert provider_id is not None
                self._invitation_provider_ids[intent.intent_id] = provider_id
            return True

    def read_profile(self, business_id: str, client_id: str) -> ClientProfile | None:
        with self._lock:
            return self._clients.get((business_id, client_id))

    def list_profiles(self, business_id: str) -> tuple[ClientProfile, ...]:
        with self._lock:
            return tuple(sorted((profile for (owner, _), profile in self._clients.items()
                                 if owner == business_id), key=lambda profile: profile.client_id))

    def list_profiles_after(self, business_id: str, cursor: str | None,
                            limit: int) -> tuple[tuple[ClientProfile, ...], str | None]:
        remaining = tuple(profile for profile in self.list_profiles(business_id)
                          if cursor is None or profile.client_id > cursor)
        page = remaining[:limit]
        return page, page[-1].client_id if len(remaining) > limit else None

    def read_verified_phone(self, business_id: str, phone_e164: str) -> ClientProfile | None:
        with self._lock:
            client_id = self._client_phones.get((business_id, phone_e164))
            profile = self._clients.get((business_id, client_id)) if client_id else None
            return (profile if profile and profile.phone_e164 == phone_e164
                    and profile.phone_verified_at is not None and profile.active else None)

    def save_profile(self, profile: ClientProfile, expected_version: int,
                     previous_phone: str | None) -> None:
        with self._lock:
            key = (profile.business_id, profile.client_id)
            current = self._clients.get(key)
            if (current.version if current else 0) != expected_version:
                raise RecordConflict("Client profile version changed")
            if (current.phone_e164 if current else None) != previous_phone:
                raise RecordConflict("Client phone changed")
            phone_key = (profile.business_id, profile.phone_e164)
            mapped = self._client_phones.get(phone_key)
            if mapped is not None and mapped != profile.client_id:
                raise RecordConflict("Phone is already assigned to another client")
            if previous_phone and previous_phone != profile.phone_e164:
                del self._client_phones[(profile.business_id, previous_phone)]
            self._clients[key] = profile
            self._client_phones[phone_key] = profile.client_id

    def read_note(self, business_id: str, client_id: str,
                  note_id: str) -> ClientNote | None:
        with self._lock:
            return self._client_notes.get((business_id, client_id, note_id))

    def list_notes(self, business_id: str, client_id: str) -> tuple[ClientNote, ...]:
        with self._lock:
            return tuple(sorted((note for (owner, client, _), note in self._client_notes.items()
                                 if owner == business_id and client == client_id),
                                key=lambda note: (note.created_at, note.note_id)))

    def put_note(self, note: ClientNote) -> None:
        with self._lock:
            key = (note.business_id, note.client_id, note.note_id)
            existing = self._client_notes.get(key)
            if existing is not None and existing != note:
                raise RecordConflict("Note ID was already used")
            self._client_notes[key] = note

    def delete_note(self, note: ClientNote, expected_revision: int | None = None) -> None:
        with self._lock:
            if (expected_revision is not None
                    and self._revisions[note.business_id] != expected_revision):
                raise RecordConflict("Calendar changed before note purge")
            key = (note.business_id, note.client_id, note.note_id)
            current = self._client_notes.get(key)
            if current is not None and current.legal_hold_reason is not None:
                raise RecordConflict("Note is under legal hold")
            if self._client_notes.get(key) == note:
                del self._client_notes[key]

    def update_note_hold(self, before: ClientNote, after: ClientNote) -> None:
        with self._lock:
            key = (before.business_id, before.client_id, before.note_id)
            if self._client_notes.get(key) != before:
                raise RecordConflict("Note changed before legal hold update")
            self._client_notes[key] = after

    def last_visit_end(self, business_id: str, client_id: str,
                       now: datetime) -> datetime | None:
        with self._lock:
            ends = [appointment.end_at for appointment in self._appointments.values()
                    if appointment.business_id == business_id
                    and appointment.client_id == client_id
                    and appointment.status.value == "CONFIRMED"
                    and appointment.end_at <= now]
            return max(ends, default=None)

    def read_revision(self, business_id: str) -> int:
        with self._lock:
            return self._revisions[business_id]

    def read_idempotency(self, command: CreateHold) -> IdempotencyRecord | None:
        with self._lock:
            return self._idempotency.get(self._idempotency_key(command))

    @staticmethod
    def _idempotency_key(command: CreateHold) -> tuple[str, str, str, str]:
        return (command.business_id, command.actor_id, "create_hold", command.idempotency_key)

    def commit_hold(self, expected_revision: int, commit: HoldCommit) -> None:
        with self._lock:
            business_id = commit.command.business_id
            if self._revisions[business_id] != expected_revision:
                raise RevisionConflict("Calendar revision changed")
            key = self._idempotency_key(commit.command)
            if key in self._idempotency:
                raise RevisionConflict("Idempotency record already exists")
            original_id = commit.result.replaces_appointment_id
            if original_id is not None:
                guard_key = (business_id, original_id)
                guard = self._replacement_guards.get(guard_key)
                if guard is not None and guard.expires_at > commit.created_at:
                    raise RevisionConflict("Original already has an active replacement")
                self._replacement_guards[guard_key] = ReplacementGuard(
                    original_id, commit.result.hold_id, commit.result.hold_expires_at
                )
            self._events[business_id] += (commit.result.calendar_event(),)
            self._holds[commit.result.hold_id] = commit
            self._appointments[commit.result.hold_id] = Appointment(
                appointment_id=commit.result.hold_id,
                business_id=business_id,
                client_id=commit.result.client_id,
                start_at=commit.result.start_at,
                end_at=commit.result.end_at,
                status=commit.result.calendar_event().status,
                hold_expires_at=commit.result.hold_expires_at,
                duration_minutes=commit.result.duration_minutes,
                buffer_minutes=commit.result.buffer_minutes,
                version=1,
                replaces_appointment_id=original_id,
                created_at=commit.result.created_at,
            )
            self._idempotency[key] = IdempotencyRecord(commit.request_hash, commit.result)
            self._audit[commit.audit_id] = commit
            for intent in commit.outbox:
                self._outbox[intent.outbox_id] = intent
            self._revisions[business_id] += 1

    def list_outbox_intents(self) -> tuple[OutboxIntent, ...]:
        """Committed notification intents in commit order (local harnesses only).

        A policy commit is stored whole; it becomes its owner notification intent.
        """
        with self._lock:
            intents: list[OutboxIntent] = []
            for outbox_id, entry in self._outbox.items():
                if isinstance(entry, OutboxIntent):
                    intents.append(entry)
                elif isinstance(entry, PolicyCommit):
                    intents.append(OutboxIntent(outbox_id, entry.command.business_id,
                                                "owner", entry.command.operation))
            return tuple(intents)

    def read_appointment(self, appointment_id: str) -> Appointment | None:
        with self._lock:
            return self._appointments.get(appointment_id)

    def read_pending_requests(self, business_id: str, now: datetime) -> tuple[Appointment, ...]:
        with self._lock:
            return tuple(sorted((appointment for appointment in self._appointments.values()
                if appointment.business_id == business_id
                and appointment.status.value == "PENDING_APPROVAL"
                and appointment.hold_expires_at is not None
                and appointment.hold_expires_at > now), key=lambda appointment: appointment.start_at))

    def list_client_appointments(self, business_id: str, client_id: str) -> tuple[Appointment, ...]:
        with self._lock:
            return tuple(sorted((appointment for appointment in self._appointments.values()
                if appointment.business_id == business_id
                and appointment.client_id == client_id),
                key=lambda appointment: appointment.start_at))

    def read_replacement_guard(
        self, business_id: str, original_id: str
    ) -> ReplacementGuard | None:
        with self._lock:
            return self._replacement_guards.get((business_id, original_id))

    @staticmethod
    def _transition_key(command: AppointmentCommand) -> tuple[str, str, str, str]:
        return (
            command.business_id,
            command.actor_id,
            command.operation.value,
            command.idempotency_key,
        )

    def read_transition_idempotency(
        self, command: AppointmentCommand
    ) -> TransitionRecord | None:
        with self._lock:
            return self._transition_idempotency.get(self._transition_key(command))

    def commit_transition(self, expected_revision: int, commit: TransitionCommit) -> None:
        with self._lock:
            business_id = commit.command.business_id
            if self._revisions[business_id] != expected_revision:
                raise RevisionConflict("Calendar revision changed")
            current = self._appointments.get(commit.before.appointment_id)
            if current != commit.before:
                raise RevisionConflict("Appointment status or version changed")
            key = self._transition_key(commit.command)
            if key in self._transition_idempotency:
                raise RevisionConflict("Idempotency record already exists")
            if commit.command.operation.value == "approve" and (
                current.hold_expires_at is None
                or current.hold_expires_at <= commit.decision_at
            ):
                raise RevisionConflict("Hold expired before approval commit")
            if commit.command.operation.value == "expire" and (
                current.hold_expires_at is None
                or current.hold_expires_at > commit.decision_at
            ):
                raise RevisionConflict("Hold is not yet expired")

            replaced = commit.result.replaced_appointment
            if replaced is not None:
                original = self._appointments.get(replaced.appointment_id)
                guard_key = (business_id, replaced.appointment_id)
                guard = self._replacement_guards.get(guard_key)
                if (
                    original is None
                    or original.status.value not in ("CONFIRMED", "PENDING_APPROVAL")
                    or original.version + 1 != replaced.version
                    or guard is None
                    or guard.replacement_id != current.appointment_id
                    or guard.expires_at <= commit.decision_at
                ):
                    raise RevisionConflict("Replacement original changed")

            self._appointments[current.appointment_id] = commit.result.appointment
            if replaced is not None:
                self._appointments[replaced.appointment_id] = replaced
            removed = {current.appointment_id}
            if replaced is not None:
                removed.add(replaced.appointment_id)
            self._events[business_id] = tuple(
                event for event in self._events[business_id] if event.event_id not in removed
            )
            if commit.result.appointment.occupies_time(commit.decision_at):
                self._events[business_id] += (commit.result.appointment.calendar_event(),)

            original_id = current.replaces_appointment_id
            if original_id is not None and commit.clear_replacement_guard:
                guard_key = (business_id, original_id)
                guard = self._replacement_guards.get(guard_key)
                if guard is not None and guard.replacement_id == current.appointment_id:
                    del self._replacement_guards[guard_key]
            self._transition_idempotency[key] = TransitionRecord(
                commit.request_hash, commit.result
            )
            self._audit[commit.audit_id] = commit
            for intent in commit.outbox:
                self._outbox[intent.outbox_id] = intent
            self._revisions[business_id] += 1

    def read_calendar(self, business_id: str) -> CalendarSnapshot:
        with self._lock:
            return CalendarSnapshot(
                business_id=business_id,
                revision=self._revisions[business_id],
                events=self._events[business_id] + tuple(
                    block.calendar_event()
                    for (owner_business, _), block in self._blocks.items()
                    if owner_business == business_id
                ),
            )

    def read_block(self, business_id: str, block_id: str) -> UnavailableBlock | None:
        with self._lock:
            return self._blocks.get((business_id, block_id))

    @staticmethod
    def _owner_calendar_key(command: OwnerCalendarCommand) -> tuple[str, str, str, str]:
        return (
            command.business_id, command.actor_id, command.operation.value,
            command.idempotency_key,
        )

    def read_owner_calendar_replay(
        self, command: OwnerCalendarCommand
    ) -> OwnerCalendarReplay | None:
        with self._lock:
            return self._owner_calendar_replays.get(self._owner_calendar_key(command))

    def commit_owner_calendar(self, expected_revision: int, commit: OwnerCalendarCommit) -> None:
        with self._lock:
            business_id = commit.command.business_id
            if self._revisions[business_id] != expected_revision:
                raise RevisionConflict("Calendar revision changed")
            before = commit.before_block
            if before is not None and self._blocks.get((business_id, before.block_id)) != before:
                raise RevisionConflict("Block version changed")
            key = self._owner_calendar_key(commit.command)
            if key in self._owner_calendar_replays:
                raise RevisionConflict("Owner calendar key already used")
            block = commit.result.block
            appointment = commit.result.appointment
            if before is not None:
                del self._blocks[(business_id, before.block_id)]
            if block is not None:
                self._blocks[(business_id, block.block_id)] = block
            if appointment is not None:
                self._appointments[appointment.appointment_id] = appointment
                self._events[business_id] += (appointment.calendar_event(),)
            self._owner_calendar_replays[key] = OwnerCalendarReplay(
                commit.request_hash, commit.result
            )
            self._audit[commit.audit_id] = commit
            for intent in commit.outbox:
                self._outbox[intent.outbox_id] = intent
            self._revisions[business_id] += 1

    def read_calendar_for_hold(
        self,
        business_id: str,
        start_at: datetime,
        end_at: datetime,
        policy: AvailabilityPolicy,
    ) -> CalendarSnapshot:
        return self.read_calendar(business_id)

    def read_policy(self, business_id: str) -> AvailabilityPolicy:
        with self._lock:
            return self._policies.get(business_id, pilot_policy())

    def read_policy_record(self, business_id: str) -> PolicyRecord | None:
        with self._lock:
            policy = self._policies.get(business_id)
            return PolicyRecord(policy, self._policy_versions[business_id]) if policy else None

    @staticmethod
    def _policy_replay_key(command: PolicyCommand) -> tuple[str, str, str, str]:
        return (
            command.business_id, command.actor_id, command.operation,
            command.idempotency_key,
        )

    def read_policy_replay(self, command: PolicyCommand) -> PolicyReplay | None:
        with self._lock:
            return self._policy_replays.get(self._policy_replay_key(command))

    def commit_policy(self, expected_revision: int, commit: PolicyCommit) -> None:
        with self._lock:
            business_id = commit.command.business_id
            if self._revisions[business_id] != expected_revision:
                raise RevisionConflict("Calendar revision changed")
            previous_version = self._policy_versions.get(business_id)
            if previous_version != commit.command.expected_version:
                raise RevisionConflict("Policy version changed")
            key = self._policy_replay_key(commit.command)
            if key in self._policy_replays:
                raise RevisionConflict("Policy idempotency key already used")
            self._policies[business_id] = commit.result.record.policy
            self._policy_versions[business_id] = commit.result.record.version
            self._policy_replays[key] = PolicyReplay(commit.request_hash, commit.result)
            self._audit[commit.audit_id] = commit
            self._outbox[f"{commit.audit_id}#owner"] = commit
            self._revisions[business_id] += 1

    def set_policy_for_test(self, business_id: str, policy: AvailabilityPolicy) -> None:
        with self._lock:
            self._policies[business_id] = policy
            self._policy_versions[business_id] = 1

    def replace_for_test(
        self, business_id: str, expected_revision: int, events: tuple[CalendarEvent, ...]
    ) -> CalendarSnapshot:
        """Seed synthetic state with the same revision guard future writes need."""
        with self._lock:
            if self._revisions[business_id] != expected_revision:
                raise RevisionConflict("Calendar revision changed")
            self._events[business_id] = events
            self._revisions[business_id] += 1
            return self.read_calendar(business_id)
