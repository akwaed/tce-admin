#!/usr/bin/env python3
"""Normalise Users.csv for Explorance Blue import.

1. Strip the corrupted HASH column (Python memory-address values).
2. Rename FIRSTNAME → FIRST_NAME and LASTNAME → LAST_NAME so the
   column names match the Blue Data144 datasource schema.

Usage:
    python scripts/strip_hash_column.py
"""

import csv
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USERS_PATH = PROJECT_ROOT / 'datasources' / 'Users.csv'

# Columns to drop outright
DROP_COLUMNS = {'HASH'}

# Columns to rename: CSV name → Blue name
RENAME_COLUMNS = {
    'FIRSTNAME': 'FIRST_NAME',
    'LASTNAME':  'LAST_NAME',
}


def main():
    if not USERS_PATH.exists():
        print(f"ERROR: {USERS_PATH} not found")
        sys.exit(1)

    original_size = USERS_PATH.stat().st_size

    temp_path = USERS_PATH.with_suffix('.csv.tmp')
    rows_written = 0
    dropped = []
    renamed = {}

    with open(USERS_PATH, 'r', newline='', encoding='utf-8') as infile, \
         open(temp_path, 'w', newline='', encoding='utf-8') as outfile:

        reader = csv.DictReader(infile)
        all_fields = reader.fieldnames or []

        # Build output fieldnames: drop unwanted, rename where configured
        output_fields = []
        for col in all_fields:
            if col.upper() in {c.upper() for c in DROP_COLUMNS}:
                dropped.append(col)
            elif col in RENAME_COLUMNS:
                new_name = RENAME_COLUMNS[col]
                output_fields.append(new_name)
                renamed[col] = new_name
            else:
                output_fields.append(col)

        if dropped:
            print(f"Dropped: {', '.join(dropped)}")
        if renamed:
            for old, new in renamed.items():
                print(f"Renamed: {old} → {new}")
        if not dropped and not renamed:
            print("Nothing to do.")
            return

        print(f"Output columns ({len(output_fields)}): {', '.join(output_fields)}")

        writer = csv.DictWriter(outfile, fieldnames=output_fields, extrasaction='ignore')
        writer.writeheader()

        for row in reader:
            # Apply renames
            for old, new in RENAME_COLUMNS.items():
                if old in row:
                    row[new] = row.pop(old)
            # Drop unwanted
            for col in dropped:
                row.pop(col, None)
            writer.writerow(row)
            rows_written += 1

    # Atomic replace
    os.replace(temp_path, USERS_PATH)

    new_size = USERS_PATH.stat().st_size
    saved = original_size - new_size
    pct = (saved / original_size * 100) if original_size else 0

    print(f"\nDone.  {rows_written:,} rows written.")
    print(f"  Before: {original_size:>12,} bytes")
    print(f"  After:  {new_size:>12,} bytes")
    if saved >= 0:
        print(f"  Delta:  {saved:>12,} bytes ({pct:.1f} %)")
    else:
        print(f"  Delta: +{-saved:>11,} bytes ({-pct:.1f} %)")


if __name__ == '__main__':
    main()
