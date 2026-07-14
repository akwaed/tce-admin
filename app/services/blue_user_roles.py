"""Blue USER role helpers (Explorance Blue BLUE_ROLE).

Super admins in TCE Admin must receive BLUE_ROLE=528 in Users.csv so Blue
grants them the super-admin user type. HANA typically emits 23 (faculty/staff).

The set of LinkBlues is resolved dynamically from the ``admins`` table
(``role == 'super_admin'``), with an optional ``SUPER_ADMIN_LINKBLUES`` env
override for emergency/extra IDs. The local fallback account ``tceadmin`` is
usually not present in Users.csv and is simply skipped when applying roles.
"""
from __future__ import annotations

import csv
import logging
import os
from pathlib import Path
from typing import Iterable, Optional, Set

logger = logging.getLogger(__name__)

# Explorance Blue numerical user roles (as stored in HANA Users.csv).
SUPER_ADMIN_BLUE_ROLE = "528"
DEFAULT_STAFF_BLUE_ROLE = "23"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_USERS_CSV = PROJECT_ROOT / "datasources" / "Users.csv"


def _normalize_linkblue(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def get_super_admin_linkblues(
    *,
    include_inactive: bool = False,
    extra: Optional[Iterable[str]] = None,
) -> Set[str]:
    """Return uppercase LinkBlue IDs that should receive BLUE_ROLE=528.

    Sources (union):
      1. Active (or all) Admin rows with role ``super_admin``
      2. Comma-separated ``SUPER_ADMIN_LINKBLUES`` env var
      3. Optional ``extra`` iterable passed by the caller
    """
    ids: Set[str] = set()

    # 1) App DB
    try:
        from flask import has_app_context

        if has_app_context():
            ids |= _query_super_admin_linkblues(include_inactive=include_inactive)
        else:
            ids |= _query_super_admin_linkblues_with_app(
                include_inactive=include_inactive
            )
    except Exception as exc:
        logger.warning("super_admin_linkblue_db_lookup_failed err=%s", exc)

    # 2) Env override / supplement
    env_raw = os.environ.get("SUPER_ADMIN_LINKBLUES", "") or ""
    for part in env_raw.split(","):
        n = _normalize_linkblue(part)
        if n:
            ids.add(n)

    # 3) Explicit extras
    if extra:
        for part in extra:
            n = _normalize_linkblue(part)
            if n:
                ids.add(n)

    return ids


def _query_super_admin_linkblues(*, include_inactive: bool) -> Set[str]:
    from app.models.admin import Admin

    q = Admin.query.filter_by(role="super_admin")
    if not include_inactive:
        q = q.filter_by(is_active=True)
    return {
        _normalize_linkblue(a.linkblue)
        for a in q.all()
        if _normalize_linkblue(a.linkblue)
    }


def _query_super_admin_linkblues_with_app(*, include_inactive: bool) -> Set[str]:
    """Create a short-lived app context when hana_sync runs outside Flask."""
    from app import create_app

    app = create_app(os.environ.get("FLASK_ENV", "production"))
    with app.app_context():
        return _query_super_admin_linkblues(include_inactive=include_inactive)


def apply_blue_roles_to_users_csv(
    users_csv_path: Optional[Path | str] = None,
    *,
    super_admin_ids: Optional[Iterable[str]] = None,
    demote_former: bool = True,
    demote_role: str = DEFAULT_STAFF_BLUE_ROLE,
) -> dict:
    """Set BLUE_ROLE in Users.csv for current super admins.

    For each row whose USER_ID is in the super-admin set, set BLUE_ROLE to 528.
    If ``demote_former`` is True, any row currently at 528 that is *not* in the
    super-admin set is set back to ``demote_role`` (default 23).

    Returns a small stats dict for logging.
    """
    path = Path(users_csv_path) if users_csv_path else DEFAULT_USERS_CSV
    stats = {
        "path": str(path),
        "promoted": [],
        "demoted": [],
        "missing": [],
        "unchanged_promotions": 0,
        "rows_written": 0,
    }

    if not path.exists():
        logger.warning("users_csv_missing path=%s", path)
        return stats

    if super_admin_ids is None:
        admin_ids = get_super_admin_linkblues()
    else:
        admin_ids = {_normalize_linkblue(x) for x in super_admin_ids if _normalize_linkblue(x)}

    temp = path.with_suffix(".csv.tmp")
    seen: Set[str] = set()

    with path.open("r", newline="", encoding="utf-8", errors="replace") as infile, \
         temp.open("w", newline="", encoding="utf-8") as outfile:
        reader = csv.DictReader(infile)
        fieldnames = list(reader.fieldnames or [])
        if "USER_ID" not in fieldnames or "BLUE_ROLE" not in fieldnames:
            logger.warning(
                "users_csv_missing_columns path=%s fields=%s", path, fieldnames
            )
            temp.unlink(missing_ok=True)
            return stats

        writer = csv.DictWriter(outfile, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        for row in reader:
            uid = _normalize_linkblue(row.get("USER_ID"))
            role = (row.get("BLUE_ROLE") or "").strip()

            if uid in admin_ids:
                seen.add(uid)
                if role != SUPER_ADMIN_BLUE_ROLE:
                    row["BLUE_ROLE"] = SUPER_ADMIN_BLUE_ROLE
                    stats["promoted"].append(uid)
                else:
                    stats["unchanged_promotions"] += 1
            elif demote_former and role == SUPER_ADMIN_BLUE_ROLE:
                row["BLUE_ROLE"] = demote_role
                stats["demoted"].append(uid)

            writer.writerow(row)
            stats["rows_written"] += 1

    temp.replace(path)
    stats["missing"] = sorted(admin_ids - seen)
    logger.info(
        "blue_roles_applied path=%s promoted=%s demoted=%s missing=%s",
        path,
        stats["promoted"],
        stats["demoted"],
        stats["missing"],
    )
    return stats


def set_user_blue_role(
    linkblue: str,
    blue_role: str,
    users_csv_path: Optional[Path | str] = None,
) -> bool:
    """Set BLUE_ROLE for a single USER_ID. Returns True if a row was updated."""
    path = Path(users_csv_path) if users_csv_path else DEFAULT_USERS_CSV
    uid = _normalize_linkblue(linkblue)
    if not uid or not path.exists():
        return False

    temp = path.with_suffix(".csv.tmp")
    updated = False

    with path.open("r", newline="", encoding="utf-8", errors="replace") as infile, \
         temp.open("w", newline="", encoding="utf-8") as outfile:
        reader = csv.DictReader(infile)
        fieldnames = list(reader.fieldnames or [])
        if "USER_ID" not in fieldnames or "BLUE_ROLE" not in fieldnames:
            temp.unlink(missing_ok=True)
            return False

        writer = csv.DictWriter(outfile, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in reader:
            if _normalize_linkblue(row.get("USER_ID")) == uid:
                if (row.get("BLUE_ROLE") or "").strip() != str(blue_role):
                    row["BLUE_ROLE"] = str(blue_role)
                    updated = True
            writer.writerow(row)

    temp.replace(path)
    return updated
