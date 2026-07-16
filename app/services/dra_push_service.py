"""DRA (Data Relationship Assignment) push service for Explorance Blue Data151.

Shared by:
  - UI manual push button
  - scripts/dra_sync.py (cron / daily_sync)
  - Admin list change hooks (queue for next daily sync)
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional, Tuple

UTC = timezone.utc
logger = logging.getLogger(__name__)

# Setting keys (also mirrored on SystemSetting for discoverability)
DRA_INCLUDE_IN_DAILY_SYNC = "dra_include_in_daily_sync"
DRA_QUEUE_ON_ADMIN_CHANGE = "dra_queue_on_admin_change"
DRA_SYNC_PENDING = "dra_sync_pending"


def _truthy(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def is_dra_include_in_daily_sync() -> bool:
    from app.models.settings import SystemSetting
    return _truthy(
        SystemSetting.get(DRA_INCLUDE_IN_DAILY_SYNC, "true"),
        default=True,
    )


def is_dra_queue_on_admin_change() -> bool:
    from app.models.settings import SystemSetting
    return _truthy(
        SystemSetting.get(DRA_QUEUE_ON_ADMIN_CHANGE, "true"),
        default=True,
    )


def is_dra_sync_pending() -> bool:
    from app.models.settings import SystemSetting
    return _truthy(SystemSetting.get(DRA_SYNC_PENDING, "false"), default=False)


def set_dra_sync_pending(pending: bool, admin=None, *, commit: bool = True) -> None:
    """Set/clear the DRA dirty flag.

    *commit=False* leaves the change on the current session so callers that
    are mid-transaction (e.g. AdminAuditLog.log_change) can commit once.
    """
    from app.models import db
    from app.models.settings import SystemSetting

    value = "true" if pending else "false"
    setting = SystemSetting.query.get(DRA_SYNC_PENDING)
    if setting:
        setting.value = value
        setting.description = (
            "DRA push queued for next daily sync after admin-list change"
        )
        if admin is not None:
            setting.updated_by_id = getattr(admin, "id", None)
        setting.updated_at = datetime.now(UTC).replace(tzinfo=None)
    else:
        setting = SystemSetting(
            key=DRA_SYNC_PENDING,
            value=value,
            description=(
                "DRA push queued for next daily sync after admin-list change"
            ),
            updated_by_id=getattr(admin, "id", None) if admin else None,
        )
        db.session.add(setting)
    if commit:
        db.session.commit()


def should_run_dra_in_daily_sync() -> bool:
    """True if daily_sync should run DRA (always-on flag or pending queue)."""
    return is_dra_include_in_daily_sync() or is_dra_sync_pending()


def queue_dra_after_admin_change(actor_admin) -> bool:
    """Mark DRA pending when a college/super admin mutates the admin list.

    Returns True if the pending flag was set.
    """
    if not actor_admin:
        return False
    role = getattr(actor_admin, "role", None)
    if role not in ("super_admin", "college_admin"):
        return False
    if not is_dra_queue_on_admin_change():
        return False
    try:
        # Do not commit here — AdminAuditLog.log_change runs mid-request;
        # the route's subsequent db.session.commit() persists the flag.
        set_dra_sync_pending(True, admin=actor_admin, commit=False)
        logger.info(
            "dra_queued_after_admin_change actor=%s role=%s",
            getattr(actor_admin, "linkblue", "?"),
            role,
        )
        return True
    except Exception as exc:
        logger.warning("dra_queue_failed err=%s", exc)
        return False


def generate_dra_rows(*, use_new_codes: bool = False):
    """Generate DRA rows using the same logic as scripts/dra_sync.py."""
    # Import from script package path used at runtime
    import sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from scripts.dra_sync import generate_dra_rows as _gen

    return _gen(None, use_new_codes=use_new_codes)


def push_dra_to_blue(
    *,
    triggered_by_id: Optional[int] = None,
    trigger_type: str = "manual",
    use_new_codes: bool = False,
    clear_pending_on_success: bool = True,
) -> Tuple[bool, str, Optional[int]]:
    """Generate DRA and push to Blue. Returns (ok, message, sync_log_id)."""
    from app.models import db
    from app.models.settings import DataSyncLog, SystemSetting
    from scripts.dra_sync import (
        BLUE_WS_URL_DEFAULT,
        DATASOURCE_ID,
        push_to_blue,
    )

    api_key = SystemSetting.get(SystemSetting.BLUE_API_KEY)
    ws_url = SystemSetting.get(SystemSetting.BLUE_WS_URL) or BLUE_WS_URL_DEFAULT
    if not api_key:
        return False, "Blue API key is not configured.", None

    sync_log = DataSyncLog(
        sync_type=DataSyncLog.TYPE_DRA_TO_BLUE,
        status=DataSyncLog.STATUS_RUNNING,
        triggered_by_id=triggered_by_id,
        trigger_type=trigger_type,
    )
    sync_log.summary = {
        "pipeline_phase": "dra_push",
        "pipeline_message": "Generating DRA and pushing to Explorance Blue Data151...",
        "datasource_id": DATASOURCE_ID,
        "use_new_codes": use_new_codes,
    }
    db.session.add(sync_log)
    db.session.commit()
    log_id = sync_log.id

    try:
        rows, errors = generate_dra_rows(use_new_codes=use_new_codes)
        sync_log.summary = {
            **(sync_log.summary or {}),
            "row_count": len(rows),
            "warning_count": len(errors),
            "warnings_sample": errors[:20],
        }
        db.session.commit()

        if not rows:
            sync_log.status = DataSyncLog.STATUS_FAILED
            sync_log.error_message = "No DRA rows generated"
            sync_log.completed_at = datetime.now(UTC).replace(tzinfo=None)
            db.session.commit()
            return False, "No DRA rows generated — nothing to push.", log_id

        ok = push_to_blue(api_key, ws_url, rows)
        sync_log = DataSyncLog.query.get(log_id)
        if ok:
            sync_log.status = DataSyncLog.STATUS_COMPLETED
            sync_log.records_processed = len(rows)
            sync_log.records_added = len(rows)
            sync_log.completed_at = datetime.now(UTC).replace(tzinfo=None)
            sync_log.summary = {
                **(sync_log.summary or {}),
                "pipeline_message": f"Pushed {len(rows)} DRA rows to {DATASOURCE_ID}",
            }
            db.session.commit()
            if clear_pending_on_success:
                try:
                    set_dra_sync_pending(False)
                except Exception:
                    pass
            return True, f"Pushed {len(rows):,} DRA rows to Blue ({DATASOURCE_ID}).", log_id

        sync_log.status = DataSyncLog.STATUS_FAILED
        sync_log.error_message = "Blue DRA push failed (see server logs)"
        sync_log.completed_at = datetime.now(UTC).replace(tzinfo=None)
        db.session.commit()
        return False, "DRA push to Blue failed. Check sync logs / server output.", log_id

    except Exception as exc:
        logger.exception("dra_push_failed")
        try:
            sync_log = DataSyncLog.query.get(log_id)
            if sync_log:
                sync_log.status = DataSyncLog.STATUS_FAILED
                sync_log.error_message = str(exc)[:500]
                sync_log.completed_at = datetime.now(UTC).replace(tzinfo=None)
                db.session.commit()
        except Exception:
            db.session.rollback()
        return False, f"DRA push error: {exc}", log_id


def start_dra_push_background(
    *,
    triggered_by_id: Optional[int] = None,
    trigger_type: str = "manual",
    use_new_codes: bool = False,
) -> None:
    """Fire-and-forget DRA push in a daemon thread with its own app context."""

    def _run():
        from app import create_app

        app = create_app()
        with app.app_context():
            ok, msg, log_id = push_dra_to_blue(
                triggered_by_id=triggered_by_id,
                trigger_type=trigger_type,
                use_new_codes=use_new_codes,
            )
            logger.info(
                "dra_background_done ok=%s log_id=%s msg=%s",
                ok,
                log_id,
                msg,
            )

    thread = threading.Thread(target=_run, name="dra-push", daemon=True)
    thread.start()
