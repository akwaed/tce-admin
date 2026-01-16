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
import os
import sys
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

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

    # Tables to sync with their configurations
    TABLES = {
        'COURSES': {
            'filename': 'Courses',
            'order': 'SECTION_KEY, SECTION_ID',
            'key': 'SECTION_KEY'
        },
        'INSTRUCTOR_COURSE': {
            'filename': 'Instructor_Course',
            'order': 'SECTION_KEY, USER_ID'
        },
        'STUDENT_COURSE': {
            'filename': 'Student_Course',
            'order': 'SECTION_KEY, USER_ID'
        },
        'USERS': {
            'filename': 'Users',
            'order': 'USER_ID',
            'key': 'USER_ID'
        }
    }

    # Columns that are allowed to be empty
    OPTIONAL_COLUMNS = ['CROSSLISTED_ID', 'SPEC_TYPE', 'STU_OBJ_ID', 'SECTION_LENGTH_DAYS']

    def __init__(self, output_path='./datasources'):
        self.output_path = Path(output_path)
        self.output_path.mkdir(parents=True, exist_ok=True)
        self.conn = None
        self.errors = []
        self.stats = {table: 0 for table in self.TABLES}

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
            'errors': self.errors
        }

    def _sync_table(self, cursor, table, config, from_term, up_to_term):
        """Sync a single table to CSV."""
        sql = f"SELECT * FROM EXPLORANCE.{table} ORDER BY {config['order']}"

        try:
            cursor.execute(sql)
            rows = cursor.fetchall()
        except Exception as e:
            self.errors.append(f"Error querying {table}: {e}")
            return

        if not rows:
            self.errors.append(f"No rows returned for {table}")
            return

        # Get column info
        column_names = [col[0] for col in cursor.description]
        key_index = column_names.index(config['key']) if 'key' in config else -1
        section_key_index = column_names.index('SECTION_KEY') if 'SECTION_KEY' in column_names else -1
        email_index = column_names.index('EMAIL') if 'EMAIL' in column_names else -1
        user_id_index = column_names.index('USER_ID') if 'USER_ID' in column_names else -1

        result = [column_names]
        last_key = None

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
                    self.errors.append(f"Duplicate key in {table}: {current_key}")
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

            result.append(rowdata)
            self.stats[table] += 1

        # Write to CSV
        self._write_csv(config['filename'], result)
        print(f"  Wrote {self.stats[table]} rows to {config['filename']}.csv")

    def _is_missing_data(self, table, column_names, data, key_index):
        """Check if row is missing required data."""
        for i, col_name in enumerate(column_names):
            if not data[i] and col_name not in self.OPTIONAL_COLUMNS:
                msg = f"Missing {col_name} in {table}"
                if key_index >= 0:
                    msg += f" for key {data[key_index]}"
                self.errors.append(msg)
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
    parser.add_argument('--output', '-o', default='./datasources',
                        help='Output directory for CSV files')
    parser.add_argument('--host', help='HANA host (or set HANA_HOST env var)')
    parser.add_argument('--port', type=int, help='HANA port (or set HANA_PORT env var)')
    parser.add_argument('--user', '-u', help='HANA user (or set HANA_USER env var)')
    parser.add_argument('--password', '-p', help='HANA password (or set HANA_PASSWORD env var)')
    args = parser.parse_args()

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
            print(f"\nWarnings/Errors ({len(result['errors'])}):")
            for err in result['errors'][:20]:
                print(f"  - {err}")
            if len(result['errors']) > 20:
                print(f"  ... and {len(result['errors']) - 20} more")

        return 0 if result['success'] else 1

    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        sync.disconnect()


if __name__ == '__main__':
    sys.exit(main())
