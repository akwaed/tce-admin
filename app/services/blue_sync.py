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

IMPORTANT (Requirement D):
  - Users.csv (Data144, the heaviest) MUST be pushed LAST after all others.
  - At least 3-minute (180s) gap between start of each non-Users push.
  - See push_all() + IMPORT_ORDER + MIN_NON_USERS_GAP_SECONDS.
  (The 3 bug fixes from scripts/push_users_to_blue.py are replicated here:
   column rename, 600s Prepare timeout, HASH column omission.)

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
import time
import threading
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any
from flask import current_app
from app.services.sync_control import (
    SyncCancelledError,
    is_sync_cancellation_requested,
    mark_sync_cancelled,
)

# ============================================================================
# CONFIGURATION
# ============================================================================

# Default Blue Web Service endpoint (can be overridden in settings)
DEFAULT_BLUE_WS_URL = "https://my-uky-ws-bc.bluera.com/BlueWebService.svc/file"

# Batch size for pushing data
BATCH_SIZE = 1000

# Delay between datasource imports (seconds)
# This helps prevent timeouts on the Blue server
IMPORT_DELAY_SECONDS = 300
# Business rule (Requirement D): minimum gap between starts of non-Users
# datasource pushes to avoid overloading Blue. Users.csv (Data144) is
# always pushed *last*.
MIN_NON_USERS_GAP_SECONDS = 180

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
            # Blue schema expects FIRSTNAME_1 / LASTNAME_1 (Bug 1 fix)
            'USER_ID', 'FIRSTNAME_1', 'LASTNAME_1', 'EMAIL', 'SECONDARY_EMAIL',
        ],
        'required': ['USER_ID', 'FIRSTNAME_1', 'LASTNAME_1', 'EMAIL'],
        # CSV columns FIRSTNAME/LASTNAME are renamed to match Blue's schema
        'column_renames': {
            'FIRSTNAME': 'FIRSTNAME_1',
            'LASTNAME': 'LASTNAME_1',
        },
        # Smaller batches for the largest datasource (138k+ rows)
        'batch_size': 500,
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

IMPORT_ORDER = ['courses', 'instructors', 'students', 'users']

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
    'updated_at': None,
    'error': None,
    'results': {}
}
_blue_sync_lock = threading.Lock()


def get_blue_sync_progress():
    """Get current Blue sync progress."""
    with _blue_sync_lock:
        return _blue_sync_progress.copy()


def _get_wait_seconds(ds_obj) -> int:
    """Get the wait-after-seconds for a datasource, with fallback."""
    if hasattr(ds_obj, 'wait_after_seconds') and ds_obj.wait_after_seconds is not None:
        return ds_obj.wait_after_seconds
    return IMPORT_DELAY_SECONDS


def _snapshot_blue_progress():
    """Return a copy of the current Blue sync progress."""
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
        _blue_sync_progress['updated_at'] = datetime.utcnow().isoformat()


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


def soap_get_current_process(api_key: str) -> str:
    """Build SOAP payload for GetCurrentImportingDataSourceProcess()."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tem="http://tempuri.org/">
   <soapenv:Header>
      <tem:TransactionID>0</tem:TransactionID>
      <tem:APIKeyHeader>{api_key}</tem:APIKeyHeader>
   </soapenv:Header>
   <soapenv:Body>
      <tem:BaseRequest/>
   </soapenv:Body>
</soapenv:Envelope>"""


# ============================================================================
# API HELPERS
# ============================================================================

# Per-action SOAP timeouts (Bug 2 fix).
# PrepareDataToFinalizeImportV2 needs 600 s for large datasets (e.g. 138k Users).
# FinalizeImport gets 300 s.  Register/push/cancel stay at their previous defaults.
ACTION_TIMEOUTS = {
    'RegisterImport': 30,
    'PushObjectDataV2': 180,
    'PrepareDataToFinzalizeImportV2': 600,
    'FinalizeImport': 300,
    'CancelImport': 30,
    'GetCurrentImportingDataSourceProcess': 30,
    'GetDataBlockInformation': 30,
}


def call_soap(ws_url: str, payload: str, action: str,
              timeout: Optional[int] = None) -> requests.Response:
    """Make a SOAP API call to Blue.

    If *timeout* is None the per-action default from ACTION_TIMEOUTS is used,
    falling back to 180 s for unknown actions.
    """
    if timeout is None:
        timeout = ACTION_TIMEOUTS.get(action, 180)
    headers = {
        'Content-Type': 'text/xml; charset=UTF-8',
        'SOAPAction': f'http://tempuri.org/IBlueWebService/{action}',
    }
    return requests.post(ws_url, headers=headers,
                         data=payload.encode('utf-8'), timeout=timeout)





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

    message_match = re.search(r'<(?![^>]*Warning)[^>]*Message[^>]*>([^<]*)</[^>]*Message>', text)
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
    match = re.search(r'<[^>]*DataBlockName[^>]*>([^<]*)</[^>]*DataBlockName>', response_text)
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
        self._parent_sync_log_id = None

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

    def _build_progress_summary(self, dry_run: bool = False) -> Dict[str, Any]:
        """Project in-memory Blue progress into a DataSyncLog summary."""
        progress = _snapshot_blue_progress()
        datasource_key = progress.get('current_datasource') or ''
        datasource_name = DATASOURCES.get(datasource_key, {}).get('name')
        if not datasource_name and datasource_key:
            datasource_name = datasource_key.replace('_', ' ').title()

        current_step = progress.get('current_step') or 'Running...'
        pipeline_message = current_step
        if datasource_name:
            pipeline_message = f'{datasource_name}: {current_step}'

        total_datasources = progress.get('total_datasources') or 1
        datasource_number = progress.get('datasource_number') or 0

        return {
            'pipeline_phase': 'blue_push',
            'pipeline_step': datasource_number,
            'pipeline_total_steps': total_datasources,
            'pipeline_message': pipeline_message,
            'current_datasource': datasource_key,
            'current_datasource_name': datasource_name,
            'current_step': current_step,
            'datasource_number': datasource_number,
            'total_datasources': total_datasources,
            'batch_number': progress.get('batch_number') or 0,
            'total_batches': progress.get('total_batches') or 0,
            'records_processed': progress.get('records_processed') or 0,
            'results': progress.get('results') or {},
            'dry_run': dry_run,
            'started_at': progress.get('started_at'),
            'updated_at': progress.get('updated_at'),
            'error': progress.get('error'),
            'parent_sync_log_id': self._parent_sync_log_id,
        }

    def _persist_sync_log_progress(self, sync_log_id: int, dry_run: bool = False,
                                   extra_summary: Optional[Dict[str, Any]] = None) -> None:
        """Persist Blue progress so the UI can poll it across workers."""
        from app.models import db
        from app.models.settings import DataSyncLog

        try:
            sync_log = DataSyncLog.query.get(sync_log_id)
            if not sync_log:
                return

            existing_summary = sync_log.summary or {}
            summary = self._build_progress_summary(dry_run=dry_run)
            for key in (
                'cancel_requested',
                'cancel_requested_at',
                'cancel_requested_by',
                'cancel_requested_by_id',
                'cancel_reason',
                'parent_sync_log_id',
            ):
                if existing_summary.get(key) is not None and summary.get(key) is None:
                    summary[key] = existing_summary.get(key)
            if extra_summary:
                summary.update(extra_summary)

            sync_log.summary = summary
            sync_log.records_processed = max(
                sync_log.records_processed or 0,
                summary.get('records_processed', 0),
            )
            if self.errors:
                sync_log.errors = self.errors[:50]
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                'Failed to persist Blue sync progress for log %s',
                sync_log_id,
            )

    def _raise_if_cancelled(self, sync_log_id: Optional[int] = None,
                            parent_sync_log_id: Optional[int] = None) -> None:
        """Stop when either the Blue log or parent full-sync log was cancelled."""
        if sync_log_id and is_sync_cancellation_requested(sync_log_id):
            raise SyncCancelledError('Blue sync cancelled by user.')
        if parent_sync_log_id and is_sync_cancellation_requested(parent_sync_log_id):
            raise SyncCancelledError('Full sync cancelled by user.')

    def _sleep_with_cancel_checks(self, seconds: int, sync_log_id: Optional[int] = None,
                                  parent_sync_log_id: Optional[int] = None) -> None:
        """Sleep in short intervals so cancel requests take effect promptly."""
        remaining = max(int(seconds), 0)
        while remaining > 0:
            self._raise_if_cancelled(sync_log_id, parent_sync_log_id)
            time.sleep(1)
            remaining -= 1

    def push_all(self, datasources: List[str] = None, dry_run: bool = False,
                 triggered_by=None, trigger_type: str = 'manual',
                 parent_sync_log_id: Optional[int] = None) -> Dict[str, Any]:
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
        if datasources:
            to_push = [self._resolve_datasource(key) for key in datasources]
            # Sort by import_order to maintain dependency ordering
            to_push.sort(key=lambda ds: ds.import_order if hasattr(ds, 'import_order') else 99)
        else:
            to_push = self._get_active_datasources()

        # Enforce Requirement D: Users.csv (Data144 / legacy 'users') is ALWAYS last.
        # Even if DB import_order or selection puts it earlier, move it to the end.
        # This prevents earlier datasources (stuck or not) from blocking it forever
        # in a single run (errors in the loop are already caught and we proceed).
        users_ds = None
        others = []
        for ds in to_push:
            lkey = getattr(ds, 'legacy_key', None)
            did = getattr(ds, 'datasource_id', None)
            if lkey == 'users' or did == 'Data144':
                users_ds = ds
            else:
                others.append(ds)
        # Keep relative order of others (already sorted or from ordered query)
        to_push = others + ([users_ds] if users_ds else [])
        self._parent_sync_log_id = parent_sync_log_id

        # Initialize progress (must happen before any wait so the frontend
        # can show a progress bar immediately).
        with _blue_sync_lock:
            started_at = datetime.utcnow().isoformat()
            _blue_sync_progress = {
                'running': True,
                'current_datasource': '',
                'current_step': 'Initializing...',
                'datasource_number': 0,
                'total_datasources': len(to_push),
                'batch_number': 0,
                'total_batches': 0,
                'records_processed': 0,
                'started_at': started_at,
                'updated_at': started_at,
                'error': None,
                'results': {}
            }

        # Create sync log BEFORE any long-running wait so the polling
        # endpoint returns running=True and the frontend shows progress.
        sync_log = DataSyncLog(
            sync_type=DataSyncLog.TYPE_DATASOURCE_TO_BLUE,
            status=DataSyncLog.STATUS_RUNNING,
            triggered_by_id=triggered_by.id if triggered_by else None,
            trigger_type=trigger_type
        )
        db.session.add(sync_log)
        db.session.commit()
        self._persist_sync_log_progress(
            sync_log.id,
            dry_run=dry_run,
            extra_summary={'parent_sync_log_id': parent_sync_log_id} if parent_sync_log_id else None,
        )

        self.errors = []
        self.results = {}
        self.stats = {
            'total_records': 0,
            'datasources_success': 0,
            'datasources_failed': 0
        }

        # When pushing only Users for testing, wait 10 min so Blue can
        # finish processing any previous large Users import still running.
        is_users_only = (
            len(to_push) == 1
            and hasattr(to_push[0], 'legacy_key')
            and to_push[0].legacy_key == 'users'
        )
        if is_users_only and not dry_run:
            _update_blue_progress(
                datasource='users',
                step='Waiting 10 min for previous Users import to clear...',
            )
            self._persist_sync_log_progress(
                sync_log.id,
                dry_run=dry_run,
                extra_summary={'parent_sync_log_id': parent_sync_log_id} if parent_sync_log_id else None,
            )
            # Sleep in 30-second increments so the frontend elapsed timer
            # stays current and cancel requests are honoured.
            for _ in range(20):
                self._raise_if_cancelled(sync_log.id, parent_sync_log_id)
                time.sleep(30)

        try:
            for idx, ds_obj in enumerate(to_push, 1):
                # Resolve key for progress/results dict
                ds_key = (ds_obj.legacy_key if hasattr(ds_obj, 'legacy_key') and ds_obj.legacy_key
                          else ds_obj.datasource_id if hasattr(ds_obj, 'datasource_id')
                          else ds_obj.display_name if hasattr(ds_obj, 'display_name')
                          else str(ds_obj))
                self._raise_if_cancelled(sync_log.id, parent_sync_log_id)
                _update_blue_progress(
                    datasource=ds_key,
                    step='Starting...',
                    ds_num=idx
                )
                self._persist_sync_log_progress(
                    sync_log.id,
                    dry_run=dry_run,
                    extra_summary={'parent_sync_log_id': parent_sync_log_id} if parent_sync_log_id else None,
                )

                try:
                    success = self._import_datasource(
                        ds_key,
                        dry_run=dry_run,
                        sync_log_id=sync_log.id,
                        parent_sync_log_id=parent_sync_log_id,
                    )
                    self.results[ds_key] = 'SUCCESS' if success else 'FAILED'
                    if success:
                        self.stats['datasources_success'] += 1
                    else:
                        self.stats['datasources_failed'] += 1

                    _update_blue_progress(
                        datasource=ds_key,
                        result='SUCCESS' if success else 'FAILED'
                    )
                    self._persist_sync_log_progress(
                        sync_log.id,
                        dry_run=dry_run,
                        extra_summary={'parent_sync_log_id': parent_sync_log_id} if parent_sync_log_id else None,
                    )

                    # Add delay between datasources to prevent timeouts
                    # Skip delay after the last datasource
                    if idx < len(to_push) and not dry_run:
                        wait = _get_wait_seconds(ds_obj)
                        # Enforce >= 3 min gap between non-Users pushes (Requirement D)
                        ds_key_for_gap = (getattr(ds_obj, 'legacy_key', None) or
                                          getattr(ds_obj, 'datasource_id', None) or '')
                        if ds_key_for_gap != 'users' and ds_key_for_gap != 'Data144':
                            wait = max(wait, MIN_NON_USERS_GAP_SECONDS)
                        _update_blue_progress(
                            step=f'Waiting {wait}s before next datasource...'
                        )
                        self._persist_sync_log_progress(
                            sync_log.id,
                            dry_run=dry_run,
                            extra_summary={'parent_sync_log_id': parent_sync_log_id} if parent_sync_log_id else None,
                        )
                        self._sleep_with_cancel_checks(
                            wait,
                            sync_log_id=sync_log.id,
                            parent_sync_log_id=parent_sync_log_id,
                        )

                except SyncCancelledError:
                    raise
                except Exception as e:
                    self.errors.append(f"{ds_key}: {str(e)}")
                    self.results[ds_key] = f'ERROR: {str(e)}'
                    self.stats['datasources_failed'] += 1
                    _update_blue_progress(
                        datasource=ds_key,
                        error=str(e),
                        result='ERROR'
                    )
                    self._persist_sync_log_progress(
                        sync_log.id,
                        dry_run=dry_run,
                        extra_summary={'parent_sync_log_id': parent_sync_log_id} if parent_sync_log_id else None,
                    )

                    # Also add delay after errors to give server time to recover
                    if idx < len(to_push):
                        wait = _get_wait_seconds(ds_obj)
                        # Enforce >= 3 min gap between non-Users pushes (Requirement D)
                        ds_key_for_gap = (getattr(ds_obj, 'legacy_key', None) or
                                          getattr(ds_obj, 'datasource_id', None) or '')
                        if ds_key_for_gap != 'users' and ds_key_for_gap != 'Data144':
                            wait = max(wait, MIN_NON_USERS_GAP_SECONDS)
                        self._sleep_with_cancel_checks(
                            wait,
                            sync_log_id=sync_log.id,
                            parent_sync_log_id=parent_sync_log_id,
                        )

            # Update sync log
            all_success = self.stats['datasources_failed'] == 0
            sync_log.status = DataSyncLog.STATUS_COMPLETED if all_success else DataSyncLog.STATUS_FAILED
            sync_log.completed_at = datetime.utcnow()
            sync_log.blue_results = self.results
            sync_log.records_processed = self.stats['total_records']
            sync_log.errors = self.errors[:50]
            summary = self._build_progress_summary(dry_run=dry_run)
            summary.update({
                'pipeline_phase': 'complete' if all_success else 'failed',
                'pipeline_message': (
                    'Datasource to Blue sync completed successfully.'
                    if all_success else
                    'Datasource to Blue sync finished with errors.'
                ),
                'datasources_success': self.stats['datasources_success'],
                'datasources_failed': self.stats['datasources_failed'],
                'results': self.results,
            })
            sync_log.summary = summary
            db.session.commit()

            # Mark as complete
            with _blue_sync_lock:
                _blue_sync_progress['running'] = False
                _blue_sync_progress['current_step'] = 'Complete'
                _blue_sync_progress['updated_at'] = datetime.utcnow().isoformat()

            return {
                'success': all_success,
                'results': self.results,
                'stats': self.stats,
                'errors': self.errors[:10],
                'sync_log_id': sync_log.id
            }

        except SyncCancelledError as e:
            mark_sync_cancelled(sync_log, str(e))
            sync_log.blue_results = self.results
            sync_log.records_processed = self.stats['total_records']
            sync_log.errors = (self.errors + [str(e)])[:50]
            summary = self._build_progress_summary(dry_run=dry_run)
            summary.update({
                'cancel_requested': True,
                'parent_sync_log_id': parent_sync_log_id,
                'pipeline_phase': 'cancelled',
                'pipeline_message': str(e),
                'results': self.results,
            })
            sync_log.summary = summary
            db.session.commit()

            with _blue_sync_lock:
                _blue_sync_progress['running'] = False
                _blue_sync_progress['current_step'] = 'Cancelled'
                _blue_sync_progress['error'] = None
                _blue_sync_progress['updated_at'] = datetime.utcnow().isoformat()

            return {
                'success': False,
                'cancelled': True,
                'message': str(e),
                'results': self.results,
                'stats': self.stats,
                'errors': self.errors[:10],
                'sync_log_id': sync_log.id,
            }

        except Exception as e:
            sync_log.fail(str(e))
            summary = self._build_progress_summary(dry_run=dry_run)
            summary.update({
                'parent_sync_log_id': parent_sync_log_id,
                'pipeline_phase': 'failed',
                'pipeline_message': f'Datasource to Blue sync failed: {e}',
            })
            sync_log.summary = summary
            db.session.commit()

            with _blue_sync_lock:
                _blue_sync_progress['running'] = False
                _blue_sync_progress['error'] = str(e)
                _blue_sync_progress['updated_at'] = datetime.utcnow().isoformat()

            raise

    def _resolve_datasource(self, datasource_key: str):
        """
        Resolve a datasource by legacy key, datasource_id, or display_name.
        Returns a BlueSyncDatasource object if found in DB, falls back to
        the hardcoded DATASOURCES dict for backward compatibility.
        """
        from app.models.settings import BlueSyncDatasource
        # Try DB lookup first (by legacy_key, datasource_id, or display_name)
        db_ds = BlueSyncDatasource.query.filter(
            (BlueSyncDatasource.legacy_key == datasource_key)
            | (BlueSyncDatasource.datasource_id == datasource_key)
            | (BlueSyncDatasource.display_name == datasource_key)
        ).first()
        if db_ds:
            # Convert to a dict-like object for backward-compatible access
            return db_ds

        # Fall back to hardcoded dict
        if datasource_key in DATASOURCES:
            # Wrap in a simple namespace for attribute access
            ds = DATASOURCES[datasource_key]
            return type('_DS', (), {
                'datasource_id': ds['id'],
                'display_name': ds.get('name', datasource_key),
                'block_name': ds.get('block_name'),
                'csv_file': ds['csv_file'],
                'columns': ds.get('columns', []),
                'required_columns': ds.get('required', []),
                'column_renames': ds.get('column_renames', {}),
                'batch_size': ds.get('batch_size', None),
                'source_type': 'hana_csv',
                'import_order': IMPORT_ORDER.index(datasource_key) if datasource_key in IMPORT_ORDER else 99,
                'is_active': True,
                'is_system': True,
                'wait_after_seconds': IMPORT_DELAY_SECONDS,
                'legacy_key': datasource_key,
            })()
        raise KeyError(f"Unknown datasource: {datasource_key}")

    def _get_active_datasources(self) -> list:
        """Load active datasources from DB ordered by import_order."""
        from app.models.settings import BlueSyncDatasource
        db_ds_list = BlueSyncDatasource.query \
            .filter_by(is_active=True) \
            .order_by(BlueSyncDatasource.import_order) \
            .all()
        if db_ds_list:
            return db_ds_list
        # Fall back to hardcoded order
        return [self._resolve_datasource(k) for k in IMPORT_ORDER]

    def _complete_file_event(self, file_event, status: str, row_count: int = 0,
                             error_message: str = None, elapsed_seconds: float = None):
        """Update a DataFileSyncEvent row on completion/failure."""
        if file_event is None:
            return
        from app.models import db as _db
        try:
            file_event.status = status
            file_event.completed_at = datetime.utcnow()
            file_event.row_count = row_count
            if error_message:
                file_event.error_message = error_message[:500]
            if elapsed_seconds is not None:
                file_event.elapsed_seconds = round(elapsed_seconds, 2)
            _db.session.commit()
        except Exception:
            _db.session.rollback()

    def _import_datasource(self, datasource_key: str, dry_run: bool = False,
                           sync_log_id: Optional[int] = None,
                           parent_sync_log_id: Optional[int] = None) -> bool:
        """Import a single datasource to Blue."""
        ds = self._resolve_datasource(datasource_key)
        ds_id = ds.datasource_id if hasattr(ds, 'datasource_id') else ds.get('id')
        ds_name = ds.display_name if hasattr(ds, 'display_name') else ds.get('name')
        ds_block = ds.block_name if hasattr(ds, 'block_name') else ds.get('block_name')
        ds_csv = ds.csv_file if hasattr(ds, 'csv_file') else ds.get('csv_file')
        ds_columns = ds.columns if hasattr(ds, 'columns') else ds.get('columns', [])
        ds_required = ds.required_columns if hasattr(ds, 'required_columns') else ds.get('required', [])
        ds_column_renames = getattr(ds, 'column_renames', None) or {}
        ds_batch_size = getattr(ds, 'batch_size', None) or BATCH_SIZE

        # Create per-file event record
        file_start = time.monotonic()
        file_event = None
        row_count = 0
        error_msg = None
        result = False

        if sync_log_id:
            from app.models import db as _db
            from app.models.settings import DataFileSyncEvent
            try:
                file_event = DataFileSyncEvent(
                    sync_log_id=sync_log_id,
                    direction='blue_push',
                    file_name=ds_csv,
                    datasource_id=ds_id,
                    status='running',
                )
                _db.session.add(file_event)
                _db.session.commit()
            except Exception:
                _db.session.rollback()


        self._raise_if_cancelled(sync_log_id, parent_sync_log_id)
        _update_blue_progress(step='Discovering schema...')
        if sync_log_id:
            self._persist_sync_log_progress(sync_log_id, dry_run=dry_run)

        # Discover block name if not configured
        block_name = ds_block
        columns = list(ds_columns) if ds_columns else []

        if not block_name:
            block_name = self._discover_block_name(ds_id)
            if not block_name:
                error_msg = "Could not discover block name"
                self.errors.append(f"{datasource_key}: {error_msg}")
                self._complete_file_event(file_event, 'failed', 0, error_msg,
                                          time.monotonic() - file_start)
                return False

        # Load CSV
        self._raise_if_cancelled(sync_log_id, parent_sync_log_id)
        _update_blue_progress(step='Loading CSV...')
        if sync_log_id:
            self._persist_sync_log_progress(sync_log_id, dry_run=dry_run)
        csv_columns, rows = self._load_csv(ds_csv, columns,
                                           column_renames=ds_column_renames)

        if not rows:
            error_msg = "No data to import"
            self.errors.append(f"{datasource_key}: {error_msg}")
            self._complete_file_event(file_event, 'failed', 0, error_msg,
                                      time.monotonic() - file_start)
            return False

        row_count = len(rows)
        self.stats['total_records'] += row_count
        columns = csv_columns

        # Dry run - stop here
        if dry_run:
            _update_blue_progress(step=f'Dry run: {row_count} rows validated')
            if sync_log_id:
                self._persist_sync_log_progress(sync_log_id, dry_run=dry_run)
            self._complete_file_event(file_event, 'success', row_count,
                                      elapsed_seconds=time.monotonic() - file_start)
            return True

        # Clear any stale import from a previous failed attempt
        if not dry_run:
            cancelled = self._cancel_stale_import()
            if cancelled:
                time.sleep(2)  # Brief pause for Blue to release the lock

        # Register import
        self._raise_if_cancelled(sync_log_id, parent_sync_log_id)
        _update_blue_progress(step='Registering import...')
        if sync_log_id:
            self._persist_sync_log_progress(sync_log_id, dry_run=dry_run)
        payload = soap_register_import(self.api_key, ds_id)
        response = call_soap(self.ws_url, payload, "RegisterImport")

        success, message, _ = check_response(response, "RegisterImport")
        if not success and message:
            error_msg = f"RegisterImport failed - {message}"
            self.errors.append(f"{datasource_key}: {error_msg}")
            self._complete_file_event(file_event, 'failed', 0, error_msg,
                                      time.monotonic() - file_start)
            return False

        transaction_id = extract_value(response.text, "TransactionID")
        if not transaction_id:
            error_msg = "Could not get transaction ID"
            self.errors.append(f"{datasource_key}: {error_msg}")
            self.errors.append(f"  RegisterImport raw: {response.text[:400]}")
            self._complete_file_event(file_event, 'failed', 0, error_msg,
                                      time.monotonic() - file_start)
            return False

        # Blue returns "0" when RegisterImport fails (e.g. datasource locked
        # by another process).  Treat it as a registration failure.
        if transaction_id.strip() == '0':
            error_msg = "RegisterImport failed (transaction ID 0)"
            self.errors.append(
                f"{datasource_key}: {error_msg}"
            )
            self.errors.append(f"  RegisterImport raw: {response.text[:400]}")
            self._complete_file_event(file_event, 'failed', 0, error_msg,
                                      time.monotonic() - file_start)
            return False

        # Push data in batches
        total_batches = (len(rows) + ds_batch_size - 1) // ds_batch_size
        _update_blue_progress(step='Pushing data...', total_batches=total_batches)
        if sync_log_id:
            self._persist_sync_log_progress(sync_log_id, dry_run=dry_run)

        try:
            for i in range(0, len(rows), ds_batch_size):
                self._raise_if_cancelled(sync_log_id, parent_sync_log_id)
                batch = rows[i:i + ds_batch_size]
                batch_num = (i // ds_batch_size) + 1

                _update_blue_progress(
                    step=f'Pushing batch {batch_num}/{total_batches}...',
                    batch=batch_num,
                    records=len(batch)
                )
                if sync_log_id:
                    self._persist_sync_log_progress(sync_log_id, dry_run=dry_run)

                payload = soap_push_data(self.api_key, transaction_id, block_name, columns, batch)
                response = call_soap(self.ws_url, payload, "PushObjectDataV2")

                success, message, has_warning = check_response(response, "PushObjectDataV2")
                if not success:
                    # Include raw SOAP response (first 500 chars) for debugging
                    raw_snippet = response.text[:500] if response.text else '(empty)'
                    error_msg = f"Batch {batch_num} failed - {message}"
                    self.errors.append(
                        f"{datasource_key}: {error_msg}\n"
                        f"  TransactionID used: {transaction_id}\n"
                        f"  Raw response: {raw_snippet}"
                    )
                    self._cancel_import(transaction_id)
                    self._complete_file_event(file_event, 'failed', 0, error_msg,
                                              time.monotonic() - file_start)
                    return False

            # Prepare for finalization
            self._raise_if_cancelled(sync_log_id, parent_sync_log_id)
            _update_blue_progress(step='Validating...')
            if sync_log_id:
                self._persist_sync_log_progress(sync_log_id, dry_run=dry_run)
            payload = soap_prepare_finalize(self.api_key, transaction_id)
            response = call_soap(self.ws_url, payload, "PrepareDataToFinzalizeImportV2")

            success, message, _ = check_response(response, "PrepareDataToFinzalizeImportV2")
            if not success:
                error_msg = f"Validation failed - {message}"
                self.errors.append(f"{datasource_key}: {error_msg}")
                self._cancel_import(transaction_id)
                self._complete_file_event(file_event, 'failed', 0, error_msg,
                                          time.monotonic() - file_start)
                return False

            # Finalize
            self._raise_if_cancelled(sync_log_id, parent_sync_log_id)
            _update_blue_progress(step='Finalizing...')
            if sync_log_id:
                self._persist_sync_log_progress(sync_log_id, dry_run=dry_run)
            payload = soap_finalize_import(self.api_key, transaction_id)
            response = call_soap(self.ws_url, payload, "FinalizeImport")

            success, message, _ = check_response(response, "FinalizeImport")
            if not success:
                error_msg = f"Finalization failed - {message}"
                self.errors.append(f"{datasource_key}: {error_msg}")
                self._complete_file_event(file_event, 'failed', 0, error_msg,
                                          time.monotonic() - file_start)
                return False

            _update_blue_progress(step=f'Complete: {row_count} rows imported')
            if sync_log_id:
                self._persist_sync_log_progress(sync_log_id, dry_run=dry_run)
            self._complete_file_event(file_event, 'success', row_count,
                                      elapsed_seconds=time.monotonic() - file_start)
            return True

        except SyncCancelledError:
            self._complete_file_event(file_event, 'failed', 0, 'Sync cancelled',
                                      time.monotonic() - file_start)
            if transaction_id:
                self._cancel_import(transaction_id)
            raise
        except KeyboardInterrupt:
            self._complete_file_event(file_event, 'failed', 0, 'Keyboard interrupt',
                                      time.monotonic() - file_start)
            if transaction_id:
                self._cancel_import(transaction_id)
            raise
        except Exception as exc:
            # Always cancel on unexpected errors so Blue doesn't hold
            # an orphaned transaction that blocks subsequent imports.
            self._complete_file_event(file_event, 'failed', 0, str(exc),
                                      time.monotonic() - file_start)
            if transaction_id:
                self._cancel_import(transaction_id)
            raise

    def _discover_block_name(self, datasource_id: str) -> Optional[str]:
        """Discover block name for a datasource."""
        payload = soap_get_datablock_info(self.api_key, datasource_id)
        response = call_soap(self.ws_url, payload, "GetDataBlockInformation")

        if response.status_code != 200:
            return None

        return extract_block_name(response.text)

    def _load_csv(self, csv_file: str, expected_columns: List[str],
                  column_renames: Optional[Dict[str, str]] = None) -> Tuple[List[str], List[List[str]]]:
        """Load CSV file and prepare data for import.

        Args:
            csv_file: CSV filename (relative to datasources_path).
            expected_columns: Blue column names in desired output order.
            column_renames: Optional mapping from CSV column names to Blue
                column names (e.g. {'FIRSTNAME': 'FIRSTNAME_1'}).

        Returns:
            (output_column_names, list_of_row_value_lists)
        """
        filepath = os.path.join(self.datasources_path, csv_file)

        if not os.path.exists(filepath):
            return [], []

        column_renames = column_renames or {}
        rows = []

        # Build a reverse map: Blue column name → CSV column name
        reverse_map: Dict[str, str] = {}
        for csv_col, blue_col in column_renames.items():
            reverse_map[blue_col] = csv_col

        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            csv_columns = reader.fieldnames or []

            # Determine which expected Blue columns we can fill from the CSV,
            # accounting for column renames.
            use_columns: List[str] = []
            for bc in expected_columns:
                # The CSV column we need to read for this Blue column
                csv_source = reverse_map.get(bc, bc)
                if csv_source in csv_columns:
                    use_columns.append(bc)

            for row in reader:
                row_data = []
                for bc in use_columns:
                    csv_source = reverse_map.get(bc, bc)
                    val = row.get(csv_source, '')
                    row_data.append(val if val else '')
                rows.append(row_data)

        return use_columns, rows

    def _cancel_import(self, transaction_id: str):
        """Cancel an in-progress import."""
        payload = soap_cancel_import(self.api_key, transaction_id)
        call_soap(self.ws_url, payload, "CancelImport")

    def _cancel_stale_import(self) -> bool:
        """Check for and cancel any in-progress import blocking this datasource.

        Returns True if a stale import was found and cancelled.
        Does NOT log to self.errors when no stale import exists.
        """
        payload = soap_get_current_process(self.api_key)
        try:
            response = call_soap(self.ws_url, payload, "GetCurrentImportingDataSourceProcess", timeout=30)
        except Exception:
            return False  # Can't reach Blue — nothing we can do

        if response.status_code != 200:
            self.errors.append(
                f"GetCurrentImportingDataSourceProcess HTTP "
                f"{response.status_code}: {response.text[:200]}"
            )
            return False

        # Extract the transaction ID of the running import (if any)
        stale_tid = extract_value(response.text, "TransactionID")
        progress = extract_value(response.text, "ProgressStatus")
        if not stale_tid or stale_tid.strip() == '0':
            return False  # No active import — nothing to do, no error to log

        # A stale import exists — cancel it and log the event
        self.errors.append(
            f"GetCurrentImportingDataSourceProcess response: "
            f"TransactionID={stale_tid}, ProgressStatus={progress}, "
            f"raw={response.text[:300]}"
        )
        self._cancel_import(stale_tid)
        return True


def get_blue_sync_service(datasources_path: str = './datasources') -> BlueSyncService:
    """Factory function to get BlueSyncService instance."""
    return BlueSyncService(datasources_path)