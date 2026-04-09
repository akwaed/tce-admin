#!/usr/bin/env python3
"""
One-time TCE Admin database migration: SQLite -> PostgreSQL.

This copies existing application data into a fresh PostgreSQL database without
changing the canonical SQLAlchemy models. It is designed for low-downtime
cutovers:

1. Take a SQLite backup while the app is still live.
2. Pre-seed a fresh PostgreSQL database using this script.
3. Stop the app briefly, take one final SQLite backup, rerun the script, then
   switch `DATABASE_URL` and restart.

The script creates the target schema from SQLAlchemy metadata, copies every
known table in dependency order, and resets PostgreSQL sequences after loading
explicit primary-key values.
"""
from __future__ import annotations

import argparse
import os
from datetime import date, datetime

from sqlalchemy import DateTime, MetaData, Table, create_engine, func, inspect, select, text

from app.models import db


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SQLITE_PATH = os.path.join(REPO_ROOT, "instance", "tce_admin.db")
DEFAULT_BATCH_SIZE = 2000


def parse_args():
    parser = argparse.ArgumentParser(
        description="Migrate TCE Admin data from SQLite to PostgreSQL."
    )
    parser.add_argument(
        "--source-sqlite",
        default=DEFAULT_SQLITE_PATH,
        help=f"Path to the SQLite database file (default: {DEFAULT_SQLITE_PATH})",
    )
    parser.add_argument(
        "--target-url",
        required=True,
        help="SQLAlchemy PostgreSQL URL, for example postgresql+psycopg2://user:pass@host:5432/dbname",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows per insert batch (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--allow-nonempty-target",
        action="store_true",
        help="Allow copying into a target database that already has rows.",
    )
    return parser.parse_args()


def _coerce_value(column, value):
    if value is None:
        return None

    try:
        python_type = column.type.python_type
    except NotImplementedError:
        return value

    if python_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "t", "yes", "y"}:
                return True
            if normalized in {"0", "false", "f", "no", "n", ""}:
                return False
        return value

    if python_type is int:
        if value == "":
            return None
        return int(value)

    if python_type is float:
        if value == "":
            return None
        return float(value)

    if python_type is date and not isinstance(column.type, DateTime):
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            candidate = value.strip()
            if not candidate:
                return None
            return datetime.fromisoformat(candidate[:10]).date()
        return value

    if python_type is datetime or isinstance(column.type, DateTime):
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            candidate = value.strip()
            if not candidate:
                return None
            candidate = candidate.replace("Z", "+00:00")
            return datetime.fromisoformat(candidate)
        return value

    return value


def _assert_empty_target(engine, table_order):
    inspector = inspect(engine)
    nonempty = []

    with engine.connect() as conn:
        for table in table_order:
            if not inspector.has_table(table.name):
                continue
            count = conn.execute(
                select(func.count()).select_from(table)
            ).scalar_one()
            if count:
                nonempty.append((table.name, count))

    if nonempty:
        details = ", ".join(f"{name}={count}" for name, count in nonempty)
        raise RuntimeError(
            "Target database is not empty. Refusing to continue without "
            f"--allow-nonempty-target. Non-empty tables: {details}"
        )


def _copy_table(source_engine, target_engine, table, batch_size):
    source_meta = MetaData()
    source_table = Table(table.name, source_meta, autoload_with=source_engine)
    source_columns = {column.name for column in source_table.columns}
    shared_columns = [column for column in table.columns if column.name in source_columns]

    missing_required = [
        column.name
        for column in table.columns
        if column.name not in source_columns
        and not column.nullable
        and not column.primary_key
        and column.default is None
        and column.server_default is None
    ]
    if missing_required:
        raise RuntimeError(
            f"Source table {table.name} is missing required columns for target schema: "
            + ", ".join(missing_required)
        )

    source_query = select(*[source_table.c[column.name] for column in shared_columns])
    inserted = 0

    with source_engine.connect() as source_conn, target_engine.begin() as target_conn:
        result = source_conn.execution_options(stream_results=True).execute(source_query)
        while True:
            rows = result.mappings().fetchmany(batch_size)
            if not rows:
                break

            payload = []
            for row in rows:
                payload.append({
                    column.name: _coerce_value(column, row[column.name])
                    for column in shared_columns
                })

            target_conn.execute(table.insert(), payload)
            inserted += len(payload)
            print(f"  {table.name}: {inserted:,} rows copied", flush=True)


def _reset_postgres_sequences(engine, table_order):
    if not engine.url.drivername.startswith("postgresql"):
        return

    with engine.begin() as conn:
        for table in table_order:
            pk_columns = list(table.primary_key.columns)
            if len(pk_columns) != 1:
                continue

            pk_column = pk_columns[0]
            try:
                if pk_column.type.python_type is not int:
                    continue
            except NotImplementedError:
                continue

            max_value = conn.execute(
                select(func.max(table.c[pk_column.name]))
            ).scalar()
            if max_value is None:
                continue

            conn.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence(:table_name, :column_name), :value, true)"
                ),
                {
                    "table_name": table.name,
                    "column_name": pk_column.name,
                    "value": max_value,
                },
            )


def main() -> int:
    args = parse_args()

    if not os.path.exists(args.source_sqlite):
        raise FileNotFoundError(f"SQLite source database not found: {args.source_sqlite}")
    if not args.target_url.startswith("postgresql"):
        raise RuntimeError("Target URL must be PostgreSQL.")

    source_url = f"sqlite:///{os.path.abspath(args.source_sqlite)}"
    source_engine = create_engine(source_url)
    target_engine = create_engine(args.target_url)
    table_order = list(db.metadata.sorted_tables)

    try:
        db.metadata.create_all(bind=target_engine)

        if not args.allow_nonempty_target:
            _assert_empty_target(target_engine, table_order)

        source_inspector = inspect(source_engine)
        for table in table_order:
            if not source_inspector.has_table(table.name):
                print(f"Skipping {table.name}: not present in source database", flush=True)
                continue

            print(f"Copying {table.name}...", flush=True)
            _copy_table(source_engine, target_engine, table, args.batch_size)

        _reset_postgres_sequences(target_engine, table_order)
        print("Migration complete.", flush=True)
        return 0
    finally:
        source_engine.dispose()
        target_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
