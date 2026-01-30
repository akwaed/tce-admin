"""
================================================================================
Explorance Blue API - Data Push Service
University of Kentucky TCE System
================================================================================

This service pushes datasource CSV files to Explorance Blue.
Datasources:
  1. Users.csv           -> Data144 (Users)
  2. Courses.csv         -> Data161 (Courses)
  3. Instructor_Course.csv -> Data162 (Course Instructors)
  4. Student_Course.csv  -> Data163 (Course Students)

IMPORTANT: Import order matters! Blue requires:
  - Users must be imported BEFORE instructor/student relationships
  - Courses must be imported BEFORE instructor/student relationships
  - Recommended order: Users -> Courses -> Instructors -> Students

API Workflow (per datasource):
  1. GetDataBlockInformation() - Get block name and schema
  2. RegisterImport()          - Start transaction, get transaction ID
  3. PushObjectDataV2()        - Push data in batches of 1000
  4. PrepareDataToFinalizeImportV2() - Validate data
  5. FinalizeImport()          - Complete the import
  6. CancelImport()            - (Only if something fails)
================================================================================
"""

import requests
import csv
import re
import os
import threading
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any
from flask import current_app

# ============================================================================
# CONFIGURATION
# ============================================================================

# Default Blue Web Service endpoint (can be overridden in settings)
DEFAULT_BLUE_WS_URL = "https://my-uky-ws-bc.bluera.com/BlueWebService.svc/file"

# Batch size for pushing data
BATCH_SIZE = 1000

# ============================================================================
# DATASOURCE DEFINITIONS
# ============================================================================

DATASOURCES = {
    'users': {
        'id': 'Data144',
        'name': 'Users',
        'csv_file': 'Users.csv',
        'block_name': None,
        'columns': [
            'USER_ID', 'FIRST_NAME', 'LAST_NAME', 'EMAIL', 'SECONDARY_EMAIL',
        ],
        'required': ['USER_ID', 'FIRST_NAME', 'LAST_NAME', 'EMAIL'],
    },
    'courses': {
        'id': 'Data161',
        'name': 'Courses',
        'csv_file': 'Courses.csv',
        'block_name': '23_Courses',
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
        'required': ['SECTION_KEY', 'TITLE'],
    },
    'instructors': {
        'id': 'Data162',
        'name': 'Course Instructors',
        'csv_file': 'Instructor_Course.csv',
        'block_name': None,
        'columns': [
            'SECTION_KEY', 'USER_ID', 'FIRST_NAME', 'LAST_NAME', 'EMAIL',
        ],
        'required': ['SECTION_KEY', 'USER_ID'],
    },
    'students': {
        'id': 'Data163',
        'name': 'Course Students',
        'csv_file': 'Student_Course.csv',
        'block_name': None,
        'columns': ['SECTION_KEY', 'USER_ID'],
        'required': ['SECTION_KEY', 'USER_ID'],
    },
}

IMPORT_ORDER = ['users', 'courses', 'instructors', 'students']

# ============================================================================
# GLOBAL PROGRESS TRACKING
# ============================================================================

_blue_sync_progress = {
    'running': False,
    'current_datasource': '',
    'current_step': '',
    'datasource_number': 0,
    'total_datasources': 4,
    'batch_number': 0,
    'total_batches': 0,
    'records_processed': 0,
    'started_at': None,
    'error': None,
    'results': {}
}
_blue_sync_lock = threading.Lock()


def get_blue_sync_progress():
    """Get current Blue sync progress."""
    with _blue_sync_lock:
        return _blue_sync_progress.copy()


def _update_blue_progress(datasource='', step='', ds_num=0, batch=0, total_batches=0,
                          records=0, error=None, result=None):
    """Update Blue sync progress."""
    with _blue_sync_lock:
        if datasource:
            _blue_sync_progress['current_datasource'] = datasource
        if step:
            _blue_sync_progress['current_step'] = step
        if ds_num:
            _blue_sync_progress['datasource_number'] = ds_num
        if batch:
            _blue_sync_progress['batch_number'] = batch
        if total_batches:
            _blue_sync_progress['total_batches'] = total_batches
        if records:
            _blue_sync_progress['records_processed'] += records
        if error:
            _blue_sync_progress['error'] = error
        if result:
            _blue_sync_progress['results'][datasource] = result


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
        <tem:APIKeyHeader>{api_key}</tem:APIKeyHeader>
        <tem:DatasourceId>{datasource_id}</tem:DatasourceId>
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
        <tem:APIKeyHeader>{api_key}</tem:APIKeyHeader>
    </soapenv:Header>
    <soapenv:Body>
        <tem:RegisterImportRequest>
            <tem:AbortOnEmpty>true</tem:AbortOnEmpty>
            <tem:DataSourceID>{datasource_id}</tem:DataSourceID>
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
        f'             <arr:string>{col}</arr:string>'
        for col in columns
    ])

    rows_xml = ""
    for row in rows:
        row_values = "\n".join([
            f'''                  <blue:IDataObj>
                     <blue:IDataObjValue>{escape_xml(str(val) if val is not None else "")}</blue:IDataObjValue>
                  </blue:IDataObj>'''
            for val in row
        ])
        rows_xml += f'''            <blue:IDataRow>
               <blue:IDataRowValue>
{row_values}
               </blue:IDataRowValue>
            </blue:IDataRow>
'''

    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/" xmlns:arr="http://schemas.microsoft.com/2003/10/Serialization/Arrays" xmlns:blue="http://schemas.datacontract.org/2004/07/Blue.Integration">
   <soapenv:Header>
      <tem:TransactionId>{transaction_id}</tem:TransactionId>
      <tem:DataBlockName>{block_name}</tem:DataBlockName>
      <tem:ColumnNamesList>
{columns_xml}
      </tem:ColumnNamesList>
      <tem:APIKeyHeader>{api_key}</tem:APIKeyHeader>
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
      <tem:TransactionID>{transaction_id}</tem:TransactionID>
      <tem:APIKeyHeader>{api_key}</tem:APIKeyHeader>
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
      <tem:TransactionID>{transaction_id}</tem:TransactionID>
      <tem:APIKeyHeader>{api_key}</tem:APIKeyHeader>
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
      <tem:TransactionID>{transaction_id}</tem:TransactionID>
      <tem:APIKeyHeader>{api_key}</tem:APIKeyHeader>
   </soapenv:Header>
   <soapenv:Body>
      <tem:CancelImportRequest/>
   </soapenv:Body>
</soapenv:Envelope>"""


# ============================================================================
# API HELPERS
# ============================================================================

def call_soap(ws_url: str, payload: str, action: str, timeout: int = 180) -> requests.Response:
    """Make a SOAP API call to Blue."""
    headers = {
        'Content-Type': 'text/xml; charset=UTF-8',
        'SOAPAction': f'http://tempuri.org/IBlueWebService/{action}',
    }
    return requests.post(ws_url, headers=headers, data=payload.encode('utf-8'), timeout=timeout)


def check_response(response: requests.Response, action: str) -> Tuple[bool, str, bool]:
    """Parse SOAP response and check for success/failure."""
    if response.status_code != 200:
        return False, f"HTTP Error {response.status_code}", False

    text = response.text

    if "INVALID_APIKEY" in text or "Invalid API_KEY" in text:
        return False, "Invalid API Key", False

    is_success = False

    result_match = re.search(r'<[^>]*Result[^>]*>(true|false)</[^>]*Result>', text, re.IGNORECASE)
    if result_match:
        is_success = result_match.group(1).lower() == 'true'

    is_success_match = re.search(r'<[^>]*IsSuccess[^>]*>(true|false)</[^>]*IsSuccess>', text, re.IGNORECASE)
    if is_success_match:
        is_success = is_success_match.group(1).lower() == 'true'

    message_match = re.search(r'<[^>]*Message[^>]*>([^<]*)</[^>]*Message>', text)
    message = message_match.group(1) if message_match else ""
    message = message.replace('&#xD;', '\n').replace('&#xA;', '\n')

    warning_match = re.search(r'<[^>]*HasWarningMessage[^>]*>(true|false)</[^>]*HasWarningMessage>',
                              text, re.IGNORECASE)
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
    match = re.search(r'<a:DataBlockName>([^<]*)</a:DataBlockName>', response_text)
    return match.group(1) if match else None


# ============================================================================
# BLUE SYNC SERVICE CLASS
# ============================================================================

class BlueSyncService:
    """Service for pushing datasource files to Explorance Blue."""

    def __init__(self, datasources_path: str = './datasources'):
        self.datasources_path = datasources_path
        self.api_key = None
        self.ws_url = DEFAULT_BLUE_WS_URL
        self.errors = []
        self.stats = {}
        self.results = {}

    def _get_api_key(self) -> Optional[str]:
        """Get API key from database settings."""
        from app.models.settings import SystemSetting
        return SystemSetting.get(SystemSetting.BLUE_API_KEY)

    def _get_ws_url(self) -> str:
        """Get Blue WS URL from database settings."""
        from app.models.settings import SystemSetting
        url = SystemSetting.get(SystemSetting.BLUE_WS_URL)
        return url if url else DEFAULT_BLUE_WS_URL

    def validate_api_key(self) -> Tuple[bool, str]:
        """Validate the API key by making a test call."""
        api_key = self._get_api_key()
        if not api_key:
            return False, "API key not configured"

        ws_url = self._get_ws_url()

        try:
            # Use GetDataBlockInformation on a known datasource to validate
            payload = soap_get_datablock_info(api_key, 'Data144')
            response = call_soap(ws_url, payload, "GetDataBlockInformation", timeout=30)

            if response.status_code != 200:
                return False, f"HTTP Error {response.status_code}"

            if "INVALID_APIKEY" in response.text:
                return False, "Invalid API Key"

            if "VALID_APIKEY" in response.text or extract_block_name(response.text):
                return True, "API key is valid"

            return False, "Could not validate API key"

        except requests.exceptions.Timeout:
            return False, "Connection timeout"
        except requests.exceptions.ConnectionError:
            return False, "Connection error"
        except Exception as e:
            return False, f"Error: {str(e)}"

    def push_all(self, datasources: List[str] = None, dry_run: bool = False,
                 triggered_by=None, trigger_type: str = 'manual') -> Dict[str, Any]:
        """
        Push all (or specified) datasources to Blue.

        Args:
            datasources: List of datasource keys to push. If None, push all.
            dry_run: If True, validate but don't push.
            triggered_by: Admin who triggered the sync.
            trigger_type: 'manual', 'scheduled', or 'api'.

        Returns:
            Dict with results, stats, and errors.
        """
        global _blue_sync_progress
        from app.models import db
        from app.models.settings import DataSyncLog

        # Get API credentials
        self.api_key = self._get_api_key()
        self.ws_url = self._get_ws_url()

        if not self.api_key:
            return {
                'success': False,
                'error': 'API key not configured. Please configure it in Settings.',
                'results': {},
                'stats': {}
            }

        # Determine which datasources to push
        to_push = datasources if datasources else IMPORT_ORDER
        to_push = [ds for ds in IMPORT_ORDER if ds in to_push]  # Maintain order

        # Initialize progress
        with _blue_sync_lock:
            _blue_sync_progress = {
                'running': True,
                'current_datasource': '',
                'current_step': 'Initializing...',
                'datasource_number': 0,
                'total_datasources': len(to_push),
                'batch_number': 0,
                'total_batches': 0,
                'records_processed': 0,
                'started_at': datetime.utcnow().isoformat(),
                'error': None,
                'results': {}
            }

        # Create sync log
        sync_log = DataSyncLog(
            sync_type=DataSyncLog.TYPE_DATASOURCE_TO_BLUE,
            status=DataSyncLog.STATUS_RUNNING,
            triggered_by_id=triggered_by.id if triggered_by else None,
            trigger_type=trigger_type
        )
        db.session.add(sync_log)
        db.session.commit()

        self.errors = []
        self.results = {}
        self.stats = {
            'total_records': 0,
            'datasources_success': 0,
            'datasources_failed': 0
        }

        try:
            for idx, ds_key in enumerate(to_push, 1):
                _update_blue_progress(
                    datasource=ds_key,
                    step='Starting...',
                    ds_num=idx
                )

                try:
                    success = self._import_datasource(ds_key, dry_run=dry_run)
                    self.results[ds_key] = 'SUCCESS' if success else 'FAILED'
                    if success:
                        self.stats['datasources_success'] += 1
                    else:
                        self.stats['datasources_failed'] += 1

                    _update_blue_progress(
                        datasource=ds_key,
                        result='SUCCESS' if success else 'FAILED'
                    )

                except Exception as e:
                    self.errors.append(f"{ds_key}: {str(e)}")
                    self.results[ds_key] = f'ERROR: {str(e)}'
                    self.stats['datasources_failed'] += 1
                    _update_blue_progress(
                        datasource=ds_key,
                        error=str(e),
                        result='ERROR'
                    )

            # Update sync log
            all_success = self.stats['datasources_failed'] == 0
            sync_log.status = DataSyncLog.STATUS_COMPLETED if all_success else DataSyncLog.STATUS_FAILED
            sync_log.completed_at = datetime.utcnow()
            sync_log.blue_results = self.results
            sync_log.records_processed = self.stats['total_records']
            sync_log.errors = self.errors[:50]
            sync_log.summary = {
                'datasources_success': self.stats['datasources_success'],
                'datasources_failed': self.stats['datasources_failed'],
                'dry_run': dry_run
            }
            db.session.commit()

            # Mark as complete
            with _blue_sync_lock:
                _blue_sync_progress['running'] = False
                _blue_sync_progress['current_step'] = 'Complete'

            return {
                'success': all_success,
                'results': self.results,
                'stats': self.stats,
                'errors': self.errors[:10],
                'sync_log_id': sync_log.id
            }

        except Exception as e:
            sync_log.fail(str(e))
            db.session.commit()

            with _blue_sync_lock:
                _blue_sync_progress['running'] = False
                _blue_sync_progress['error'] = str(e)

            raise

    def _import_datasource(self, datasource_key: str, dry_run: bool = False) -> bool:
        """Import a single datasource to Blue."""
        ds = DATASOURCES[datasource_key]

        _update_blue_progress(step='Discovering schema...')

        # Discover block name if not configured
        block_name = ds.get('block_name')
        columns = ds.get('columns', [])

        if not block_name:
            block_name = self._discover_block_name(ds['id'])
            if not block_name:
                self.errors.append(f"{datasource_key}: Could not discover block name")
                return False

        # Load CSV
        _update_blue_progress(step='Loading CSV...')
        csv_columns, rows = self._load_csv(ds['csv_file'], columns)

        if not rows:
            self.errors.append(f"{datasource_key}: No data to import")
            return False

        self.stats['total_records'] += len(rows)
        columns = csv_columns

        # Dry run - stop here
        if dry_run:
            _update_blue_progress(step=f'Dry run: {len(rows)} rows validated')
            return True

        # Register import
        _update_blue_progress(step='Registering import...')
        payload = soap_register_import(self.api_key, ds['id'])
        response = call_soap(self.ws_url, payload, "RegisterImport")

        success, message, _ = check_response(response, "RegisterImport")
        if not success and message:
            self.errors.append(f"{datasource_key}: RegisterImport failed - {message}")
            return False

        transaction_id = extract_value(response.text, "TransactionID")
        if not transaction_id:
            self.errors.append(f"{datasource_key}: Could not get transaction ID")
            return False

        # Push data in batches
        total_batches = (len(rows) + BATCH_SIZE - 1) // BATCH_SIZE
        _update_blue_progress(step='Pushing data...', total_batches=total_batches)

        try:
            for i in range(0, len(rows), BATCH_SIZE):
                batch = rows[i:i + BATCH_SIZE]
                batch_num = (i // BATCH_SIZE) + 1

                _update_blue_progress(
                    step=f'Pushing batch {batch_num}/{total_batches}...',
                    batch=batch_num,
                    records=len(batch)
                )

                payload = soap_push_data(self.api_key, transaction_id, block_name, columns, batch)
                response = call_soap(self.ws_url, payload, "PushObjectDataV2")

                success, message, has_warning = check_response(response, "PushObjectDataV2")
                if not success:
                    self.errors.append(f"{datasource_key}: Batch {batch_num} failed - {message}")
                    self._cancel_import(transaction_id)
                    return False

            # Prepare for finalization
            _update_blue_progress(step='Validating...')
            payload = soap_prepare_finalize(self.api_key, transaction_id)
            response = call_soap(self.ws_url, payload, "PrepareDataToFinzalizeImportV2")

            success, message, _ = check_response(response, "PrepareDataToFinzalizeImportV2")
            if not success:
                self.errors.append(f"{datasource_key}: Validation failed - {message}")
                self._cancel_import(transaction_id)
                return False

            # Finalize
            _update_blue_progress(step='Finalizing...')
            payload = soap_finalize_import(self.api_key, transaction_id)
            response = call_soap(self.ws_url, payload, "FinalizeImport")

            success, message, _ = check_response(response, "FinalizeImport")
            if not success:
                self.errors.append(f"{datasource_key}: Finalization failed - {message}")
                return False

            _update_blue_progress(step=f'Complete: {len(rows)} rows imported')
            return True

        except KeyboardInterrupt:
            self._cancel_import(transaction_id)
            raise

    def _discover_block_name(self, datasource_id: str) -> Optional[str]:
        """Discover block name for a datasource."""
        payload = soap_get_datablock_info(self.api_key, datasource_id)
        response = call_soap(self.ws_url, payload, "GetDataBlockInformation")

        if response.status_code != 200:
            return None

        return extract_block_name(response.text)

    def _load_csv(self, csv_file: str, expected_columns: List[str]) -> Tuple[List[str], List[List[str]]]:
        """Load CSV file and prepare data for import."""
        filepath = os.path.join(self.datasources_path, csv_file)

        if not os.path.exists(filepath):
            return [], []

        rows = []

        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            csv_columns = reader.fieldnames or []

            # Use only columns that exist in both CSV and expected list
            use_columns = [c for c in expected_columns if c in csv_columns]

            for row in reader:
                row_data = []
                for col in use_columns:
                    val = row.get(col, '')
                    row_data.append(val if val else '')
                rows.append(row_data)

        return use_columns, rows

    def _cancel_import(self, transaction_id: str):
        """Cancel an in-progress import."""
        payload = soap_cancel_import(self.api_key, transaction_id)
        call_soap(self.ws_url, payload, "CancelImport")


def get_blue_sync_service(datasources_path: str = './datasources') -> BlueSyncService:
    """Factory function to get BlueSyncService instance."""
    return BlueSyncService(datasources_path)
