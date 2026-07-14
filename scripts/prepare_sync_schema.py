#!/usr/bin/env python3
"""
Prepare additive sync-history schema with no destructive changes.

Works with both SQLite (dev) and PostgreSQL (production).

This script:
1. Boots the Flask app with the same config the web process uses.
2. Verifies the core course tables are present.
3. Ensures the additive sync history tables (sync_runs, change_log) exist.
4. Adds any missing columns on existing tables (e.g. blue_sync_datasources.column_renames).
5. Adds any missing indexes needed for PostgreSQL performance at scale.
6. Seeds default Blue datasources (including DRA Data151 when missing).

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
        'columns': [
            'USER_ID',
            'UKID_NBR',
            'STU_OBJ_ID',
            'FIRSTNAME_1',
            'LASTNAME_1',
            'EMAIL',
            'SECONDARY_EMAIL',
            'BLUE_ROLE',
        ],
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


def _sql_type_for_column(col, dialect_name: str) -> str:
    """Best-effort SQL type string for ADD COLUMN on existing tables."""
    from sqlalchemy import Boolean, DateTime, Integer, String, Text
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.sql.sqltypes import JSON as SAJSON

    t = col.type
    # Prefer dialect-compiled type when possible
    try:
        compiled = t.compile(dialect=db.engine.dialect)
        if compiled:
            return str(compiled)
    except Exception:
        pass

    if isinstance(t, Boolean):
        return 'BOOLEAN' if 'postgresql' in dialect_name else 'INTEGER'
    if isinstance(t, Integer):
        return 'INTEGER'
    if isinstance(t, DateTime):
        return 'TIMESTAMP' if 'postgresql' in dialect_name else 'DATETIME'
    if isinstance(t, Text):
        return 'TEXT'
    if isinstance(t, String):
        length = getattr(t, 'length', None) or 255
        return f'VARCHAR({length})'
    # JSON / JSONB
    if 'postgresql' in dialect_name:
        return 'JSONB'
    return 'TEXT'  # SQLite JSON affinity is fine as TEXT


def _ensure_missing_model_columns(model, inspector) -> list[str]:
    """ADD COLUMN for model fields missing from an existing table (non-destructive).

    SQLAlchemy create_all() never adds columns to existing tables — this closes
    that gap for additive schema drift (e.g. column_renames on blue_sync_datasources).
    """
    table_name = model.__tablename__
    if not inspector.has_table(table_name):
        return []

    existing = {c['name'] for c in inspector.get_columns(table_name)}
    dialect_name = str(db.engine.url.drivername)
    added: list[str] = []

    for col in model.__table__.columns:
        if col.name in existing:
            continue
        # Skip pure PK autoincrement if somehow missing (should not happen)
        sql_type = _sql_type_for_column(col, dialect_name)
        nullable = 'NULL' if col.nullable else 'NOT NULL'
        default_sql = ''
        if col.default is not None and col.default.is_scalar:
            val = col.default.arg
            if isinstance(val, bool):
                default_sql = f" DEFAULT {'TRUE' if val else 'FALSE'}" if 'postgresql' in dialect_name else f" DEFAULT {1 if val else 0}"
            elif isinstance(val, (int, float)):
                default_sql = f' DEFAULT {val}'
            elif isinstance(val, str):
                default_sql = f" DEFAULT '{val.replace(chr(39), chr(39)+chr(39))}'"
        elif not col.nullable and col.default is None and col.server_default is None:
            # Avoid failing NOT NULL adds without a default on populated tables
            nullable = 'NULL'

        # IF NOT EXISTS is supported on Postgres 9.1+ and modern SQLite
        stmt = (
            f'ALTER TABLE {table_name} '
            f'ADD COLUMN IF NOT EXISTS {col.name} {sql_type} {nullable}{default_sql}'
        )
        try:
            db.session.execute(text(stmt))
            added.append(f'{table_name}.{col.name}')
        except Exception as exc:
            # SQLite < 3.35 may not support IF NOT EXISTS on ADD COLUMN
            if 'duplicate column' in str(exc).lower() or 'already exists' in str(exc).lower():
                continue
            # Retry without IF NOT EXISTS
            try:
                stmt2 = (
                    f'ALTER TABLE {table_name} '
                    f'ADD COLUMN {col.name} {sql_type} {nullable}{default_sql}'
                )
                db.session.execute(text(stmt2))
                added.append(f'{table_name}.{col.name}')
            except Exception as exc2:
                print(f'WARNING: could not add {table_name}.{col.name}: {exc2}')

    if added:
        db.session.commit()
    return added


def _ensure_users_column_list():
    """Ensure Data144 columns include UKID_NBR, STU_OBJ_ID, BLUE_ROLE, etc."""
    from datetime import datetime, timezone
    UTC = timezone.utc
    try:
        row = BlueSyncDatasource.query.filter_by(datasource_id='Data144').first()
    except Exception as exc:
        print(f'WARNING: could not load Data144 row (schema still migrating?): {exc}')
        db.session.rollback()
        return
    if not row:
        return
    seed = next(d for d in INITIAL_BLUE_DATASOURCES if d['datasource_id'] == 'Data144')
    desired = list(seed['columns'])
    cols = list(row.columns or [])
    if not cols:
        row.columns = desired
        added = desired
    else:
        added = [c for c in desired if c not in cols]
        if not added:
            return
        # Preserve existing order; append newly required fields.
        row.columns = cols + added
    renames = dict(row.column_renames or {})
    renames.setdefault('FIRSTNAME', 'FIRSTNAME_1')
    renames.setdefault('LASTNAME', 'LASTNAME_1')
    row.column_renames = renames
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    db.session.commit()
    print(f'Updated Data144 columns; added: {", ".join(added)}')


def _ensure_dra_datasource():
    """Register DRA (Data151) if missing — college/dept admin report viewers."""
    from datetime import datetime, timezone
    UTC = timezone.utc
    try:
        existing = BlueSyncDatasource.query.filter_by(datasource_id='Data151').first()
    except Exception as exc:
        print(f'WARNING: could not check Data151: {exc}')
        db.session.rollback()
        return
    if existing:
        print('DRA datasource Data151 already registered.')
        return
    now = datetime.now(UTC).replace(tzinfo=None)
    row = BlueSyncDatasource(
        datasource_id='Data151',
        display_name='DRA Report Viewers',
        legacy_key='dra',
        block_name='ReportViewersToUsers',
        csv_file='DRA.csv',  # generated by scripts/dra_sync.py
        source_type='generated_csv',
        import_order=5,
        is_active=True,
        is_system=False,
        wait_after_seconds=300,
        columns=['source_1', 'target_1', 'targetType'],
        required_columns=['source_1', 'target_1', 'targetType'],
        column_renames={
            'source': 'source_1',
            'target': 'target_1',
        },
        notes=(
            'College/department admin → Blue report-viewer relationships. '
            'Generated by scripts/dra_sync.py (or tracking DRA export).'
        ),
        created_at=now,
        updated_at=now,
    )
    db.session.add(row)
    db.session.commit()
    print('Seeded DRA datasource Data151 (ReportViewersToUsers).')


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
        # Additive column upgrades (create_all never ALTERs existing tables).
        from app.models.admin import Admin
        col_added = []
        for model in (BlueSyncDatasource, Admin, DataFileSyncEvent):
            col_added.extend(_ensure_missing_model_columns(model, inspector))
        if col_added:
            print('Added missing columns:', ', '.join(col_added))
        else:
            print('No missing model columns on blue_sync_datasources / admins.')

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
            else:
                # Keep Users (Data144) columns current without a full reseed.
                _ensure_users_column_list()
            # DRA (college/dept admin report viewers) → Data151
            _ensure_dra_datasource()

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
