#!/usr/bin/env python3
"""
============================================================================
Explorance Blue - Standalone Users.csv Push Script
University of Kentucky TCE System
============================================================================

Pushes Users.csv (Data144) to Explorance Blue via SOAP API.

Fixes three known bugs from the BlueSyncService integration:

  Bug 1 — Wrong column names
    The CSV has FIRSTNAME/LASTNAME; Blue expects FIRSTNAME_1/LASTNAME_1.
    Columns are remapped before the SOAP payload is built.

  Bug 2 — Timeout on PrepareDataToFinalizeImportV2
    The global 180 s timeout is too short; this script uses 600 s.

  Bug 3 — HASH column contains corrupted Python memory-address strings
    The HASH column is silently dropped during CSV loading.

Also pushes UKID_NBR, STU_OBJ_ID, and BLUE_ROLE (Explorance Blue user-type
id, e.g. 23 staff / 03 student / 528 super admin).

Usage:
    # Dry run — validate CSV + BLUE_ROLE, no SOAP (no API key needed)
    python scripts/push_users_to_blue.py --dry-run

    # Preview only super-admin rows (role 528 / specific LinkBlues)
    python scripts/push_users_to_blue.py --dry-run --filter-users EDAK223,MALU227,KJTU228
    python scripts/push_users_to_blue.py --dry-run --filter-role 528

    # Apply Admin-table super-admin overrides, then dry-run
    python scripts/push_users_to_blue.py --dry-run --apply-super-admin-roles

    # Small live test — push only super admins
    python scripts/push_users_to_blue.py --filter-role 528 --verbose

    # Full push
    python scripts/push_users_to_blue.py --wait-before 600 --batch-size 500

Dependencies:
    requests
    python-dotenv
"""

import argparse
import csv
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import requests

# ---------------------------------------------------------------------------
# Optional dotenv support — load .env from project root
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    _ENV_PATH = _PROJECT_ROOT / '.env'
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
except ImportError:
    pass  # dotenv is optional at runtime

# Make project imports available (super-admin role helpers)
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================================
# CONFIGURATION DEFAULTS
# ============================================================================

DEFAULT_WS_URL = "https://my-uky-ws-bc.bluera.com/BlueWebService.svc/file"
DEFAULT_DATASOURCE_ID = "Data144"
DEFAULT_BATCH_SIZE = 500
DEFAULT_CSV_PATH = "datasources/Users.csv"

# Timeouts (seconds)
TIMEOUT_REGISTER = 30
TIMEOUT_PUSH_BATCH = 180
# Prepare is the slow step for full Users (~140k rows). 600s is too short
# for Data144; scale by row count (see recommended_prepare_timeout).
TIMEOUT_PREPARE = 600          # minimum / historical Bug-2 default
TIMEOUT_PREPARE_MAX = 3600     # 1 hour hard cap unless CLI overrides
TIMEOUT_FINALIZE = 600         # finalize can also be slow for large sets
TIMEOUT_FINALIZE_MAX = 1800

# The CSV column → Blue column mapping (Bug 1 fix).
# Also accepts FIRST_NAME / LAST_NAME (strip_hash_column rename).
COLUMN_MAP: Dict[str, str] = {
    'USER_ID':          'USER_ID',
    'UKID_NBR':         'UKID_NBR',
    'STU_OBJ_ID':       'STU_OBJ_ID',
    'FIRSTNAME':        'FIRSTNAME_1',
    'FIRST_NAME':       'FIRSTNAME_1',
    'LASTNAME':         'LASTNAME_1',
    'LAST_NAME':        'LASTNAME_1',
    'EMAIL':            'EMAIL',
    'SECONDARY_EMAIL':  'SECONDARY_EMAIL',
    'BLUE_ROLE':        'BLUE_ROLE',
}

# Columns that Blue expects, in order
BLUE_COLUMNS = [
    'USER_ID',
    'UKID_NBR',
    'STU_OBJ_ID',
    'FIRSTNAME_1',
    'LASTNAME_1',
    'EMAIL',
    'SECONDARY_EMAIL',
    'BLUE_ROLE',
]

# Required for a valid Users push. Identity fields + BLUE_ROLE are preferred
# when present in the CSV; they are not hard-required so older files still load.
REQUIRED_BLUE_COLUMNS = ['USER_ID', 'FIRSTNAME_1', 'LASTNAME_1', 'EMAIL']
PREFERRED_OPTIONAL_COLUMNS = ['UKID_NBR', 'STU_OBJ_ID', 'BLUE_ROLE', 'SECONDARY_EMAIL']


# ============================================================================
# SOAP PAYLOAD BUILDERS
# ============================================================================

def escape_xml(text: str) -> str:
    """Escape special XML characters."""
    if text is None:
        return ""
    text = str(text)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    text = text.replace("'", "&apos;")
    return text


def soap_get_datablock_info(api_key: str, datasource_id: str) -> str:
    """Build SOAP payload for GetDataBlockInformation()."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
    <soapenv:Header>
        <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
        <tem:DatasourceId>{escape_xml(datasource_id)}</tem:DatasourceId>
    </soapenv:Header>
    <soapenv:Body>
        <tem:BasicRequestDataSourceId/>
    </soapenv:Body>
</soapenv:Envelope>"""


def soap_register_import(api_key: str, datasource_id: str) -> str:
    """Build SOAP payload for RegisterImport()."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
    <soapenv:Header>
        <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
    </soapenv:Header>
    <soapenv:Body>
        <tem:RegisterImportRequest>
            <tem:AbortOnEmpty>true</tem:AbortOnEmpty>
            <tem:DataSourceID>{escape_xml(datasource_id)}</tem:DataSourceID>
            <tem:ReplaceBlueRole>false</tem:ReplaceBlueRole>
            <tem:ReplaceDataSourceAccessKey>false</tem:ReplaceDataSourceAccessKey>
            <tem:ReplaceLanguagePreferences>false</tem:ReplaceLanguagePreferences>
        </tem:RegisterImportRequest>
    </soapenv:Body>
</soapenv:Envelope>"""


def soap_push_data(api_key: str, transaction_id: str, block_name: str,
                   columns: List[str], rows: List[List[str]]) -> str:
    """Build SOAP payload for PushObjectDataV2()."""
    columns_xml = "\n".join([
        f'             <arr:string>{escape_xml(col)}</arr:string>'
        for col in columns
    ])

    rows_xml = ""
    for row in rows:
        row_values = "\n".join([
            f"""                  <blue:IDataObj>
                     <blue:IDataObjValue>{escape_xml(str(val) if val is not None else "")}</blue:IDataObjValue>
                  </blue:IDataObj>"""
            for val in row
        ])
        rows_xml += f"""            <blue:IDataRow>
               <blue:IDataRowValue>
{row_values}
               </blue:IDataRowValue>
            </blue:IDataRow>
"""

    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/" xmlns:arr="http://schemas.microsoft.com/2003/10/Serialization/Arrays" xmlns:blue="http://schemas.datacontract.org/2004/07/Blue.Integration">
   <soapenv:Header>
      <tem:TransactionId>{escape_xml(transaction_id)}</tem:TransactionId>
      <tem:DataBlockName>{escape_xml(block_name)}</tem:DataBlockName>
      <tem:ColumnNamesList>
{columns_xml}
      </tem:ColumnNamesList>
      <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
   </soapenv:Header>
   <soapenv:Body>
      <tem:DataObjectTransferRequestV2>
         <tem:Data>
{rows_xml}
         </tem:Data>
      </tem:DataObjectTransferRequestV2>
   </soapenv:Body>
</soapenv:Envelope>"""


def soap_prepare_finalize(api_key: str, transaction_id: str) -> str:
    """Build SOAP payload for PrepareDataToFinalizeImportV2()."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
   <soapenv:Header>
      <tem:TransactionID>{escape_xml(transaction_id)}</tem:TransactionID>
      <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
   </soapenv:Header>
   <soapenv:Body>
      <tem:BasicRequest/>
   </soapenv:Body>
</soapenv:Envelope>"""


def soap_finalize_import(api_key: str, transaction_id: str) -> str:
    """Build SOAP payload for FinalizeImport()."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
   <soapenv:Header>
      <tem:TransactionID>{escape_xml(transaction_id)}</tem:TransactionID>
      <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
   </soapenv:Header>
   <soapenv:Body>
      <tem:FinalizeImportRequest/>
   </soapenv:Body>
</soapenv:Envelope>"""


def soap_cancel_import(api_key: str, transaction_id: str) -> str:
    """Build SOAP payload for CancelImport()."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
   <soapenv:Header>
      <tem:TransactionID>{escape_xml(transaction_id)}</tem:TransactionID>
      <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
   </soapenv:Header>
   <soapenv:Body>
      <tem:CancelImportRequest/>
    </soapenv:Body>
 </soapenv:Envelope>"""


def soap_get_current_process(api_key: str) -> str:
    """Build SOAP payload for GetCurrentImportingDataSourceProcess()."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
   <soapenv:Header>
      <tem:TransactionID>0</tem:TransactionID>
      <tem:APIKeyHeader>{escape_xml(api_key)}</tem:APIKeyHeader>
   </soapenv:Header>
   <soapenv:Body>
      <tem:BaseRequest/>
   </soapenv:Body>
</soapenv:Envelope>"""


# ============================================================================
# API HELPERS
# ============================================================================

def recommended_prepare_timeout(row_count: int, base: int = TIMEOUT_PREPARE) -> int:
    """Scale Prepare timeout by payload size.

    Full Users (~140k rows) routinely needs 20–45+ minutes on Blue.
    Formula: ~1s per 40 rows, clamped to [base, TIMEOUT_PREPARE_MAX].
    """
    if row_count <= 0:
        return base
    scaled = max(base, int(row_count / 40))
    return min(scaled, TIMEOUT_PREPARE_MAX)


def recommended_finalize_timeout(row_count: int, base: int = TIMEOUT_FINALIZE) -> int:
    """Scale Finalize timeout modestly by payload size."""
    if row_count <= 0:
        return base
    scaled = max(base, int(row_count / 100))
    return min(scaled, TIMEOUT_FINALIZE_MAX)


def call_soap(ws_url: str, payload: str, action: str,
              timeout: int = 180) -> requests.Response:
    """Make a SOAP API call to Blue."""
    headers = {
        'Content-Type': 'text/xml; charset=UTF-8',
        'SOAPAction': f'http://tempuri.org/IBlueWebService/{action}',
    }
    # (connect timeout, read timeout) — prepare can sit idle while Blue works
    return requests.post(
        ws_url,
        headers=headers,
        data=payload.encode('utf-8'),
        timeout=(30, timeout),
    )


def check_response(response: requests.Response, action: str) -> Tuple[bool, str, bool]:
    """Parse SOAP response and check for success/failure.

    Returns: (is_success, message, has_warning)
    """
    if response.status_code != 200:
        return False, f"HTTP Error {response.status_code}", False

    text = response.text

    if "INVALID_APIKEY" in text or "Invalid API_KEY" in text:
        return False, "Invalid API Key", False

    is_success = False

    result_match = re.search(
        r'<[^>]*Result[^>]*>(true|false)</[^>]*Result>',
        text, re.IGNORECASE,
    )
    if result_match:
        is_success = result_match.group(1).lower() == 'true'

    is_success_match = re.search(
        r'<[^>]*IsSuccess[^>]*>(true|false)</[^>]*IsSuccess>',
        text, re.IGNORECASE,
    )
    if is_success_match:
        is_success = is_success_match.group(1).lower() == 'true'

    message_match = re.search(
        r'<(?![^>]*Warning)[^>]*Message[^>]*>([^<]*)</[^>]*Message>',
        text,
    )
    message = message_match.group(1) if message_match else ""
    message = message.replace('&#xD;', '\n').replace('&#xA;', '\n')

    warning_match = re.search(
        r'<[^>]*HasWarningMessage[^>]*>(true|false)</[^>]*HasWarningMessage>',
        text, re.IGNORECASE,
    )
    has_warning = warning_match and warning_match.group(1).lower() == 'true'

    if result_match is None and is_success_match is None:
        if "VALID_APIKEY" in text:
            is_success = True

    return is_success, message, has_warning


def extract_value(response_text: str, tag_name: str) -> Optional[str]:
    """Extract a single value from SOAP response by tag name."""
    pattern = rf'<[^>]*{tag_name}[^>]*>([^<]*)</[^>]*{tag_name}>'
    match = re.search(pattern, response_text, re.IGNORECASE)
    return match.group(1) if match else None


def extract_block_name(response_text: str) -> Optional[str]:
    """Extract DataBlockName from GetDataBlockInformation response."""
    match = re.search(
        r'<[^>]*DataBlockName[^>]*>([^<]*)</[^>]*DataBlockName>',
        response_text,
    )
    return match.group(1) if match else None


# ============================================================================
# CORE LOGIC
# ============================================================================

def discover_block_name(api_key: str, ws_url: str,
                        datasource_id: str) -> Optional[str]:
    """Call GetDataBlockInformation and return the DataBlockName."""
    payload = soap_get_datablock_info(api_key, datasource_id)
    response = call_soap(ws_url, payload, "GetDataBlockInformation",
                         timeout=30)
    if response.status_code != 200:
        return None
    return extract_block_name(response.text)


def cancel_import(api_key: str, ws_url: str, transaction_id: str) -> None:
    """Send CancelImport to release an orphaned transaction."""
    try:
        payload = soap_cancel_import(api_key, transaction_id)
        call_soap(ws_url, payload, "CancelImport", timeout=30)
    except Exception:
        pass  # Best-effort cancellation


def cancel_stale_import(api_key: str, ws_url: str) -> bool:
    """Check for and cancel any in-progress import blocking Data144.

    Returns True if a stale import was found and cancelled.
    """
    payload = soap_get_current_process(api_key)
    try:
        response = call_soap(ws_url, payload,
                             "GetCurrentImportingDataSourceProcess",
                             timeout=30)
    except Exception:
        return False

    if response.status_code != 200:
        return False

    stale_tid = extract_value(response.text, "TransactionID")
    progress = extract_value(response.text, "ProgressStatus")
    print(f"  Current Blue import: TransactionID={stale_tid}, "
          f"ProgressStatus={progress}")

    if not stale_tid or stale_tid.strip() == '0':
        return False

    print(f"  Cancelling stale import (TransactionID={stale_tid})...")
    cancel_import(api_key, ws_url, stale_tid)
    time.sleep(2)  # Brief pause for Blue to release the lock
    return True


def _build_reverse_map(csv_fieldnames: Sequence[str]) -> Tuple[List[str], Dict[str, str]]:
    """Return (blue_columns present in CSV, reverse map Blue→CSV)."""
    field_set = set(csv_fieldnames)
    blue_columns: List[str] = []
    reverse_map: Dict[str, str] = {}

    for bc in BLUE_COLUMNS:
        # Prefer earlier COLUMN_MAP entries (FIRSTNAME before FIRST_NAME).
        for csv_col, blue_col in COLUMN_MAP.items():
            if blue_col != bc:
                continue
            if csv_col in field_set:
                blue_columns.append(bc)
                reverse_map[bc] = csv_col
                break
        else:
            # Identity fallback: CSV already uses Blue name
            if bc in field_set and bc not in reverse_map:
                blue_columns.append(bc)
                reverse_map[bc] = bc

    return blue_columns, reverse_map


def load_users_csv(csv_path: str) -> Tuple[List[str], List[List[str]], int, Counter]:
    """Load and validate Users.csv.

    - Strips the HASH column entirely (Bug 3 fix).
    - Applies FIRSTNAME/FIRST_NAME → FIRSTNAME_1, LASTNAME/LAST_NAME → LASTNAME_1.
    - Includes BLUE_ROLE when present in the CSV.
    - Counts rows with blank FIRSTNAME or LASTNAME.
    - Returns BLUE_ROLE value counts for the full file.

    Returns: (blue_columns, rows, blank_name_count, role_counts)
    """
    if not os.path.exists(csv_path):
        print(f"[ERROR] CSV file not found: {csv_path}")
        sys.exit(1)

    with open(csv_path, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f)
        csv_fieldnames = list(reader.fieldnames or [])

    # Drop HASH from consideration (Bug 3)
    csv_fieldnames = [c for c in csv_fieldnames if c.upper() != 'HASH']

    blue_columns, reverse_map = _build_reverse_map(csv_fieldnames)

    missing_required = [c for c in REQUIRED_BLUE_COLUMNS if c not in blue_columns]
    if missing_required:
        print(f"[ERROR] Users.csv is missing required columns for Blue: "
              f"{', '.join(missing_required)}")
        print(f"        CSV headers: {', '.join(csv_fieldnames)}")
        sys.exit(1)

    for col in PREFERRED_OPTIONAL_COLUMNS:
        if col in blue_columns:
            print(f"[ok] {col} column detected — will be included in the push.")
        else:
            print(f"[WARN] {col} not found in Users.csv — will not be pushed.")

    rows: List[List[str]] = []
    blank_name_count = 0
    role_counts: Counter = Counter()

    firstname_idx = blue_columns.index('FIRSTNAME_1') if 'FIRSTNAME_1' in blue_columns else None
    lastname_idx = blue_columns.index('LASTNAME_1') if 'LASTNAME_1' in blue_columns else None
    role_idx = blue_columns.index('BLUE_ROLE') if 'BLUE_ROLE' in blue_columns else None
    user_id_idx = blue_columns.index('USER_ID') if 'USER_ID' in blue_columns else None

    with open(csv_path, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_data = []
            for bc in blue_columns:
                csv_col = reverse_map.get(bc, bc)
                val = row.get(csv_col, '')
                if val is None:
                    val = ''
                else:
                    val = str(val).strip()
                row_data.append(val)

            first_val = row_data[firstname_idx] if firstname_idx is not None else ''
            last_val = row_data[lastname_idx] if lastname_idx is not None else ''
            if not first_val or not last_val:
                blank_name_count += 1

            if role_idx is not None:
                role_counts[row_data[role_idx] or '(blank)'] += 1

            # Skip completely empty USER_ID rows
            if user_id_idx is not None and not row_data[user_id_idx]:
                continue

            rows.append(row_data)

    return blue_columns, rows, blank_name_count, role_counts


def _normalize_id(value: str) -> str:
    return (value or '').strip().upper()


def apply_super_admin_roles_in_memory(
    blue_columns: List[str],
    rows: List[List[str]],
    super_admin_ids: Optional[Set[str]] = None,
) -> Tuple[List[List[str]], Set[str], int]:
    """Force BLUE_ROLE=528 for super-admin LinkBlues in the in-memory rows.

    Does not rewrite the CSV on disk. Returns (rows, resolved_ids, changed_count).
    """
    if 'BLUE_ROLE' not in blue_columns or 'USER_ID' not in blue_columns:
        print("[WARN] Cannot apply super-admin roles — BLUE_ROLE or USER_ID missing.")
        return rows, set(), 0

    if super_admin_ids is None:
        try:
            from app.services.blue_user_roles import get_super_admin_linkblues
            super_admin_ids = get_super_admin_linkblues()
        except Exception as exc:
            print(f"[WARN] Could not load super-admin LinkBlues from Admin DB: {exc}")
            env_raw = os.environ.get('SUPER_ADMIN_LINKBLUES', '') or ''
            super_admin_ids = {
                _normalize_id(p) for p in env_raw.split(',') if p.strip()
            }

    super_admin_ids = {_normalize_id(x) for x in (super_admin_ids or set()) if _normalize_id(x)}
    if not super_admin_ids:
        print("[WARN] No super-admin LinkBlues resolved "
              "(Admin table empty of super_admins and SUPER_ADMIN_LINKBLUES unset).")
        return rows, set(), 0

    try:
        from app.services.blue_user_roles import SUPER_ADMIN_BLUE_ROLE
        role_value = SUPER_ADMIN_BLUE_ROLE
    except Exception:
        role_value = '528'

    uid_idx = blue_columns.index('USER_ID')
    role_idx = blue_columns.index('BLUE_ROLE')
    changed = 0
    found: Set[str] = set()

    for row in rows:
        uid = _normalize_id(row[uid_idx])
        if uid in super_admin_ids:
            found.add(uid)
            if row[role_idx] != role_value:
                row[role_idx] = role_value
                changed += 1

    missing = sorted(super_admin_ids - found)
    print(f"[roles] Super-admin BLUE_ROLE={role_value} for: "
          f"{', '.join(sorted(super_admin_ids))}")
    print(f"        Found in CSV: {len(found)}  Updated: {changed}  "
          f"Missing from CSV: {len(missing)}")
    if missing:
        print(f"        Missing USER_IDs: {', '.join(missing)}")

    return rows, super_admin_ids, changed


def filter_rows(
    blue_columns: List[str],
    rows: List[List[str]],
    *,
    filter_users: Optional[Set[str]] = None,
    filter_role: Optional[str] = None,
) -> List[List[str]]:
    """Filter rows by USER_ID set and/or BLUE_ROLE value."""
    if not filter_users and not filter_role:
        return rows

    uid_idx = blue_columns.index('USER_ID') if 'USER_ID' in blue_columns else None
    role_idx = blue_columns.index('BLUE_ROLE') if 'BLUE_ROLE' in blue_columns else None
    want_users = {_normalize_id(u) for u in (filter_users or set()) if _normalize_id(u)}
    want_role = (filter_role or '').strip()

    out: List[List[str]] = []
    for row in rows:
        if want_users:
            if uid_idx is None or _normalize_id(row[uid_idx]) not in want_users:
                continue
        if want_role:
            if role_idx is None or (row[role_idx] or '').strip() != want_role:
                continue
        out.append(row)
    return out


def _role_counts_from_rows(blue_columns: List[str], rows: List[List[str]]) -> Counter:
    if 'BLUE_ROLE' not in blue_columns:
        return Counter()
    idx = blue_columns.index('BLUE_ROLE')
    return Counter((r[idx] or '(blank)') for r in rows)


def _print_role_summary(role_counts: Counter, label: str = "BLUE_ROLE distribution") -> None:
    if not role_counts:
        print(f"  {label}: (no BLUE_ROLE data)")
        return
    print(f"  {label}:")
    for role, count in sorted(role_counts.items(), key=lambda x: (-x[1], x[0])):
        print(f"    role {role:>8}: {count:,}")


def _print_sample_rows(
    blue_columns: List[str],
    rows: List[List[str]],
    *,
    limit: int = 5,
    prefer_roles: Optional[Sequence[str]] = None,
) -> None:
    """Print a sample of rows, preferring rows with special roles when present."""
    sample = list(rows[:limit])
    if prefer_roles and 'BLUE_ROLE' in blue_columns and 'USER_ID' in blue_columns:
        role_idx = blue_columns.index('BLUE_ROLE')
        preferred = [r for r in rows if (r[role_idx] or '') in set(prefer_roles)]
        if preferred:
            # Unique by USER_ID, prefer special roles first then fill from top
            seen: Set[str] = set()
            sample = []
            uid_idx = blue_columns.index('USER_ID')
            for r in preferred + rows:
                uid = r[uid_idx]
                if uid in seen:
                    continue
                seen.add(uid)
                sample.append(r)
                if len(sample) >= limit:
                    break

    header = " | ".join(blue_columns)
    print(f"  {header}")
    print(f"  {'-' * min(len(header), 120)}")
    for row_data in sample:
        print(f"  {' | '.join(str(v) for v in row_data)}")


def push_users_to_blue(
    csv_path: str,
    api_key: Optional[str],
    ws_url: str,
    datasource_id: str,
    batch_size: int,
    test_rows: Optional[int],
    dry_run: bool,
    wait_before: int,
    skip_cancel_check: bool,
    verbose: bool,
    filter_users: Optional[Set[str]] = None,
    filter_role: Optional[str] = None,
    apply_super_admin_roles: bool = False,
    sample_limit: int = 10,
    prepare_timeout: Optional[int] = None,
    finalize_timeout: Optional[int] = None,
    cancel_on_timeout: bool = False,
    prepare_retries: int = 2,
) -> int:
    """Push Users.csv to Explorance Blue via SOAP API.

    Returns 0 on success, 1 on failure.
    """
    start_time = time.monotonic()
    transaction_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Step 0 — Load and validate CSV
    # ------------------------------------------------------------------
    print(f"[load] Reading {csv_path} ...")
    blue_columns, rows, blank_name_count, full_role_counts = load_users_csv(csv_path)
    total_loaded = len(rows)

    if apply_super_admin_roles:
        rows, _, _ = apply_super_admin_roles_in_memory(blue_columns, rows)

    # Optional filters for targeted BLUE_ROLE testing
    if filter_users or filter_role:
        before = len(rows)
        rows = filter_rows(
            blue_columns, rows,
            filter_users=filter_users,
            filter_role=filter_role,
        )
        parts = []
        if filter_users:
            parts.append(f"users={','.join(sorted(filter_users))}")
        if filter_role:
            parts.append(f"role={filter_role}")
        print(f"[filter] {' '.join(parts)} → {len(rows):,} / {before:,} rows")
        if not rows:
            print("[ERROR] No rows matched the filter. Nothing to push.")
            return 1

    # Apply --test-rows limit (after filters)
    if test_rows is not None and test_rows > 0:
        rows = rows[:test_rows]
        print(f"[test] Limiting to first {len(rows)} rows (--test-rows={test_rows})")

    push_role_counts = _role_counts_from_rows(blue_columns, rows)
    total_batches = (len(rows) + batch_size - 1) // batch_size if rows else 0

    # Pre-push summary
    print()
    print(f"  Users.csv loaded:    {total_loaded:,} rows")
    print(f"  Rows to push:        {len(rows):,}")
    print(f"  Columns to push:     {', '.join(blue_columns)}")
    print(f"  Batch size:          {batch_size}")
    print(f"  Total batches:       {total_batches}")
    if blank_name_count > 0:
        print(f"  Blank name rows:     {blank_name_count} (will push as empty strings)")
    _print_role_summary(full_role_counts, "Full-file BLUE_ROLE distribution")
    if filter_users or filter_role or test_rows or apply_super_admin_roles:
        _print_role_summary(push_role_counts, "Payload BLUE_ROLE distribution")
    print(f"  Target:              {datasource_id} @ {ws_url}")
    print()

    # --dry-run: print sample rows and exit (no API key required)
    if dry_run:
        print("  [DRY RUN] Sample rows as they would be sent to Blue:\n")
        _print_sample_rows(
            blue_columns, rows,
            limit=sample_limit,
            prefer_roles=['528'],
        )
        print()
        if 'BLUE_ROLE' in blue_columns:
            role_idx = blue_columns.index('BLUE_ROLE')
            uid_idx = blue_columns.index('USER_ID')
            role_528 = [r for r in rows if (r[role_idx] or '') == '528']
            if role_528:
                print(f"  Super-admin rows (BLUE_ROLE=528) in payload: {len(role_528)}")
                for r in role_528[:20]:
                    print(f"    {r[uid_idx]}  role={r[role_idx]}  "
                          f"{r[blue_columns.index('FIRSTNAME_1')]} "
                          f"{r[blue_columns.index('LASTNAME_1')]}")
                if len(role_528) > 20:
                    print(f"    ... and {len(role_528) - 20} more")
            else:
                print("  [WARN] No BLUE_ROLE=528 rows in this payload.")
        print()
        print("  [DRY RUN] No SOAP calls were made. Exiting.")
        return 0

    if not api_key:
        print("[ERROR] Blue API key is required for a live push.")
        print("        Set BLUE_API_KEY in .env or pass --api-key.")
        print("        (Dry runs do not need an API key: add --dry-run)")
        return 1

    # ------------------------------------------------------------------
    # Step 1 — Optional wait
    # ------------------------------------------------------------------
    if wait_before > 0:
        print(f"[wait] Waiting {wait_before}s for Blue to finish processing "
              f"previous datasources...")
        remaining = wait_before
        while remaining > 0:
            if remaining % 60 == 0 or remaining <= 10:
                print(f"       {remaining}s remaining...")
            time.sleep(1)
            remaining -= 1
        print()

    # ------------------------------------------------------------------
    # Step 2 — Cancel any stale import
    # ------------------------------------------------------------------
    if not skip_cancel_check:
        print("[0/4] Checking for stale imports...")
        cancelled = cancel_stale_import(api_key, ws_url)
        if cancelled:
            print()
        else:
            print("       No active import found.")
            print()

    # ------------------------------------------------------------------
    # Step 0.5 — Discover block name
    # ------------------------------------------------------------------
    print(f"[0/4] Discovering block name for {datasource_id}...")
    block_name = discover_block_name(api_key, ws_url, datasource_id)
    if not block_name:
        print(f"[ERROR] Could not discover block name for {datasource_id}.")
        return 1
    print(f"       Block name: {block_name}")
    print()

    # ------------------------------------------------------------------
    # Main workflow wrapped in try/except for cancel-on-failure
    # ------------------------------------------------------------------
    try:
        # --------------------------------------------------------------
        # Step 3 — RegisterImport
        # --------------------------------------------------------------
        payload = soap_register_import(api_key, datasource_id)
        response = call_soap(ws_url, payload, "RegisterImport",
                             timeout=TIMEOUT_REGISTER)
        if verbose:
            print(f"  [SOAP] RegisterImport response:\n{response.text[:500]}\n")

        success, message, _ = check_response(response, "RegisterImport")
        if not success and message:
            print(f"[ERROR] RegisterImport failed: {message}")
            return 1

        transaction_id = extract_value(response.text, "TransactionID")
        if not transaction_id or transaction_id.strip() == '0':
            print("[ERROR] Could not get transaction ID from RegisterImport.")
            print(f"  Raw response: {response.text[:400]}")
            return 1

        print(f"[1/4] RegisterImport OK — TransactionID: {transaction_id}")
        print()

        # --------------------------------------------------------------
        # Step 4 — PushObjectDataV2 in batches
        # --------------------------------------------------------------
        rows_pushed = 0
        for batch_num in range(1, total_batches + 1):
            start_idx = (batch_num - 1) * batch_size
            batch = rows[start_idx:start_idx + batch_size]

            payload = soap_push_data(api_key, transaction_id, block_name,
                                     blue_columns, batch)
            response = call_soap(ws_url, payload, "PushObjectDataV2",
                                 timeout=TIMEOUT_PUSH_BATCH)
            if verbose:
                print(f"  [SOAP] PushObjectDataV2 batch {batch_num} "
                      f"response:\n{response.text[:300]}\n")

            success, message, has_warning = check_response(
                response, "PushObjectDataV2",
            )
            if not success:
                raw_snippet = response.text[:500] if response.text else '(empty)'
                print(f"[ERROR] Batch {batch_num}/{total_batches} failed: {message}")
                print(f"  TransactionID: {transaction_id}")
                print(f"  Raw response: {raw_snippet}")
                if transaction_id:
                    cancel_import(api_key, ws_url, transaction_id)
                return 1

            rows_pushed += len(batch)

            # Print progress every 10 batches to avoid flooding
            if batch_num % 10 == 0 or batch_num == total_batches:
                print(f"[2/4] Batch {batch_num}/{total_batches} pushed "
                      f"({rows_pushed:,} / {len(rows):,} rows)")

        print()

        # --------------------------------------------------------------
        # Step 5 — PrepareDataToFinalizeImportV2
        # Full Users (~140k) routinely exceeds the old 600s client timeout.
        # Scale by row count; retry without cancelling (server may still be
        # validating when the first client read timeout fires).
        # --------------------------------------------------------------
        prep_timeout = (
            prepare_timeout
            if prepare_timeout is not None
            else recommended_prepare_timeout(len(rows))
        )
        fin_timeout = (
            finalize_timeout
            if finalize_timeout is not None
            else recommended_finalize_timeout(len(rows))
        )
        retries = max(1, int(prepare_retries))

        print("[3/4] Validating import (this can take a long time "
              "for large datasets)...")
        print(f"      rows={len(rows):,}  prepare_timeout={prep_timeout}s  "
              f"retries={retries}  cancel_on_timeout={cancel_on_timeout}")
        if len(rows) > 50_000 and prep_timeout < 1800:
            print("      [hint] For full Users, prefer --prepare-timeout 3600")

        response = None
        for attempt in range(1, retries + 1):
            # Stretch timeout slightly on later attempts
            attempt_timeout = prep_timeout if attempt == 1 else min(
                prep_timeout + 600 * (attempt - 1),
                max(prep_timeout, TIMEOUT_PREPARE_MAX),
            )
            print(f"      Prepare attempt {attempt}/{retries} "
                  f"(timeout {attempt_timeout}s)...")
            payload = soap_prepare_finalize(api_key, transaction_id)
            try:
                response = call_soap(
                    ws_url, payload,
                    "PrepareDataToFinzalizeImportV2",
                    timeout=attempt_timeout,
                )
                break
            except requests.exceptions.Timeout:
                print(f"[WARN] Prepare attempt {attempt}/{retries} timed out "
                      f"after {attempt_timeout}s "
                      f"(TransactionID={transaction_id}).")
                if attempt < retries:
                    # Give Blue time to finish server-side work before retry
                    wait_s = min(120, 30 * attempt)
                    print(f"       Waiting {wait_s}s then retrying prepare "
                          f"(import NOT cancelled)...")
                    time.sleep(wait_s)
                    continue

                print(f"[ERROR] Timeout after {retries} prepare attempt(s) "
                      f"on PrepareDataToFinalizeImportV2.")
                print("        All batches were already uploaded successfully.")
                print(f"        TransactionID: {transaction_id}")
                print("        Blue may still be validating server-side.")
                print("        Re-run with a longer timeout, e.g.:")
                print(f"          python scripts/push_users_to_blue.py "
                      f"--prepare-timeout 3600")
                print("        Do NOT re-push batches while this transaction "
                      f"is still active — check Blue import status first.")
                if cancel_on_timeout and transaction_id:
                    print("        --cancel-on-timeout set: cancelling import.")
                    cancel_import(api_key, ws_url, transaction_id)
                else:
                    print("        Import left open (not cancelled) so Blue "
                          "can finish. Use Blue UI or CancelImport if stuck.")
                return 1

        if verbose and response is not None:
            print(f"  [SOAP] PrepareDataToFinzalizeImportV2 response:\n"
                  f"{response.text[:500]}\n")

        success, message, _ = check_response(
            response, "PrepareDataToFinzalizeImportV2",
        )
        if not success:
            print(f"[ERROR] Validation failed: {message}")
            if transaction_id:
                cancel_import(api_key, ws_url, transaction_id)
            return 1

        print("[3/4] Validation OK")
        print()

        # --------------------------------------------------------------
        # Step 6 — FinalizeImport
        # --------------------------------------------------------------
        print(f"[4/4] Finalizing (timeout {fin_timeout}s)...")
        payload = soap_finalize_import(api_key, transaction_id)
        try:
            response = call_soap(ws_url, payload, "FinalizeImport",
                                 timeout=fin_timeout)
        except requests.exceptions.Timeout:
            print(f"[ERROR] Timeout after {fin_timeout}s on FinalizeImport.")
            print(f"        TransactionID: {transaction_id}")
            print("        Import may still complete on the Blue server.")
            print("        Check the Blue transaction log to confirm.")
            if cancel_on_timeout and transaction_id:
                print("        --cancel-on-timeout set: cancelling import.")
                cancel_import(api_key, ws_url, transaction_id)
            else:
                print("        Import left open (not cancelled).")
            return 1

        if verbose:
            print(f"  [SOAP] FinalizeImport response:\n"
                  f"{response.text[:500]}\n")

        success, message, _ = check_response(response, "FinalizeImport")
        if not success:
            print(f"[ERROR] Finalization failed: {message}")
            return 1

        elapsed = time.monotonic() - start_time
        elapsed_min = int(elapsed // 60)
        elapsed_sec = int(elapsed % 60)

        print("[4/4] Import finalized successfully.")
        print("=" * 45)
        print(f"  Push complete!")
        print(f"  Rows pushed:   {rows_pushed:,}")
        print(f"  Batches:       {total_batches}")
        print(f"  Elapsed:       {elapsed_min}m {elapsed_sec}s")
        print("=" * 45)

        return 0

    except KeyboardInterrupt:
        print("\n[ERROR] Interrupted by user.")
        if transaction_id:
            cancel_import(api_key, ws_url, transaction_id)
        return 1

    except requests.exceptions.Timeout as e:
        print(f"\n[ERROR] Timeout: {e}")
        print("        Consider --prepare-timeout 3600 or smaller --filter-*.")
        if cancel_on_timeout and transaction_id:
            cancel_import(api_key, ws_url, transaction_id)
        elif transaction_id:
            print(f"        TransactionID {transaction_id} left open "
                  f"(not cancelled).")
        return 1

    except Exception as e:
        print(f"\n[ERROR] Unexpected error: {e}")
        if transaction_id:
            cancel_import(api_key, ws_url, transaction_id)
        return 1


# ============================================================================
# CLI
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Push Users.csv to Explorance Blue (Data144) via SOAP API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run — validate CSV + BLUE_ROLE mapping (no API key needed)
  python scripts/push_users_to_blue.py --dry-run

  # Preview only super-admin LinkBlues
  python scripts/push_users_to_blue.py --dry-run \\
      --filter-users EDAK223,MALU227,KJTU228

  # Preview everyone currently marked BLUE_ROLE=528
  python scripts/push_users_to_blue.py --dry-run --filter-role 528

  # Apply Admin-table super-admin overrides in memory, then dry-run
  python scripts/push_users_to_blue.py --dry-run --apply-super-admin-roles

  # Live test: push only the super-admin rows
  python scripts/push_users_to_blue.py --filter-role 528 --verbose

  # Push first 50 rows as a column-mapping test
  python scripts/push_users_to_blue.py --test-rows 50 --verbose

  # Full push (prepare timeout auto-scales; override if needed)
  python scripts/push_users_to_blue.py --prepare-timeout 3600

  # Full push with longer wait after previous datasources
  python scripts/push_users_to_blue.py --wait-before 600 --batch-size 500

Environment variables:
  BLUE_API_KEY           Blue API key (or use --api-key)
  BLUE_WS_URL            Blue WS URL (or use --ws-url)
  BLUE_DATASOURCE_ID     Blue datasource ID (or use --datasource-id)
  SUPER_ADMIN_LINKBLUES  Comma-separated LinkBlues for --apply-super-admin-roles
                         (also read from Admin table when Flask app is available)
  BLUE_PREPARE_TIMEOUT   Override prepare timeout seconds
  BLUE_FINALIZE_TIMEOUT  Override finalize timeout seconds
""",
    )

    parser.add_argument(
        '--csv', default=DEFAULT_CSV_PATH,
        help=f'Path to Users.csv (default: {DEFAULT_CSV_PATH})',
    )
    parser.add_argument(
        '--api-key', default=None,
        help='Blue API key (or set BLUE_API_KEY in .env). Not required for --dry-run.',
    )
    parser.add_argument(
        '--ws-url', default=DEFAULT_WS_URL,
        help=f'Blue WS URL (default: {DEFAULT_WS_URL})',
    )
    parser.add_argument(
        '--datasource-id', default=DEFAULT_DATASOURCE_ID,
        help=f'Blue datasource ID (default: {DEFAULT_DATASOURCE_ID})',
    )
    parser.add_argument(
        '--batch-size', type=int, default=DEFAULT_BATCH_SIZE,
        help=f'Rows per push batch (default: {DEFAULT_BATCH_SIZE})',
    )
    parser.add_argument(
        '--test-rows', type=int, default=None,
        help='Only push the first N rows then stop (applied after filters).',
    )
    parser.add_argument(
        '--filter-users', default=None,
        help='Comma-separated USER_IDs / LinkBlues to push (case-insensitive). '
             'Useful for testing BLUE_ROLE on specific super admins.',
    )
    parser.add_argument(
        '--filter-role', default=None,
        help='Only push rows whose BLUE_ROLE equals this value (e.g. 528).',
    )
    parser.add_argument(
        '--apply-super-admin-roles', action='store_true',
        help='Before push, force BLUE_ROLE=528 for super admins resolved from '
             'the Admin table and/or SUPER_ADMIN_LINKBLUES (in memory only).',
    )
    parser.add_argument(
        '--sample-limit', type=int, default=10,
        help='How many sample rows to print on --dry-run (default: 10).',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Load and validate CSV, print BLUE_ROLE summary + sample rows, '
             'but do NOT call any SOAP endpoints. No API key required.',
    )
    parser.add_argument(
        '--wait-before', type=int, default=0,
        help='Seconds to wait before starting (default: 0).',
    )
    parser.add_argument(
        '--skip-cancel-check', action='store_true',
        help='Skip the GetCurrentImportingDataSourceProcess check at '
             'startup.',
    )
    parser.add_argument(
        '--verbose', action='store_true',
        help='Print raw SOAP responses for every call.',
    )
    parser.add_argument(
        '--prepare-timeout', type=int, default=None,
        help='Seconds to wait for PrepareDataToFinalizeImportV2 '
             f'(default: auto-scale by row count, max {TIMEOUT_PREPARE_MAX}s). '
             'Also reads BLUE_PREPARE_TIMEOUT.',
    )
    parser.add_argument(
        '--finalize-timeout', type=int, default=None,
        help='Seconds to wait for FinalizeImport '
             f'(default: auto-scale by row count, max {TIMEOUT_FINALIZE_MAX}s). '
             'Also reads BLUE_FINALIZE_TIMEOUT.',
    )
    parser.add_argument(
        '--prepare-retries', type=int, default=2,
        help='How many prepare attempts on client timeout (default: 2). '
             'Does not cancel between retries.',
    )
    parser.add_argument(
        '--cancel-on-timeout', action='store_true',
        help='Cancel the Blue import if prepare/finalize times out. '
             'Default is to LEAVE the import open — cancelling often kills '
             'server-side work that was still running.',
    )

    args = parser.parse_args()

    # Resolve API key: CLI arg → env var (optional for dry-run)
    api_key = args.api_key or os.getenv('BLUE_API_KEY')
    if not api_key and not args.dry_run:
        print("[ERROR] Blue API key is required for a live push.")
        print("        Set BLUE_API_KEY in .env or pass --api-key.")
        print("        For CSV/BLUE_ROLE validation only: add --dry-run")
        return 1

    # Resolve CSV path — support relative paths from cwd or project root
    csv_path = Path(args.csv)
    if not csv_path.is_absolute():
        # Try cwd first, then project root
        cwd_path = Path.cwd() / csv_path
        prj_path = _PROJECT_ROOT / csv_path
        if cwd_path.exists():
            csv_path = str(cwd_path)
        elif prj_path.exists():
            csv_path = str(prj_path)
        else:
            csv_path = str(cwd_path)  # Let file-not-found error fire later

    filter_users = None
    if args.filter_users:
        filter_users = {
            _normalize_id(p) for p in args.filter_users.split(',') if p.strip()
        }

    # Timeout overrides: CLI > env > auto-scale (None)
    prepare_timeout = args.prepare_timeout
    if prepare_timeout is None and os.getenv('BLUE_PREPARE_TIMEOUT'):
        try:
            prepare_timeout = int(os.getenv('BLUE_PREPARE_TIMEOUT', ''))
        except ValueError:
            print("[WARN] Invalid BLUE_PREPARE_TIMEOUT; using auto-scale.")
            prepare_timeout = None

    finalize_timeout = args.finalize_timeout
    if finalize_timeout is None and os.getenv('BLUE_FINALIZE_TIMEOUT'):
        try:
            finalize_timeout = int(os.getenv('BLUE_FINALIZE_TIMEOUT', ''))
        except ValueError:
            print("[WARN] Invalid BLUE_FINALIZE_TIMEOUT; using auto-scale.")
            finalize_timeout = None

    return push_users_to_blue(
        csv_path=str(csv_path),
        api_key=api_key,
        ws_url=args.ws_url or os.getenv('BLUE_WS_URL') or DEFAULT_WS_URL,
        datasource_id=(
            args.datasource_id
            or os.getenv('BLUE_DATASOURCE_ID')
            or DEFAULT_DATASOURCE_ID
        ),
        batch_size=args.batch_size,
        test_rows=args.test_rows,
        dry_run=args.dry_run,
        wait_before=args.wait_before,
        skip_cancel_check=args.skip_cancel_check,
        verbose=args.verbose,
        filter_users=filter_users,
        filter_role=args.filter_role,
        apply_super_admin_roles=args.apply_super_admin_roles,
        sample_limit=args.sample_limit,
        prepare_timeout=prepare_timeout,
        finalize_timeout=finalize_timeout,
        cancel_on_timeout=args.cancel_on_timeout,
        prepare_retries=args.prepare_retries,
    )


if __name__ == '__main__':
    sys.exit(main())
