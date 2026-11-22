#!/usr/bin/env bash
# fix_docker_compose_companions — create the Docker secret files that
# docker/docker-compose.yml expects, BEFORE the first `docker compose up`.
#
#   bash scripts/init_secrets.sh
#
# Creates <repo>/.secrets/{postgres_password,vault_master_key,
# vault_backup_passphrase}.txt with random values, chmod 600.
# (Compose references them as ../.secrets/*.txt relative to docker/,
# i.e. <repo>/.secrets/ — this script resolves the repo root from its
# own location, so it works from any cwd.)
#
# Idempotent and SAFE to re-run: an existing file is KEPT, never
# rotated — silently replacing vault_master_key.txt would orphan every
# credential the vault encrypted with it. Delete a file by hand if you
# really mean to rotate that one secret.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="$REPO_ROOT/.secrets"
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

gen_random() {
  # 32 random bytes, base64 — suitable for passwords, the vault master
  # key (KEK material) and the backup passphrase alike.
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 32
  else
    head -c 32 /dev/urandom | base64
  fi
}

for name in postgres_password vault_master_key vault_backup_passphrase; do
  target="$SECRETS_DIR/$name.txt"
  if [ -s "$target" ]; then
    echo "keep   $target (already exists, not rotated)"
  else
    gen_random > "$target"
    echo "create $target"
  fi
  chmod 600 "$target"
done

echo "Done. Secrets live in $SECRETS_DIR (git-ignored via .gitignore: .secrets/)."
echo "Next: docker compose -f docker/docker-compose.yml up -d --build"
