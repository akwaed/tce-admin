#!/usr/bin/env python3
"""
Standalone CSV -> PostgreSQL sync script.

Run by the web UI as a subprocess (not in-thread) to avoid SQLAlchemy
connection pool contention with the running Flask app.  Also used by the
daily cron via daily_sync.sh.

Usage:
    python scripts/db_sync.py [--datasources PATH] [--scheduled]

Flags:
    --scheduled   Write a DataSyncLog row so the UI can see this run.
                  Use this when called from cron / daily_sync.sh.
                  Without it the script still syncs but produces no
                  UI-visible log entry (the web UI creates its own row
                  before calling this script).

Exit codes:
    0  success
    1  sync failed (error written to stdout as JSON)

Output: single JSON object on stdout with keys:
    success, stats, errors, elapsed_seconds, sync_run_id, sync_log_id
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / '.env')
except ImportError:
    pass


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--datasources', default=str(PROJECT_ROOT / 'datasources'))
    parser.add_argument(
        '--scheduled', action='store_true',
        help='Create a DataSyncLog entry so this run appears in the UI sync log.'
    )
    args = parser.parse_args()

    from app import create_app
    from app.services.course_sync import CourseSyncService, resolve_datasources_path

    app = create_app(os.environ.get('FLASK_ENV', 'production'))
    with app.app_context():
        from app.models import db
        from app.models.settings import DataSyncLog

        ds_path = resolve_datasources_path(args.datasources)

        # ------------------------------------------------------------------
        # Optionally create a DataSyncLog row so the UI shows this run.
        # ------------------------------------------------------------------
        sync_log = None
        if args.scheduled:
            sync_log = DataSyncLog(
                sync_type=DataSyncLog.TYPE_HANA_TO_DATASOURCE,
                status=DataSyncLog.STATUS_RUNNING,
                trigger_type='scheduled',
                triggered_by_id=None,  # no user — automated
            )
            sync_log.summary = {
                'pipeline_phase': 'database_sync',
                'pipeline_step': 1,
                'pipeline_total_steps': 1,
                'pipeline_message': 'Scheduled: Syncing CSV datasources into the database...',
            }
            db.session.add(sync_log)
            db.session.commit()

        service = CourseSyncService(ds_path)
        try:
            result = service.sync_all()

            if sync_log:
                stats = result.get('stats', {})
                sync_log.status = DataSyncLog.STATUS_COMPLETED
                sync_log.records_processed = (
                    stats.get('courses_added', 0)
                    + stats.get('courses_updated', 0)
                    + stats.get('instructors_added', 0)
                    + stats.get('students_counted', 0)
                )
                sync_log.complete(success=True)
                sync_log.summary = {
                    'pipeline_phase': 'done',
                    'pipeline_step': 1,
                    'pipeline_total_steps': 1,
                    'pipeline_message': 'Scheduled sync completed successfully.',
                    'courses_added': stats.get('courses_added', 0),
                    'courses_updated': stats.get('courses_updated', 0),
                    'courses_removed': stats.get('courses_removed', 0),
                    'instructors_added': stats.get('instructors_added', 0),
                    'students_counted': stats.get('students_counted', 0),
                    'elapsed_seconds': result.get('elapsed_seconds', 0),
                }
                db.session.commit()
                result['sync_log_id'] = sync_log.id

            print(json.dumps(result, default=str))
            sys.exit(0)

        except Exception as e:
            if sync_log:
                sync_log.status = DataSyncLog.STATUS_FAILED
                sync_log.complete(success=False)
                sync_log.summary = {
                    'pipeline_phase': 'error',
                    'pipeline_message': f'Scheduled sync failed: {e}',
                    'error': str(e),
                }
                db.session.commit()

            print(json.dumps({
                'success': False,
                'errors': [str(e)],
                'stats': {},
                'elapsed_seconds': 0,
                'sync_log_id': sync_log.id if sync_log else None,
            }))
            sys.exit(1)


if __name__ == '__main__':
    main()
