#!/usr/bin/env python3
"""
DRA -> Explorance Blue Data151 sync script.

Generates the DRA (Data Relationship Assignment) CSV directly from the
live PostgreSQL database, then pushes it to Explorance Blue Data151.

Runs standalone (no web request needed) — safe to call from cron or
daily_sync.sh without touching the running Flask app's connection pool.

Usage:
    python scripts/dra_sync.py [--dry-run]

    --dry-run   Generate the CSV and print a preview, but do NOT push to Blue.

Exit codes:
    0  success
    1  failed (error printed to stderr)

Output: timestamped log lines on stdout.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / '.env')
except ImportError:
    pass

import requests

# ---------------------------------------------------------------------------
# Config (pulled from DB at runtime, not hardcoded)
# ---------------------------------------------------------------------------

BLUE_WS_URL_DEFAULT = "https://my-uky-ws-bc.bluera.com/BlueWebService.svc/file"
DATASOURCE_ID  = "Data151"
BLOCK_NAME     = "ReportViewersToUsers"
BLUE_COLUMNS   = ['source_1', 'target_1', 'targetType']
BATCH_SIZE     = 1000


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# DRA CSV generation (mirrors export_dra() in app/routes/tracking.py)
# ---------------------------------------------------------------------------

def _clean_dra_source(value) -> str:
    return str(value).strip() if value is not None else ''


def _resolve_college_code_from_courses(college_value) -> str | None:
    """Resolve a stored college value to CLASS_COLLEGE_SHORT from synced Courses.csv data."""
    from app.models import db
    from app.models.course import College

    college_value = _clean_dra_source(college_value)
    if not college_value:
        return None

    college = College.query.get(college_value)
    if not college:
        college = College.query.filter(
            db.func.lower(College.name) == college_value.lower()
        ).first()

    return college.code if college else None


def _get_college_source(admin) -> tuple[str | None, str | None]:
    college_code = _resolve_college_code_from_courses(admin.college_code)
    if college_code:
        return college_code, None

    if admin.college_code:
        return None, f"College not found in synced Courses.csv data: {admin.college_code} ({admin.linkblue})"
    return None, f"No college code for college admin {admin.linkblue}"


def _resolve_department_source(department_value, college_value=None) -> str | None:
    """Resolve a stored department value to CLASS_DEPARTMENT_ID."""
    from app.models import db
    from app.models.course import Department

    department_value = _clean_dra_source(department_value)
    if not department_value or department_value.lower() == 'all':
        return None

    department = Department.query.get(department_value)
    if department:
        return department.id

    query = Department.query.filter(
        db.func.lower(Department.name) == department_value.lower()
    )
    college_code = _resolve_college_code_from_courses(college_value)
    if college_code:
        query = query.filter(Department.college_code == college_code)

    departments = query.all()
    if len(departments) == 1:
        return departments[0].id
    return None


def _get_department_sources(admin) -> tuple[list[str], list[str]]:
    departments = admin.departments.all()
    if departments:
        return [_clean_dra_source(dept.id) for dept in departments if dept.id], []

    if admin.department_id:
        department_id = _resolve_department_source(admin.department_id, admin.college_code)
        if department_id:
            return [department_id], []
        return [], [f"Department not found or ambiguous in synced Courses.csv data: {admin.department_id} ({admin.linkblue})"]

    return [], [f"No department ID for dept admin {admin.linkblue}"]


def generate_dra_rows(app_ctx) -> tuple[list[list], list[str]]:
    """Return (rows, errors) where each row is [source, target, targetType].

    Must be called inside a Flask app context.
    """
    from app.models.admin import Admin

    rows: list[list] = []
    errors: list[str] = []

    admins = Admin.query.filter(
        Admin.is_active == True,
        Admin.role != 'super_admin',
        Admin.has_static_report_access == True,
    ).all()

    for admin in admins:
        try:
            if admin.is_course_coordinator:
                class_ids, coord_errors = _get_course_class_ids(admin)
                errors.extend(coord_errors)
                for class_id in class_ids:
                    rows.append([class_id, admin.linkblue, 'CRS1'])

            elif admin.contact_type == 'College' or admin.role == 'college_admin':
                college_source, college_error = _get_college_source(admin)
                if college_error:
                    errors.append(college_error)
                    continue
                rows.append([college_source, admin.linkblue, 'C4'])

            elif admin.contact_type == 'Department' or admin.role == 'dept_admin':
                department_sources, department_errors = _get_department_sources(admin)
                errors.extend(department_errors)
                for department_source in department_sources:
                    rows.append([department_source, admin.linkblue, 'D3'])

        except Exception as e:
            errors.append(f"Error processing {admin.linkblue}: {e}")

    return rows, errors


def _get_course_class_ids(admin) -> tuple[set, list]:
    from app.models.course import Course
    class_ids: set = set()
    errors: list[str] = []

    assignments = admin.course_assignments.all()
    if assignments:
        for assignment in assignments:
            if assignment.is_wildcard:
                courses = Course.query.filter(
                    Course.class_code.like(f"{assignment.course_prefix} %")
                ).all()
            else:
                pattern = f"{assignment.course_prefix} {assignment.course_number}"
                courses = Course.query.filter(
                    Course.class_code.like(f"{pattern}%")
                ).all()
            if courses:
                for c in courses:
                    if c.class_id:
                        class_ids.add(c.class_id)
            else:
                errors.append(f"No courses for {admin.linkblue} ({assignment.display_name})")
    elif admin.course_prefix and admin.course_prefix.upper() not in ('ALL', '*'):
        if admin.course_number:
            courses = Course.query.filter(
                Course.class_code.like(f"{admin.course_prefix} {admin.course_number}%")
            ).all()
        else:
            courses = Course.query.filter(
                Course.class_code.like(f"{admin.course_prefix} %")
            ).all()
        for c in courses:
            if c.class_id:
                class_ids.add(c.class_id)

    return class_ids, errors


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _esc(text) -> str:
    if text is None:
        return ''
    s = str(text)
    s = s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    s = s.replace('"', '&quot;').replace("'", '&apos;')
    return s


# ---------------------------------------------------------------------------
# SOAP payload builders
# ---------------------------------------------------------------------------

def soap_register_import(api_key: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:tem="http://tempuri.org/">
  <soapenv:Header>
    <tem:APIKeyHeader>{api_key}</tem:APIKeyHeader>
  </soapenv:Header>
  <soapenv:Body>
    <tem:RegisterImportRequest>
      <tem:AbortOnEmpty>true</tem:AbortOnEmpty>
      <tem:DataSourceID>{DATASOURCE_ID}</tem:DataSourceID>
      <tem:ReplaceBlueRole>false</tem:ReplaceBlueRole>
      <tem:ReplaceDataSourceAccessKey>false</tem:ReplaceDataSourceAccessKey>
      <tem:ReplaceLanguagePreferences>false</tem:ReplaceLanguagePreferences>
    </tem:RegisterImportRequest>
  </soapenv:Body>
</soapenv:Envelope>"""


def soap_push_data(api_key: str, transaction_id: str, rows: list) -> str:
    cols_xml = '\n'.join(
        f'    <arr:string>{c}</arr:string>' for c in BLUE_COLUMNS
    )
    rows_xml = ''
    for row in rows:
        vals = '\n'.join(
            f'      <blue:IDataObj><blue:IDataObjValue>{_esc(v)}</blue:IDataObjValue></blue:IDataObj>'
            for v in row
        )
        rows_xml += f'  <blue:IDataRow><blue:IDataRowValue>\n{vals}\n  </blue:IDataRowValue></blue:IDataRow>\n'

    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:tem="http://tempuri.org/"
                  xmlns:arr="http://schemas.microsoft.com/2003/10/Serialization/Arrays"
                  xmlns:blue="http://schemas.datacontract.org/2004/07/Blue.Integration">
  <soapenv:Header>
    <tem:TransactionId>{transaction_id}</tem:TransactionId>
    <tem:DataBlockName>{BLOCK_NAME}</tem:DataBlockName>
    <tem:ColumnNamesList>
{cols_xml}
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
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:tem="http://tempuri.org/">
  <soapenv:Header>
    <tem:TransactionID>{transaction_id}</tem:TransactionID>
    <tem:APIKeyHeader>{api_key}</tem:APIKeyHeader>
  </soapenv:Header>
  <soapenv:Body><tem:BasicRequest/></soapenv:Body>
</soapenv:Envelope>"""


def soap_finalize(api_key: str, transaction_id: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:tem="http://tempuri.org/">
  <soapenv:Header>
    <tem:TransactionID>{transaction_id}</tem:TransactionID>
    <tem:APIKeyHeader>{api_key}</tem:APIKeyHeader>
  </soapenv:Header>
  <soapenv:Body><tem:FinalizeImportRequest/></soapenv:Body>
</soapenv:Envelope>"""


def soap_cancel(api_key: str, transaction_id: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:tem="http://tempuri.org/">
  <soapenv:Header>
    <tem:TransactionID>{transaction_id}</tem:TransactionID>
    <tem:APIKeyHeader>{api_key}</tem:APIKeyHeader>
  </soapenv:Header>
  <soapenv:Body><tem:CancelImportRequest/></soapenv:Body>
</soapenv:Envelope>"""


# ---------------------------------------------------------------------------
# SOAP call + response parsing
# ---------------------------------------------------------------------------

def _call(ws_url: str, payload: str, action: str) -> requests.Response:
    return requests.post(
        ws_url,
        headers={
            'Content-Type': 'text/xml; charset=UTF-8',
            'SOAPAction': f'http://tempuri.org/IBlueWebService/{action}',
        },
        data=payload.encode('utf-8'),
        timeout=120,
    )


def _parse(resp: requests.Response) -> tuple[bool, str]:
    """Return (success, message)."""
    if resp.status_code != 200:
        return False, f'HTTP {resp.status_code}'
    txt = resp.text
    if 'INVALID_APIKEY' in txt:
        return False, 'Invalid API key'
    # IsSuccess or Result
    for tag in ('IsSuccess', 'Result'):
        m = re.search(rf'<[^>]*{tag}[^>]*>(true|false)<', txt, re.IGNORECASE)
        if m:
            ok = m.group(1).lower() == 'true'
            msg_m = re.search(r'<[^>]*Message[^>]*>([^<]*)<', txt)
            msg = msg_m.group(1) if msg_m else ''
            return ok, msg.replace('&#xD;', ' ').replace('&#xA;', ' ')
    if 'VALID_APIKEY' in txt:
        return True, ''
    return False, f'Unrecognised response: {txt[:200]}'


def _extract(txt: str, tag: str) -> str | None:
    m = re.search(rf'<[^>]*{tag}[^>]*>([^<]+)<', txt, re.IGNORECASE)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Blue push workflow
# ---------------------------------------------------------------------------

def push_to_blue(api_key: str, ws_url: str, rows: list) -> bool:
    """Run the full RegisterImport -> Push -> Prepare -> Finalize flow.
    Returns True on success.
    """
    # 1. Register
    log(f"  Registering import for {DATASOURCE_ID}...")
    resp = _call(ws_url, soap_register_import(api_key), 'RegisterImport')
    ok, msg = _parse(resp)
    transaction_id = _extract(resp.text, 'TransactionID')
    if not transaction_id:
        log(f"  ERROR: Could not get transaction ID. Response: {resp.text[:300]}")
        return False
    log(f"  Transaction ID: {transaction_id}")

    try:
        # 2. Push in batches
        total = len(rows)
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        log(f"  Pushing {total:,} rows in {total_batches} batch(es)...")
        for i in range(0, total, BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            batch_num = i // BATCH_SIZE + 1
            resp = _call(ws_url, soap_push_data(api_key, transaction_id, batch), 'PushObjectDataV2')
            ok, msg = _parse(resp)
            if not ok:
                log(f"  ERROR on batch {batch_num}: {msg}")
                raise RuntimeError(f'Push batch {batch_num} failed: {msg}')
            log(f"  Batch {batch_num}/{total_batches} OK ({len(batch)} rows)")

        # 3. Prepare finalize
        log("  Preparing finalize...")
        resp = _call(ws_url, soap_prepare_finalize(api_key, transaction_id),
                     'PrepareDataToFinzalizeImportV2')
        ok, msg = _parse(resp)
        if not ok:
            raise RuntimeError(f'PrepareFinalize failed: {msg}')
        log("  Prepare OK")

        # 4. Finalize
        log("  Finalizing...")
        resp = _call(ws_url, soap_finalize(api_key, transaction_id), 'FinalizeImport')
        ok, msg = _parse(resp)
        if not ok:
            raise RuntimeError(f'Finalize failed: {msg}')
        log("  Finalized successfully.")
        return True

    except Exception as e:
        log(f"  ERROR: {e}  — cancelling import.")
        try:
            _call(ws_url, soap_cancel(api_key, transaction_id), 'CancelImport')
            log("  Import cancelled.")
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    dry_run = '--dry-run' in sys.argv

    log("=" * 60)
    log("TCE Admin DRA Sync" + (" (DRY RUN)" if dry_run else ""))
    log("=" * 60)

    from app import create_app
    from app.models.settings import SystemSetting

    app = create_app(os.environ.get('FLASK_ENV', 'production'))

    with app.app_context():
        # Load API key + WS URL from the database (set via Settings UI)
        api_key = SystemSetting.get(SystemSetting.BLUE_API_KEY)
        ws_url  = SystemSetting.get(SystemSetting.BLUE_WS_URL) or BLUE_WS_URL_DEFAULT

        if not api_key:
            log("ERROR: Blue API key not configured. "
                "Set it in Settings → Blue API Configuration.")
            return 1

        log(f"Target   : {DATASOURCE_ID} / {BLOCK_NAME}")
        log(f"Endpoint : {ws_url}")
        log(f"API key  : {api_key[:8]}...{api_key[-4:]}")

        # Generate DRA rows from live DB
        log("Generating DRA data from database...")
        rows, errors = generate_dra_rows(app)
        log(f"  {len(rows):,} rows generated, {len(errors)} warning(s)")
        for e in errors[:10]:
            log(f"  WARN: {e}")
        if len(errors) > 10:
            log(f"  ... and {len(errors) - 10} more warnings")

        if not rows:
            log("ERROR: No rows generated — nothing to push.")
            return 1

        # Dry-run: show sample and exit
        if dry_run:
            log("\nDRY RUN — sample rows (first 10):")
            log("source, target, targetType")
            for row in rows[:10]:
                log(f"  {','.join(str(v) for v in row)}")
            log(f"\nTotal rows that would be pushed: {len(rows):,}")
            log("Dry run complete — no data was sent to Blue.")
            return 0

        # Push to Blue
        log("Pushing to Explorance Blue...")
        success = push_to_blue(api_key, ws_url, rows)

        if success:
            log("=" * 60)
            log(f"DRA sync complete. {len(rows):,} rows pushed to {DATASOURCE_ID}.")
            log("=" * 60)
            return 0
        else:
            log("ERROR: DRA push failed.")
            return 1


if __name__ == '__main__':
    sys.exit(main())
