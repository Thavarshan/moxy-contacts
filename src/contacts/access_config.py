"""Pure validation for the Cloudflare Access policy expected by deployment."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def allows_only_owner(
    applications: Sequence[Mapping[str, object]],
    policies_by_application: Mapping[str, Sequence[Mapping[str, object]]],
    worker_id: str,
    owner_email: str,
) -> bool:
    matching: list[Mapping[str, object]] = []
    for application in applications:
        destinations = application.get("destinations", [])
        if not isinstance(destinations, list):
            continue
        if any(
            isinstance(destination, Mapping)
            and destination.get("type") == "worker"
            and destination.get("worker_id") == worker_id
            for destination in destinations
        ):
            matching.append(application)

    if len(matching) != 1:
        return False
    application = matching[0]
    if application.get("type") != "self_hosted":
        return False
    policies = policies_by_application.get(str(application.get("id", "")), ())
    if len(policies) != 1:
        return False
    policy = policies[0]
    include = policy.get("include", [])
    email_rule = (
        include[0].get("email")
        if isinstance(include, list) and len(include) == 1 and isinstance(include[0], Mapping)
        else None
    )
    return (
        policy.get("decision") == "allow"
        and isinstance(include, list)
        and len(include) == 1
        and isinstance(include[0], Mapping)
        and set(include[0]) == {"email"}
        and isinstance(email_rule, Mapping)
        and set(email_rule) == {"email"}
        and str(email_rule.get("email", "")).casefold() == owner_email.casefold()
        and not policy.get("exclude")
        and not policy.get("require")
    )
