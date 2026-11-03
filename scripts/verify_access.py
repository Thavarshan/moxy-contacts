"""Fail closed unless Cloudflare Access protects this Worker for one exact email."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from contacts.access_config import allows_only_owner

API = "https://api.cloudflare.com/client/v4"
WORKER_NAME = "moxy-contacts"


class AccessConfigurationError(RuntimeError):
    """Cloudflare does not have the expected private Worker policy."""


def _get_json(token: str, url: str) -> Mapping[str, object]:
    request = Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read())
    except HTTPError as error:
        raise AccessConfigurationError(
            f"Cloudflare API request failed (HTTP {error.code}); check the API token permissions."
        ) from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise AccessConfigurationError(
            "Cloudflare Access settings could not be verified."
        ) from error
    if not isinstance(payload, Mapping) or payload.get("success") is not True:
        raise AccessConfigurationError("Cloudflare Access settings could not be verified.")
    result = payload.get("result")
    if not isinstance(result, (Mapping, list)):
        raise AccessConfigurationError("Cloudflare Access returned an unexpected response.")
    return payload


def verify_from_environment() -> None:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    owner_email = os.environ.get("ACCESS_OWNER_EMAIL", "").casefold()
    if not token or not account_id or not owner_email:
        raise AccessConfigurationError(
            "CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, and ACCESS_OWNER_EMAIL are required."
        )

    account = quote(account_id, safe="")
    worker = _get_json(token, f"{API}/accounts/{account}/workers/workers/{WORKER_NAME}")["result"]
    if not isinstance(worker, Mapping) or worker.get("name") != WORKER_NAME:
        raise AccessConfigurationError(
            "The production Worker was not found in this Cloudflare account."
        )
    worker_id = str(worker.get("id", ""))
    if not worker_id:
        raise AccessConfigurationError("Cloudflare did not return a Worker ID.")
    worker_name = str(worker.get("name", ""))

    applications: list[Mapping[str, object]] = []
    page = 1
    while True:
        response = _get_json(
            token,
            f"{API}/accounts/{account}/access/apps?page={page}&per_page=100",
        )
        results = response["result"]
        if not isinstance(results, list):
            raise AccessConfigurationError(
                "Cloudflare Access returned an unexpected application list."
            )
        applications.extend(item for item in results if isinstance(item, Mapping))
        info = response.get("result_info", {})
        total_pages = info.get("total_pages", page) if isinstance(info, Mapping) else page
        if page >= int(total_pages):
            break
        page += 1

    target_apps = []
    for application in applications:
        destinations = application.get("destinations", [])
        if isinstance(destinations, list) and any(
            isinstance(destination, Mapping)
            and destination.get("type") == "worker"
            and destination.get("worker_id") == worker_id
            for destination in destinations
        ):
            target_apps.append(application)
    if len(target_apps) != 1:
        destination_summary = []
        for application in applications[:10]:
            destinations = application.get("destinations", [])
            if not isinstance(destinations, list):
                continue
            for destination in destinations:
                if not isinstance(destination, Mapping):
                    continue
                destination_id = str(destination.get("worker_id", ""))
                destination_summary.append(
                    f"{destination.get('type', 'unknown')}"
                    f"(id_matches={destination_id == worker_id}, "
                    f"name_matches={destination_id == worker_name})"
                )
        summary = ", ".join(destination_summary) or "no worker destinations returned"
        raise AccessConfigurationError(
            "Cloudflare Access is not configured for this Worker "
            f"(received {len(applications)} applications; destinations: {summary})."
        )

    policies_by_application: dict[str, list[Mapping[str, object]]] = {}
    app_id = str(target_apps[0].get("id", ""))
    policies_response = _get_json(
        token,
        f"{API}/accounts/{account}/access/apps/{quote(app_id, safe='')}/policies",
    )
    policies = policies_response["result"]
    if not isinstance(policies, list):
        raise AccessConfigurationError("Cloudflare Access returned an unexpected policy list.")
    policies_by_application[app_id] = [item for item in policies if isinstance(item, Mapping)]

    if not allows_only_owner(applications, policies_by_application, worker_id, owner_email):
        raise AccessConfigurationError(
            "Cloudflare Access must have exactly one allow policy "
            "for the owner's email on this Worker."
        )


def main() -> int:
    try:
        verify_from_environment()
    except AccessConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 1
    print("Verified: Cloudflare Access allows only the configured owner email for this Worker.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
