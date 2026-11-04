# GitHub runners

Docker-based GitHub Actions runners, an unattended issue coding dispatcher, and
an independent Codex PR review runner. This repository contains the runner source,
tests, container definitions, and workflow examples. Application repositories and
private runtime state stay outside version control.

| Component | Files | Documentation |
| --- | --- | --- |
| Issue coding dispatcher | `coding-runner/`, `run-issues.sh`, `Dockerfile.codex-coding` | [Coding runner](coding-runner/README.md) |
| Independent PR reviewer | `codex-review/`, `compose.codex-review.yml`, `Dockerfile.codex-review` | [Review runner](codex-review/README.md) |
| Three general Actions runners | `docker-compose.yml`, `Dockerfile.runner` | Configuration comments in the Compose file |
| Target repository review workflow | `.github/workflows/codex-review.yml` | Install in the repository being reviewed; expects its `Verify` workflow |
| Parallel runner smoke test | `parallel.yml` | Copy into the target repository's `.github/workflows/` to run manually |

## Setup

Use Docker with Compose, Python 3, and Git. Docker Desktop installations need host
networking enabled for coding tests that reach temporary databases through localhost.
Runner labels in the target workflows must match the configured runner labels.

Create local configuration without overwriting an existing file:

```sh
test -f .env || cp .env.example .env
```

Set `GITHUB_URL` and any required registration tokens in `.env`. Follow the component
guides above for GitHub and Codex login, account permissions, and first registration.
The current coding/review configuration uses separate GitHub identities; see those
guides before adapting it to another account.

Build the coding and review images:

```sh
./run-issues.sh --build-only
```

Inspect eligible issues, then start the dispatcher when ready:

```sh
./run-issues.sh --dry-run
./run-issues.sh --concurrency 2
```

Starting the dispatcher authorizes its documented issue-to-PR lifecycle, including
merging after CI and independent review pass. The general Actions runners can be
started separately with `docker compose up -d --build`; the review runner uses
`docker compose -f compose.codex-review.yml up -d --build`.

## Local validation

Run the runner/controller tests without starting jobs or contacting GitHub:

```sh
python3 -m unittest discover -s coding-runner/tests -v
python3 -m unittest discover -s codex-review/tests -v
```

For application work, the coding AI follows the target repository's automatic CI
test selection and setup. It runs only `pnpm test:all` plus required automatic CI
checks that command omits. No focused tests or other extra runs are allowed, even
while debugging. Unchanged passing results can be reused on resume. See the coding
runner guide for the full validation policy and how to rebuild images after changing
the runner.

## Local files

Git excludes `.env`, `credentials/`, `.coding-state/`, local caches, logs, backups,
and the separate `oaxontax/` and `oaxontax-ai-review-design/` checkouts. Keep additional
application checkouts under `workspaces/` or add an explicit ignore entry. `.env.example`
contains placeholders only and is tracked.

Job state includes saved work and conversations; ignoring it does not make it
disposable. Follow the coding runner recovery instructions before moving or removing
state. Credentials and state are also excluded from Docker build contexts by the
existing allowlist in `.dockerignore`.
