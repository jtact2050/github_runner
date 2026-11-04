#!/usr/bin/env bash
set -euo pipefail
cd /tmp
if [[ $(id -u) == 0 ]]; then
  exec runuser -u codex -- env CODEX_HOME=/home/codex/.codex \
    codex -c 'cli_auth_credentials_store="file"' login --device-auth
fi
exec sudo -H -u codex env CODEX_HOME=/home/codex/.codex \
  codex -c 'cli_auth_credentials_store="file"' login --device-auth
