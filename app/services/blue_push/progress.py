"""In-memory progress tracking for Blue push (UI polling)."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

UTC = timezone.utc

_lock = threading.Lock()
_progress: Dict[str, Any] = {
    "running": False,
    "current_datasource": "",
    "current_step": "",
    "datasource_number": 0,
    "total_datasources": 0,
    "batch_number": 0,
    "total_batches": 0,
    "records_processed": 0,
    "started_at": None,
    "updated_at": None,
    "error": None,
    "results": {},
}


def get_blue_sync_progress() -> Dict[str, Any]:
    with _lock:
        return _progress.copy()


def reset_progress(total_datasources: int = 0) -> None:
    with _lock:
        now = datetime.now(UTC).isoformat()
        _progress.clear()
        _progress.update({
            "running": True,
            "current_datasource": "",
            "current_step": "Initializing...",
            "datasource_number": 0,
            "total_datasources": total_datasources,
            "batch_number": 0,
            "total_batches": 0,
            "records_processed": 0,
            "started_at": now,
            "updated_at": now,
            "error": None,
            "results": {},
        })


def update_progress(
    datasource: str = "",
    step: str = "",
    ds_num: int = 0,
    batch: int = 0,
    total_batches: int = 0,
    records: int = 0,
    error: Optional[str] = None,
    result: Optional[str] = None,
    running: Optional[bool] = None,
) -> None:
    with _lock:
        if datasource:
            _progress["current_datasource"] = datasource
        if step:
            _progress["current_step"] = step
        if ds_num:
            _progress["datasource_number"] = ds_num
        if batch:
            _progress["batch_number"] = batch
        if total_batches:
            _progress["total_batches"] = total_batches
        if records:
            _progress["records_processed"] = (
                _progress.get("records_processed") or 0
            ) + records
        if error is not None:
            _progress["error"] = error
        if result is not None and datasource:
            _progress.setdefault("results", {})[datasource] = result
        if running is not None:
            _progress["running"] = running
        _progress["updated_at"] = datetime.now(UTC).isoformat()


def finish_progress(step: str = "Complete", error: Optional[str] = None) -> None:
    with _lock:
        _progress["running"] = False
        _progress["current_step"] = step
        if error is not None:
            _progress["error"] = error
        _progress["updated_at"] = datetime.now(UTC).isoformat()
