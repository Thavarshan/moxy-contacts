"""Check that public requests cannot reach private Worker routes."""

from __future__ import annotations

import os
import sys
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def request_status(opener, url: str) -> tuple[int, str]:  # type: ignore[no-untyped-def]
    request = Request(url, headers={"User-Agent": "moxy-access-smoke/1.0"})
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, response.headers.get("Location", "")
    except HTTPError as error:
        return error.code, error.headers.get("Location", "")


def blocked_by_access(status: int, location: str) -> bool:
    if status in {401, 403}:
        return True
    return status in {301, 302, 303, 307, 308} and (
        "/cdn-cgi/access/login" in location or "cloudflareaccess.com" in location
    )


def main() -> int:
    base_url = os.environ.get("PRODUCTION_WORKER_URL", "").rstrip("/")
    if not base_url.startswith("https://"):
        print("PRODUCTION_WORKER_URL must be an https URL", file=sys.stderr)
        return 2

    opener = build_opener(NoRedirect)
    for path in ("/", "/contacts", "/archive/backup.zip", "/archive/restore"):
        status, location = request_status(opener, f"{base_url}{path}")
        if not blocked_by_access(status, location):
            print(
                f"Unauthenticated access was not blocked for {path} (HTTP {status}).",
                file=sys.stderr,
            )
            return 1
    print("Cloudflare Access blocks unauthenticated app, archive, and backup routes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
