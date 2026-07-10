"""CSV loading with column remaps and HASH drop for Blue push."""
from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from app.services.blue_push.config import DROP_COLUMNS, DatasourceConfig

logger = logging.getLogger("blue_push")


@dataclass
class LoadedCsv:
    """Result of loading a datasource CSV for Blue."""

    columns: List[str]       # Blue column names actually present
    rows: List[List[str]]    # Row value lists aligned to columns
    source_path: str
    dropped_columns: List[str]
    total_rows: int


def resolve_csv_path(datasources_path: str, csv_file: str) -> str:
    """Resolve a CSV path relative to datasources dir or absolute."""
    if os.path.isabs(csv_file):
        return csv_file
    return os.path.join(datasources_path, csv_file)


def load_datasource_csv(
    csv_path: str,
    config: DatasourceConfig,
    test_rows: Optional[int] = None,
) -> LoadedCsv:
    """Load and prepare a CSV for a Blue datasource push.

    - Applies config.column_map (CSV name → Blue name).
    - Silently drops HASH and any other DROP_COLUMNS (Bug 3).
    - Only includes expected Blue columns that can be filled from the CSV.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    column_map: Dict[str, str] = dict(config.column_map or {})
    expected: List[str] = list(config.columns or [])

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        csv_fieldnames = list(reader.fieldnames or [])

    dropped = [c for c in csv_fieldnames if c in DROP_COLUMNS]
    usable = [c for c in csv_fieldnames if c not in DROP_COLUMNS]

    # Blue column → CSV column used to fill it
    reverse_map: Dict[str, str] = {}

    # Explicit renames: CSV col → Blue col
    for csv_col, blue_col in column_map.items():
        if csv_col in usable:
            reverse_map[blue_col] = csv_col

    # Identity mapping for usable CSV columns that already match Blue names
    for csv_col in usable:
        if csv_col not in reverse_map:
            reverse_map[csv_col] = csv_col

    if expected:
        blue_columns: List[str] = []
        for bc in expected:
            if bc in DROP_COLUMNS:
                continue
            # Prefer reverse_map entry
            if bc in reverse_map and reverse_map[bc] in usable:
                blue_columns.append(bc)
            elif bc in usable:
                reverse_map[bc] = bc
                blue_columns.append(bc)
    else:
        # No expected list: push all usable columns after rename
        seen = set()
        blue_columns = []
        for csv_col in usable:
            blue_col = column_map.get(csv_col, csv_col)
            if blue_col in DROP_COLUMNS or blue_col in seen:
                continue
            seen.add(blue_col)
            reverse_map[blue_col] = csv_col
            blue_columns.append(blue_col)

    rows: List[List[str]] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_data = []
            for bc in blue_columns:
                csv_col = reverse_map.get(bc, bc)
                val = row.get(csv_col, "")
                if val is None:
                    val = ""
                row_data.append(val)
            rows.append(row_data)

    total = len(rows)
    if test_rows is not None and test_rows > 0:
        rows = rows[:test_rows]

    logger.info(
        "csv_loaded path=%s rows=%s columns=%s dropped=%s remaps=%s",
        csv_path,
        total,
        blue_columns,
        dropped,
        column_map or {},
    )

    return LoadedCsv(
        columns=blue_columns,
        rows=rows,
        source_path=csv_path,
        dropped_columns=dropped,
        total_rows=total,
    )


def sample_rows(
    columns: Sequence[str], rows: Sequence[Sequence[str]], n: int = 5
) -> List[Dict[str, str]]:
    """Return first n rows as dicts for dry-run display."""
    out: List[Dict[str, str]] = []
    for row in rows[:n]:
        out.append({
            col: (row[i] if i < len(row) else "")
            for i, col in enumerate(columns)
        })
    return out
