"""Owner-issued account invitations; email and SMS identities remain separate."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol

from scheduling.identity_links import IdentityLink, IdentityLinks, LinkRole, LinkState


class InvitationDenied(ValueError):
    """An invitation or activation cannot grant the requested profile."""


@dataclass(frozen=True)
class ClientInvitation:
    business_id: str
    client_id: str
    subject: str
    email_hash: str
    expires_at: datetime
    issued_at: datetime
    issued_by: str
    consumed_at: datetime | None = None
    sent_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class VerifiedAccount:
    """Identity from a signature-verified Cognito ID token only."""

    subject: str
    email: str
    email_verified: bool


class InvitationStore(Protocol):
    def read_invitation(self, business_id: str, client_id: str) -> ClientInvitation | None: ...
    def write_invitation(self, invitation: ClientInvitation,
                         previous: ClientInvitation | None) -> None: ...
    def activate_link(self, invitation: ClientInvitation,
                      pending: IdentityLink, now: datetime) -> None: ...
    def mark_sent(self, invitation: ClientInvitation, now: datetime,
                  expires_at: datetime) -> ClientInvitation: ...
    def revoke_invitation(self, invitation: ClientInvitation, now: datetime) -> None: ...
    def read_profile(self, business_id: str, client_id: str) -> object | None: ...


class AccountDirectory(Protocol):
    def create_or_find_invitee(self, email: str) -> tuple[str, bool]:
        """Return (subject, created). Existing unrelated accounts must be rejected."""

    def send_invitation(self, email: str, *, created: bool) -> None: ...


def normalize_email(email: str) -> str:
    value = email.strip().casefold()
    if not value or value.count("@") != 1 or any(c.isspace() for c in value):
        raise InvitationDenied("Invalid invitation email")
    return value


def email_fingerprint(email: str) -> str:
    return sha256(normalize_email(email).encode()).hexdigest()


class ClientAccountInvitations:
    def __init__(self, store: InvitationStore, links: IdentityLinks,
                 directory: AccountDirectory,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._store = store
        self._links = links
        self._directory = directory
        self._lifetime = timedelta(hours=24)
        self._now = clock or (lambda: datetime.now(UTC))

    def status(self, business_id: str, client_id: str) -> ClientInvitation | None:
        if self._store.read_profile(business_id, client_id) is None:
            raise InvitationDenied("Client profile does not exist")
        return self._store.read_invitation(business_id, client_id)

    def invite(self, business_id: str, client_id: str, email: str,
               owner_subject: str, now: datetime | None = None) -> ClientInvitation:
        issued_at = now or self._now()
        address = normalize_email(email)
        if self._store.read_profile(business_id, client_id) is None:
            raise InvitationDenied("Client profile does not exist")
        prior = self._store.read_invitation(business_id, client_id)
        if prior is not None and prior.consumed_at is not None and prior.revoked_at is None:
            raise InvitationDenied("Client profile already activated")
        if (prior is not None and prior.revoked_at is None
                and (prior.sent_at is None or prior.expires_at > issued_at)):
            if prior.email_hash != email_fingerprint(address):
                raise InvitationDenied("Revoke the previous invitation before changing email")
            if prior.sent_at is None:
                self._directory.send_invitation(address, created=False)
                sent_at = now or self._now()
                return self._store.mark_sent(prior, sent_at, sent_at + self._lifetime)
            return prior
        if prior is not None and prior.revoked_at is None:
            self.revoke(business_id, client_id, owner_subject, issued_at)
        subject, created = self._directory.create_or_find_invitee(address)
        link = self._links.read_link(subject)
        if link is None:
            self._links.change(subject, LinkRole.CLIENT, business_id, client_id,
                               LinkState.PENDING, owner_subject, "client invited",
                               expected_version=None, now=issued_at)
        elif (link.role != LinkRole.CLIENT or link.business_id != business_id
              or link.client_id != client_id or link.state == LinkState.ACTIVE):
            raise InvitationDenied("Account is already linked")
        elif link.state == LinkState.REVOKED:
            self._links.change(subject, LinkRole.CLIENT, business_id, client_id,
                               LinkState.PENDING, owner_subject, "client reinvited",
                               expected_version=link.version, now=issued_at)
        invitation = ClientInvitation(business_id, client_id, subject,
                                      email_fingerprint(address),
                                      issued_at + self._lifetime, issued_at,
                                      owner_subject)
        self._store.write_invitation(invitation, prior)
        self._directory.send_invitation(address, created=created)
        sent_at = now or self._now()
        return self._store.mark_sent(invitation, sent_at, sent_at + self._lifetime)

    def activate(self, business_id: str, client_id: str, account: VerifiedAccount,
                 now: datetime | None = None) -> None:
        instant = now or self._now()
        invite = self._store.read_invitation(business_id, client_id)
        if (invite is None or invite.consumed_at is not None or invite.expires_at <= instant
                or invite.sent_at is None or invite.revoked_at is not None
                or invite.subject != account.subject or not account.email_verified
                or invite.email_hash != email_fingerprint(account.email)
                or self._store.read_profile(business_id, client_id) is None):
            raise InvitationDenied("Invitation is unavailable or identity is unverified")
        link = self._links.read_link(account.subject)
        if (link is None or link.role != LinkRole.CLIENT or link.state != LinkState.PENDING
                or link.business_id != business_id or link.client_id != client_id):
            raise InvitationDenied("Pending identity link is unavailable")
        self._store.activate_link(invite, link, instant)

    def activate_pending(self, account: VerifiedAccount, now: datetime | None = None) -> None:
        """Resolve the invitation from the verified subject, never browser-supplied IDs."""
        link = self._links.read_link(account.subject)
        if (link is None or link.role != LinkRole.CLIENT or link.state != LinkState.PENDING
                or not link.client_id):
            raise InvitationDenied("Pending invitation is unavailable")
        self.activate(link.business_id, link.client_id, account, now)

    def revoke(self, business_id: str, client_id: str, owner_subject: str,
               now: datetime | None = None) -> None:
        instant = now or self._now()
        invite = self._store.read_invitation(business_id, client_id)
        if invite is None or invite.revoked_at is not None:
            raise InvitationDenied("Invitation is unavailable")
        link = self._links.read_link(invite.subject)
        if (link is not None and link.role == LinkRole.CLIENT
                and link.business_id == business_id and link.client_id == client_id
                and link.state != LinkState.REVOKED):
            self._links.change(invite.subject, LinkRole.CLIENT, business_id, client_id,
                               LinkState.REVOKED, owner_subject, "invitation revoked",
                               expected_version=link.version, now=instant)
        self._store.revoke_invitation(invite, instant)
