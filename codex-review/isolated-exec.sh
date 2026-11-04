#!/usr/bin/env bash
set -euo pipefail
umask 077
# Each review starts with a clean runtime home, even on the long-lived runner.
mkdir -p /home/codex/reviews
review_home=$(mktemp -d /home/codex/reviews/review-XXXXXXXX)
trap 'rm -rf -- "$review_home"' EXIT
ln -s /home/codex/.codex-auth/auth.json "$review_home/auth.json"
ln -s /home/codex/.codex-auth/.credential.lock "$review_home/.credential.lock"
export CODEX_HOME="$review_home"
export CODEX_SQLITE_HOME="$review_home/runtime"
codex -c "log_dir=\"$review_home/log\"" -c 'cli_auth_credentials_store="file"' "$@"
