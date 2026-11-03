"""Cloudflare preflight accepts only a single exact-email Worker policy."""

from collections.abc import Mapping, Sequence

from contacts.access_config import allows_only_owner

OWNER = "tjthavarshan@gmail.com"
WORKER_ID = "0123456789abcdef0123456789abcdef"
APPLICATION: Mapping[str, object] = {
    "id": "access-app-id",
    "type": "self_hosted",
    "destinations": [{"type": "worker", "worker_id": WORKER_ID}],
}
POLICY: Mapping[str, object] = {
    "decision": "allow",
    "include": [{"email": {"email": OWNER}}],
    "exclude": [],
    "require": [],
}


def check(
    applications: Sequence[Mapping[str, object]],
    policies: Sequence[Mapping[str, object]],
) -> bool:
    return allows_only_owner(
        applications,
        {"access-app-id": policies},
        WORKER_ID,
        OWNER,
    )


def test_accepts_exact_owner_email_on_the_target_worker() -> None:
    assert check([APPLICATION], [POLICY])


def test_rejects_domain_wide_or_everyone_policies() -> None:
    domain_policy = {"decision": "allow", "include": [{"email_domain": {"domain": "gmail.com"}}]}
    everyone_policy = {"decision": "allow", "include": [{"everyone": {}}]}

    assert not check([APPLICATION], [domain_policy])
    assert not check([APPLICATION], [everyone_policy])


def test_rejects_extra_allow_policies_and_wrong_worker() -> None:
    second_allow = {"decision": "allow", "include": [{"email": {"email": "other@example.test"}}]}
    wrong_worker = {
        **APPLICATION,
        "destinations": [{"type": "worker", "worker_id": "another-worker"}],
    }

    assert not check([APPLICATION], [POLICY, second_allow])
    assert not check([wrong_worker], [POLICY])
