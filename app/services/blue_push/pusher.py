"""Single-datasource 7-step Blue push.

SOAP sequence (same for every datasource):
  1. GetCurrentImportingDataSourceProcess — cancel stale import
  2. GetDataBlockInformation — discover DataBlockName
  3. RegisterImport — get TransactionID
  4. PushObjectDataV2 — batches
  5. PrepareDataToFinalizeImportV2 — 600s timeout
  6. FinalizeImport — 300s timeout
  7. CancelImport — only on failure
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import requests

from app.services.blue_push.client import BlueSoapClient, BlueSoapError
from app.services.blue_push.config import DatasourceConfig
from app.services.blue_push.csv_loader import (
    LoadedCsv,
    load_datasource_csv,
    resolve_csv_path,
    sample_rows,
)
from app.services.blue_push.logging_setup import get_logger
from app.services.blue_push.progress import update_progress

logger = get_logger()

CancelCheck = Optional[Callable[[], None]]


@dataclass
class PushResult:
    """Outcome of pushing one datasource."""

    key: str
    datasource_id: str
    display_name: str
    success: bool
    dry_run: bool = False
    rows_pushed: int = 0
    total_rows: int = 0
    columns: List[str] = field(default_factory=list)
    block_name: Optional[str] = None
    transaction_id: Optional[str] = None
    error: Optional[str] = None
    elapsed_seconds: float = 0.0
    sample: List[Dict[str, str]] = field(default_factory=list)
    dropped_columns: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "datasource_id": self.datasource_id,
            "display_name": self.display_name,
            "success": self.success,
            "dry_run": self.dry_run,
            "rows_pushed": self.rows_pushed,
            "total_rows": self.total_rows,
            "columns": self.columns,
            "block_name": self.block_name,
            "transaction_id": self.transaction_id,
            "error": self.error,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "sample": self.sample,
            "dropped_columns": self.dropped_columns,
        }


def push_datasource(
    config: DatasourceConfig,
    api_key: str,
    ws_url: str,
    datasources_path: str,
    *,
    dry_run: bool = False,
    test_rows: Optional[int] = None,
    skip_cancel_check: bool = False,
    cancel_check: CancelCheck = None,
    progress_key: Optional[str] = None,
) -> PushResult:
    """Run the full 7-step push for one datasource.

    Always attempts CancelImport on failure when a transaction is open.
    """
    key = progress_key or config.key
    started = time.monotonic()
    client = BlueSoapClient(api_key, ws_url, config.datasource_id)
    transaction_id: Optional[str] = None

    result = PushResult(
        key=config.key,
        datasource_id=config.datasource_id,
        display_name=config.display_name,
        success=False,
        dry_run=dry_run,
    )

    def _check_cancel() -> None:
        if cancel_check:
            cancel_check()

    try:
        # ------------------------------------------------------------------
        # Load CSV (always; needed for dry-run validation too)
        # ------------------------------------------------------------------
        update_progress(datasource=key, step="Loading CSV...")
        _check_cancel()
        csv_path = resolve_csv_path(datasources_path, config.csv_file)
        loaded: LoadedCsv = load_datasource_csv(csv_path, config, test_rows=test_rows)

        result.columns = list(loaded.columns)
        result.total_rows = loaded.total_rows
        result.dropped_columns = list(loaded.dropped_columns)
        result.sample = sample_rows(loaded.columns, loaded.rows, 5)

        if not loaded.rows:
            result.error = "No data to import"
            result.elapsed_seconds = time.monotonic() - started
            logger.error(
                "push_failed key=%s reason=no_rows path=%s",
                config.key, csv_path,
            )
            return result

        logger.info(
            "push_start key=%s datasource_id=%s rows=%s columns=%s dry_run=%s",
            config.key, config.datasource_id, len(loaded.rows),
            loaded.columns, dry_run,
        )

        if dry_run:
            result.success = True
            result.rows_pushed = len(loaded.rows)
            result.block_name = config.block_name or "(would discover)"
            result.elapsed_seconds = time.monotonic() - started
            update_progress(
                datasource=key,
                step=f"Dry run: {len(loaded.rows)} rows validated",
                records=len(loaded.rows),
            )
            logger.info(
                "dry_run_ok key=%s rows=%s sample_cols=%s",
                config.key, len(loaded.rows), loaded.columns,
            )
            return result

        # ------------------------------------------------------------------
        # Step 1 — Cancel stale import
        # ------------------------------------------------------------------
        if not skip_cancel_check:
            update_progress(datasource=key, step="Checking for stale imports...")
            _check_cancel()
            cancelled = client.cancel_stale_import()
            logger.info(
                "stale_import_check key=%s cancelled=%s",
                config.key, cancelled,
            )

        # ------------------------------------------------------------------
        # Step 2 — Discover block name
        # ------------------------------------------------------------------
        update_progress(datasource=key, step="Discovering schema...")
        _check_cancel()
        block_name = config.block_name
        if not block_name:
            block_name = client.discover_block_name(config.datasource_id)
        if not block_name:
            result.error = "Could not discover block name"
            result.elapsed_seconds = time.monotonic() - started
            logger.error("push_failed key=%s reason=no_block_name", config.key)
            return result
        result.block_name = block_name
        logger.info(
            "block_name key=%s block=%s", config.key, block_name
        )

        # ------------------------------------------------------------------
        # Steps 3–6 wrapped for cancel-on-failure
        # ------------------------------------------------------------------
        try:
            # Step 3 — RegisterImport
            update_progress(datasource=key, step="Registering import...")
            _check_cancel()
            transaction_id = client.register_import(config.datasource_id)
            result.transaction_id = transaction_id
            logger.info(
                "register_ok key=%s transaction_id=%s",
                config.key, transaction_id,
            )

            # Step 4 — PushObjectDataV2 batches
            batch_size = config.batch_size or 500
            total_batches = (len(loaded.rows) + batch_size - 1) // batch_size
            update_progress(
                datasource=key,
                step="Pushing data...",
                total_batches=total_batches,
            )
            rows_pushed = 0
            for i in range(0, len(loaded.rows), batch_size):
                _check_cancel()
                batch = loaded.rows[i:i + batch_size]
                batch_num = (i // batch_size) + 1
                update_progress(
                    datasource=key,
                    step=f"Pushing batch {batch_num}/{total_batches}...",
                    batch=batch_num,
                    records=len(batch),
                )
                client.push_batch(
                    transaction_id, block_name, loaded.columns, batch
                )
                rows_pushed += len(batch)
                if batch_num % 10 == 0 or batch_num == total_batches:
                    logger.info(
                        "batch_ok key=%s batch=%s/%s rows_pushed=%s",
                        config.key, batch_num, total_batches, rows_pushed,
                    )

            result.rows_pushed = rows_pushed

            # Step 5 — PrepareDataToFinalizeImportV2 (600s)
            update_progress(datasource=key, step="Validating import...")
            _check_cancel()
            logger.info(
                "prepare_start key=%s transaction_id=%s timeout=600s",
                config.key, transaction_id,
            )
            try:
                client.prepare_finalize(transaction_id)
            except requests.exceptions.Timeout:
                err = "Timeout after 600s on PrepareDataToFinalizeImportV2"
                logger.error("prepare_timeout key=%s", config.key)
                if transaction_id:
                    client.cancel_import(transaction_id)
                result.error = err
                result.elapsed_seconds = time.monotonic() - started
                return result

            # Step 6 — FinalizeImport (300s)
            update_progress(datasource=key, step="Finalizing...")
            _check_cancel()
            try:
                client.finalize_import(transaction_id)
            except requests.exceptions.Timeout:
                err = "Timeout after 300s on FinalizeImport"
                logger.error("finalize_timeout key=%s", config.key)
                if transaction_id:
                    client.cancel_import(transaction_id)
                result.error = err
                result.elapsed_seconds = time.monotonic() - started
                return result

            result.success = True
            result.elapsed_seconds = time.monotonic() - started
            update_progress(
                datasource=key,
                step=f"Complete: {rows_pushed} rows imported",
            )
            logger.info(
                "push_success key=%s rows=%s elapsed_s=%.1f",
                config.key, rows_pushed, result.elapsed_seconds,
            )
            return result

        except Exception:
            # Step 7 — CancelImport on any failure path
            if transaction_id:
                logger.warning(
                    "cancel_on_failure key=%s transaction_id=%s",
                    config.key, transaction_id,
                )
                client.cancel_import(transaction_id)
            raise

    except BlueSoapError as exc:
        result.error = f"{exc.action}: {exc.message}"
        result.elapsed_seconds = time.monotonic() - started
        logger.error(
            "push_failed key=%s action=%s error=%s snippet=%s",
            config.key, exc.action, exc.message, (exc.response_text or "")[:500],
        )
        return result

    except Exception as exc:
        # Re-raise cancellation so orchestrator can stop cleanly
        from app.services.sync_control import SyncCancelledError
        if isinstance(exc, SyncCancelledError):
            if transaction_id:
                client.cancel_import(transaction_id)
            result.error = str(exc)
            result.elapsed_seconds = time.monotonic() - started
            raise

        result.error = str(exc)
        result.elapsed_seconds = time.monotonic() - started
        logger.exception("push_unexpected_error key=%s", config.key)
        return result
