#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $(id -u) == 0 ]]; then
  install -d -m 700 -o runner -g runner /runner-state /home/runner/_work
  /opt/codex-review/init-auth.sh
  install -d -m 700 -o runner -g runner /home/runner/.config "${GH_CONFIG_DIR:-/home/runner/.config/gh}"
  for file in hosts.yml config.yml; do
    if [[ -f "${GH_CONFIG_DIR:-/home/runner/.config/gh}/$file" && ! -L "${GH_CONFIG_DIR:-/home/runner/.config/gh}/$file" ]]; then
      chown runner:runner "${GH_CONFIG_DIR:-/home/runner/.config/gh}/$file"
      chmod 600 "${GH_CONFIG_DIR:-/home/runner/.config/gh}/$file"
    fi
  done
  exec runuser -u runner -- /opt/codex-review/entrypoint.sh "$@"
fi

if [[ ${1:-} == login ]]; then
  exec /opt/codex-review/login.sh
fi

cd /home/runner
files=(.runner .credentials .credentials_rsaparams .env .path)
for file in "${files[@]}"; do
  if [[ -f /runner-state/$file ]]; then
    ln -sfn "/runner-state/$file" "$file"
  fi
done

if [[ ! -f .runner ]]; then
  : "${GITHUB_URL:?Set GITHUB_URL}"
  : "${RUNNER_TOKEN:?Set a fresh REVIEW_RUNNER_TOKEN in .env}"
  ./config.sh --url "$GITHUB_URL" --token "$RUNNER_TOKEN" \
    --name "${RUNNER_NAME:-codex-pr-review-1}" --labels codex-review \
    --no-default-labels --work _work --unattended
  for file in "${files[@]}"; do
    if [[ -f $file && ! -L $file ]]; then
      mv "$file" "/runner-state/$file"
      ln -s "/runner-state/$file" "$file"
    fi
  done
fi
unset RUNNER_TOKEN
# Register before waiting, because GitHub registration tokens expire quickly.
# The listener only starts after the operator has completed Codex login.
until sudo -H -u codex env CODEX_HOME="$CODEX_HOME" codex login status >/dev/null 2>&1; do
  echo 'Codex login required. Run: docker compose -f compose.codex-review.yml exec codex-review /opt/codex-review/login.sh'
  sleep 30
done
exec ./run.sh
