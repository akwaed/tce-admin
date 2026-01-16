#!/bin/bash
# Daily TCE Admin Data Sync
# Fetches data from HANA and syncs to the database
#
# Usage: ./scripts/daily_sync.sh
# Cron:  0 5 * * * /path/to/tce-admin/scripts/daily_sync.sh >> /var/log/tce-sync.log 2>&1

set -e

# Change to project directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Load environment variables
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
elif [ -d ".venv" ]; then
    source .venv/bin/activate
fi

echo "========================================"
echo "TCE Admin Daily Sync - $(date)"
echo "========================================"

# Step 1: Fetch data from HANA
echo ""
echo "Step 1: Fetching data from HANA..."
python scripts/hana_sync.py --output ./datasources

# Step 2: Sync to database
echo ""
echo "Step 2: Syncing to database..."
python -c "
import sys
sys.path.insert(0, '.')
from app import create_app
from app.services.course_sync import CourseSyncService, resolve_datasources_path

app = create_app()
with app.app_context():
    path = resolve_datasources_path('./datasources')
    sync = CourseSyncService(path)
    result = sync.sync_all()

    print('Sync Results:')
    print(f'  Courses added: {result[\"stats\"][\"courses_added\"]}')
    print(f'  Courses updated: {result[\"stats\"][\"courses_updated\"]}')
    print(f'  Instructors added: {result[\"stats\"][\"instructors_added\"]}')
    print(f'  Students counted: {result[\"stats\"][\"students_counted\"]}')

    if result['errors']:
        print(f'  Warnings: {len(result[\"errors\"])}')
        for err in result['errors'][:5]:
            print(f'    - {err}')

    sys.exit(0 if result['success'] else 1)
"

echo ""
echo "Sync completed at $(date)"
