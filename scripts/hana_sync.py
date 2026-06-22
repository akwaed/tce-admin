#!/usr/bin/env python3
"""
HANA Datasource Sync Script
Fetches course data from UK HANA database and generates CSV files for TCE Admin.

Usage:
    python scripts/hana_sync.py

Environment Variables (or use .env file):
    HANA_HOST     - HANA server address (default: hana.uky.edu)
    HANA_PORT     - HANA server port (default: 30015)
    HANA_USER     - HANA username
    HANA_PASSWORD - HANA password

Output Files (in ./datasources/):
    - Courses.csv
    - Instructor_Course.csv
    - Student_Course.csv
    - Users.csv
"""
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Repository root and default datasource directory.
# Computed from this file's location so the script always writes to the
# project's canonical ./datasources folder regardless of the caller's CWD.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / 'datasources'

# Add parent directory to path for imports
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from hdbcli import dbapi
except ImportError:
    print("ERROR: hdbcli not installed. Run: pip install hdbcli")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is optional


class HANADatasourceSync:
    """Syncs course data from HANA database to CSV files."""

    # Tables to sync with their configurations.
    #   key       - column used for de-duplication during sync (single column)
    #   diff_keys - columns that form the natural primary key used for diffing
    #               an existing CSV against a new sync. Composite keys are
    #               joined with '|' to form a stable identity string.
    TABLES = {
        'COURSES': {
            'filename': 'Courses',
            'order': 'SECTION_KEY, SECTION_ID',
            'key': 'SECTION_KEY',
            'diff_keys': ['SECTION_KEY']
        },
        'INSTRUCTOR_COURSE': {
            'filename': 'Instructor_Course',
            'order': 'SECTION_KEY, USER_ID',
            'diff_keys': ['SECTION_KEY', 'USER_ID']
        },
        'STUDENT_COURSE': {
            'filename': 'Student_Course',
            'order': 'SECTION_KEY, USER_ID',
            'diff_keys': ['SECTION_KEY', 'USER_ID']
        },
        'USERS': {
            'filename': 'Users',
            'order': 'USER_ID',
            'key': 'USER_ID',
            'diff_keys': ['USER_ID'],
            'exclude_columns': ['HASH'],
        }
    }

    # Columns that are allowed to be NULL / empty.
    # TCE scheduling fields and other metadata are frequently NULL for courses
    # that have not yet been scheduled or configured.
    OPTIONAL_COLUMNS = [
        'CROSSLISTED_ID', 'SPEC_TYPE', 'STU_OBJ_ID', 'SECTION_LENGTH_DAYS',
        'TCE_INVITE', 'TCE_R1', 'TCE_R2', 'TCE_END_DATE', 'TCE_REPORT_DATE',
        'CANVAS_SIS_ID', 'DISTANCE_LEARNING', 'UK_CORE_TYPE', 'CLASS_LEVEL',
        'EMAIL', 'SECONDARY_EMAIL',
    ]

    def __init__(self, output_path=None):
        # Always resolve to an absolute path. If no output_path is supplied,
        # fall back to <project_root>/datasources so that running this script
        # from any working directory writes to the canonical location instead
        # of creating a stray ./datasources next to the caller.
        self.output_path = Path(output_path).resolve() if output_path else DEFAULT_OUTPUT_PATH
        self.output_path.mkdir(parents=True, exist_ok=True)
        self.conn = None
        self.errors = []       # hard failures (DB query errors) — gate exit code
        self.warnings = []     # soft data-quality warnings (missing fields, dup keys)
        self.stats = {table: 0 for table in self.TABLES}
        # Per-file diff results, populated by _sync_table().
        # Shape: { 'Courses.csv': { 'added': int, 'updated': int,
        #                           'removed': int, 'unchanged': int,
        #                           'added_keys': [...], 'updated_keys': [...],
        #                           'removed_keys': [...] } }
        self.file_stats = {}
        # Per-file event timeline records, populated by _sync_table().
        self.file_events = []

    def connect(self, host=None, port=None, user=None, password=None):
        """Connect to HANA database."""
        host = host or os.getenv('HANA_HOST', 'hana.uky.edu')
        port = port or int(os.getenv('HANA_PORT', '30015'))
        user = user or os.getenv('HANA_USER')
        password = password or os.getenv('HANA_PASSWORD')

        if not user or not password:
            raise ValueError("HANA_USER and HANA_PASSWORD must be set")

        print(f"Connecting to {host}:{port}...")
        self.conn = dbapi.connect(
            address=host,
            port=port,
            user=user,
            password=password
        )
        print("Connected successfully.")

    def disconnect(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def get_term_range(self):
        """Calculate the term range to sync based on current date."""
        month_day = datetime.now().strftime("%m%d")
        year = datetime.now().year

        if month_day < '0301':
            from_term = f'{year - 1}050'
            up_to_term = f'{year}030'
        elif month_day < '0601':
            from_term = f'{year}010'
            up_to_term = f'{year}050'
        elif month_day < '1001':
            from_term = f'{year}020'
            up_to_term = f'{year + 1}010'
        else:
            from_term = f'{year}050'
            up_to_term = f'{year + 1}030'

        return from_term, up_to_term

    def sync_all(self, progress_callback=None):
        """Sync all tables from HANA to CSV files."""
        from_term, up_to_term = self.get_term_range()
        print(f"Syncing terms from {from_term} to {up_to_term}")

        cursor = self.conn.cursor()
        total_tables = len(self.TABLES)

        for idx, (table, config) in enumerate(self.TABLES.items()):
            if progress_callback:
                progress_callback(table, idx, total_tables)

            print(f"\nSyncing {table}...")
            self._sync_table(cursor, table, config, from_term, up_to_term)

        if progress_callback:
            progress_callback('complete', total_tables, total_tables)

        return {
            'success': len(self.errors) == 0,
            'stats': self.stats,
            'file_stats': self.file_stats,
            'file_events': self.file_events,
            'errors': self.errors,
            'warnings': self.warnings,
        }

    def _sync_table(self, cursor, table, config, from_term, up_to_term):
        """Sync a single table to CSV, computing a diff against the prior file."""
        table_start = time.monotonic()
        table_started_at = datetime.utcnow()
        filename = f"{config['filename']}.csv"
        row_count = 0
        status = 'failed'
        error_msg = None

        sql = f"SELECT * FROM EXPLORANCE.{table} ORDER BY {config['order']}"

        try:
            cursor.execute(sql)
            rows = cursor.fetchall()
        except Exception as e:
            self.errors.append(f"Error querying {table}: {e}")
            self.file_events.append({
                'direction': 'hana_pull',
                'file_name': filename,
                'datasource_id': None,
                'started_at': table_started_at.isoformat(),
                'completed_at': datetime.utcnow().isoformat(),
                'status': 'failed',
                'row_count': 0,
                'rows_added': 0,
                'rows_updated': 0,
                'rows_removed': 0,
                'error_message': str(e),
                'elapsed_seconds': round(time.monotonic() - table_start, 2),
            })
            return

        if not rows:
            self.warnings.append(f"No rows returned for {table}")
            self.file_events.append({
                'direction': 'hana_pull',
                'file_name': filename,
                'datasource_id': None,
                'started_at': table_started_at.isoformat(),
                'completed_at': datetime.utcnow().isoformat(),
                'status': 'success',
                'row_count': 0,
                'rows_added': 0,
                'rows_updated': 0,
                'rows_removed': 0,
                'error_message': None,
                'elapsed_seconds': round(time.monotonic() - table_start, 2),
            })
            return

        # Get column info
        column_names = [col[0] for col in cursor.description]

        # Pre-compute indices of columns to exclude from output (e.g. HASH blob)
        exclude_cols = set(config.get('exclude_columns', []))
        excluded_indices = frozenset(
            i for i, c in enumerate(column_names) if c in exclude_cols
        ) if exclude_cols else frozenset()

        key_index = column_names.index(config['key']) if 'key' in config else -1
        section_key_index = column_names.index('SECTION_KEY') if 'SECTION_KEY' in column_names else -1
        email_index = column_names.index('EMAIL') if 'EMAIL' in column_names else -1
        user_id_index = column_names.index('USER_ID') if 'USER_ID' in column_names else -1

        # Diff key indices (composite primary key for change tracking).
        diff_key_cols = config.get('diff_keys') or ([config['key']] if 'key' in config else [])
        diff_key_indices = [column_names.index(c) for c in diff_key_cols if c in column_names]

        # Build header, excluding unwanted columns
        output_columns = [
            c for i, c in enumerate(column_names) if i not in excluded_indices
        ]
        result = [output_columns]
        last_key = None
        new_rows_by_diff_key = {}

        for row in rows:
            rowdata = list(row)

            # Filter by term range if SECTION_KEY exists
            if section_key_index >= 0:
                section_key = rowdata[section_key_index]
                if section_key:
                    term = section_key[-7:]
                    if term > up_to_term or term < from_term:
                        continue

            # Skip duplicates based on key
            if key_index >= 0:
                current_key = rowdata[key_index]
                if current_key == last_key:
                    self.warnings.append(f"Duplicate key in {table}: {current_key}")
                    continue
                last_key = current_key

            # Skip rows with missing required data
            if self._is_missing_data(table, column_names, rowdata, key_index):
                continue

            # Fix email addresses with apostrophes
            if email_index >= 0 and user_id_index >= 0:
                email = rowdata[email_index]
                if email and "'" in email:
                    rowdata[email_index] = f"{rowdata[user_id_index]}@uky.edu"

            # Track for diff: stringified column-name -> value dict so the
            # comparison is independent of column ordering on disk.
            if diff_key_indices:
                string_row = {
                    col: ('' if rowdata[i] is None else str(rowdata[i]))
                    for i, col in enumerate(column_names)
                }
                diff_key = '|'.join(string_row[c] for c in diff_key_cols)
                new_rows_by_diff_key[diff_key] = string_row

            # Strip excluded columns from the row before writing
            if excluded_indices:
                rowdata = [v for i, v in enumerate(rowdata) if i not in excluded_indices]
            result.append(rowdata)
            self.stats[table] += 1

        # Compute the diff against the existing CSV (if any) before overwriting.
        self.file_stats[filename] = self._compute_diff(
            filename, diff_key_cols, diff_key_indices,
            column_names, new_rows_by_diff_key
        )

        row_count = self.stats[table]

        # Write to CSV
        self._write_csv(config['filename'], result)
        diff = self.file_stats[filename]
        print(f"  Wrote {row_count} rows to {filename} "
              f"(+{diff['added']} ~{diff['updated']} -{diff['removed']})")

        # Record per-file sync event
        elapsed = round(time.monotonic() - table_start, 2)
        self.file_events.append({
            'direction': 'hana_pull',
            'file_name': filename,
            'datasource_id': None,
            'started_at': table_started_at.isoformat(),
            'completed_at': datetime.utcnow().isoformat(),
            'status': 'success',
            'row_count': row_count,
            'rows_added': diff.get('added', 0),
            'rows_updated': diff.get('updated', 0),
            'rows_removed': diff.get('removed', 0),
            'error_message': None,
            'elapsed_seconds': elapsed,
        })

    def _compute_diff(self, filename, diff_key_cols, diff_key_indices,
                      column_names, new_rows_by_diff_key):
        """Compare the new in-memory rows against the existing CSV on disk.

        Returns a dict with added/updated/removed/unchanged counts plus the
        list of keys in each bucket. Keys for composite-key tables are
        joined with '|'.
        """
        empty = {
            'added': 0, 'updated': 0, 'removed': 0, 'unchanged': 0,
            'added_keys': [], 'updated_keys': [], 'removed_keys': [],
            'diff_key_columns': diff_key_cols,
        }

        if not diff_key_indices:
            # No primary key configured for this table - skip diff tracking.
            return empty

        existing_path = self.output_path / filename
        existing_by_key = {}
        common_cols = None
        if existing_path.exists():
            try:
                with open(existing_path, 'r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    if reader.fieldnames is None:
                        return empty
                    if not all(c in reader.fieldnames for c in diff_key_cols):
                        return empty  # diff key column missing - can't diff
                    # Only compare columns that exist in BOTH files - this
                    # avoids false "updated" diffs when the schema changes.
                    common_cols = [c for c in reader.fieldnames if c in column_names]
                    for row in reader:
                        key = '|'.join(row.get(c, '') for c in diff_key_cols)
                        existing_by_key[key] = row
            except Exception as e:
                self.warnings.append(f"Could not read existing {filename} for diff: {e}")
                return empty
        else:
            # First-ever sync of this file - everything is "added".
            return {
                'added': len(new_rows_by_diff_key),
                'updated': 0, 'removed': 0, 'unchanged': 0,
                'added_keys': sorted(new_rows_by_diff_key.keys()),
                'updated_keys': [], 'removed_keys': [],
                'diff_key_columns': diff_key_cols,
            }

        if common_cols is None:
            common_cols = list(column_names)

        added_keys, updated_keys, unchanged = [], [], 0
        for key, new_row in new_rows_by_diff_key.items():
            old_row = existing_by_key.get(key)
            if old_row is None:
                added_keys.append(key)
                continue
            # Compare only on shared columns.
            changed = False
            for col in common_cols:
                if (old_row.get(col, '') or '') != (new_row.get(col, '') or ''):
                    changed = True
                    break
            if changed:
                updated_keys.append(key)
            else:
                unchanged += 1

        removed_keys = [k for k in existing_by_key if k not in new_rows_by_diff_key]

        return {
            'added': len(added_keys),
            'updated': len(updated_keys),
            'removed': len(removed_keys),
            'unchanged': unchanged,
            'added_keys': sorted(added_keys),
            'updated_keys': sorted(updated_keys),
            'removed_keys': sorted(removed_keys),
            'diff_key_columns': diff_key_cols,
        }

    def _is_missing_data(self, table, column_names, data, key_index):
        """Check if row is missing required data.

        Only ``None`` (SQL NULL) is treated as missing.  Empty strings and
        integer ``0`` are legitimate values and must not be flagged.
        """
        for i, col_name in enumerate(column_names):
            if data[i] is None and col_name not in self.OPTIONAL_COLUMNS:
                msg = f"Missing {col_name} in {table}"
                if key_index >= 0:
                    msg += f" for key {data[key_index]}"
                self.warnings.append(msg)
                return True
        return False

    def _write_csv(self, filename, data):
        """Write data to CSV file."""
        filepath = self.output_path / f"{filename}.csv"
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
            for row in data:
                writer.writerow(row)


def main():
    """Main entry point for command-line usage."""
    import argparse

    parser = argparse.ArgumentParser(description='Sync HANA datasources to CSV files')
    parser.add_argument('--output', '-o', default=str(DEFAULT_OUTPUT_PATH),
                        help='Output directory for CSV files '
                             '(default: <project_root>/datasources)')
    parser.add_argument('--json-output',
                        help='Optional path to write a JSON file with the '
                             'sync result (per-file row counts and diff). '
                             'Used by the web UI to populate sync logs.')
    parser.add_argument(
        '--scheduled', action='store_true',
        help='Create a DataSyncLog entry so this run appears in the UI sync log.'
    )
    parser.add_argument('--host', help='HANA host (or set HANA_HOST env var)')
    parser.add_argument('--port', type=int, help='HANA port (or set HANA_PORT env var)')
    parser.add_argument('--user', '-u', help='HANA user (or set HANA_USER env var)')
    parser.add_argument('--password', '-p', help='HANA password (or set HANA_PASSWORD env var)')
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # --scheduled: create a DataSyncLog row so the UI shows this run.
    # ------------------------------------------------------------------
    sync_log_id = None
    if args.scheduled:
        from app import create_app
        app = create_app(os.environ.get('FLASK_ENV', 'production'))
        with app.app_context():
            from app.models import db as flask_db
            from app.models.settings import DataSyncLog
            sync_log = DataSyncLog(
                sync_type=DataSyncLog.TYPE_HANA_TO_DATASOURCE,
                status=DataSyncLog.STATUS_RUNNING,
                trigger_type='scheduled',
                triggered_by_id=None,
            )
            sync_log.summary = {
                'pipeline_phase': 'hana_pull',
                'pipeline_step': 1,
                'pipeline_total_steps': 1,
                'pipeline_message': (
                    'Scheduled: Fetching data from SAP HANA and '
                    'writing datasource CSV files...'
                ),
                'process_pid': os.getpid(),
                'process_started_at': datetime.utcnow().isoformat(),
            }
            flask_db.session.add(sync_log)
            flask_db.session.commit()
            sync_log_id = sync_log.id

    sync = HANADatasourceSync(args.output)

    try:
        sync.connect(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password
        )

        result = sync.sync_all()

        print("\n" + "="*50)
        print("Sync Complete!")
        print("="*50)
        print(f"Stats: {result['stats']}")

        if result['errors']:
            print(f"\nHard Errors ({len(result['errors'])}):")
            for err in result['errors'][:20]:
                print(f"  - {err}")
            if len(result['errors']) > 20:
                print(f"  ... and {len(result['errors']) - 20} more")

        if result['warnings']:
            print(f"\nWarnings ({len(result['warnings'])}):")
            for warn in result['warnings'][:20]:
                print(f"  - {warn}")
            if len(result['warnings']) > 20:
                print(f"  ... and {len(result['warnings']) - 20} more")

        # Build the structured payload once.
        payload = {
            'success': result['success'],
            'output_path': str(sync.output_path),
            'stats': result['stats'],
            'file_stats': result['file_stats'],
            'file_events': result.get('file_events', []),
            'errors': result['errors'],
            'warnings': result['warnings'],
            'records_processed': sum(result['stats'].values()),
            'records_added': sum(
                fs.get('added', 0) for fs in result['file_stats'].values()
            ),
            'records_updated': sum(
                fs.get('updated', 0) for fs in result['file_stats'].values()
            ),
            'records_removed': sum(
                fs.get('removed', 0) for fs in result['file_stats'].values()
            ),
        }

        # Write JSON payload file when requested.
        if args.json_output:
            try:
                with open(args.json_output, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, default=str)
            except Exception as e:
                print(f"WARNING: failed to write --json-output file: {e}")

        # Update the DataSyncLog row for --scheduled runs.
        if sync_log_id is not None:
            from app import create_app as _create_app
            _app = _create_app(os.environ.get('FLASK_ENV', 'production'))
            with _app.app_context():
                from app.models import db as _flask_db
                from app.models.settings import DataSyncLog
                log = DataSyncLog.query.get(sync_log_id)
                if log:
                    log.records_processed = payload['records_processed']
                    log.records_added = payload['records_added']
                    log.records_updated = payload['records_updated']
                    log.file_stats = result['file_stats'] or {}
                    log.field_changes = {
                        fname: {
                            'added': fs.get('added_keys', []),
                            'updated': fs.get('updated_keys', []),
                            'removed': fs.get('removed_keys', []),
                            'diff_key_columns': fs.get('diff_key_columns', []),
                        }
                        for fname, fs in (result.get('file_stats') or {}).items()
                    }
                    log.summary = {
                        'pipeline_phase': 'done' if result['success'] else 'failed',
                        'pipeline_step': 1,
                        'pipeline_total_steps': 1,
                        'pipeline_message': (
                            'Scheduled HANA sync completed successfully.'
                            if result['success']
                            else 'Scheduled HANA sync completed with hard errors.'
                        ),
                        'table_stats': result['stats'],
                        'hana_warnings': result.get('warnings', [])[:50],
                        'process_pid': os.getpid(),
                    }
                    if result['success']:
                        log.status = DataSyncLog.STATUS_COMPLETED
                        log.complete(success=True)
                    else:
                        log.status = DataSyncLog.STATUS_FAILED
                        log.complete(success=False)
                        log.errors = result['errors'][:50]

                    # Bulk-insert DataFileSyncEvent rows
                    from app.models.settings import DataFileSyncEvent
                    def _safe_parse(iso_str):
                        try:
                            return datetime.fromisoformat(iso_str) if iso_str else None
                        except Exception:
                            return datetime.utcnow()
                    for fe in result.get('file_events', []):
                        _flask_db.session.add(DataFileSyncEvent(
                            sync_log_id=log.id,
                            direction=fe['direction'],
                            file_name=fe['file_name'],
                            datasource_id=fe.get('datasource_id'),
                            started_at=_safe_parse(fe.get('started_at')),
                            completed_at=_safe_parse(fe.get('completed_at')),
                            status=fe['status'],
                            row_count=fe.get('row_count'),
                            rows_added=fe.get('rows_added'),
                            rows_updated=fe.get('rows_updated'),
                            rows_removed=fe.get('rows_removed'),
                            error_message=fe.get('error_message'),
                            elapsed_seconds=fe.get('elapsed_seconds'),
                        ))
                    _flask_db.session.commit()

        return 0 if result['success'] else 1

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

        # Update DataSyncLog on hard failure.
        if sync_log_id is not None:
            try:
                from app import create_app as _create_app
                _app = _create_app(os.environ.get('FLASK_ENV', 'production'))
                with _app.app_context():
                    from app.models import db as _flask_db
                    from app.models.settings import DataSyncLog
                    log = DataSyncLog.query.get(sync_log_id)
                    if log:
                        log.status = DataSyncLog.STATUS_FAILED
                        log.complete(success=False)
                        log.summary = {
                            'pipeline_phase': 'failed',
                            'pipeline_message': (
                                'Scheduled HANA sync failed with an exception.'
                            ),
                            'error': str(e),
                        }
                        log.errors = [str(e)]
                        _flask_db.session.commit()
            except Exception:
                pass

        return 1
    finally:
        sync.disconnect()


if __name__ == '__main__':
    sys.exit(main())
