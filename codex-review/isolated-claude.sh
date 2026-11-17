#!/usr/bin/env bash
set -euo pipefail
umask 077
# Each review starts with a clean Claude config home, even on the long-lived runner.
# Only the long-lived subscription token from `claude setup-token` is shared.
token_file=/home/codex/.claude-auth/oauth-token
if [[ ! -s $token_file ]]; then
  echo 'Claude login missing; run /opt/codex-review/claude-login.sh' >&2
  exit 3
fi
mkdir -p /home/codex/reviews
review_home=$(mktemp -d /home/codex/reviews/claude-XXXXXXXX)
trap 'rm -rf -- "$review_home"' EXIT
export HOME="$review_home" CLAUDE_CONFIG_DIR="$review_home/.claude"
export DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# The token arrives on a one-shot pipe (fd 3), never in the environment, so
# sandboxed tool commands cannot recover it from inherited env or /proc.
export CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3
claude "$@" 3< <(tr -d '[:space:]' < "$token_file")
