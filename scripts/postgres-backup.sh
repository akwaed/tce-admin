#!/usr/bin/env bash
set -euo pipefail

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
default_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/backups"
output_file="${1:-${default_dir}/tce-admin-${timestamp}.dump}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is not set."
  echo "Example:"
  echo "  export DATABASE_URL='postgresql://user:pass@host:5432/dbname'"
  exit 1
fi

mkdir -p "$(dirname "${output_file}")"

echo "Creating PostgreSQL backup..."
echo "  Output: ${output_file}"

pg_dump \
  --format=custom \
  --no-owner \
  --no-privileges \
  --file "${output_file}" \
  "${DATABASE_URL}"

echo "Backup complete."
echo "Restore with:"
echo "  ./scripts/postgres-restore.sh '${output_file}' --yes-i-understand"
