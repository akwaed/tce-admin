"""Structured file logging for Blue push operations."""
from __future__ import annotations

import logging
import os
from pathlib import Path


LOG_NAME = "blue_push"
_CONFIGURED = False


def get_logger() -> logging.Logger:
    """Return the blue_push logger, configuring file handler once."""
    global _CONFIGURED
    logger = logging.getLogger(LOG_NAME)
    if _CONFIGURED:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = True  # also hit Flask / root handlers

    # Prefer project logs/ directory
    project_root = Path(__file__).resolve().parents[3]
    log_dir = project_root / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "blue_push.log"
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        # Avoid duplicate handlers on re-import
        if not any(
            isinstance(h, logging.FileHandler)
            and getattr(h, "baseFilename", None) == str(log_path)
            for h in logger.handlers
        ):
            logger.addHandler(fh)
    except OSError:
        # Fall back to stderr only
        if not logger.handlers:
            sh = logging.StreamHandler()
            sh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s"
            ))
            logger.addHandler(sh)

    _CONFIGURED = True
    return logger


def log_soap_call(
    action: str,
    datasource_id: str,
    transaction_id: str | None,
    http_status: int | None,
    elapsed_s: float,
    success: bool,
    message: str = "",
    response_snippet: str = "",
) -> None:
    """Log one SOAP call with the fields required for debugging."""
    logger = get_logger()
    level = logging.INFO if success else logging.ERROR
    snippet = (response_snippet or "")[:500]
    logger.log(
        level,
        "soap action=%s datasource=%s transaction_id=%s http_status=%s "
        "elapsed_s=%.2f success=%s message=%s response_snippet=%s",
        action,
        datasource_id,
        transaction_id or "-",
        http_status if http_status is not None else "-",
        elapsed_s,
        success,
        (message or "")[:300],
        snippet if not success else "",
    )
