"""Owner invitation and verified-email activation at the HTTP boundary."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from scheduling.account_invitations import (
    ClientAccountInvitations,
    ClientInvitation,
    InvitationDenied,
    VerifiedAccount,
)
from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.client_records import ClientRecordService, HomeSize
from scheduling.identity_links import IdentityLink, IdentityLinks, LinkConflict, LinkRole, LinkState
from scheduling.owner_api import OwnerPrincipal, create_owner_app

NOW = datetime(2026, 10, 9, 17, tzinfo=UTC)


class Records:
    def __init__(self, profiles: InMemoryCalendarRepository) -> None:
        self.profiles = profiles
        self.deleted: set[tuple[str, str]] = set()
        self.invites: dict[tuple[str, str], ClientInvitation] = {}
        self.links: dict[str, IdentityLink] = {}
        self.targets: dict[tuple[LinkRole, str, str | None], str] = {}

    def read_profile(self, business_id: str, client_id: str) -> object | None:
        if (business_id, client_id) in self.deleted:
            return None
        return self.profiles.read_profile(business_id, client_id)

    def read_policy_record(self, business_id: str) -> object | None:
        return object() if business_id == "pilot" else None

    def read_invitation(self, business_id: str, client_id: str) -> ClientInvitation | None:
        return self.invites.get((business_id, client_id))

    def write_invitation(self, invitation: ClientInvitation,
                         previous: ClientInvitation | None) -> None:
        key = (invitation.business_id, invitation.client_id)
        if self.invites.get(key) != previous:
            raise InvitationDenied("Invitation changed")
        self.invites[key] = invitation

    def mark_sent(self, invitation: ClientInvitation, now: datetime,
                  expires_at: datetime) -> ClientInvitation:
        updated = replace(invitation, sent_at=now, expires_at=expires_at)
        self.invites[(invitation.business_id, invitation.client_id)] = updated
        return updated

    def activate_link(self, invitation: ClientInvitation,
                      pending: IdentityLink, now: datetime) -> None:
        key = (invitation.business_id, invitation.client_id)
        current = self.invites[key]
        if (current.consumed_at is not None or current.revoked_at is not None
                or current.expires_at <= now or self.links.get(pending.subject) != pending
                or self.read_profile(invitation.business_id, invitation.client_id) is None):
            raise InvitationDenied("Invitation already used")
        self.invites[key] = replace(current, consumed_at=now)
        self.links[pending.subject] = replace(
            pending, state=LinkState.ACTIVE, version=pending.version + 1,
            updated_at=now, changed_by=pending.subject, reason="invited email verified")

    def revoke_invitation(self, invitation: ClientInvitation, now: datetime) -> None:
        key = (invitation.business_id, invitation.client_id)
        self.invites[key] = replace(self.invites[key], revoked_at=now)

    def read_link(self, subject: str) -> IdentityLink | None:
        return self.links.get(subject)

    def read_target_subject(self, role: LinkRole, business_id: str,
                            client_id: str | None) -> str | None:
        return self.targets.get((role, business_id, client_id))

    def write_link(self, link: IdentityLink, previous: IdentityLink | None) -> None:
        if self.links.get(link.subject) != previous:
            raise LinkConflict("Link changed")
        target = (link.role, link.business_id, link.client_id)
        holder = self.targets.get(target)
        if link.state == LinkState.REVOKED:
            if holder != link.subject:
                raise LinkConflict("Target changed")
            del self.targets[target]
        elif holder not in (None, link.subject):
            raise LinkConflict("Target already linked")
        else:
            self.targets[target] = link.subject
        self.links[link.subject] = link


class Directory:
    def __init__(self) -> None:
        self.users: dict[str, str] = {}
        self.sent: list[str] = []
        self.fail_next = False

    def create_or_find_invitee(self, email: str) -> tuple[str, bool]:
        if email in self.users:
            return self.users[email], False
        subject = f"sub-{len(self.users) + 1}"
        self.users[email] = subject
        return subject, True

    def send_invitation(self, email: str, *, created: bool) -> None:
        del created
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("synthetic delivery failure")
        self.sent.append(email)


def test_owner_invite_activation_and_rejected_claims() -> None:
    profiles = InMemoryCalendarRepository()
    ClientRecordService(profiles).save_profile(
        "pilot", "client-a", "Synthetic Client", "+14155550101",
        "123 Test Street", HomeSize.SMALL, 60, True, 0, 180, NOW)
    records = Records(profiles)
    directory = Directory()
    links = IdentityLinks(records)
    service = ClientAccountInvitations(records, links, directory, lambda: NOW)

    def verify_owner(token: str) -> OwnerPrincipal:
        if token != "owner-token":
            raise ValueError("Invalid owner")
        return OwnerPrincipal("owner-sub", "pilot")

    def verify_account(token: str) -> VerifiedAccount:
        identities = {
            "client-token": VerifiedAccount("sub-1", "client@example.test", True),
            "unverified": VerifiedAccount("sub-1", "client@example.test", False),
            "other-email": VerifiedAccount("sub-1", "other@example.test", True),
            "other-sub": VerifiedAccount("sub-other", "client@example.test", True),
        }
        return identities[token]

    api = TestClient(create_owner_app(profiles, verify_owner, lambda: NOW,
                                       account_invitations=service,
                                       verify_account=verify_account))
    invite_url = "/v1/owner/businesses/pilot/clients/client-a/account-invitation"
    activate_url = "/v1/account/invitations/activate"
    owner = {"Authorization": "Bearer owner-token"}
    assert api.post(invite_url, json={"email": "client@example.test"}).status_code == 401
    assert api.post(invite_url.replace("/pilot/", "/other/"),
                    json={"email": "client@example.test"}, headers=owner).status_code == 403
    assert api.post(invite_url.replace("client-a", "missing"),
                    json={"email": "client@example.test"}, headers=owner).status_code == 409
    response = api.post(invite_url, json={"email": "Client@Example.Test"}, headers=owner)
    assert response.status_code == 200
    assert datetime.fromisoformat(response.json()["expires_at"]) == NOW + timedelta(hours=24)
    assert directory.sent == ["client@example.test"]
    assert api.post(invite_url, json={"email": "client@example.test"},
                    headers=owner).status_code == 200
    assert directory.sent == ["client@example.test"]
    assert api.post(invite_url, json={"email": "changed@example.test"},
                    headers=owner).status_code == 409
    for token in ("unverified", "other-email", "other-sub"):
        assert api.post(activate_url, headers={"Authorization": f"Bearer {token}"}).status_code == 403
    assert links.resolve("sub-1") is None
    assert api.post(activate_url, headers={"Authorization": "Bearer client-token"}).status_code == 200
    assert links.resolve("sub-1").client_id == "client-a"
    assert api.post(activate_url, headers={"Authorization": "Bearer client-token"}).status_code == 403
    assert api.get(invite_url, headers=owner).json()["state"] == "active"
    assert api.delete(invite_url, headers=owner).status_code == 204
    assert links.resolve("sub-1") is None
    assert api.get(invite_url, headers=owner).json()["state"] == "revoked"
    assert api.post(invite_url, json={"email": "changed@example.test"},
                    headers=owner).status_code == 200
    assert directory.sent[-1] == "changed@example.test"
    assert profiles.read_profile("pilot", "client-a").phone_verified_at is None


def test_expired_deleted_and_changed_email_invitations_fail_closed() -> None:
    profiles = InMemoryCalendarRepository()
    ClientRecordService(profiles).save_profile(
        "pilot", "client-a", "Synthetic Client", "+14155550101",
        "123 Test Street", HomeSize.SMALL, 60, True, 0, 180, NOW)
    records = Records(profiles)
    directory = Directory()
    links = IdentityLinks(records)
    service = ClientAccountInvitations(records, links, directory, lambda: NOW)
    service.invite("pilot", "client-a", "first@example.test", "owner-sub", NOW)
    old = VerifiedAccount("sub-1", "first@example.test", True)
    try:
        service.activate_pending(old, NOW + timedelta(hours=24))
    except InvitationDenied:
        pass
    else:
        raise AssertionError("Expired invitation activated")
    service.revoke("pilot", "client-a", "owner-sub", NOW + timedelta(hours=24))
    service.invite("pilot", "client-a", "new@example.test", "owner-sub",
                   NOW + timedelta(hours=24, minutes=1))
    assert links.resolve("sub-1") is None
    try:
        service.activate_pending(old, NOW + timedelta(hours=24, minutes=1))
    except InvitationDenied:
        pass
    else:
        raise AssertionError("Old address activated")
    records.deleted.add(("pilot", "client-a"))
    try:
        service.activate_pending(VerifiedAccount("sub-2", "new@example.test", True),
                         NOW + timedelta(hours=24, minutes=2))
    except InvitationDenied:
        pass
    else:
        raise AssertionError("Deleted profile activated")


def test_failed_send_retried_after_provisional_expiry_gets_full_24_hours() -> None:
    profiles = InMemoryCalendarRepository()
    ClientRecordService(profiles).save_profile(
        "pilot", "client-a", "Synthetic Client", "+14155550101",
        "123 Test Street", HomeSize.SMALL, 60, True, 0, 180, NOW)
    records = Records(profiles)
    directory = Directory()
    links = IdentityLinks(records)
    service = ClientAccountInvitations(records, links, directory)
    directory.fail_next = True
    try:
        service.invite("pilot", "client-a", "client@example.test", "owner-sub", NOW)
    except RuntimeError:
        pass
    else:
        raise AssertionError("First synthetic delivery should fail")
    pending = records.read_invitation("pilot", "client-a")
    assert pending is not None and pending.sent_at is None
    assert links.resolve("sub-1") is None

    sent_at = NOW + timedelta(hours=25)
    invite = service.invite("pilot", "client-a", "client@example.test",
                            "owner-sub", sent_at)
    assert invite.subject == pending.subject
    assert invite.sent_at == sent_at
    assert invite.expires_at == sent_at + timedelta(hours=24)
    assert directory.sent == ["client@example.test"]
    service.activate("pilot", "client-a", VerifiedAccount(
        "sub-1", "client@example.test", True), sent_at + timedelta(hours=23))
    assert links.resolve("sub-1").client_id == "client-a"
