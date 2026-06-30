#!/usr/bin/env python3
"""
Prepare additive sync-history schema with no destructive changes.

Works with both SQLite (dev) and PostgreSQL (production).

This script:
1. Boots the Flask app with the same config the web process uses.
2. Verifies the core course tables are present.
3. Ensures the additive sync history tables (sync_runs, change_log) exist.
4. Adds any missing indexes needed for PostgreSQL performance at scale.

It does NOT drop or rewrite any existing Course / Instructor / College /
Department data.

Usage:
    FLASK_ENV=production python scripts/prepare_sync_schema.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the project root is on sys.path so 'app' is importable
# regardless of where the script is invoked from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env from the project root before importing app so DATABASE_URL etc.
# are in the environment before Flask/SQLAlchemy reads them.
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / '.env')
except ImportError:
    pass

from sqlalchemy import func, inspect, select, text
from app import create_app
from app.models import db
from app.models.course import College, Course, Department, Instructor
from app.models.sync_history import ChangeLog, SyncRun
from app.models.settings import DataFileSyncEvent, BlueSyncDatasource


def _count_rows(model):
    return db.session.execute(
        select(func.count()).select_from(model)
    ).scalar_one()


def _is_postgres():
    return 'postgresql' in str(db.engine.url.drivername)


def _ensure_indexes(inspector):
    """Add performance indexes that SQLAlchemy models don't declare."""
    if not _is_postgres():
        return  # SQLite manages its own indexes fine

    needed = [
        # (table, index_name, columns)
        ('courses',     'ix_courses_term_code',         'term_code'),
        ('courses',     'ix_courses_marked_for_tce',    'marked_for_tce'),
        ('courses',     'ix_courses_college_code',      'college_code'),
        ('courses',     'ix_courses_class_id',          'class_id'),
        ('instructors', 'ix_instructors_section_key',   'section_key'),
        ('instructors', 'ix_instructors_user_id',       'user_id'),
        ('change_log',  'ix_change_log_created_at',     'created_at'),
        ('sync_runs',   'ix_sync_runs_status',          'status'),
        ('sync_runs',   'ix_sync_runs_started_at',      'started_at'),
        ('data_file_sync_events', 'ix_file_events_sync_log_id', 'sync_log_id'),
        ('data_file_sync_events', 'ix_file_events_direction', 'direction'),
        ('blue_sync_datasources', 'ix_blue_ds_import_order', 'import_order'),
        ('blue_sync_datasources', 'ix_blue_ds_active', 'is_active'),
    ]

    created = []
    for table, idx_name, col in needed:
        if inspector.has_table(table):
            existing = {i['name'] for i in inspector.get_indexes(table)}
            if idx_name not in existing:
                db.session.execute(
                    text(f'CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({col})')
                )
                created.append(idx_name)

    if created:
        db.session.commit()
        print('Created indexes:', ', '.join(created))
    else:
        print('All performance indexes already present.')


INITIAL_BLUE_DATASOURCES = [
    {
        'datasource_id': 'Data161',
        'display_name': 'Courses',
        'legacy_key': 'courses',
        'block_name': '23_Courses',
        'csv_file': 'Courses.csv',
        'source_type': 'hana_csv',
        'import_order': 1,
        'is_system': True,
        'wait_after_seconds': 300,
        'columns': [
            'SECTION_KEY', 'TITLE', 'CANVAS_SIS_ID', 'CRS_SECTION', 'PREFIX',
            'CLASS', 'CLASS_ID', 'SECTION', 'SECTION_ID', 'ACADEMIC_YEAR',
            'ACADEMIC_TERM_ID', 'ACADEMIC_TERM', 'SECTION_TITLE',
            'SECTION_BEGIN_DATE', 'SECTION_END_DATE', 'SECTION_LENGTH_DAYS',
            'TCE_INVITE', 'TCE_R1', 'TCE_R2', 'TCE_END_DATE', 'TCE_REPORT_DATE',
            'CLASS_DEPARTMENT', 'CLASS_DEPARTMENT_ID', 'CLASS_COLLEGE',
            'CLASS_COLLEGE_SHORT', 'CLASS_LEVEL', 'IS_CROSSLISTED',
            'CROSSLISTED_ID', 'DISTANCE_LEARNING', 'IS_UK_CORE', 'UK_CORE_TYPE',
            'SPEC_TYPE',
        ],
        'required_columns': ['SECTION_KEY', 'TITLE'],
    },
    {
        'datasource_id': 'Data162',
        'display_name': 'Course Instructors',
        'legacy_key': 'instructors',
        'block_name': None,
        'csv_file': 'Instructor_Course.csv',
        'source_type': 'hana_csv',
        'import_order': 2,
        'is_system': True,
        'wait_after_seconds': 300,
        'columns': ['SECTION_KEY', 'USER_ID', 'FIRST_NAME', 'LAST_NAME', 'EMAIL'],
        'required_columns': ['SECTION_KEY', 'USER_ID'],
    },
    {
        'datasource_id': 'Data163',
        'display_name': 'Course Students',
        'legacy_key': 'students',
        'block_name': None,
        'csv_file': 'Student_Course.csv',
        'source_type': 'hana_csv',
        'import_order': 3,
        'is_system': True,
        'wait_after_seconds': 300,
        'columns': ['SECTION_KEY', 'USER_ID'],
        'required_columns': ['SECTION_KEY', 'USER_ID'],
    },
    {
        'datasource_id': 'Data144',
        'display_name': 'Users',
        'legacy_key': 'users',
        'block_name': None,
        'csv_file': 'Users.csv',
        'source_type': 'hana_csv',
        'import_order': 4,
        'is_system': True,
        'wait_after_seconds': 300,
        'columns': ['USER_ID', 'FIRSTNAME_1', 'LASTNAME_1', 'EMAIL', 'SECONDARY_EMAIL'],
        'required_columns': ['USER_ID', 'FIRSTNAME_1', 'LASTNAME_1', 'EMAIL'],
        # Bug fix port: remap CSV FIRSTNAME/LASTNAME -> Blue's _1 names
        'column_renames': {
            'FIRSTNAME': 'FIRSTNAME_1',
            'LASTNAME': 'LASTNAME_1',
        },
    },
]


def _seed_blue_datasources():
    """Insert the initial 4 HANA datasources if the table is empty."""
    from datetime import datetime, timezone
UTC = timezone.utc
    now = datetime.now(UTC)
    for ds in INITIAL_BLUE_DATASOURCES:
        row = BlueSyncDatasource(
            datasource_id=ds['datasource_id'],
            display_name=ds['display_name'],
            legacy_key=ds['legacy_key'],
            block_name=ds['block_name'],
            csv_file=ds['csv_file'],
            source_type=ds['source_type'],
            import_order=ds['import_order'],
            is_active=True,
            is_system=ds['is_system'],
            wait_after_seconds=ds['wait_after_seconds'],
            columns=ds['columns'],
            required_columns=ds.get('required_columns'),
            column_renames=ds.get('column_renames'),
            created_at=now,
            updated_at=now,
        )
        db.session.add(row)
    db.session.commit()


def main() -> int:
    app = create_app(os.environ.get('FLASK_ENV', 'default'))

    with app.app_context():
        inspector = inspect(db.engine)
        core_tables = ('colleges', 'departments', 'courses', 'instructors')
        missing_core = [t for t in core_tables if not inspector.has_table(t)]

        if missing_core:
            print('ERROR: Missing required core tables:', ', '.join(missing_core))
            print('Run the app once (python run.py) to let SQLAlchemy create them,')
            print('or apply your Alembic migrations first.')
            return 1

        # Ensure sync history tables exist.
        sync_tables = {
            'sync_runs': SyncRun.__table__,
            'change_log': ChangeLog.__table__,
            'data_file_sync_events': DataFileSyncEvent.__table__,
            'blue_sync_datasources': BlueSyncDatasource.__table__,
        }
        created_tables = []
        for name, table in sync_tables.items():
            if not inspector.has_table(name):
                table.create(bind=db.engine, checkfirst=True)
                created_tables.append(name)

        if created_tables:
            print('Created additive sync tables:', ', '.join(created_tables))
        else:
            print('Additive sync tables already present: sync_runs, change_log')

        # Re-inspect after potential table creation.
        inspector = inspect(db.engine)
        _ensure_indexes(inspector)

        backend = db.engine.url.drivername
        print(f'\nDatabase backend : {backend}')
        print('Core table counts:')
        print(f'  colleges    : {_count_rows(College):,}')
        print(f'  departments : {_count_rows(Department):,}')
        print(f'  courses     : {_count_rows(Course):,}')
        print(f'  instructors : {_count_rows(Instructor):,}')

        # Seed initial BlueSyncDatasource rows if the table is empty.
        if inspector.has_table('blue_sync_datasources'):
            existing = db.session.execute(
                select(func.count()).select_from(BlueSyncDatasource)
            ).scalar_one()
            if existing == 0:
                _seed_blue_datasources()
                print('Seeded 4 initial Blue datasources.')

        if _is_postgres():
            try:
                size = db.session.execute(
                    text("SELECT pg_size_pretty(pg_database_size(current_database()))")
                ).scalar_one()
                print(f'  database size: {size}')
            except Exception:
                pass

        print('\nNo destructive schema changes were made.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
