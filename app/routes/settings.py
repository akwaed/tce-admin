"""
Settings Routes for Super Admins
Manage system settings including Explorance Blue API configuration
Unified sync log management with manual sync triggers
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app.models import db
from app.models.settings import SystemSetting, DataSyncLog
from app.models.sync_history import SyncRun, ChangeLog
from app.services.blue_sync import get_blue_sync_service, get_blue_sync_progress
from app.services.course_sync import CourseSyncService, get_sync_progress
from functools import wraps
from datetime import datetime
import json
import tempfile
import threading
import time
import os
import subprocess
import sys
from app.services.sync_control import (
    SyncCancelledError,
    mark_sync_cancelled,
    raise_if_sync_cancelled,
    register_sync_process,
    request_sync_cancellation,
    terminate_sync_process,
    unregister_sync_process,
)

settings_bp = Blueprint('settings', __name__)
PROCESS_STARTED_AT = datetime.utcnow()


def super_admin_required(f):
    """Decorator for super admin only routes."""
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_super_admin():
            flash('You do not have permission to access this page.', 'danger')
            return redirect(url_for('main.dashboard'))
        return f(*args, **kwargs)
    return decorated_function


def _get_project_root():
    """Return the repository root for stable script execution paths."""
    return os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _mark_stale_running_logs():
    """
    Mark orphaned running logs as failed after an app restart.

    Sync work runs in background threads inside this process. If the web app is
    restarted, those threads disappear but their ``data_sync_logs`` rows remain
    in ``running`` state. Any running log started before this process booted is
    stale and should not keep the UI stuck in a fake running state.
    """
    stale_logs = DataSyncLog.query.filter(
        DataSyncLog.status == DataSyncLog.STATUS_RUNNING,
        DataSyncLog.started_at < PROCESS_STARTED_AT
    ).all()

    if not stale_logs:
        return

    for log in stale_logs:
        summary = log.summary
        summary['pipeline_phase'] = 'failed'
        summary['pipeline_message'] = 'Sync interrupted by application restart.'
        log.summary = summary
        log.status = DataSyncLog.STATUS_FAILED
        if not log.completed_at:
            log.completed_at = PROCESS_STARTED_AT
        errors = log.errors
        if 'Sync interrupted by application restart.' not in errors:
            errors.append('Sync interrupted by application restart.')
        log.errors = errors[:50]

    db.session.commit()


def _get_latest_running_sync_log():
    """Return the newest non-stale running sync log, if any."""
    _mark_stale_running_logs()
    return DataSyncLog.query.filter(
        DataSyncLog.status == DataSyncLog.STATUS_RUNNING
    ).order_by(DataSyncLog.started_at.desc()).first()


def _format_elapsed(seconds):
    """Human-readable duration for live sync status."""
    if seconds is None:
        return None
    total_seconds = max(int(seconds), 0)
    if total_seconds < 60:
        return f'{total_seconds}s'
    minutes, rem_seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f'{minutes}m {rem_seconds:02d}s'
    hours, rem_minutes = divmod(minutes, 60)
    return f'{hours}h {rem_minutes:02d}m'


def _build_sync_status_payload(running_log):
    """Serialize a running sync log for the polling UI."""
    summary = running_log.summary or {}
    elapsed_seconds = None
    if running_log.started_at:
        elapsed_seconds = (datetime.utcnow() - running_log.started_at).total_seconds()

    detail_message = ''
    datasource_name = summary.get('current_datasource_name')
    datasource_number = summary.get('datasource_number')
    total_datasources = summary.get('total_datasources')
    batch_number = summary.get('batch_number')
    total_batches = summary.get('total_batches')

    if datasource_name:
        detail_message = datasource_name
        if datasource_number and total_datasources:
            detail_message = f'Datasource {datasource_number}/{total_datasources}: {datasource_name}'
        if batch_number and total_batches:
            detail_message = f'{detail_message} | Batch {batch_number}/{total_batches}'
    elif summary.get('pipeline_phase') == 'database_sync':
        detail_message = 'Database sync in progress...'
    elif summary.get('pipeline_phase') == 'hana_pull':
        detail_message = 'Fetching data from SAP HANA...'

    return {
        'running': True,
        'log_id': running_log.id,
        'sync_type': running_log.sync_type,
        'sync_type_display': running_log.sync_type_display,
        'started_at': running_log.started_at.strftime('%Y-%m-%d %H:%M:%S') if running_log.started_at else None,
        'pipeline_phase': summary.get('pipeline_phase', ''),
        'pipeline_step': summary.get('pipeline_step', 0),
        'pipeline_total_steps': summary.get('pipeline_total_steps', 1),
        'pipeline_message': summary.get('pipeline_message', 'Running...'),
        'detail_message': detail_message,
        'elapsed_seconds': elapsed_seconds,
        'elapsed_display': _format_elapsed(elapsed_seconds),
        'records_processed': summary.get('records_processed', running_log.records_processed or 0),
        'current_datasource': summary.get('current_datasource'),
        'current_datasource_name': datasource_name,
        'datasource_number': datasource_number,
        'total_datasources': total_datasources,
        'batch_number': batch_number,
        'total_batches': total_batches,
        'cancel_requested': bool(summary.get('cancel_requested')),
        'error': summary.get('pipeline_message', '') if summary.get('pipeline_phase') == 'failed' else None,
    }


def _merge_cancel_summary(log, summary):
    """Preserve cancel-request metadata when overwriting a sync summary."""
    existing_summary = log.summary or {}
    merged = dict(summary or {})
    for key in (
        'cancel_requested',
        'cancel_requested_at',
        'cancel_requested_by',
        'cancel_requested_by_id',
        'cancel_reason',
        'parent_sync_log_id',
    ):
        if existing_summary.get(key) is not None and merged.get(key) is None:
            merged[key] = existing_summary.get(key)
    return merged


def _run_cancellable_subprocess(command, cwd, timeout, sync_log_id=None):
    """Run a subprocess with timeout and cooperative cancellation support."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
    )
    register_sync_process(sync_log_id, process)
    deadline = time.monotonic() + timeout

    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=1)
                return subprocess.CompletedProcess(
                    command, process.returncode, stdout, stderr,
                )
            except subprocess.TimeoutExpired:
                if sync_log_id:
                    raise_if_sync_cancelled(
                        sync_log_id,
                        message='Sync cancelled by user.',
                    )
                if time.monotonic() >= deadline:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise subprocess.TimeoutExpired(command, timeout)
    except SyncCancelledError:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise
    finally:
        unregister_sync_process(sync_log_id)


def _run_hana_sync_script(sync_log_id=None):
    """
    Run the HANA sync script with the same interpreter as the Flask app.

    This avoids mismatches between the web app environment and a manually
    activated virtualenv shell session.

    Returns a tuple ``(completed_process, json_result_or_None)``. ``json_result``
    is the parsed contents of the script's --json-output payload, which
    contains per-file row counts and diffs. It is ``None`` if the script
    failed to write the file (e.g. crashed before reaching that step).
    """
    project_root = _get_project_root()
    script_path = os.path.join(project_root, 'scripts', 'hana_sync.py')
    datasources_path = os.path.join(project_root, 'datasources')

    if not os.path.exists(script_path):
        raise FileNotFoundError(f'HANA sync script not found: {script_path}')

    # Have the script write its structured result to a temp file we read back.
    json_fd, json_path = tempfile.mkstemp(prefix='hana_sync_', suffix='.json')
    os.close(json_fd)

    try:
        completed = _run_cancellable_subprocess(
            [
                sys.executable, script_path,
                '--output', datasources_path,
                '--json-output', json_path,
            ],
            cwd=project_root,
            timeout=600,
            sync_log_id=sync_log_id,
        )

        json_result = None
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                json_result = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        return completed, json_result
    finally:
        try:
            os.remove(json_path)
        except OSError:
            pass


# ============================================================================
# SYSTEM SETTINGS
# ============================================================================

@settings_bp.route('/')
@super_admin_required
def index():
    """System settings overview page."""
    # Get current settings
    api_key_setting = SystemSetting.query.get(SystemSetting.BLUE_API_KEY)
    ws_url_setting = SystemSetting.query.get(SystemSetting.BLUE_WS_URL)

    # Validate API key if configured
    api_key_valid = None
    api_key_message = None
    if api_key_setting and api_key_setting.value:
        blue_service = get_blue_sync_service()
        api_key_valid, api_key_message = blue_service.validate_api_key()

    return render_template('settings/index.html',
                           api_key_setting=api_key_setting,
                           ws_url_setting=ws_url_setting,
                           api_key_valid=api_key_valid,
                           api_key_message=api_key_message)


@settings_bp.route('/api-key', methods=['GET', 'POST'])
@super_admin_required
def api_key():
    """
    Manage Explorance Blue API key.
    Uses a two-step confirmation process to prevent accidental changes.
    """
    current_setting = SystemSetting.query.get(SystemSetting.BLUE_API_KEY)

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'confirm':
            # Step 1: Show confirmation dialog with the new key
            new_key = request.form.get('api_key', '').strip()

            if not new_key:
                flash('API key cannot be empty.', 'danger')
                return redirect(url_for('settings.api_key'))

            # Validate key format (should look like a UUID)
            if len(new_key) < 20:
                flash('API key appears too short. Please verify you copied the full key.', 'warning')

            return render_template('settings/api_key_confirm.html',
                                   new_key=new_key,
                                   current_setting=current_setting)

        elif action == 'save':
            # Step 2: Actually save the key after confirmation
            new_key = request.form.get('api_key', '').strip()
            confirm_key = request.form.get('confirm_key', '').strip()

            if new_key != confirm_key:
                flash('Keys do not match. Please try again.', 'danger')
                return redirect(url_for('settings.api_key'))

            # Test the key before saving
            temp_setting = SystemSetting.query.get(SystemSetting.BLUE_API_KEY)
            old_value = temp_setting.value if temp_setting else None

            # Temporarily set to test
            SystemSetting.set(
                SystemSetting.BLUE_API_KEY,
                new_key,
                description='Explorance Blue API Key for SOAP web service authentication',
                admin=current_user
            )

            # Validate
            blue_service = get_blue_sync_service()
            is_valid, message = blue_service.validate_api_key()

            if is_valid:
                flash('API key saved and validated successfully.', 'success')
                return redirect(url_for('settings.index'))
            else:
                # Rollback to old value
                if old_value:
                    SystemSetting.set(SystemSetting.BLUE_API_KEY, old_value, admin=current_user)
                else:
                    # Delete the invalid key
                    setting = SystemSetting.query.get(SystemSetting.BLUE_API_KEY)
                    if setting:
                        db.session.delete(setting)
                        db.session.commit()

                flash(f'API key validation failed: {message}. The key was not saved.', 'danger')
                return redirect(url_for('settings.api_key'))

        elif action == 'delete':
            # Delete the API key
            setting = SystemSetting.query.get(SystemSetting.BLUE_API_KEY)
            if setting:
                db.session.delete(setting)
                db.session.commit()
                flash('API key has been removed.', 'success')
            return redirect(url_for('settings.index'))

    return render_template('settings/api_key.html',
                           current_setting=current_setting)


@settings_bp.route('/ws-url', methods=['GET', 'POST'])
@super_admin_required
def ws_url():
    """Manage Explorance Blue Web Service URL."""
    current_setting = SystemSetting.query.get(SystemSetting.BLUE_WS_URL)
    default_url = "https://my-uky-ws-bc.bluera.com/BlueWebService.svc/file"

    if request.method == 'POST':
        new_url = request.form.get('ws_url', '').strip()

        if not new_url:
            # Reset to default
            setting = SystemSetting.query.get(SystemSetting.BLUE_WS_URL)
            if setting:
                db.session.delete(setting)
                db.session.commit()
            flash(f'Web Service URL reset to default.', 'success')
        else:
            SystemSetting.set(
                SystemSetting.BLUE_WS_URL,
                new_url,
                description='Explorance Blue SOAP Web Service endpoint URL',
                admin=current_user
            )
            flash('Web Service URL updated.', 'success')

        return redirect(url_for('settings.index'))

    return render_template('settings/ws_url.html',
                           current_setting=current_setting,
                           default_url=default_url)


@settings_bp.route('/api/sync-status')
@super_admin_required
def api_sync_status():
    """API: Return current sync status from DB (cross-process safe).

    Used by the sync_logs page to poll real progress without relying on the
    in-memory _sync_progress dict, which is not visible across threads that
    spin up their own app context.
    """
    running_log = _get_latest_running_sync_log()

    if not running_log:
        return jsonify({'running': False})

    return jsonify(_build_sync_status_payload(running_log))


@settings_bp.route('/api/validate-key', methods=['POST'])
@super_admin_required
def api_validate_key():
    """API: Validate the current API key."""
    blue_service = get_blue_sync_service()
    is_valid, message = blue_service.validate_api_key()

    return jsonify({
        'valid': is_valid,
        'message': message
    })


# ============================================================================
# SYNC LOGS
# ============================================================================

@settings_bp.route('/sync-logs')
@super_admin_required
def sync_logs():
    """View unified sync logs with filtering."""
    _mark_stale_running_logs()

    # Get filter parameters
    sync_type = request.args.get('type', '')
    status_filter = request.args.get('status', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    page = request.args.get('page', 1, type=int)
    per_page = 20

    # Build query
    query = DataSyncLog.query

    if sync_type:
        query = query.filter(DataSyncLog.sync_type == sync_type)

    if status_filter:
        query = query.filter(DataSyncLog.status == status_filter)

    if date_from:
        try:
            from_date = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(DataSyncLog.started_at >= from_date)
        except ValueError:
            pass

    if date_to:
        try:
            to_date = datetime.strptime(date_to, '%Y-%m-%d')
            to_date = to_date.replace(hour=23, minute=59, second=59)
            query = query.filter(DataSyncLog.started_at <= to_date)
        except ValueError:
            pass

    # Order and paginate
    query = query.order_by(DataSyncLog.started_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    logs = pagination.items

    # Check for running syncs
    hana_progress = get_sync_progress()
    blue_progress = get_blue_sync_progress()
    running_log = _get_latest_running_sync_log()

    return render_template('settings/sync_logs.html',
                           logs=logs,
                           pagination=pagination,
                           hana_progress=hana_progress,
                           blue_progress=blue_progress,
                           running_log=running_log,
                           sync_running=bool(running_log),
                           current_filters={
                               'type': sync_type,
                               'status': status_filter,
                               'date_from': date_from,
                               'date_to': date_to
                           })


@settings_bp.route('/sync-logs/<int:log_id>')
@super_admin_required
def sync_log_detail(log_id):
    """View detailed sync log information."""
    _mark_stale_running_logs()
    log = DataSyncLog.query.get_or_404(log_id)
    return render_template('settings/sync_log_detail.html', log=log)


@settings_bp.route('/sync-logs/<int:log_id>/cancel', methods=['POST'])
@super_admin_required
def cancel_sync(log_id):
    """Request cancellation for a running sync and stop any active subprocess."""
    _mark_stale_running_logs()
    log = DataSyncLog.query.get_or_404(log_id)

    if log.status != DataSyncLog.STATUS_RUNNING:
        flash('That sync is no longer running.', 'warning')
        return redirect(url_for('settings.sync_logs'))

    request_sync_cancellation(
        log.id,
        requested_by=current_user,
        reason='Stop requested from the sync logs page.',
    )

    parent_log_id = (log.summary or {}).get('parent_sync_log_id')
    if parent_log_id:
        request_sync_cancellation(
            parent_log_id,
            requested_by=current_user,
            reason='Child Blue sync cancellation requested from the sync logs page.',
        )

    terminated = terminate_sync_process(log.id)

    if terminated:
        flash('Stop requested. The active sync subprocess was terminated.', 'warning')
    else:
        flash('Stop requested. The sync will stop after the current step reaches a cancellation check.', 'warning')

    return redirect(url_for('settings.sync_logs'))


# ============================================================================
# MANUAL SYNC TRIGGERS
# ============================================================================

@settings_bp.route('/sync/hana-to-datasource', methods=['POST'])
@super_admin_required
def trigger_hana_sync():
    """Trigger a HANA to datasource sync."""
    from app.models.settings import DataSyncLog

    if _get_latest_running_sync_log():
        flash('A HANA sync is already running. Please wait for it to complete.', 'warning')
        return redirect(url_for('settings.sync_logs'))

    # Create sync log
    sync_log = DataSyncLog(
        sync_type=DataSyncLog.TYPE_HANA_TO_DATASOURCE,
        status=DataSyncLog.STATUS_RUNNING,
        triggered_by_id=current_user.id,
        trigger_type='manual'
    )
    db.session.add(sync_log)
    db.session.commit()

    # Start sync in background thread
    def run_hana_sync(log_id):
        from app import create_app
        app = create_app()
        with app.app_context():
            log = DataSyncLog.query.get(log_id)
            try:
                raise_if_sync_cancelled(log_id)
                log.summary = _merge_cancel_summary(log, {
                    'pipeline_phase': 'hana_pull',
                    'pipeline_step': 1,
                    'pipeline_total_steps': 2,
                    'pipeline_message': 'Fetching data from SAP HANA and writing datasource CSV files...'
                })
                db.session.commit()

                result, json_result = _run_hana_sync_script(sync_log_id=log_id)

                log.summary = _merge_cancel_summary(log, {
                    'stdout': result.stdout[:2000],
                    'stderr': result.stderr[:1000],
                    'command': f'{sys.executable} scripts/hana_sync.py --output datasources',
                    'output_path': (json_result or {}).get('output_path'),
                    'table_stats': (json_result or {}).get('stats'),
                    'pipeline_phase': 'hana_pull',
                    'pipeline_step': 1,
                    'pipeline_total_steps': 2,
                    'pipeline_message': 'Fetched HANA data. Preparing database sync...'
                })

                # Populate row counts and per-file diffs from the script's
                # JSON result so the sync log accurately reflects what the
                # script actually did instead of always showing 0 records.
                if json_result:
                    log.records_processed = json_result.get('records_processed', 0) or 0
                    log.records_added = json_result.get('records_added', 0) or 0
                    log.records_updated = json_result.get('records_updated', 0) or 0
                    log.file_stats = json_result.get('file_stats') or {}
                    # Field-level changes index: per-file changed-key buckets,
                    # used by the sync detail page to show what moved.
                    log.field_changes = {
                        fname: {
                            'added': fs.get('added_keys', []),
                            'updated': fs.get('updated_keys', []),
                            'removed': fs.get('removed_keys', []),
                            'diff_key_columns': fs.get('diff_key_columns', []),
                        }
                        for fname, fs in (json_result.get('file_stats') or {}).items()
                    }

                if result.returncode != 0:
                    log.status = DataSyncLog.STATUS_FAILED
                    log.errors = [result.stderr[:1000] or result.stdout[:1000] or 'HANA sync failed with no output']
                else:
                    raise_if_sync_cancelled(log_id)
                    # Step 2: run CSV -> DB sync as a *subprocess* so it gets
                    # its own process, its own connection pool, and can't
                    # deadlock against the web worker's SQLAlchemy pool.
                    db_summary = None
                    db_error = None
                    try:
                        summary = log.summary
                        summary['pipeline_phase'] = 'database_sync'
                        summary['pipeline_step'] = 2
                        summary['pipeline_total_steps'] = 2
                        summary['pipeline_message'] = 'Syncing datasource CSV files into the application database...'
                        log.summary = _merge_cancel_summary(log, summary)
                        db.session.commit()

                        project_root = _get_project_root()
                        ds_path = os.path.join(project_root, 'datasources')
                        db_script = os.path.join(project_root, 'scripts', 'db_sync.py')
                        db_proc = _run_cancellable_subprocess(
                            [sys.executable, db_script, '--datasources', ds_path],
                            cwd=project_root,
                            timeout=900,   # 15 min hard limit
                            sync_log_id=log_id,
                        )
                        # Script emits a single JSON line on stdout.
                        db_result = {}
                        if db_proc.stdout.strip():
                            try:
                                db_result = json.loads(db_proc.stdout.strip())
                            except json.JSONDecodeError:
                                db_error = f'Could not parse db_sync output: {db_proc.stdout[:500]}'

                        if db_proc.returncode != 0 and not db_error:
                            db_error = db_result.get('errors', [db_proc.stderr[:500] or 'db_sync.py exited non-zero'])[0]

                        if not db_error:
                            db_summary = {
                                'success': db_result.get('success', False),
                                'stats': db_result.get('stats', {}),
                                'errors': db_result.get('errors', [])[:10],
                                'elapsed_seconds': db_result.get('elapsed_seconds'),
                                'sync_run_id': db_result.get('sync_run_id'),
                            }
                    except SyncCancelledError:
                        raise
                    except subprocess.TimeoutExpired:
                        db_error = 'CSV->DB sync timed out after 15 minutes'
                    except Exception as db_e:
                        import traceback
                        db_error = str(db_e)
                        traceback.print_exc()

                    summary = log.summary
                    summary['database_sync'] = db_summary or {'error': db_error}
                    log.summary = _merge_cancel_summary(log, summary)

                    if db_error:
                        log.status = DataSyncLog.STATUS_FAILED
                        existing_errors = log.errors or []
                        existing_errors.append(f'CSV->DB sync failed: {db_error}')
                        log.errors = existing_errors
                    else:
                        log.status = DataSyncLog.STATUS_COMPLETED
                        # Use DB-side counts as the authoritative record figures.
                        # records_processed = total unique DB rows touched (courses + instructors + students counted)
                        # records_added / records_updated = net changes to courses and instructors only.
                        ds_stats = (db_summary or {}).get('stats') or {}
                        log.records_processed = (
                            ds_stats.get('courses_added', 0)
                            + ds_stats.get('courses_updated', 0)
                            + ds_stats.get('instructors_added', 0)
                            + ds_stats.get('students_counted', 0)
                        )
                        log.records_added = (
                            ds_stats.get('courses_added', 0)
                            + ds_stats.get('instructors_added', 0)
                        )
                        log.records_updated = ds_stats.get('courses_updated', 0)
                        summary = log.summary
                        summary['pipeline_phase'] = 'complete'
                        summary['pipeline_step'] = 2
                        summary['pipeline_total_steps'] = 2
                        summary['pipeline_message'] = 'HANA pull and database sync completed.'
                        log.summary = _merge_cancel_summary(log, summary)

            except SyncCancelledError as e:
                mark_sync_cancelled(log, str(e))
                errors = log.errors or []
                errors.append(str(e))
                log.errors = errors[:50]
            except subprocess.TimeoutExpired:
                log.status = DataSyncLog.STATUS_FAILED
                log.errors = ['HANA sync timed out after 10 minutes']
                summary = log.summary
                summary['pipeline_phase'] = 'failed'
                summary['pipeline_message'] = 'HANA sync timed out after 10 minutes.'
                log.summary = _merge_cancel_summary(log, summary)
            except FileNotFoundError as e:
                log.status = DataSyncLog.STATUS_FAILED
                log.errors = [str(e)]
                summary = log.summary
                summary['pipeline_phase'] = 'failed'
                summary['pipeline_message'] = str(e)
                log.summary = _merge_cancel_summary(log, summary)
            except Exception as e:
                log.status = DataSyncLog.STATUS_FAILED
                log.errors = [str(e)]
                summary = log.summary
                summary['pipeline_phase'] = 'failed'
                summary['pipeline_message'] = str(e)
                log.summary = _merge_cancel_summary(log, summary)

            log.completed_at = datetime.utcnow()
            db.session.commit()

    thread = threading.Thread(target=run_hana_sync, args=(sync_log.id,))
    thread.start()

    flash('HANA to Datasource sync started. Check back for results.', 'info')
    return redirect(url_for('settings.sync_logs'))


@settings_bp.route('/sync/datasource-to-blue', methods=['POST'])
@super_admin_required
def trigger_blue_sync():
    """Trigger a datasource to Blue sync."""
    # Check if API key is configured
    api_key = SystemSetting.get(SystemSetting.BLUE_API_KEY)
    if not api_key:
        flash('Explorance Blue API key is not configured. Please configure it first.', 'danger')
        return redirect(url_for('settings.index'))

    if _get_latest_running_sync_log():
        flash('A Blue sync is already running. Please wait for it to complete.', 'warning')
        return redirect(url_for('settings.sync_logs'))

    # Get optional datasource selection
    datasources = request.form.getlist('datasources')
    if not datasources:
        datasources = None  # Push all

    # Start sync in background thread
    def run_blue_sync():
        from app import create_app
        app = create_app()
        with app.app_context():
            from flask_login import current_user as bg_user
            try:
                # Get admin for logging
                from app.models.admin import Admin
                admin = Admin.query.get(current_user.id) if current_user else None

                blue_service = get_blue_sync_service()
                blue_service.push_all(
                    datasources=datasources,
                    triggered_by=admin,
                    trigger_type='manual'
                )
            except Exception as e:
                import traceback
                traceback.print_exc()

    thread = threading.Thread(target=run_blue_sync)
    thread.start()

    flash('Datasource to Blue sync started. Check back for results.', 'info')
    return redirect(url_for('settings.sync_logs'))


@settings_bp.route('/sync/full', methods=['POST'])
@super_admin_required
def trigger_full_sync():
    """Trigger a full sync (HANA to datasource, then datasource to Blue)."""
    # Check if API key is configured
    api_key = SystemSetting.get(SystemSetting.BLUE_API_KEY)
    if not api_key:
        flash('Explorance Blue API key is not configured. Please configure it first.', 'danger')
        return redirect(url_for('settings.index'))

    if _get_latest_running_sync_log():
        flash('A sync is already running. Please wait for it to complete.', 'warning')
        return redirect(url_for('settings.sync_logs'))

    # Create sync log
    sync_log = DataSyncLog(
        sync_type=DataSyncLog.TYPE_FULL_SYNC,
        status=DataSyncLog.STATUS_RUNNING,
        triggered_by_id=current_user.id,
        trigger_type='manual'
    )
    db.session.add(sync_log)
    db.session.commit()

    # Start sync in background thread
    def run_full_sync(log_id):
        from app import create_app
        app = create_app()
        with app.app_context():
            log = DataSyncLog.query.get(log_id)
            errors = []
            summary = {
                'pipeline_phase': 'hana_pull',
                'pipeline_step': 1,
                'pipeline_total_steps': 3,
                'pipeline_message': 'Fetching data from SAP HANA and writing datasource CSV files...'
            }
            log.summary = _merge_cancel_summary(log, summary)
            db.session.commit()

            try:
                raise_if_sync_cancelled(log_id)
                # Step 1: HANA to datasource
                result, json_result = _run_hana_sync_script(sync_log_id=log_id)

                summary['hana_sync'] = {
                    'success': result.returncode == 0,
                    'stdout': result.stdout[:2000],
                    'stderr': result.stderr[:1000],
                    'output_path': (json_result or {}).get('output_path'),
                    'table_stats': (json_result or {}).get('stats'),
                }
                log.summary = _merge_cancel_summary(log, summary)

                if json_result:
                    log.records_processed = json_result.get('records_processed', 0) or 0
                    log.records_added = json_result.get('records_added', 0) or 0
                    log.records_updated = json_result.get('records_updated', 0) or 0
                    log.file_stats = json_result.get('file_stats') or {}
                    log.field_changes = {
                        fname: {
                            'added': fs.get('added_keys', []),
                            'updated': fs.get('updated_keys', []),
                            'removed': fs.get('removed_keys', []),
                            'diff_key_columns': fs.get('diff_key_columns', []),
                        }
                        for fname, fs in (json_result.get('file_stats') or {}).items()
                    }

                if result.returncode != 0:
                    log.status = DataSyncLog.STATUS_FAILED
                    errors.append(result.stderr[:1000] or result.stdout[:1000] or 'HANA sync failed')
                    log.errors = errors
                    summary['pipeline_phase'] = 'failed'
                    summary['pipeline_message'] = 'HANA sync step failed.'
                    log.summary = _merge_cancel_summary(log, summary)
                    log.completed_at = datetime.utcnow()
                    db.session.commit()
                    return

                raise_if_sync_cancelled(log_id)
                # Step 2: CSV -> database via CourseSyncService (PostgreSQL-native).
                summary['pipeline_phase'] = 'database_sync'
                summary['pipeline_step'] = 2
                summary['pipeline_message'] = 'Syncing datasource CSV files into the application database...'
                log.summary = _merge_cancel_summary(log, summary)
                db.session.commit()

                project_root = _get_project_root()
                from app.services.course_sync import (
                    CourseSyncService, resolve_datasources_path,
                )
                ds_path = resolve_datasources_path(
                    os.path.join(project_root, 'datasources')
                )
                course_sync = CourseSyncService(ds_path)
                db_result = course_sync.sync_all(sync_log_id=log_id)

                summary['database_sync'] = {
                    'success': db_result.get('success', False),
                    'stats': db_result.get('stats', {}),
                    'errors': db_result.get('errors', [])[:10],
                    'elapsed_seconds': db_result.get('elapsed_seconds'),
                    'sync_run_id': db_result.get('sync_run_id'),
                }
                log.summary = _merge_cancel_summary(log, summary)

                ds_stats = db_result.get('stats') or {}
                log.records_added = (log.records_added or 0) + (
                    ds_stats.get('courses_added', 0)
                    + ds_stats.get('instructors_added', 0)
                )
                log.records_updated = (log.records_updated or 0) + ds_stats.get(
                    'courses_updated', 0
                )
                log.records_processed = (log.records_processed or 0) + (
                    ds_stats.get('students_counted', 0)
                )

                raise_if_sync_cancelled(log_id)
                # Step 3: Push to Blue.
                summary['pipeline_phase'] = 'blue_push'
                summary['pipeline_step'] = 3
                summary['pipeline_message'] = 'Pushing datasources to Explorance Blue...'
                log.summary = _merge_cancel_summary(log, summary)
                db.session.commit()

                try:
                    from app.models.admin import Admin
                    admin = Admin.query.get(log.triggered_by_id) if log.triggered_by_id else None
                    blue_service = get_blue_sync_service()
                    blue_result = blue_service.push_all(
                        triggered_by=admin,
                        trigger_type='manual',
                        parent_sync_log_id=log.id,
                    )
                    if blue_result.get('cancelled'):
                        raise SyncCancelledError(
                            blue_result.get('message') or 'Sync cancelled by user.'
                        )
                    summary['pipeline_phase'] = 'complete'
                    summary['pipeline_message'] = 'Full pipeline completed successfully.'
                    log.status = DataSyncLog.STATUS_COMPLETED
                except SyncCancelledError as cancel_exc:
                    summary['pipeline_phase'] = 'cancelled'
                    summary['pipeline_message'] = str(cancel_exc)
                    log.status = DataSyncLog.STATUS_CANCELLED
                    errors.append(str(cancel_exc))
                except Exception as blue_exc:
                    errors.append(f'Blue push failed: {blue_exc}')
                    summary['pipeline_phase'] = 'failed'
                    summary['pipeline_message'] = f'Blue push failed: {blue_exc}'
                    log.status = DataSyncLog.STATUS_FAILED

                log.summary = _merge_cancel_summary(log, summary)
                log.errors = errors

            except SyncCancelledError as e:
                errors.append(str(e))
                log.errors = errors[:50]
                mark_sync_cancelled(log, str(e))
            except subprocess.TimeoutExpired:
                log.status = DataSyncLog.STATUS_FAILED
                errors.append('HANA sync timed out after 10 minutes')
                log.errors = errors
                summary['pipeline_phase'] = 'failed'
                summary['pipeline_message'] = 'HANA sync timed out after 10 minutes.'
                log.summary = _merge_cancel_summary(log, summary)
            except Exception as e:
                import traceback
                traceback.print_exc()
                log.status = DataSyncLog.STATUS_FAILED
                errors.append(str(e))
                log.errors = errors
                summary['pipeline_phase'] = 'failed'
                summary['pipeline_message'] = str(e)
                log.summary = _merge_cancel_summary(log, summary)

            log.completed_at = datetime.utcnow()
            db.session.commit()

    thread = threading.Thread(target=run_full_sync, args=(sync_log.id,))
    thread.start()

    flash('Full sync started. Check back for results.', 'info')
    return redirect(url_for('settings.sync_logs'))

# ============================================================================
# CHANGE LOG VIEWER (new — replaces the old daily-CSV-archive workflow)
# ============================================================================

@settings_bp.route('/changes')
@super_admin_required
def changes_index():
    """List recent SyncRuns with change counts."""
    page = request.args.get('page', 1, type=int)
    runs = SyncRun.query.order_by(SyncRun.started_at.desc()).paginate(
        page=page, per_page=25, error_out=False,
    )
    return render_template('settings/changes_index.html', runs=runs)


@settings_bp.route('/changes/<int:run_id>')
@super_admin_required
def changes_detail(run_id):
    """Show per-field changes for a specific SyncRun."""
    run = SyncRun.query.get_or_404(run_id)
    entity_type = request.args.get('entity', '')
    change_type = request.args.get('change', '')
    page = request.args.get('page', 1, type=int)

    q = ChangeLog.query.filter_by(sync_run_id=run_id)
    if entity_type:
        q = q.filter_by(entity_type=entity_type)
    if change_type:
        q = q.filter_by(change_type=change_type)
    q = q.order_by(ChangeLog.entity_type, ChangeLog.change_type, ChangeLog.entity_key)
    pagination = q.paginate(page=page, per_page=100, error_out=False)

    counts_by_type = dict(
        db.session.query(ChangeLog.change_type, db.func.count(ChangeLog.id))
        .filter_by(sync_run_id=run_id)
        .group_by(ChangeLog.change_type).all()
    )
    counts_by_entity = dict(
        db.session.query(ChangeLog.entity_type, db.func.count(ChangeLog.id))
        .filter_by(sync_run_id=run_id)
        .group_by(ChangeLog.entity_type).all()
    )

    return render_template(
        'settings/changes_detail.html',
        run=run,
        changes=pagination.items,
        pagination=pagination,
        counts_by_type=counts_by_type,
        counts_by_entity=counts_by_entity,
        filters={'entity': entity_type, 'change': change_type},
    )


@settings_bp.route('/changes/entity/<entity_type>/<path:entity_key>')
@super_admin_required
def changes_entity_history(entity_type, entity_key):
    """Full change history for a single course or instructor across all syncs."""
    changes = (
        ChangeLog.query
        .filter_by(entity_type=entity_type, entity_key=entity_key)
        .order_by(ChangeLog.created_at.desc())
        .limit(500)
        .all()
    )
    return render_template(
        'settings/changes_entity.html',
        entity_type=entity_type,
        entity_key=entity_key,
        changes=changes,
    )
