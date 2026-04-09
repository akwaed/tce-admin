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
