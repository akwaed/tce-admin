#!/usr/bin/env python3
"""
Standalone Blue datasource push script.

Usage:
    python scripts/blue_sync_cli.py [--dry-run] [--datasource Data161] [--scheduled]

Flags:
    --dry-run         Validate CSVs and print row counts; do NOT push to Blue.
    --datasource ID   Push only one datasource (repeat for multiple).
                      Default: push all active datasources.
    --scheduled       Create a DataSyncLog entry visible in the UI.

Exit codes:
    0  all pushes succeeded
    1  one or more pushes failed
"""
from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(
        description='Push datasource CSV files to Explorance Blue.'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Validate CSVs without pushing to Blue.'
    )
    parser.add_argument(
        '--datasource', action='append', dest='datasources',
        help='Push only this datasource (repeat for multiple). '
             'Use datasource ID (e.g. Data161) or legacy key (e.g. courses).'
    )
    parser.add_argument(
        '--scheduled', action='store_true',
        help='Create a DataSyncLog entry for the UI.'
    )
    parser.add_argument(
        '--datasources-path', default=str(PROJECT_ROOT / 'datasources'),
        help='Path to the datasources directory.'
    )
    args = parser.parse_args()

    from app import create_app

    app = create_app(os.environ.get('FLASK_ENV', 'production'))
    with app.app_context():
        from app.models import db
        from app.models.settings import DataSyncLog, BlueSyncDatasource
        from app.services.blue_sync import BlueSyncService, get_blue_sync_service

        # Resolve datasources to push
        ds_keys = None
        if args.datasources:
            ds_keys = args.datasources

        service = get_blue_sync_service(args.datasources_path)

        try:
            result = service.push_all(
                datasources=ds_keys,
                dry_run=args.dry_run,
                trigger_type='scheduled' if args.scheduled else 'manual',
                triggered_by=None,
            )

            if args.dry_run:
                print(json.dumps({
                    'success': result.get('success', True),
                    'dry_run': True,
                    'datasources_checked': result.get('stats', {}).get('total_records', 0),
                    'results': result.get('results', {}),
                    'errors': result.get('errors', []),
                }, default=str))
            else:
                # For scheduled runs, the sync_log was already created by push_all()
                print(json.dumps({
                    'success': result.get('success', False),
                    'sync_log_id': result.get('sync_log_id'),
                    'results': result.get('results', {}),
                    'stats': result.get('stats', {}),
                    'errors': result.get('errors', []),
                }, default=str))

            if result.get('success'):
                sys.exit(0)
            else:
                sys.exit(1)

        except Exception as e:
            print(json.dumps({
                'success': False,
                'error': str(e),
                'errors': [str(e)],
            }))
            sys.exit(1)


if __name__ == '__main__':
    main()
