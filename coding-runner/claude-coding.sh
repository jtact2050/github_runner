#!/usr/bin/env bash
set -euo pipefail
# Runs Claude Code for one issue as the codex UID.
#   claude-coding.sh --claude-session <uuid> <claude args...>
# The controller assigns the session ID before launch. The first run creates the
# conversation with --session-id; later runs (retries, CI/review rounds) resume
# it from the issue's persistent CLAUDE_CONFIG_DIR (/workspace/.claude).
# Coding files must be readable by the controller through the workspace group.
umask 0002
token_file=${CLAUDE_CODING_TOKEN_FILE:-/home/codex/.claude-auth/oauth-token}
if [[ ! -s $token_file ]]; then
  echo 'Claude login missing; run /opt/codex-review/claude-login.sh in the review container' >&2
  exit 3
fi
if [[ ${1:-} != --claude-session || ! ${2:-} =~ ^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$ ]]; then
  echo 'usage: claude-coding.sh --claude-session <uuid> <claude args...>' >&2
  exit 2
fi
session=$2
shift 2
config=${CLAUDE_CONFIG_DIR:?CLAUDE_CONFIG_DIR must point at the issue Claude home}
mkdir -p "$config"
chmod 700 "$config"
if compgen -G "$config/projects/*/$session.jsonl" >/dev/null; then
  mode=(--resume "$session")
else
  mode=(--session-id "$session")
fi
echo "claude-coding: ${mode[0]} $session" >&2
export DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# The token arrives on a one-shot pipe (fd 3), never in the environment.
export CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3
exec claude "${mode[@]}" "$@" 3< <(tr -d '[:space:]' < "$token_file")
