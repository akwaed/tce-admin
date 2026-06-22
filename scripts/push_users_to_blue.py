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

Usage:
    # Dry run — validate CSV, print first 5 rows as they would be sent
    python scripts/push_users_to_blue.py --dry-run

    # Small test — push first 50 rows only
    python scripts/push_users_to_blue.py --test-rows 50 --verbose

    # Full push with wait after previous datasources
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
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Optional dotenv support — load .env from project root
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
    _ENV_PATH = _PROJECT_ROOT / '.env'
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
except ImportError:
    pass  # dotenv is optional at runtime


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
TIMEOUT_PREPARE = 600   # 10 minutes (Bug 2 fix)
TIMEOUT_FINALIZE = 300  #  5 minutes

# The CSV column → Blue column mapping (Bug 1 fix)
COLUMN_MAP: Dict[str, str] = {
    'USER_ID':          'USER_ID',
    'FIRSTNAME':        'FIRSTNAME_1',
    'LASTNAME':         'LASTNAME_1',
    'EMAIL':            'EMAIL',
    'SECONDARY_EMAIL':  'SECONDARY_EMAIL',
}

# Columns that Blue expects, in order
BLUE_COLUMNS = ['USER_ID', 'FIRSTNAME_1', 'LASTNAME_1', 'EMAIL']


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

def call_soap(ws_url: str, payload: str, action: str,
              timeout: int = 180) -> requests.Response:
    """Make a SOAP API call to Blue."""
    headers = {
        'Content-Type': 'text/xml; charset=UTF-8',
        'SOAPAction': f'http://tempuri.org/IBlueWebService/{action}',
    }
    return requests.post(ws_url, headers=headers,
                         data=payload.encode('utf-8'), timeout=timeout)


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


def load_users_csv(csv_path: str) -> Tuple[List[str], List[List[str]], int]:
    """Load and validate Users.csv.

    - Strips the HASH column entirely (Bug 3 fix).
    - Applies FIRSTNAME → FIRSTNAME_1, LASTNAME → LASTNAME_1 rename (Bug 1 fix).
    - Counts rows with blank FIRSTNAME or LASTNAME.

    Returns: (blue_columns, rows, blank_name_count)
    """
    if not os.path.exists(csv_path):
        print(f"[ERROR] CSV file not found: {csv_path}")
        sys.exit(1)

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        csv_fieldnames = reader.fieldnames or []

    # Determine which Blue columns we can actually fill
    blue_columns = []
    for bc in BLUE_COLUMNS:
        # Check if any CSV column maps to this Blue column
        for csv_col, blue_col in COLUMN_MAP.items():
            if blue_col == bc and csv_col in csv_fieldnames:
                blue_columns.append(bc)
                break

    # Append SECONDARY_EMAIL if present in CSV
    if 'SECONDARY_EMAIL' in csv_fieldnames:
        if 'SECONDARY_EMAIL' not in blue_columns:
            blue_columns.append('SECONDARY_EMAIL')

    # Build reverse map: Blue column → CSV column
    reverse_map: Dict[str, str] = {}
    for csv_col, blue_col in COLUMN_MAP.items():
        if csv_col in csv_fieldnames and blue_col in blue_columns:
            reverse_map[blue_col] = csv_col

    rows = []
    blank_name_count = 0

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_data = []
            for bc in blue_columns:
                csv_col = reverse_map.get(bc, bc)
                val = row.get(csv_col, '')
                if val is None:
                    val = ''
                row_data.append(val)

            # Check blank names
            firstname_idx = blue_columns.index('FIRSTNAME_1') if 'FIRSTNAME_1' in blue_columns else None
            lastname_idx = blue_columns.index('LASTNAME_1') if 'LASTNAME_1' in blue_columns else None
            first_val = row_data[firstname_idx] if firstname_idx is not None else ''
            last_val = row_data[lastname_idx] if lastname_idx is not None else ''

            if not first_val.strip() or not last_val.strip():
                blank_name_count += 1

            rows.append(row_data)

    return blue_columns, rows, blank_name_count


def push_users_to_blue(
    csv_path: str,
    api_key: str,
    ws_url: str,
    datasource_id: str,
    batch_size: int,
    test_rows: Optional[int],
    dry_run: bool,
    wait_before: int,
    skip_cancel_check: bool,
    verbose: bool,
) -> int:
    """Push Users.csv to Explorance Blue via SOAP API.

    Returns 0 on success, 1 on failure.
    """
    start_time = time.monotonic()
    transaction_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Step 0 — Load and validate CSV
    # ------------------------------------------------------------------
    blue_columns, rows, blank_name_count = load_users_csv(csv_path)
    total_rows = len(rows)

    # Apply --test-rows limit
    if test_rows is not None and test_rows > 0:
        rows = rows[:test_rows]
        print(f"[test] Limiting to first {len(rows)} rows (--test-rows={test_rows})")

    total_batches = (len(rows) + batch_size - 1) // batch_size if rows else 0

    # Pre-push summary
    print()
    print(f"  Users.csv loaded:   {total_rows:,} rows")
    print(f"  Columns to push:     {', '.join(blue_columns)}")
    print(f"  Batch size:          {batch_size}")
    print(f"  Total batches:       {total_batches}")
    if blank_name_count > 0:
        print(f"  Blank name rows:     {blank_name_count} (will push as empty strings)")
    print(f"  Target:              {datasource_id} @ {ws_url}")
    print()

    # --dry-run: print sample rows and exit
    if dry_run:
        print("  [DRY RUN] First 5 rows as they would be sent to Blue:\n")
        header = " | ".join(blue_columns)
        print(f"  {header}")
        print(f"  {'-' * len(header)}")
        for row_data in rows[:5]:
            print(f"  {' | '.join(str(v) for v in row_data)}")
        print()
        print("  [DRY RUN] No SOAP calls were made. Exiting.")
        return 0

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
        # Step 5 — PrepareDataToFinalizeImportV2 (Bug 2 fix: 600 s)
        # --------------------------------------------------------------
        print("[3/4] Validating import (this can take several minutes "
              "for large datasets)...")
        print(f"      timeout: {TIMEOUT_PREPARE}s")
        payload = soap_prepare_finalize(api_key, transaction_id)
        try:
            response = call_soap(ws_url, payload,
                                 "PrepareDataToFinzalizeImportV2",
                                 timeout=TIMEOUT_PREPARE)
        except requests.exceptions.Timeout:
            print(f"[ERROR] Timeout after {TIMEOUT_PREPARE}s on "
                  f"PrepareDataToFinalizeImportV2.")
            print("        If this persists, the dataset may be too large "
                  "for Blue to validate.")
            print("        Import cancelled.")
            if transaction_id:
                cancel_import(api_key, ws_url, transaction_id)
            return 1

        if verbose:
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
        # Step 6 — FinalizeImport (timeout: 300 s)
        # --------------------------------------------------------------
        print("[4/4] Finalizing...")
        payload = soap_finalize_import(api_key, transaction_id)
        try:
            response = call_soap(ws_url, payload, "FinalizeImport",
                                 timeout=TIMEOUT_FINALIZE)
        except requests.exceptions.Timeout:
            print(f"[ERROR] Timeout after {TIMEOUT_FINALIZE}s on "
                  f"FinalizeImport.")
            print("        Import may still complete on the Blue server.")
            print("        Check the Blue transaction log to confirm.")
            if transaction_id:
                cancel_import(api_key, ws_url, transaction_id)
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
        print("        Consider increasing timeouts or reducing --batch-size.")
        if transaction_id:
            cancel_import(api_key, ws_url, transaction_id)
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
  # Dry run — see what would be sent
  python scripts/push_users_to_blue.py --dry-run

  # Push first 50 rows as a column-mapping test
  python scripts/push_users_to_blue.py --test-rows 50 --verbose

  # Full push with longer wait after previous datasources
  python scripts/push_users_to_blue.py --wait-before 600 --batch-size 500

Environment variables:
  BLUE_API_KEY    Blue API key (or use --api-key)
  BLUE_WS_URL     Blue WS URL (or use --ws-url)
  BLUE_DATASOURCE_ID  Blue datasource ID (or use --datasource-id)
""",
    )

    parser.add_argument(
        '--csv', default=DEFAULT_CSV_PATH,
        help=f'Path to Users.csv (default: {DEFAULT_CSV_PATH})',
    )
    parser.add_argument(
        '--api-key', default=None,
        help='Blue API key (or set BLUE_API_KEY in .env)',
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
        help='Only push the first N rows then stop.',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Load and validate CSV, print first 5 rows, but do NOT '
             'call any SOAP endpoints.',
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

    args = parser.parse_args()

    # Resolve API key: CLI arg → env var
    api_key = args.api_key or os.getenv('BLUE_API_KEY')
    if not api_key:
        print("[ERROR] Blue API key is required.")
        print("        Set BLUE_API_KEY in .env or pass --api-key.")
        return 1

    # Resolve CSV path — support relative paths from cwd or project root
    csv_path = Path(args.csv)
    if not csv_path.is_absolute():
        # Try cwd first, then project root
        cwd_path = Path.cwd() / csv_path
        prj_path = Path(__file__).resolve().parent.parent / csv_path
        if cwd_path.exists():
            csv_path = str(cwd_path)
        elif prj_path.exists():
            csv_path = str(prj_path)
        else:
            csv_path = str(cwd_path)  # Let file-not-found error fire later

    return push_users_to_blue(
        csv_path=str(csv_path),
        api_key=api_key,
        ws_url=args.ws_url,
        datasource_id=args.datasource_id,
        batch_size=args.batch_size,
        test_rows=args.test_rows,
        dry_run=args.dry_run,
        wait_before=args.wait_before,
        skip_cancel_check=args.skip_cancel_check,
        verbose=args.verbose,
    )


if __name__ == '__main__':
    sys.exit(main())
