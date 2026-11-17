#!/usr/bin/env bash
set -euo pipefail
umask 077
# Stores a long-lived Claude subscription token (from `claude setup-token`) in
# credentials/claude/oauth-token. It does not refresh, so concurrent containers
# can share it without OAuth refresh races.
#   Interactive:  claude-login.sh
#   From host:    claude setup-token   # then paste via: claude-login.sh --stdin
if [[ $(id -u) == 0 ]]; then
  exec runuser -u codex -- "$0" "$@"
fi
if [[ $(id -un) != codex ]]; then
  exec sudo -H -u codex "$0" "$@"
fi
cd /tmp
auth_dir=/home/codex/.claude-auth
if [[ ${1:-} == --stdin ]]; then
  IFS= read -r token
else
  setup_home=$(mktemp -d)
  trap 'rm -rf -- "$setup_home"' EXIT
  HOME="$setup_home" CLAUDE_CONFIG_DIR="$setup_home/.claude" DISABLE_AUTOUPDATER=1 \
    claude setup-token
  echo
  IFS= read -rsp 'Paste the token printed above (input hidden): ' token
  echo
fi
token=$(printf '%s' "$token" | tr -d '[:space:]')
if [[ -z $token ]]; then
  echo 'No token received; nothing saved.' >&2
  exit 1
fi
printf '%s\n' "$token" > "$auth_dir/.oauth-token.tmp"
chmod 600 "$auth_dir/.oauth-token.tmp"
mv -f "$auth_dir/.oauth-token.tmp" "$auth_dir/oauth-token"
echo "Saved Claude subscription token to credentials/claude/oauth-token."
