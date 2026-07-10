#!/usr/bin/env python3
"""
Standalone Blue datasource push CLI (rebuild entry point).

Usage:
    python scripts/blue_sync_cli.py --dry-run
    python scripts/blue_sync_cli.py --dry-run --datasource courses
    python scripts/blue_sync_cli.py --datasource Data161 --datasource Data144
    python scripts/blue_sync_cli.py --scheduled

Flags:
    --dry-run         Validate CSVs, print column maps / samples; no SOAP calls.
    --datasource ID   Push only this datasource (repeat for multiple).
                      Accepts ID (Data161), legacy key (courses), or name.
    --scheduled       Create a DataSyncLog entry visible in the UI.
    --test-rows N     Limit each datasource to first N rows (live push only).
    --skip-gaps       Skip the 3-minute inter-datasource start gaps.

Exit codes:
    0  all pushes succeeded (or dry-run completed)
    1  one or more pushes failed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Push datasource CSV files to Explorance Blue."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate CSVs without any SOAP calls to Blue.",
    )
    parser.add_argument(
        "--datasource",
        action="append",
        dest="datasources",
        help="Push only this datasource (repeat for multiple). "
        "Use datasource ID (e.g. Data161) or legacy key (e.g. courses).",
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Create a DataSyncLog entry for the UI.",
    )
    parser.add_argument(
        "--datasources-path",
        default=str(PROJECT_ROOT / "datasources"),
        help="Path to the datasources directory.",
    )
    parser.add_argument(
        "--test-rows",
        type=int,
        default=None,
        help="Only push the first N rows per datasource.",
    )
    parser.add_argument(
        "--skip-gaps",
        action="store_true",
        help="Skip the 3-minute gap between non-Users datasource starts.",
    )
    args = parser.parse_args()

    # Offline dry-run: no Flask app, no DB, no SOAP — pure CSV validation.
    if args.dry_run:
        return _run_offline_dry_run(
            datasources_path=args.datasources_path,
            datasource_keys=args.datasources,
            test_rows=args.test_rows,
        )

    from app import create_app

    app = create_app(os.environ.get("FLASK_ENV", "production"))
    with app.app_context():
        from app.services.blue_push import get_blue_sync_service

        service = get_blue_sync_service(args.datasources_path)

        try:
            result = service.push_all(
                datasources=args.datasources,
                dry_run=False,
                trigger_type="scheduled" if args.scheduled else "manual",
                triggered_by=None,
                test_rows=args.test_rows,
                skip_gaps=args.skip_gaps,
            )

            print(json.dumps({
                "success": result.get("success", False),
                "sync_log_id": result.get("sync_log_id"),
                "results": result.get("results", {}),
                "stats": result.get("stats", {}),
                "errors": result.get("errors", []),
            }, default=str))

            return 0 if result.get("success") else 1

        except Exception as e:
            import traceback
            traceback.print_exc()
            print(json.dumps({
                "success": False,
                "error": str(e),
                "errors": [str(e)],
            }))
            return 1


def _run_offline_dry_run(
    datasources_path: str,
    datasource_keys=None,
    test_rows=None,
) -> int:
    """Validate CSVs + column maps without Flask/DB/SOAP."""
    from app.services.blue_push.config import (
        DEFAULT_DATASOURCES,
        DEFAULT_IMPORT_ORDER,
        MIN_NON_USERS_GAP_SECONDS,
    )
    from app.services.blue_push.csv_loader import (
        load_datasource_csv,
        resolve_csv_path,
        sample_rows,
    )
    from app.services.blue_push.logging_setup import get_logger

    logger = get_logger()
    configs = []
    order = list(DEFAULT_IMPORT_ORDER)

    if datasource_keys:
        wanted = {k.lower() for k in datasource_keys}
        for key in DEFAULT_IMPORT_ORDER:
            cfg = DEFAULT_DATASOURCES[key]
            if (
                key.lower() in wanted
                or cfg.datasource_id.lower() in wanted
                or cfg.display_name.lower() in wanted
            ):
                configs.append(cfg)
        # Users last
        users = [c for c in configs if c.is_users]
        others = [c for c in configs if not c.is_users]
        configs = others + users
    else:
        configs = [DEFAULT_DATASOURCES[k] for k in DEFAULT_IMPORT_ORDER]

    print("=" * 60)
    print("BLUE PUSH DRY RUN (offline — no SOAP, no DB)")
    print(f"datasources_path: {datasources_path}")
    print(f"order: {[c.key for c in configs]}")
    print(f"min_non_users_gap_seconds: {MIN_NON_USERS_GAP_SECONDS}")
    print("=" * 60)

    all_ok = True
    details = {}
    for cfg in configs:
        path = resolve_csv_path(datasources_path, cfg.csv_file)
        try:
            loaded = load_datasource_csv(path, cfg, test_rows=test_rows)
            sample = sample_rows(loaded.columns, loaded.rows, 5)
            details[cfg.key] = {
                "display_name": cfg.display_name,
                "datasource_id": cfg.datasource_id,
                "total_rows": loaded.total_rows,
                "columns": loaded.columns,
                "dropped_columns": loaded.dropped_columns,
                "column_map": dict(cfg.column_map),
                "batch_size": cfg.batch_size,
                "block_name": cfg.block_name or "(auto-discover)",
                "is_users": cfg.is_users,
                "success": True,
                "sample": sample,
            }
            print(f"\n[{cfg.key}] {cfg.display_name} → {cfg.datasource_id}")
            print(f"  csv:      {path}")
            print(f"  rows:     {loaded.total_rows:,}")
            print(f"  columns:  {', '.join(loaded.columns)}")
            if loaded.dropped_columns:
                print(f"  dropped:  {', '.join(loaded.dropped_columns)}")
            if cfg.column_map:
                print(f"  remaps:   {cfg.column_map}")
            print(f"  batch:    {cfg.batch_size}")
            print(f"  block:    {cfg.block_name or '(auto-discover)'}")
            if sample:
                print("  sample (as would be sent to Blue):")
                print("    " + " | ".join(loaded.columns))
                for row in sample[:3]:
                    print("    " + " | ".join(
                        str(row.get(c, ""))[:40] for c in loaded.columns
                    ))
            logger.info(
                "dry_run_ok key=%s rows=%s cols=%s",
                cfg.key, loaded.total_rows, loaded.columns,
            )
        except Exception as exc:
            all_ok = False
            details[cfg.key] = {
                "display_name": cfg.display_name,
                "datasource_id": cfg.datasource_id,
                "success": False,
                "error": str(exc),
            }
            print(f"\n[{cfg.key}] FAILED: {exc}")
            logger.error("dry_run_failed key=%s err=%s", cfg.key, exc)

    # Ordering assertions for the walkthrough
    keys = list(details.keys())
    if "users" in keys and keys[-1] != "users":
        all_ok = False
        print("\n[ERROR] Users is not last in push order!")
    else:
        print("\n[OK] Users is last (or not selected).")

    print("\n" + "=" * 60)
    print(json.dumps({
        "success": all_ok,
        "dry_run": True,
        "order": keys,
        "soap_calls": 0,
        "details_summary": {
            k: {
                "datasource_id": v.get("datasource_id"),
                "rows": v.get("total_rows"),
                "columns": v.get("columns"),
                "dropped": v.get("dropped_columns"),
                "success": v.get("success"),
                "error": v.get("error"),
            }
            for k, v in details.items()
        },
    }, default=str, indent=2))

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
