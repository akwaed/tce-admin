"""Multi-datasource Blue push orchestrator.

Ordering rules (non-negotiable):
  1. Users (Data144) always pushed last.
  2. ≥ 180s between starts of each non-Users datasource.
  3. Non-Users failures: log and continue; still push Users last.
  4. Users failure is terminal for overall success status.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from app.services.blue_push.client import BlueSoapClient
from app.services.blue_push.config import (
    DEFAULT_DATASOURCES,
    DEFAULT_IMPORT_ORDER,
    DEFAULT_WS_URL,
    MIN_NON_USERS_GAP_SECONDS,
    DatasourceConfig,
)
from app.services.blue_push.logging_setup import get_logger
from app.services.blue_push.progress import (
    finish_progress,
    get_blue_sync_progress,
    reset_progress,
    update_progress,
)
from app.services.blue_push.pusher import PushResult, push_datasource
from app.services.sync_control import (
    SyncCancelledError,
    is_sync_cancellation_requested,
    mark_sync_cancelled,
)

UTC = timezone.utc
logger = get_logger()


def _utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _default_datasources_path() -> str:
    root = Path(__file__).resolve().parents[3]
    return str(root / "datasources")


def _safe_rollback() -> None:
    """Roll back a failed SQLAlchemy transaction so later work can proceed.

    PostgreSQL aborts the whole transaction after any error; without an
    explicit rollback, every subsequent statement raises
    InFailedSqlTransaction (even unrelated inserts).
    """
    try:
        from app.models import db
        db.session.rollback()
    except Exception:
        pass


def resolve_api_credentials() -> tuple[Optional[str], str]:
    """API key from SystemSetting DB, then BLUE_API_KEY env. WS URL similar."""
    api_key = None
    ws_url = DEFAULT_WS_URL
    try:
        from app.models.settings import SystemSetting
        api_key = SystemSetting.get(SystemSetting.BLUE_API_KEY)
        db_url = SystemSetting.get(SystemSetting.BLUE_WS_URL)
        if db_url:
            ws_url = db_url
    except Exception as exc:
        logger.warning("resolve_api_credentials db lookup failed: %s", exc)
        _safe_rollback()

    if not api_key:
        api_key = os.environ.get("BLUE_API_KEY")
    env_url = os.environ.get("BLUE_WS_URL")
    if env_url:
        ws_url = env_url
    return api_key, ws_url


def config_from_db_row(row) -> DatasourceConfig:
    """Convert a BlueSyncDatasource model row to DatasourceConfig."""
    renames = row.column_renames or {}
    # Normalize renames dict (may be stored as column_map style)
    column_map = dict(renames) if isinstance(renames, dict) else {}
    is_users = (
        (getattr(row, "legacy_key", None) == "users")
        or (row.datasource_id == "Data144")
    )
    # Ensure Users remap even if DB row is missing it
    if is_users and not column_map:
        column_map = {
            "FIRSTNAME": "FIRSTNAME_1",
            "LASTNAME": "LASTNAME_1",
        }

    key = getattr(row, "legacy_key", None) or row.datasource_id
    batch_size = 500 if is_users else 500

    return DatasourceConfig(
        key=key,
        datasource_id=row.datasource_id,
        display_name=row.display_name or key,
        csv_file=row.csv_file,
        columns=list(row.columns or []),
        required_columns=list(row.required_columns or []),
        column_map=column_map,
        block_name=row.block_name,
        batch_size=batch_size,
        is_users=is_users,
        wait_after_seconds=getattr(row, "wait_after_seconds", None) or 300,
    )


def load_datasource_configs(
    keys: Optional[Sequence[str]] = None,
) -> List[DatasourceConfig]:
    """Load active datasource configs from DB, falling back to defaults.

    Always returns Users last when present.
    """
    configs: List[DatasourceConfig] = []

    try:
        from app.models.settings import BlueSyncDatasource
        q = BlueSyncDatasource.query.filter_by(is_active=True)
        q = q.order_by(BlueSyncDatasource.import_order)
        rows = q.all()
        if rows:
            configs = [config_from_db_row(r) for r in rows]
    except Exception as exc:
        # Critical: Postgres leaves the session unusable until rollback.
        logger.warning("db_config_load_failed err=%s; using defaults", exc)
        _safe_rollback()

    if not configs:
        for k in DEFAULT_IMPORT_ORDER:
            configs.append(DEFAULT_DATASOURCES[k])

    if keys:
        wanted = {k.lower() for k in keys}
        filtered = []
        for c in configs:
            if (
                c.key.lower() in wanted
                or c.datasource_id.lower() in wanted
                or c.display_name.lower() in wanted
            ):
                filtered.append(c)
        # Also allow resolving keys not in active list via defaults
        if not filtered:
            for k in keys:
                kl = k.lower()
                if kl in DEFAULT_DATASOURCES:
                    filtered.append(DEFAULT_DATASOURCES[kl])
                else:
                    # Match by datasource_id in defaults
                    for dc in DEFAULT_DATASOURCES.values():
                        if dc.datasource_id.lower() == kl:
                            filtered.append(dc)
                            break
        configs = filtered

    # Enforce Users last
    users = [c for c in configs if c.is_users or c.datasource_id == "Data144"]
    others = [c for c in configs if not (c.is_users or c.datasource_id == "Data144")]
    return others + users


class BluePushOrchestrator:
    """Coordinates multi-datasource Blue pushes with gaps and sync logging."""

    def __init__(self, datasources_path: Optional[str] = None):
        self.datasources_path = datasources_path or _default_datasources_path()
        self.api_key: Optional[str] = None
        self.ws_url: str = DEFAULT_WS_URL
        self.errors: List[str] = []
        self.results: Dict[str, Any] = {}
        self.stats: Dict[str, Any] = {}
        self._parent_sync_log_id: Optional[int] = None

    # ------------------------------------------------------------------
    # Public API (compatible with old BlueSyncService surface)
    # ------------------------------------------------------------------

    def validate_api_key(self):
        api_key, ws_url = resolve_api_credentials()
        if not api_key:
            return False, "API key not configured"
        client = BlueSoapClient(api_key, ws_url, "Data144")
        return client.validate_api_key()

    def push_all(
        self,
        datasources: Optional[List[str]] = None,
        dry_run: bool = False,
        triggered_by=None,
        trigger_type: str = "manual",
        parent_sync_log_id: Optional[int] = None,
        test_rows: Optional[int] = None,
        skip_gaps: bool = False,
    ) -> Dict[str, Any]:
        """Push all (or selected) datasources. Writes DataSyncLog + file events."""
        from app.models import db
        from app.models.settings import DataSyncLog, DataFileSyncEvent

        # Ensure we start with a clean session (prior create_app seed queries
        # or failed lookups must not leave Postgres in aborted state).
        _safe_rollback()

        self.api_key, self.ws_url = resolve_api_credentials()
        self._parent_sync_log_id = parent_sync_log_id
        self.errors = []
        self.results = {}
        self.stats = {
            "total_records": 0,
            "datasources_success": 0,
            "datasources_failed": 0,
        }

        if not self.api_key and not dry_run:
            return {
                "success": False,
                "error": "API key not configured. Please configure it in Settings "
                         "or set BLUE_API_KEY.",
                "results": {},
                "stats": self.stats,
            }

        configs = load_datasource_configs(datasources)
        if not configs:
            return {
                "success": False,
                "error": "No datasources to push.",
                "results": {},
                "stats": self.stats,
            }

        reset_progress(total_datasources=len(configs))
        logger.info(
            "pipeline_start count=%s order=%s dry_run=%s trigger=%s",
            len(configs),
            [c.key for c in configs],
            dry_run,
            trigger_type,
        )

        # Create sync log early so UI can poll
        try:
            sync_log = DataSyncLog(
                sync_type=DataSyncLog.TYPE_DATASOURCE_TO_BLUE,
                status=DataSyncLog.STATUS_RUNNING,
                triggered_by_id=triggered_by.id if triggered_by else None,
                trigger_type=trigger_type,
            )
            db.session.add(sync_log)
            db.session.commit()
        except Exception as exc:
            _safe_rollback()
            logger.exception("sync_log_create_failed")
            return {
                "success": False,
                "error": f"Could not create sync log (DB error): {exc}",
                "results": {},
                "stats": self.stats,
            }
        self._persist_progress(sync_log.id, dry_run=dry_run)

        push_results: Dict[str, PushResult] = {}
        last_non_users_start: Optional[float] = None
        users_failed = False

        try:
            for idx, cfg in enumerate(configs, 1):
                self._raise_if_cancelled(sync_log.id, parent_sync_log_id)
                is_users = cfg.is_users or cfg.datasource_id == "Data144"

                # Gap between starts of non-Users datasources
                if (
                    not dry_run
                    and not skip_gaps
                    and not is_users
                    and last_non_users_start is not None
                ):
                    elapsed_since = time.monotonic() - last_non_users_start
                    wait = max(0, MIN_NON_USERS_GAP_SECONDS - int(elapsed_since))
                    if wait > 0:
                        update_progress(
                            datasource=cfg.key,
                            step=f"Waiting {wait}s (3-min gap between starts)...",
                            ds_num=idx,
                        )
                        self._persist_progress(sync_log.id, dry_run=dry_run)
                        self._sleep_with_cancel(
                            wait, sync_log.id, parent_sync_log_id
                        )

                update_progress(
                    datasource=cfg.key,
                    step="Starting...",
                    ds_num=idx,
                )
                self._persist_progress(sync_log.id, dry_run=dry_run)

                if not is_users:
                    last_non_users_start = time.monotonic()

                file_event = self._start_file_event(sync_log.id, cfg)
                file_start = time.monotonic()

                def cancel_check():
                    self._raise_if_cancelled(sync_log.id, parent_sync_log_id)

                try:
                    pr = push_datasource(
                        cfg,
                        api_key=self.api_key or "",
                        ws_url=self.ws_url,
                        datasources_path=self.datasources_path,
                        dry_run=dry_run,
                        test_rows=test_rows,
                        cancel_check=cancel_check,
                        progress_key=cfg.key,
                    )
                except SyncCancelledError:
                    self._complete_file_event(
                        file_event, "failed", 0, "Sync cancelled",
                        time.monotonic() - file_start,
                    )
                    raise
                except Exception as exc:
                    pr = PushResult(
                        key=cfg.key,
                        datasource_id=cfg.datasource_id,
                        display_name=cfg.display_name,
                        success=False,
                        error=str(exc),
                        elapsed_seconds=time.monotonic() - file_start,
                    )

                push_results[cfg.key] = pr
                self.stats["total_records"] += pr.rows_pushed or pr.total_rows or 0

                if pr.success:
                    self.stats["datasources_success"] += 1
                    self.results[cfg.key] = "SUCCESS"
                    update_progress(datasource=cfg.key, result="SUCCESS")
                    self._complete_file_event(
                        file_event,
                        "success",
                        pr.rows_pushed or pr.total_rows,
                        elapsed_seconds=pr.elapsed_seconds,
                    )
                else:
                    self.stats["datasources_failed"] += 1
                    err = pr.error or "Unknown error"
                    self.errors.append(f"{cfg.key}: {err}")
                    self.results[cfg.key] = f"FAILED: {err}"
                    update_progress(
                        datasource=cfg.key, error=err, result="FAILED"
                    )
                    self._complete_file_event(
                        file_event,
                        "failed",
                        pr.total_rows or 0,
                        error_message=err,
                        elapsed_seconds=pr.elapsed_seconds,
                    )
                    if is_users:
                        users_failed = True
                        logger.error(
                            "users_push_failed terminal=True error=%s", err
                        )
                    else:
                        logger.warning(
                            "non_users_push_failed key=%s continuing=True error=%s",
                            cfg.key, err,
                        )

                self._persist_progress(sync_log.id, dry_run=dry_run)

            # Terminal status
            all_success = self.stats["datasources_failed"] == 0
            # partial if some non-users failed but users ok, or vice versa
            if all_success:
                status = DataSyncLog.STATUS_COMPLETED
                phase = "complete"
                msg = "Datasource to Blue sync completed successfully."
            else:
                status = DataSyncLog.STATUS_FAILED
                phase = "failed"
                if users_failed:
                    msg = "Datasource to Blue sync failed (Users push failed)."
                else:
                    msg = "Datasource to Blue sync finished with errors (partial)."

            sync_log.status = status
            sync_log.completed_at = _utc_now_naive()
            sync_log.blue_results = self.results
            sync_log.records_processed = self.stats["total_records"]
            sync_log.errors = self.errors[:50]
            summary = self._build_summary(dry_run=dry_run)
            summary.update({
                "pipeline_phase": phase,
                "pipeline_message": msg,
                "datasources_success": self.stats["datasources_success"],
                "datasources_failed": self.stats["datasources_failed"],
                "results": self.results,
                "push_details": {
                    k: v.to_dict() for k, v in push_results.items()
                },
                "users_failed": users_failed,
            })
            sync_log.summary = summary
            db.session.commit()

            finish_progress(step="Complete" if all_success else "Finished with errors")
            logger.info(
                "pipeline_end success=%s stats=%s errors=%s",
                all_success, self.stats, self.errors[:5],
            )

            return {
                "success": all_success,
                "results": self.results,
                "stats": self.stats,
                "errors": self.errors[:10],
                "sync_log_id": sync_log.id,
                "push_details": {
                    k: v.to_dict() for k, v in push_results.items()
                },
            }

        except SyncCancelledError as e:
            mark_sync_cancelled(sync_log, str(e))
            sync_log.blue_results = self.results
            sync_log.records_processed = self.stats["total_records"]
            sync_log.errors = (self.errors + [str(e)])[:50]
            summary = self._build_summary(dry_run=dry_run)
            summary.update({
                "cancel_requested": True,
                "parent_sync_log_id": parent_sync_log_id,
                "pipeline_phase": "cancelled",
                "pipeline_message": str(e),
                "results": self.results,
            })
            sync_log.summary = summary
            db.session.commit()
            finish_progress(step="Cancelled")
            return {
                "success": False,
                "cancelled": True,
                "message": str(e),
                "results": self.results,
                "stats": self.stats,
                "errors": self.errors[:10],
                "sync_log_id": sync_log.id,
            }

        except Exception as e:
            sync_log.fail(str(e))
            summary = self._build_summary(dry_run=dry_run)
            summary.update({
                "parent_sync_log_id": parent_sync_log_id,
                "pipeline_phase": "failed",
                "pipeline_message": f"Datasource to Blue sync failed: {e}",
            })
            sync_log.summary = summary
            db.session.commit()
            finish_progress(step="Failed", error=str(e))
            logger.exception("pipeline_crash")
            raise

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _raise_if_cancelled(
        self,
        sync_log_id: Optional[int] = None,
        parent_sync_log_id: Optional[int] = None,
    ) -> None:
        if sync_log_id and is_sync_cancellation_requested(sync_log_id):
            raise SyncCancelledError("Blue sync cancelled by user.")
        if parent_sync_log_id and is_sync_cancellation_requested(parent_sync_log_id):
            raise SyncCancelledError("Full sync cancelled by user.")

    def _sleep_with_cancel(
        self,
        seconds: int,
        sync_log_id: Optional[int] = None,
        parent_sync_log_id: Optional[int] = None,
    ) -> None:
        remaining = max(int(seconds), 0)
        while remaining > 0:
            self._raise_if_cancelled(sync_log_id, parent_sync_log_id)
            time.sleep(1)
            remaining -= 1

    def _build_summary(self, dry_run: bool = False) -> Dict[str, Any]:
        progress = get_blue_sync_progress()
        datasource_key = progress.get("current_datasource") or ""
        current_step = progress.get("current_step") or "Running..."
        total = progress.get("total_datasources") or 1
        num = progress.get("datasource_number") or 0
        return {
            "pipeline_phase": "blue_push",
            "pipeline_step": num,
            "pipeline_total_steps": total,
            "pipeline_message": (
                f"{datasource_key}: {current_step}" if datasource_key else current_step
            ),
            "current_datasource": datasource_key,
            "current_step": current_step,
            "datasource_number": num,
            "total_datasources": total,
            "batch_number": progress.get("batch_number") or 0,
            "total_batches": progress.get("total_batches") or 0,
            "records_processed": progress.get("records_processed") or 0,
            "results": progress.get("results") or {},
            "dry_run": dry_run,
            "started_at": progress.get("started_at"),
            "updated_at": progress.get("updated_at"),
            "error": progress.get("error"),
            "parent_sync_log_id": self._parent_sync_log_id,
        }

    def _persist_progress(
        self, sync_log_id: int, dry_run: bool = False
    ) -> None:
        from app.models import db
        from app.models.settings import DataSyncLog

        try:
            sync_log = DataSyncLog.query.get(sync_log_id)
            if not sync_log:
                return
            existing = sync_log.summary or {}
            summary = self._build_summary(dry_run=dry_run)
            for key in (
                "cancel_requested",
                "cancel_requested_at",
                "cancel_requested_by",
                "cancel_requested_by_id",
                "cancel_reason",
                "parent_sync_log_id",
            ):
                if existing.get(key) is not None and summary.get(key) is None:
                    summary[key] = existing.get(key)
            sync_log.summary = summary
            sync_log.records_processed = max(
                sync_log.records_processed or 0,
                summary.get("records_processed", 0),
            )
            if self.errors:
                sync_log.errors = self.errors[:50]
            db.session.commit()
        except Exception:
            _safe_rollback()
            logger.exception("persist_progress_failed sync_log_id=%s", sync_log_id)

    def _start_file_event(self, sync_log_id: int, cfg: DatasourceConfig):
        from app.models import db
        from app.models.settings import DataFileSyncEvent

        try:
            event = DataFileSyncEvent(
                sync_log_id=sync_log_id,
                direction="blue_push",
                file_name=cfg.csv_file,
                datasource_id=cfg.datasource_id,
                status="running",
                row_count=0,
                rows_added=0,
                rows_updated=0,
                rows_removed=0,
            )
            db.session.add(event)
            db.session.commit()
            return event
        except Exception:
            _safe_rollback()
            logger.exception("file_event_create_failed")
            return None

    def _complete_file_event(
        self,
        file_event,
        status: str,
        row_count: int = 0,
        error_message: Optional[str] = None,
        elapsed_seconds: Optional[float] = None,
    ) -> None:
        if file_event is None:
            return
        from app.models import db

        try:
            file_event.status = status
            file_event.completed_at = _utc_now_naive()
            file_event.row_count = row_count or 0
            if status == "success":
                file_event.rows_added = row_count or 0
            else:
                file_event.rows_added = file_event.rows_added or 0
            file_event.rows_updated = file_event.rows_updated or 0
            file_event.rows_removed = file_event.rows_removed or 0
            if error_message:
                file_event.error_message = error_message[:500]
            if elapsed_seconds is not None:
                file_event.elapsed_seconds = round(elapsed_seconds, 2)
            db.session.commit()
        except Exception:
            _safe_rollback()
            logger.exception("file_event_complete_failed")


# Alias for drop-in replacement of BlueSyncService
BlueSyncService = BluePushOrchestrator


def get_blue_sync_service(datasources_path: str = "./datasources") -> BluePushOrchestrator:
    """Factory used by routes and CLI."""
    path = datasources_path
    if path == "./datasources":
        path = _default_datasources_path()
    return BluePushOrchestrator(path)
