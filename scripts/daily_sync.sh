#!/usr/bin/env bash
# =============================================================================
# daily_sync.sh  -  TCE Admin nightly HANA -> PostgreSQL sync
#
# Runs two steps:
#   1. scripts/hana_sync.py   : pulls CSV files from SAP HANA
#   2. scripts/db_sync.py     : loads CSVs into PostgreSQL
#
# Usage:
#   bash /var/www/tce-admin/scripts/daily_sync.sh
#
# Cron (runs as ofa-user at 3:00 AM every day):
#   0 3 * * * /var/www/tce-admin/scripts/daily_sync.sh >> /var/log/tce-sync.log 2>&1
#
# Log rotation (add to /etc/logrotate.d/tce-sync):
#   /var/log/tce-sync.log { daily rotate 30 compress missingok notifempty }
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
LOG_FILE="/var/log/tce-sync.log"
LOCK_FILE="/tmp/tce-admin-sync.lock"
DATASOURCES_DIR="$PROJECT_DIR/datasources"
HANA_SCRIPT="$PROJECT_DIR/scripts/hana_sync.py"
DB_SCRIPT="$PROJECT_DIR/scripts/db_sync.py"
DRA_SCRIPT="$PROJECT_DIR/scripts/dra_sync.py"

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# ---------------------------------------------------------------------------
# Lock: prevent overlapping runs
# ---------------------------------------------------------------------------
if [ -e "$LOCK_FILE" ]; then
    OLD_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "unknown")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        log "ERROR: Another sync is already running (PID $OLD_PID). Exiting."
        exit 1
    else
        log "WARNING: Stale lock file found (PID $OLD_PID no longer running). Removing."
        rm -f "$LOCK_FILE"
    fi
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"; log "Sync process exited."' EXIT

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
cd "$PROJECT_DIR"

if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/.env"
    set +a
fi

if [ ! -x "$VENV_PYTHON" ]; then
    log "ERROR: Python venv not found at $VENV_PYTHON"
    exit 1
fi

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
log "========================================================"
log "TCE Admin Daily Sync starting"
log "========================================================"
OVERALL_START=$(date +%s)

# ---------------------------------------------------------------------------
# Step 1: HANA -> CSV
# ---------------------------------------------------------------------------
log "Step 1/2: Fetching data from SAP HANA..."
HANA_START=$(date +%s)

if ! "$VENV_PYTHON" "$HANA_SCRIPT" --output "$DATASOURCES_DIR"; then
    log "ERROR: HANA sync failed (exit code $?). Aborting."
    exit 1
fi

HANA_END=$(date +%s)
log "Step 1/2 complete in $(( HANA_END - HANA_START ))s."

# ---------------------------------------------------------------------------
# Step 2: CSV -> PostgreSQL
# ---------------------------------------------------------------------------
log "Step 2/2: Loading CSV data into PostgreSQL..."
DB_START=$(date +%s)

DB_OUTPUT=$("$VENV_PYTHON" "$DB_SCRIPT" --datasources "$DATASOURCES_DIR" 2>&1)
DB_EXIT=$?

DB_END=$(date +%s)

if [ $DB_EXIT -ne 0 ]; then
    log "ERROR: db_sync.py failed (exit code $DB_EXIT)."
    log "Output: $DB_OUTPUT"
    exit 1
fi

# Parse key stats from JSON output (last line)
DB_JSON=$(echo "$DB_OUTPUT" | tail -1)
COURSES_ADDED=$(echo "$DB_JSON"  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('stats',{}).get('courses_added',0))" 2>/dev/null || echo "?")
COURSES_UPDATED=$(echo "$DB_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('stats',{}).get('courses_updated',0))" 2>/dev/null || echo "?")
INSTRUCTORS=$(echo "$DB_JSON"    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('stats',{}).get('instructors_added',0))" 2>/dev/null || echo "?")
STUDENTS=$(echo "$DB_JSON"       | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('stats',{}).get('students_counted',0))" 2>/dev/null || echo "?")
ELAPSED=$(echo "$DB_JSON"        | python3 -c "import sys,json; d=json.load(sys.stdin); print(round(d.get('elapsed_seconds',0),1))" 2>/dev/null || echo "?")

log "Step 2/2 complete in $(( DB_END - DB_START ))s."
log "  Courses  : +${COURSES_ADDED} added, ~${COURSES_UPDATED} updated"
log "  Instructors: +${INSTRUCTORS} added"
log "  Students counted: ${STUDENTS}"
log "  DB sync took: ${ELAPSED}s"

# ---------------------------------------------------------------------------
# Step 3: DRA -> Explorance Blue
# ---------------------------------------------------------------------------
log "Step 3/3: Pushing DRA data to Explorance Blue..."
DRA_START=$(date +%s)

if ! "$VENV_PYTHON" "$DRA_SCRIPT"; then
    log "WARNING: DRA sync failed (exit code $?). Daily DB sync was successful."
    # DRA failure is non-fatal — do not abort or change exit code
fi

DRA_END=$(date +%s)
log "Step 3/3 complete in $(( DRA_END - DRA_START ))s."

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
OVERALL_END=$(date +%s)
log "========================================================"
log "Sync complete. Total time: $(( OVERALL_END - OVERALL_START ))s"
log "========================================================"
exit 0
