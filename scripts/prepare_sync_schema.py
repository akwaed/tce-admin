#!/usr/bin/env python3
"""
Prepare additive sync-history schema with no destructive changes.

This script is intended for low-downtime rollouts:
1. It boots the Flask app with the same configuration the web process uses.
2. It verifies the core course tables are present.
3. It ensures the additive sync history tables exist.

It does not drop or rewrite any existing Course / Instructor / College /
Department data.
"""
from __future__ import annotations

import os

from sqlalchemy import func, inspect, select

from app import create_app
from app.models import db
from app.models.course import College, Course, Department, Instructor
from app.models.sync_history import ChangeLog, SyncRun


def _count_rows(model):
    return db.session.execute(
        select(func.count()).select_from(model)
    ).scalar_one()


def main() -> int:
    app = create_app(os.environ.get("FLASK_ENV", "default"))

    with app.app_context():
        inspector = inspect(db.engine)
        core_tables = ("colleges", "departments", "courses", "instructors")
        missing_core = [name for name in core_tables if not inspector.has_table(name)]

        if missing_core:
            print("Missing required core tables:", ", ".join(missing_core))
            return 1

        sync_tables = {
            "sync_runs": SyncRun.__table__,
            "change_log": ChangeLog.__table__,
        }

        created = []
        for name, table in sync_tables.items():
            if not inspector.has_table(name):
                table.create(bind=db.engine, checkfirst=True)
                created.append(name)

        inspector = inspect(db.engine)
        print(f"Database backend: {db.engine.url.drivername}")
        print("Core table counts:")
        print(f"  colleges: {_count_rows(College)}")
        print(f"  departments: {_count_rows(Department)}")
        print(f"  courses: {_count_rows(Course)}")
        print(f"  instructors: {_count_rows(Instructor)}")

        if created:
            print("Created additive sync tables:", ", ".join(created))
        else:
            print("Additive sync tables already present: sync_runs, change_log")

        print("No destructive schema changes were made.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
