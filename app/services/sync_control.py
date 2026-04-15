"""Shared sync cancellation helpers.

This module keeps cancellation state in ``data_sync_logs.summary_json`` so
every request thread and worker thread can see it, and also stores in-process
subprocess handles for hard-stop support during stalled script execution.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime

from app.models import db
from app.models.settings import DataSyncLog


class SyncCancelledError(Exception):
    """Raised when a running sync has been cancelled by a user."""


_active_processes = {}
_active_processes_lock = threading.Lock()


def register_sync_process(sync_log_id, process):
    """Track a live subprocess for a running sync log."""
    if not sync_log_id or process is None:
        return
    with _active_processes_lock:
        _active_processes[sync_log_id] = process


def unregister_sync_process(sync_log_id):
    """Remove any tracked subprocess for a sync log."""
    if not sync_log_id:
        return
    with _active_processes_lock:
        _active_processes.pop(sync_log_id, None)


def terminate_sync_process(sync_log_id):
    """Terminate a tracked subprocess if one is still active."""
    if not sync_log_id:
        return False

    with _active_processes_lock:
        process = _active_processes.get(sync_log_id)

    if not process or process.poll() is not None:
        return False

    try:
        process.terminate()
        return True
    except Exception:
        return False


def _read_summary_from_connection(conn, sync_log_id):
    cur = conn.cursor()
    try:
        cur.execute(
            'SELECT summary_json FROM data_sync_logs WHERE id = %s',
            (sync_log_id,),
        )
        row = cur.fetchone()
    finally:
        cur.close()

    if not row or not row[0]:
        return {}

    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return {}


def is_sync_cancellation_requested(sync_log_id, conn=None):
    """Return True when a running sync log has a pending cancel request."""
    if not sync_log_id:
        return False

    if conn is not None:
        summary = _read_summary_from_connection(conn, sync_log_id)
        return bool(summary.get('cancel_requested'))

    log = DataSyncLog.query.get(sync_log_id)
    if not log:
        return False
    return bool((log.summary or {}).get('cancel_requested'))


def raise_if_sync_cancelled(sync_log_id, conn=None, message='Sync cancelled by user.'):
    """Raise ``SyncCancelledError`` if cancellation has been requested."""
    if is_sync_cancellation_requested(sync_log_id, conn=conn):
        raise SyncCancelledError(message)


def request_sync_cancellation(sync_log_id, requested_by=None, reason=None):
    """Mark a running sync as cancellation-requested."""
    log = DataSyncLog.query.get(sync_log_id)
    if not log:
        return None

    summary = log.summary or {}
    if summary.get('cancel_requested'):
        return log

    now = datetime.utcnow().isoformat()
    summary['cancel_requested'] = True
    summary['cancel_requested_at'] = now
    if requested_by is not None:
        summary['cancel_requested_by'] = getattr(requested_by, 'linkblue', None)
        summary['cancel_requested_by_id'] = getattr(requested_by, 'id', None)
    if reason:
        summary['cancel_reason'] = reason
    summary['pipeline_message'] = 'Cancellation requested. Waiting for the current step to stop...'
    log.summary = summary
    db.session.commit()
    return log


def mark_sync_cancelled(log, message='Sync cancelled by user.'):
    """Finalize a sync log as cancelled with a clear pipeline summary."""
    summary = log.summary or {}
    summary['cancel_requested'] = True
    summary['pipeline_phase'] = 'cancelled'
    summary['pipeline_message'] = message
    log.summary = summary
    log.status = DataSyncLog.STATUS_CANCELLED
    log.completed_at = datetime.utcnow()
    return log
