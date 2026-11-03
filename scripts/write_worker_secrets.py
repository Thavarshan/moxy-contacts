"""Write or remove a temporary JSON file consumed by Wrangler during deployment."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--remove":
        if len(sys.argv) != 3:
            print("Usage: write_worker_secrets.py --remove PATH", file=sys.stderr)
            return 2
        Path(sys.argv[2]).unlink(missing_ok=True)
        return 0
    if len(sys.argv) != 2:
        print("Usage: write_worker_secrets.py PATH", file=sys.stderr)
        return 2

    secret = os.environ.get("SESSION_SECRET", "")
    if not secret:
        print("SESSION_SECRET is required", file=sys.stderr)
        return 1
    secret_file = Path(sys.argv[1])
    secret_file.write_text(json.dumps({"SESSION_SECRET": secret}), encoding="utf-8")
    secret_file.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
