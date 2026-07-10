"""
Compatibility shim — Blue push lives in app.services.blue_push.

All previous imports of app.services.blue_sync continue to work:

    from app.services.blue_sync import get_blue_sync_service, get_blue_sync_progress
    from app.services.blue_sync import BlueSyncService, DATASOURCES, IMPORT_ORDER
"""
from app.services.blue_push import (  # noqa: F401
    DEFAULT_DATASOURCES,
    DEFAULT_IMPORT_ORDER,
    DEFAULT_WS_URL,
    MIN_NON_USERS_GAP_SECONDS,
    BluePushOrchestrator,
    BlueSyncService,
    DatasourceConfig,
    get_blue_sync_progress,
    get_blue_sync_service,
    load_datasource_configs,
    push_datasource,
    resolve_api_credentials,
)
from app.services.blue_push.config import DEFAULT_BATCH_SIZE

# Legacy names expected by older call sites / tests
DEFAULT_BLUE_WS_URL = DEFAULT_WS_URL
BATCH_SIZE = DEFAULT_BATCH_SIZE
IMPORT_DELAY_SECONDS = 300
IMPORT_ORDER = DEFAULT_IMPORT_ORDER

# Shape expected by code that iterated DATASOURCES[key]['id'] etc.
DATASOURCES = {
    key: {
        "id": cfg.datasource_id,
        "name": cfg.display_name,
        "csv_file": cfg.csv_file,
        "block_name": cfg.block_name,
        "columns": list(cfg.columns),
        "required": list(cfg.required_columns),
        "column_renames": dict(cfg.column_map),
        "batch_size": cfg.batch_size,
    }
    for key, cfg in DEFAULT_DATASOURCES.items()
}

__all__ = [
    "BATCH_SIZE",
    "BluePushOrchestrator",
    "BlueSyncService",
    "DATASOURCES",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_BLUE_WS_URL",
    "DEFAULT_DATASOURCES",
    "DEFAULT_IMPORT_ORDER",
    "DEFAULT_WS_URL",
    "DatasourceConfig",
    "IMPORT_DELAY_SECONDS",
    "IMPORT_ORDER",
    "MIN_NON_USERS_GAP_SECONDS",
    "get_blue_sync_progress",
    "get_blue_sync_service",
    "load_datasource_configs",
    "push_datasource",
    "resolve_api_credentials",
]
