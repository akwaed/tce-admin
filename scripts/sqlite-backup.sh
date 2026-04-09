#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
database_file="${1:-${project_root}/instance/tce_admin.db}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
output_file="${2:-${project_root}/backups/tce-admin-sqlite-${timestamp}.db}"

if [[ ! -f "${database_file}" ]]; then
  echo "SQLite database not found: ${database_file}"
  exit 1
fi

mkdir -p "$(dirname "${output_file}")"

echo "Creating SQLite backup..."
echo "  Source: ${database_file}"
echo "  Output: ${output_file}"

sqlite3 "${database_file}" ".backup '${output_file}'"

echo "Backup complete."
