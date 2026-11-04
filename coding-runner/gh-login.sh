#!/usr/bin/env bash
set -euo pipefail
umask 077
export GH_CONFIG_DIR=/home/runner/.config/gh
unset GH_TOKEN GITHUB_TOKEN
if [[ $(id -u) == 0 ]]; then
  install -d -m 700 -o runner -g runner "$GH_CONFIG_DIR"
  for file in hosts.yml config.yml; do
    if [[ -f "$GH_CONFIG_DIR/$file" ]]; then
      chown runner:runner "$GH_CONFIG_DIR/$file"
      chmod 600 "$GH_CONFIG_DIR/$file"
    fi
  done
  exec runuser -u runner -- /opt/codex-coding/gh-login.sh
fi
echo 'Sign in as jtact2050. This login is only for coding containers.'
gh auth login --hostname github.com --git-protocol https --web --insecure-storage
identity="$(gh api user --jq .login)"
if [[ "$identity" != jtact2050 ]]; then
  echo "Expected jtact2050, got $identity. Run login again with the correct account." >&2
  exit 1
fi
echo 'Coding GitHub identity verified: jtact2050'
