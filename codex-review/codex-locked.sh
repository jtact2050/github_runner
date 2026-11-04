#!/usr/bin/env bash
set -euo pipefail
# Coding files must be readable by the controller through the workspace group.
# Credentials still live in a private 0700 home and auth.json is managed by Codex.
if [[ ${CODEX_WORKSPACE_GROUP_WRITE:-0} == 1 ]]; then
  umask 0002
else
  umask 0077
fi
# Compatibility filename: CLI calls are intentionally NOT serialized.
# All containers use the same live auth cache, not independently rotating copies.
# Temporary helpers remain container-local via the tmpfs mount.
codex_state_dir="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$codex_state_dir"
exec /usr/local/lib/node_modules/@openai/codex/bin/codex.js "$@"
