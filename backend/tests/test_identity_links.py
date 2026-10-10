"""Identity links grant only the active, unique, existing target."""

from dataclasses import replace

import pytest

from scheduling.identity_links import (
    IdentityLink,
    IdentityLinks,
    LinkConflict,
    LinkRole,
    LinkState,
)


class Records:
    def __init__(self) -> None:
        self.links: dict[str, IdentityLink] = {}
        self.targets: dict[tuple[LinkRole, str, str | None], str] = {}
        self.profiles = {("one", "client-a"), ("two", "client-b")}
        self.businesses = {"one", "two"}

    def read_link(self, subject: str) -> IdentityLink | None:
        return self.links.get(subject)

    def read_target_subject(self, role: LinkRole, business_id: str,
                            client_id: str | None) -> str | None:
        return self.targets.get((role, business_id, client_id))

    def read_profile(self, business_id: str, client_id: str) -> object | None:
        return object() if (business_id, client_id) in self.profiles else None

    def read_policy_record(self, business_id: str) -> object | None:
        return object() if business_id in self.businesses else None

    def write_link(self, link: IdentityLink, previous: IdentityLink | None) -> None:
        target = (link.role, link.business_id, link.client_id)
        if previous is None and link.subject in self.links:
            raise LinkConflict("Subject already linked")
        if previous is not None and self.links[link.subject].version != previous.version:
            raise LinkConflict("Version changed")
        owner = self.targets.get(target)
        if link.state == LinkState.REVOKED:
            if owner != link.subject:
                raise LinkConflict("Target changed")
            del self.targets[target]
        elif owner not in (None, link.subject):
            raise LinkConflict("Target already linked")
        else:
            self.targets[target] = link.subject
        self.links[link.subject] = link


def test_owner_and_client_links_fail_closed_across_role_scope_and_lifecycle() -> None:
    records = Records()
    links = IdentityLinks(records)
    assert links.resolve("unlinked") is None

    owner = links.change("owner-sub", LinkRole.OWNER, "one", None,
                         LinkState.ACTIVE, "admin", "approved", expected_version=None)
    client = links.change("client-sub", LinkRole.CLIENT, "one", "client-a",
                          LinkState.PENDING, "owner-sub", "invited", expected_version=None)
    assert links.resolve("owner-sub") == owner
    assert links.resolve("client-sub") is None
    client = links.change("client-sub", LinkRole.CLIENT, "one", "client-a",
                          LinkState.ACTIVE, "system", "email verified",
                          expected_version=client.version)
    assert links.resolve("client-sub") == client
    assert links.resolve("client-sub").role != LinkRole.OWNER
    assert links.resolve("client-sub").business_id != "two"
    assert links.resolve("client-sub").client_id != "client-b"

    with pytest.raises(LinkConflict):
        links.change("another", LinkRole.CLIENT, "one", "client-a",
                     LinkState.ACTIVE, "owner-sub", "duplicate", expected_version=None)
    with pytest.raises(LinkConflict):
        links.change("missing", LinkRole.CLIENT, "one", "missing",
                     LinkState.ACTIVE, "owner-sub", "missing", expected_version=None)
    with pytest.raises(LinkConflict):
        links.change("client-sub", LinkRole.CLIENT, "two", "client-b",
                     LinkState.ACTIVE, "owner-sub", "move", expected_version=client.version)

    # Corrupt or stale target pointers cannot grant a second identity or target.
    records.targets[(LinkRole.CLIENT, "one", "client-a")] = "another"
    assert links.resolve("client-sub") is None
    records.targets[(LinkRole.CLIENT, "one", "client-a")] = "client-sub"
    records.profiles.remove(("one", "client-a"))
    assert links.resolve("client-sub") is None
    records.profiles.add(("one", "client-a"))
    client = links.change("client-sub", LinkRole.CLIENT, "one", "client-a",
                          LinkState.REVOKED, "owner-sub", "revoked",
                          expected_version=client.version)
    assert client.state == LinkState.REVOKED
    assert links.resolve("client-sub") is None


def test_duplicate_subject_record_without_target_is_denied() -> None:
    records = Records()
    links = IdentityLinks(records)
    owner = links.change("owner", LinkRole.OWNER, "one", None,
                         LinkState.ACTIVE, "admin", "approved", expected_version=None)
    records.links["duplicate"] = replace(owner, subject="duplicate")
    assert links.resolve("duplicate") is None
