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
import signal
import sys
from datetime import datetime, timezone
UTC = timezone.utc
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
        from app.services.sync_control import SyncCancelledError, mark_sync_cancelled

        ds_path = resolve_datasources_path(args.datasources)

        # ------------------------------------------------------------------
        # Optionally create a DataSyncLog row so the UI shows this run.
        # ------------------------------------------------------------------
        sync_log = None
        if args.scheduled:
            # Guard against duplicate scheduled runs (see similar guard in hana_sync.py for Bug A)
            from datetime import timedelta
            existing = DataSyncLog.query.filter(
                DataSyncLog.sync_type == DataSyncLog.TYPE_HANA_TO_DATASOURCE,
                DataSyncLog.status == DataSyncLog.STATUS_RUNNING,
                DataSyncLog.trigger_type == 'scheduled',
                DataSyncLog.started_at >= datetime.now(UTC) - timedelta(minutes=30),
            ).order_by(DataSyncLog.started_at.desc()).first()
            if existing:
                print("WARNING: Another scheduled HANA/DB sync appears running "
                      f"(log id={existing.id}). Aborting this one to prevent overlap.")
                # For db we are lenient (still proceed) but warn loudly.
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
                'process_pid': os.getpid(),
                'process_started_at': datetime.now(UTC).isoformat(),
            }
            db.session.add(sync_log)
            db.session.commit()
            sync_log_id = sync_log.id  # capture ID; the sync service does db.session.remove() which detaches prior objects
        else:
            sync_log_id = None

        if args.scheduled:
            # Register signal handlers using safe re-query by ID (object may be detached later)
            def handle_stop_signal(signum, _frame):
                current_log = db.session.get(DataSyncLog, sync_log_id) if sync_log_id else None
                if current_log and current_log.status == DataSyncLog.STATUS_RUNNING:
                    summary = current_log.summary
                    summary['pipeline_phase'] = 'failed'
                    summary['pipeline_message'] = f'Scheduled sync terminated by signal {signum}.'
                    current_log.summary = summary
                    current_log.status = DataSyncLog.STATUS_FAILED
                    current_log.completed_at = datetime.now(UTC)
                    errors = current_log.errors
                    errors.append(f'Scheduled sync terminated by signal {signum}.')
                    current_log.errors = errors[:50]
                    db.session.commit()
                sys.exit(128 + signum)

            signal.signal(signal.SIGTERM, handle_stop_signal)
            signal.signal(signal.SIGINT, handle_stop_signal)

        service = CourseSyncService(ds_path)
        try:
            result = service.sync_all(sync_log_id=sync_log_id)

            if sync_log_id:
                # Re-fetch after sync_all (which calls db.session.remove()) to get an attached instance
                sync_log = db.session.get(DataSyncLog, sync_log_id)
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

        except SyncCancelledError as e:
            if sync_log_id:
                current_log = db.session.get(DataSyncLog, sync_log_id)
                if current_log:
                    mark_sync_cancelled(current_log, str(e))
                    errors = current_log.errors
                    errors.append(str(e))
                    current_log.errors = errors[:50]
                    db.session.commit()

            print(json.dumps({
                'success': False,
                'cancelled': True,
                'errors': [str(e)],
                'stats': {},
                'elapsed_seconds': 0,
                'sync_log_id': sync_log_id,
            }))
            sys.exit(1)

        except Exception as e:
            if sync_log_id:
                current_log = db.session.get(DataSyncLog, sync_log_id)
                if current_log:
                    current_log.status = DataSyncLog.STATUS_FAILED
                    current_log.complete(success=False)
                    current_log.summary = {
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
                'sync_log_id': sync_log_id,
            }))
            sys.exit(1)


if __name__ == '__main__':
    main()
