#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage:"
  echo "  ./scripts/postgres-restore.sh <backup.dump> --yes-i-understand"
  echo
  echo "This script overwrites objects in DATABASE_URL using pg_restore --clean."
}

if [[ $# -ne 2 ]]; then
  usage
  exit 1
fi

backup_file="$1"
confirmation="$2"

if [[ "${confirmation}" != "--yes-i-understand" ]]; then
  usage
  exit 1
fi

if [[ ! -f "${backup_file}" ]]; then
  echo "Backup file not found: ${backup_file}"
  exit 1
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is not set."
  echo "Example:"
  echo "  export DATABASE_URL='postgresql://user:pass@host:5432/dbname'"
  exit 1
fi

echo "Restoring PostgreSQL backup into DATABASE_URL..."
echo "  Backup: ${backup_file}"
echo "  Make sure the web app is stopped before continuing."

pg_restore \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  --dbname "${DATABASE_URL}" \
  "${backup_file}"

echo "Restore complete."
