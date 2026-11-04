#!/usr/bin/env bash
set -euo pipefail
# Share only the live credential cache, never Codex's runtime home.
local_home=${CODEX_HOME:-/home/codex/.codex}
shared_home=/home/codex/.codex-auth
install -d -m 700 -o codex -g codex "$local_home" "$shared_home"
touch "$shared_home/.credential.lock"
for file in auth.json .credential.lock; do
  if [[ -f "$shared_home/$file" ]]; then
    chown codex:codex "$shared_home/$file"
    chmod 600 "$shared_home/$file"
  fi
  ln -sfn "$shared_home/$file" "$local_home/$file"
done
printf 'cli_auth_credentials_store = "file"\n' > "$local_home/config.toml"
chown codex:codex "$local_home/config.toml"
chmod 600 "$local_home/config.toml"
