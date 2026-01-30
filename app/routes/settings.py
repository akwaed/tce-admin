"""
Settings Routes for Super Admins
Manage system settings including Explorance Blue API configuration
Unified sync log management with manual sync triggers
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app.models import db
from app.models.settings import SystemSetting, DataSyncLog
from app.services.blue_sync import get_blue_sync_service, get_blue_sync_progress
from app.services.course_sync import CourseSyncService, get_sync_progress
from functools import wraps
from datetime import datetime
import threading

settings_bp = Blueprint('settings', __name__)


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

    return render_template('settings/sync_logs.html',
                           logs=logs,
                           pagination=pagination,
                           hana_progress=hana_progress,
                           blue_progress=blue_progress,
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
    log = DataSyncLog.query.get_or_404(log_id)
    return render_template('settings/sync_log_detail.html', log=log)


# ============================================================================
# MANUAL SYNC TRIGGERS
# ============================================================================

@settings_bp.route('/sync/hana-to-datasource', methods=['POST'])
@super_admin_required
def trigger_hana_sync():
    """Trigger a HANA to datasource sync."""
    from app.models.settings import DataSyncLog

    # Check if sync is already running
    hana_progress = get_sync_progress()
    if hana_progress.get('running'):
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
                # Import and run HANA sync script
                import subprocess
                import os

                script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                                           'scripts', 'hana_sync.py')

                if os.path.exists(script_path):
                    result = subprocess.run(
                        ['python3', script_path],
                        capture_output=True,
                        text=True,
                        timeout=600  # 10 minute timeout
                    )

                    if result.returncode == 0:
                        log.status = DataSyncLog.STATUS_COMPLETED
                        log.summary = {'output': result.stdout[:1000]}
                    else:
                        log.status = DataSyncLog.STATUS_FAILED
                        log.errors = [result.stderr[:500]]
                else:
                    log.status = DataSyncLog.STATUS_FAILED
                    log.errors = [f'HANA sync script not found: {script_path}']

            except subprocess.TimeoutExpired:
                log.status = DataSyncLog.STATUS_FAILED
                log.errors = ['HANA sync timed out after 10 minutes']
            except Exception as e:
                log.status = DataSyncLog.STATUS_FAILED
                log.errors = [str(e)]

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

    # Check if sync is already running
    blue_progress = get_blue_sync_progress()
    if blue_progress.get('running'):
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

    # Check if any sync is already running
    hana_progress = get_sync_progress()
    blue_progress = get_blue_sync_progress()
    if hana_progress.get('running') or blue_progress.get('running'):
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
            summary = {}

            try:
                # Step 1: HANA to datasource
                import subprocess
                import os

                script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                                           'scripts', 'hana_sync.py')

                if os.path.exists(script_path):
                    result = subprocess.run(
                        ['python3', script_path],
                        capture_output=True,
                        text=True,
                        timeout=600
                    )

                    if result.returncode != 0:
                        errors.append(f'HANA sync failed: {result.stderr[:500]}')
                        log.status = DataSyncLog.STATUS_FAILED
                        log.errors = errors
                        log.completed_at = datetime.utcnow()
                        db.session.commit()
                        return

                    summary['hana_sync'] = 'SUCCESS'
                else:
                    errors.append(f'HANA sync script not found')
                    summary['hana_sync'] = 'SKIPPED'

                # Step 2: Datasource to database
                sync_service = CourseSyncService()
                result = sync_service.sync_all()
                summary['database_sync'] = result

                # Step 3: Datasource to Blue
                from app.models.admin import Admin
                admin = Admin.query.get(log.triggered_by_id) if log.triggered_by_id else None

                blue_service = get_blue_sync_service()
                blue_result = blue_service.push_all(
                    triggered_by=admin,
                    trigger_type='manual'
                )

                summary['blue_sync'] = blue_result
                errors.extend(blue_result.get('errors', []))

                if blue_result.get('success'):
                    log.status = DataSyncLog.STATUS_COMPLETED
                else:
                    log.status = DataSyncLog.STATUS_FAILED

            except Exception as e:
                errors.append(str(e))
                log.status = DataSyncLog.STATUS_FAILED

            log.summary = summary
            log.errors = errors[:50]
            log.completed_at = datetime.utcnow()
            db.session.commit()

    thread = threading.Thread(target=run_full_sync, args=(sync_log.id,))
    thread.start()

    flash('Full sync started (HANA -> Datasource -> Blue). Check back for results.', 'info')
    return redirect(url_for('settings.sync_logs'))


@settings_bp.route('/api/sync/progress')
@super_admin_required
def api_sync_progress():
    """API: Get current sync progress for both HANA and Blue."""
    hana_progress = get_sync_progress()
    blue_progress = get_blue_sync_progress()

    return jsonify({
        'hana': hana_progress,
        'blue': blue_progress
    })


@settings_bp.route('/api/sync-logs')
@super_admin_required
def api_sync_logs():
    """API: Get sync logs as JSON."""
    limit = request.args.get('limit', 10, type=int)
    logs = DataSyncLog.query.order_by(
        DataSyncLog.started_at.desc()
    ).limit(limit).all()

    return jsonify([log.to_dict() for log in logs])
