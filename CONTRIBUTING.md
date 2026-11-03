# Contributing

Thanks for helping improve Moxy Contacts. By participating, you agree to follow
the project's [Code of Conduct](CODE_OF_CONDUCT.md).

## Development

1. Create a branch from `main` and keep personal contact data and credentials out of the repository.
2. Install with `uv sync --locked --all-groups` and `npm ci`.
3. Build assets with `npm run build`.
4. Run `uv run ruff format`, `uv run ruff check`, `uv run mypy`, and `uv run pytest` before opening a pull request.
5. Update `uv.lock` with `uv lock` when Python dependencies change and commit the lockfile. Commit `package-lock.json` when npm dependencies change.

## Issues and pull requests

Use the issue forms for bug reports and feature requests. Bug reports should be
reproducible and use synthetic examples. For changes, explain the behavior and
include tests or documentation updates where useful. Pull requests run the full
CI and local Cloudflare runtime smoke test.

Do not include real contact data, secrets, or private information in issues,
pull requests, fixtures, screenshots, or logs. Report vulnerabilities privately
using the process in [SECURITY.md](SECURITY.md).

Production deployment is a manual workflow dispatch from `main`, after the exact
commit passes checks and a Cloudflare Access policy preflight. Never commit
`.dev.vars`, personal data, or credentials.
