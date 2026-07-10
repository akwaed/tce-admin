"""Datasource configuration for Explorance Blue push.

Adding a new datasource is a config entry here (and optionally a DB row
in ``blue_sync_datasources``). Push logic itself does not need to change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


DEFAULT_WS_URL = "https://my-uky-ws-bc.bluera.com/BlueWebService.svc/file"
DEFAULT_BATCH_SIZE = 500

# SOAP timeouts (seconds) — Prepare must be 600s (Bug 2)
TIMEOUT_REGISTER = 30
TIMEOUT_PUSH_BATCH = 180
TIMEOUT_PREPARE = 600
TIMEOUT_FINALIZE = 300
TIMEOUT_CANCEL = 30
TIMEOUT_DISCOVER = 30
TIMEOUT_STALE_CHECK = 30

# Minimum gap between *starts* of non-Users datasource pushes
MIN_NON_USERS_GAP_SECONDS = 180

# Dropped from every CSV before building SOAP payloads (Bug 3)
DROP_COLUMNS = frozenset({"HASH"})


@dataclass(frozen=True)
class DatasourceConfig:
    """Immutable config for one Blue datasource push target."""

    key: str                          # legacy key: courses, instructors, ...
    datasource_id: str                # Blue ID: Data161, ...
    display_name: str
    csv_file: str
    columns: List[str]                # Blue column names, output order
    required_columns: List[str] = field(default_factory=list)
    column_map: Dict[str, str] = field(default_factory=dict)  # CSV → Blue
    block_name: Optional[str] = None  # None = discover via GetDataBlockInformation
    batch_size: int = DEFAULT_BATCH_SIZE
    is_users: bool = False            # Users always pushed last
    wait_after_seconds: int = 300     # delay after this push (if used)


# ---------------------------------------------------------------------------
# Hardcoded defaults — used when DB registry is empty / unavailable.
# Datasource IDs confirmed from existing integration + prepare_sync_schema.py.
# ---------------------------------------------------------------------------

DEFAULT_DATASOURCES: Dict[str, DatasourceConfig] = {
    "courses": DatasourceConfig(
        key="courses",
        datasource_id="Data161",
        display_name="Courses",
        csv_file="Courses.csv",
        block_name="23_Courses",
        columns=[
            "SECTION_KEY", "TITLE", "CANVAS_SIS_ID", "CRS_SECTION", "PREFIX",
            "CLASS", "CLASS_ID", "SECTION", "SECTION_ID", "ACADEMIC_YEAR",
            "ACADEMIC_TERM_ID", "ACADEMIC_TERM", "SECTION_TITLE",
            "SECTION_BEGIN_DATE", "SECTION_END_DATE", "SECTION_LENGTH_DAYS",
            "TCE_INVITE", "TCE_R1", "TCE_R2", "TCE_END_DATE", "TCE_REPORT_DATE",
            "CLASS_DEPARTMENT", "CLASS_DEPARTMENT_ID", "CLASS_COLLEGE",
            "CLASS_COLLEGE_SHORT", "CLASS_LEVEL", "IS_CROSSLISTED",
            "CROSSLISTED_ID", "DISTANCE_LEARNING", "IS_UK_CORE", "UK_CORE_TYPE",
            "SPEC_TYPE",
        ],
        required_columns=["SECTION_KEY", "TITLE"],
        batch_size=DEFAULT_BATCH_SIZE,
    ),
    "instructors": DatasourceConfig(
        key="instructors",
        datasource_id="Data162",
        display_name="Course Instructors",
        csv_file="Instructor_Course.csv",
        # Live CSV currently only has SECTION_KEY, USER_ID — loader keeps
        # only columns present in the file.
        columns=["SECTION_KEY", "USER_ID", "FIRST_NAME", "LAST_NAME", "EMAIL"],
        required_columns=["SECTION_KEY", "USER_ID"],
        batch_size=DEFAULT_BATCH_SIZE,
    ),
    "students": DatasourceConfig(
        key="students",
        datasource_id="Data163",
        display_name="Course Students",
        csv_file="Student_Course.csv",
        columns=["SECTION_KEY", "USER_ID"],
        required_columns=["SECTION_KEY", "USER_ID"],
        batch_size=DEFAULT_BATCH_SIZE,
    ),
    "users": DatasourceConfig(
        key="users",
        datasource_id="Data144",
        display_name="Users",
        csv_file="Users.csv",
        # Blue expects FIRSTNAME_1 / LASTNAME_1 (Bug 1 fix)
        columns=["USER_ID", "FIRSTNAME_1", "LASTNAME_1", "EMAIL", "SECONDARY_EMAIL"],
        required_columns=["USER_ID", "FIRSTNAME_1", "LASTNAME_1", "EMAIL"],
        column_map={
            "USER_ID": "USER_ID",
            "FIRSTNAME": "FIRSTNAME_1",
            "LASTNAME": "LASTNAME_1",
            "EMAIL": "EMAIL",
            "SECONDARY_EMAIL": "SECONDARY_EMAIL",
        },
        batch_size=500,
        is_users=True,
    ),
}

# Non-Users first (import order), Users always last regardless of this list
DEFAULT_IMPORT_ORDER = ["courses", "instructors", "students", "users"]
