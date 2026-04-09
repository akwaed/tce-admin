#!/usr/bin/env python3
"""
Standalone CSV -> PostgreSQL sync script.

Run by the web UI as a subprocess (not in-thread) to avoid SQLAlchemy
connection pool contention with the running Flask app.

Usage:
    python scripts/db_sync.py [--datasources PATH]

Exit codes:
    0  success
    1  sync failed (error written to stdout as JSON)

Output: single JSON object on stdout with keys:
    success, stats, errors, elapsed_seconds, sync_run_id
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
    args = parser.parse_args()

    from app import create_app
    from app.services.course_sync import CourseSyncService, resolve_datasources_path

    app = create_app(os.environ.get('FLASK_ENV', 'production'))
    with app.app_context():
        ds_path = resolve_datasources_path(args.datasources)
        service = CourseSyncService(ds_path)
        try:
            result = service.sync_all()
            print(json.dumps(result, default=str))
            sys.exit(0)
        except Exception as e:
            print(json.dumps({
                'success': False,
                'errors': [str(e)],
                'stats': {},
                'elapsed_seconds': 0,
            }))
            sys.exit(1)


if __name__ == '__main__':
    main()
