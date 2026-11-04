#!/usr/bin/env bash
set -euo pipefail
if [[ $(id -u) == 0 ]]; then
  install -d -m 700 -o runner -g runner /job-state /home/runner/.config /home/runner/.config/gh
  install -d -m 2770 -o codex -g runner /workspace
  # The issue's host workspace retains its conversation and SQLite index.
  # Credentials remain links to the shared login; they are never copied into this folder.
  CODEX_HOME=/workspace/.codex /opt/codex-review/init-auth.sh
  install -d -m 700 -o codex -g codex /workspace/.codex-runtime
  if [[ -S /var/run/docker.sock ]]; then
    socket_gid="$(stat -c '%g' /var/run/docker.sock)"
    socket_group="$(getent group "$socket_gid" | cut -d: -f1 || true)"
    if [[ -z "$socket_group" ]]; then
      socket_group=codex-docker
      groupadd --gid "$socket_gid" "$socket_group"
    fi
    usermod -aG "$socket_group" codex
    usermod -aG "$socket_group" runner
  fi
  for file in hosts.yml config.yml; do
    if [[ -f /home/runner/.config/gh/$file ]]; then
      chown runner:runner "/home/runner/.config/gh/$file"
      chmod 600 "/home/runner/.config/gh/$file"
    fi
  done
  # Codex login is shared; CLI execution and temporary files are independent.
  exec runuser -u runner -- /opt/codex-coding/entrypoint.sh "$@"
fi
umask 0002
exec python3 -u /opt/codex-coding/worker.py "$@"
