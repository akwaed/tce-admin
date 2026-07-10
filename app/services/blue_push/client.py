"""HTTP SOAP client for Explorance Blue with structured logging."""
from __future__ import annotations

import time
from typing import Optional, Tuple

import requests

from app.services.blue_push.config import (
    TIMEOUT_CANCEL,
    TIMEOUT_DISCOVER,
    TIMEOUT_FINALIZE,
    TIMEOUT_PREPARE,
    TIMEOUT_PUSH_BATCH,
    TIMEOUT_REGISTER,
    TIMEOUT_STALE_CHECK,
)
from app.services.blue_push.logging_setup import get_logger, log_soap_call
from app.services.blue_push.soap import (
    check_response,
    extract_block_name,
    extract_value,
    soap_cancel_import,
    soap_finalize_import,
    soap_get_current_process,
    soap_get_datablock_info,
    soap_prepare_finalize,
    soap_push_data,
    soap_register_import,
)

logger = get_logger()

# Note: Blue's SOAP action name has a known typo: Finzalize (not Finalize)
ACTION_PREPARE = "PrepareDataToFinzalizeImportV2"

ACTION_TIMEOUTS = {
    "RegisterImport": TIMEOUT_REGISTER,
    "PushObjectDataV2": TIMEOUT_PUSH_BATCH,
    ACTION_PREPARE: TIMEOUT_PREPARE,
    "FinalizeImport": TIMEOUT_FINALIZE,
    "CancelImport": TIMEOUT_CANCEL,
    "GetCurrentImportingDataSourceProcess": TIMEOUT_STALE_CHECK,
    "GetDataBlockInformation": TIMEOUT_DISCOVER,
}


class BlueSoapError(Exception):
    """Raised when a SOAP step fails."""

    def __init__(self, action: str, message: str, response_text: str = ""):
        self.action = action
        self.message = message
        self.response_text = response_text or ""
        super().__init__(f"{action}: {message}")


class BlueSoapClient:
    """Thin wrapper around Blue SOAP endpoints with logging + timeouts."""

    def __init__(self, api_key: str, ws_url: str, datasource_id: str = ""):
        self.api_key = api_key
        self.ws_url = ws_url
        self.datasource_id = datasource_id

    def call(
        self,
        action: str,
        payload: str,
        timeout: Optional[int] = None,
        transaction_id: Optional[str] = None,
    ) -> requests.Response:
        """POST a SOAP action and log outcome."""
        if timeout is None:
            timeout = ACTION_TIMEOUTS.get(action, 180)

        headers = {
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": f"http://tempuri.org/IBlueWebService/{action}",
        }
        started = time.monotonic()
        try:
            response = requests.post(
                self.ws_url,
                headers=headers,
                data=payload.encode("utf-8"),
                timeout=timeout,
            )
            elapsed = time.monotonic() - started
            success, message, _ = check_response(response, action)
            log_soap_call(
                action=action,
                datasource_id=self.datasource_id,
                transaction_id=transaction_id,
                http_status=response.status_code,
                elapsed_s=elapsed,
                success=success,
                message=message,
                response_snippet=response.text if not success else "",
            )
            return response
        except requests.exceptions.Timeout:
            elapsed = time.monotonic() - started
            log_soap_call(
                action=action,
                datasource_id=self.datasource_id,
                transaction_id=transaction_id,
                http_status=None,
                elapsed_s=elapsed,
                success=False,
                message=f"Timeout after {timeout}s",
            )
            raise
        except requests.exceptions.RequestException as exc:
            elapsed = time.monotonic() - started
            log_soap_call(
                action=action,
                datasource_id=self.datasource_id,
                transaction_id=transaction_id,
                http_status=None,
                elapsed_s=elapsed,
                success=False,
                message=str(exc),
            )
            raise

    def discover_block_name(self, datasource_id: Optional[str] = None) -> Optional[str]:
        ds_id = datasource_id or self.datasource_id
        payload = soap_get_datablock_info(self.api_key, ds_id)
        response = self.call("GetDataBlockInformation", payload)
        if response.status_code != 200:
            return None
        return extract_block_name(response.text)

    def cancel_stale_import(self) -> bool:
        """Cancel any in-progress import. Returns True if one was cancelled."""
        payload = soap_get_current_process(self.api_key)
        try:
            response = self.call("GetCurrentImportingDataSourceProcess", payload)
        except Exception:
            return False

        if response.status_code != 200:
            return False

        stale_tid = extract_value(response.text, "TransactionID")
        progress = extract_value(response.text, "ProgressStatus")
        logger.info(
            "stale_check datasource=%s transaction_id=%s progress=%s",
            self.datasource_id, stale_tid, progress,
        )
        if not stale_tid or stale_tid.strip() == "0":
            return False

        self.cancel_import(stale_tid)
        time.sleep(2)
        return True

    def register_import(self, datasource_id: Optional[str] = None) -> str:
        ds_id = datasource_id or self.datasource_id
        payload = soap_register_import(self.api_key, ds_id)
        response = self.call("RegisterImport", payload)
        success, message, _ = check_response(response, "RegisterImport")
        if not success and message:
            raise BlueSoapError("RegisterImport", message, response.text)

        tid = extract_value(response.text, "TransactionID")
        if not tid or tid.strip() == "0":
            raise BlueSoapError(
                "RegisterImport",
                "Could not get transaction ID (got 0 or empty)",
                response.text,
            )
        return tid

    def push_batch(
        self,
        transaction_id: str,
        block_name: str,
        columns: list,
        rows: list,
    ) -> None:
        payload = soap_push_data(
            self.api_key, transaction_id, block_name, columns, rows
        )
        response = self.call(
            "PushObjectDataV2", payload, transaction_id=transaction_id
        )
        success, message, _ = check_response(response, "PushObjectDataV2")
        if not success:
            raise BlueSoapError("PushObjectDataV2", message, response.text)

    def prepare_finalize(self, transaction_id: str) -> None:
        payload = soap_prepare_finalize(self.api_key, transaction_id)
        response = self.call(
            ACTION_PREPARE,
            payload,
            timeout=TIMEOUT_PREPARE,
            transaction_id=transaction_id,
        )
        success, message, _ = check_response(response, ACTION_PREPARE)
        if not success:
            raise BlueSoapError(ACTION_PREPARE, message, response.text)

    def finalize_import(self, transaction_id: str) -> None:
        payload = soap_finalize_import(self.api_key, transaction_id)
        response = self.call(
            "FinalizeImport",
            payload,
            timeout=TIMEOUT_FINALIZE,
            transaction_id=transaction_id,
        )
        success, message, _ = check_response(response, "FinalizeImport")
        if not success:
            raise BlueSoapError("FinalizeImport", message, response.text)

    def cancel_import(self, transaction_id: str) -> None:
        """Best-effort cancel — never raises."""
        try:
            payload = soap_cancel_import(self.api_key, transaction_id)
            self.call(
                "CancelImport",
                payload,
                timeout=TIMEOUT_CANCEL,
                transaction_id=transaction_id,
            )
        except Exception as exc:
            logger.warning(
                "cancel_import failed transaction_id=%s err=%s",
                transaction_id, exc,
            )

    def validate_api_key(self) -> Tuple[bool, str]:
        """Lightweight API key check via GetDataBlockInformation on Data144."""
        try:
            block = self.discover_block_name("Data144")
            if block:
                return True, "API key is valid"
            # Some responses don't include block but do include VALID_APIKEY
            payload = soap_get_datablock_info(self.api_key, "Data144")
            response = self.call("GetDataBlockInformation", payload)
            if "INVALID_APIKEY" in response.text:
                return False, "Invalid API Key"
            if "VALID_APIKEY" in response.text:
                return True, "API key is valid"
            return False, "Could not validate API key"
        except requests.exceptions.Timeout:
            return False, "Connection timeout"
        except requests.exceptions.ConnectionError:
            return False, "Connection error"
        except Exception as exc:
            return False, f"Error: {exc}"
