#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# backup_db.sh  -  Snapshot the tce_admin PostgreSQL database + datasources
#
# Usage:
#   bash scripts/backup_db.sh
#
# Environment variables (or set them in .env):
#   PGUSER      - PostgreSQL username  (default: tceadmin)
#   PGDATABASE  - PostgreSQL database  (default: tce_admin)
#   PGHOST      - PostgreSQL host      (default: localhost)
#   PGPORT      - PostgreSQL port      (default: 5432)
#   BACKUP_DIR  - Output directory     (default: /var/backups/tce-admin)
#   DATASOURCES_DIR - CSV source dir   (default: <project>/datasources)
#   PG_KEEP     - How many DB dumps to keep rolling (default: 10)
#   DS_KEEP     - How many CSV snapshots to keep    (default: 3)
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Load .env if present (key=value format, no export required)
if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env"
    set +a
fi

PGUSER="${PGUSER:-tceadmin}"
PGDATABASE="${PGDATABASE:-tce_admin}"
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/tce-admin}"
DATASOURCES_DIR="${DATASOURCES_DIR:-$PROJECT_ROOT/datasources}"
PG_KEEP="${PG_KEEP:-10}"
DS_KEEP="${DS_KEEP:-3}"

TS="$(date +%Y%m%d_%H%M%S)"
DB_BACKUP_DIR="$BACKUP_DIR/db"
DS_BACKUP_DIR="$BACKUP_DIR/datasources_snapshots"

mkdir -p "$DB_BACKUP_DIR" "$DS_BACKUP_DIR"

# ---------------------------------------------------------------------------
# 1. PostgreSQL dump
# ---------------------------------------------------------------------------
DUMP_FILE="$DB_BACKUP_DIR/tce_admin_${TS}.dump"
echo "[backup] Dumping PostgreSQL database '$PGDATABASE' to $DUMP_FILE ..."
PGPASSWORD="${PGPASSWORD:-}" pg_dump \
    --format=custom \
    --host="$PGHOST" \
    --port="$PGPORT" \
    --username="$PGUSER" \
    --no-privileges \
    --file="$DUMP_FILE" \
    "$PGDATABASE"
echo "[backup] Database dump complete: $(du -sh "$DUMP_FILE" | cut -f1)"

# Rolling cleanup: keep only the $PG_KEEP most recent dumps.
DUMP_COUNT=$(find "$DB_BACKUP_DIR" -name '*.dump' | wc -l)
if (( DUMP_COUNT > PG_KEEP )); then
    echo "[backup] Pruning old DB dumps (keeping $PG_KEEP)..."
    find "$DB_BACKUP_DIR" -name '*.dump' -printf '%T+ %p\n' \
        | sort | head -n $(( DUMP_COUNT - PG_KEEP )) \
        | awk '{print $2}' | xargs -r rm -v
fi

# ---------------------------------------------------------------------------
# 2. Datasources CSV snapshot
# ---------------------------------------------------------------------------
if [[ -d "$DATASOURCES_DIR" ]]; then
    SNAP_FILE="$DS_BACKUP_DIR/datasources_${TS}.tar.gz"
    echo "[backup] Snapshotting datasources directory to $SNAP_FILE ..."
    tar -czf "$SNAP_FILE" -C "$(dirname "$DATASOURCES_DIR")" "$(basename "$DATASOURCES_DIR")"
    echo "[backup] Datasources snapshot complete: $(du -sh "$SNAP_FILE" | cut -f1)"

    # Rolling cleanup: keep only the $DS_KEEP most recent snapshots.
    SNAP_COUNT=$(find "$DS_BACKUP_DIR" -name '*.tar.gz' | wc -l)
    if (( SNAP_COUNT > DS_KEEP )); then
        echo "[backup] Pruning old CSV snapshots (keeping $DS_KEEP)..."
        find "$DS_BACKUP_DIR" -name '*.tar.gz' -printf '%T+ %p\n' \
            | sort | head -n $(( SNAP_COUNT - DS_KEEP )) \
            | awk '{print $2}' | xargs -r rm -v
    fi
else
    echo "[backup] WARNING: datasources directory not found at $DATASOURCES_DIR — skipping snapshot."
fi

echo "[backup] Done. Backups stored in $BACKUP_DIR"
