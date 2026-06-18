#!/usr/bin/env python3
"""Strip the corrupted HASH column from Users.csv.

The HASH column contains Python memory-address representations
('<memory at 0x...>') because the hdbcli driver cannot serialise
the HANA HASH blob type.  This column is never pushed to Blue and
is never read by any application code, so we drop it to save ~30 %
disk space.

Usage:
    python scripts/strip_hash_column.py
"""

import csv
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USERS_PATH = PROJECT_ROOT / 'datasources' / 'Users.csv'

def main():
    if not USERS_PATH.exists():
        print(f"ERROR: {USERS_PATH} not found")
        sys.exit(1)

    original_size = USERS_PATH.stat().st_size
    
    temp_path = USERS_PATH.with_suffix('.csv.tmp')
    rows_written = 0
    dropped_col = None

    with open(USERS_PATH, 'r', newline='', encoding='utf-8') as infile, \
         open(temp_path, 'w', newline='', encoding='utf-8') as outfile:
        
        reader = csv.DictReader(infile)
        
        # Identify the HASH column and build output fieldnames
        all_fields = reader.fieldnames or []
        hash_cols = [c for c in all_fields if c.upper() == 'HASH']
        output_fields = [c for c in all_fields if c not in hash_cols]
        
        if not hash_cols:
            print("No HASH column found — nothing to do.")
            return
        
        dropped_col = hash_cols[0]
        print(f"Dropping column: {dropped_col}")
        print(f"Output columns ({len(output_fields)}): {', '.join(output_fields)}")

        writer = csv.DictWriter(outfile, fieldnames=output_fields, extrasaction='ignore')
        writer.writeheader()

        for row in reader:
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
    print(f"  Saved:  {saved:>12,} bytes ({pct:.1f} %)")

if __name__ == '__main__':
    main()
