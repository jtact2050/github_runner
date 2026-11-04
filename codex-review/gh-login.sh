#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /tmp
# File storage is intentional: this login travels with credentials/gh-review/.
echo "Sign in as ${REVIEW_GITHUB_USER:-jacktian8888}; the reviewer verifies this identity before posting."
if [[ $(id -u) == 0 ]]; then
  exec runuser -u runner -- env -u GH_TOKEN -u GITHUB_TOKEN \
    GH_CONFIG_DIR=/home/runner/.config/gh \
    gh auth login --hostname github.com --git-protocol https --web --insecure-storage
fi
exec env -u GH_TOKEN -u GITHUB_TOKEN GH_CONFIG_DIR=/home/runner/.config/gh \
  gh auth login --hostname github.com --git-protocol https --web --insecure-storage
