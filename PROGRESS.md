# UK TCE Admin - Development Progress

This document tracks the development progress of the TCE Admin System.

## Phase 1: Admin Management ✅ COMPLETE

- [x] Flask application setup with factory pattern
- [x] SQLAlchemy database models (Admin, College, Department)
- [x] Super admin authentication with fallback credentials
- [x] Admin CRUD operations (create, read, update, delete)
- [x] Role-based access control (Super Admin, College Admin, Dept Admin)
- [x] Primary contact designation per college
- [x] Access flags: Dashboard, Static Reports, Question Bank
- [x] CSV import/export for bulk admin management
- [x] UK branding with brand colors
- [x] UK logo in navigation bar

## Phase 2: Verification Reports ✅ COMPLETE

- [x] Course model with all required fields
- [x] Instructor model linked to courses
- [x] Student enrollment tracking
- [x] Course sync service from UKDIG CSV files
  - [x] Courses.csv parsing with correct column names (SECTION_BEGIN_DATE, etc.)
  - [x] Instructor_Course.csv parsing
  - [x] Users.csv lookup for instructor names
  - [x] Student_Course.csv for enrollment counts
- [x] Verification report list view
  - [x] Marked/Not Marked/Zero Enrollment status indicators
  - [x] College and department filtering
  - [x] Student count display
  - [x] Visual indicators (bold for questions, yellow glow for child questions)
  - [x] Usage hint text explaining navigation
- [x] Course detail view
  - [x] Course Start/End dates
  - [x] TCE Start/End dates
  - [x] Crosslisted ID field
  - [x] Instructor names and LinkBlue IDs
- [x] Export to CSV functionality
- [x] Command line sync: `python run.py --sync-courses`
- [x] Web interface sync for super admins

## Phase 3: Question Bank ✅ COMPLETE

- [x] Question Bank browser with hierarchical tree view
- [x] College → Department → Course → Section navigation
- [x] Term filtering (Fall 2025, Spring 2026, etc.)
- [x] Question types: Selection (multiple choice), Comment (open-ended)
- [x] Separate tabs for Course Questions vs Instructor Questions
- [x] Add existing questions from search
- [x] Create new questions with auto-placeholder assignment
- [x] Edit question text
- [x] Remove questions from units
- [x] Approval workflow for department admins
- [x] Pending changes review for college/super admins
- [x] Import QB.xlsx (Question Bank file)
- [x] Import QM.xlsx (Question Mapping file)
- [x] Export QB.xlsx and QM.xlsx
- [x] Visual indicators in tree view
  - [x] Bold text = unit has questions directly assigned
  - [x] Yellow glow on expand icon = child units have questions

## Phase 4: Azure AD Integration 🔜 PENDING

- [ ] Azure AD authentication setup
- [ ] SAML/OAuth integration
- [ ] Role mapping from Azure AD groups
- [ ] Session management with Azure tokens

---

## Recent Updates (January 2025)

### Session: Verification & Question Bank Improvements

1. **Verification Page Enhancements**
   - Made yellow highlight more visible (gradient background with glow effect)
   - Added usage hint text explaining bold text and yellow indicators

2. **Course Detail Page Fixes**
   - Fixed date display (was using wrong CSV column names)
   - Added Crosslisted ID field
   - Removed Email and Role columns from instructor table
   - Fixed instructor names by looking up from Users.csv

3. **Question Bank Improvements**
   - Enhanced yellow glow indicator for items with child questions
   - Added usage hint in welcome message
   - Auto-placeholder assignment for new questions

4. **UI/Branding**
   - Added UK logo (UKsmall.jpg) to navigation bar
   - Logo displays next to "UK TCE Admin" text

### Data Sync Fixes

- Fixed CSV column name mappings in `course_sync.py`:
  - `SECTION_BEGIN_DATE` → `course_start`
  - `SECTION_END_DATE` → `course_end`
  - `TCE_INVITE` → `tce_start`
  - `TCE_END_DATE` → `tce_end`
  - `TCE_R2` → `tce_reminder`

- Fixed instructor name lookup:
  - `Instructor_Course.csv` only has SECTION_KEY and USER_ID
  - Names are now looked up from `Users.csv` by matching USER_ID
  - Existing instructors are updated with names on re-sync

---

## File Reference

### Key Files Modified Recently

| File | Purpose |
| ---- | ------- |
| `app/services/course_sync.py` | UKDIG data sync with correct column mappings |
| `app/templates/base.html` | Navigation with UK logo |
| `app/templates/verification/list.html` | Verification page with improved indicators |
| `app/templates/verification/detail.html` | Course detail with dates and crosslisted ID |
| `app/templates/questions/browser.html` | Question bank with enhanced visual indicators |
| `app/static/images/UKsmall.jpg` | UK logo file |

### Data Files Required

| File | Source | Purpose |
| ---- | ------ | ------- |
| `datasources/Courses.csv` | UKDIG | Course/section data |
| `datasources/Instructor_Course.csv` | UKDIG | Instructor assignments |
| `datasources/Users.csv` | UKDIG | User names and emails |
| `datasources/Student_Course.csv` | UKDIG | Student enrollments |
| `datasources/QB.xlsx` | Explorance Blue | Question definitions (optional) |
| `datasources/QM.xlsx` | Explorance Blue | Question mappings (optional) |

---

## 2026-06-26: Blue Sync Pipeline + Admin UI Crash Fixes

**Bug A — Scheduled "HANA to Datasource" hangs / long failed durations (root cause before changes):**
- Scheduling: `scripts/daily_sync.sh` (cron `0 3 * * *`) invokes `hana_sync.py --scheduled` then `db_sync.py --scheduled`. Both create `DataSyncLog` rows typed `TYPE_HANA_TO_DATASOURCE` (see hana_sync.py:479 and db_sync.py:72). Possible duplicate cron lines or direct `hana_sync.py` crons on server caused starts 41s apart (03:00:03 success vs 03:00:44).
- Shell lock (`/tmp/tce-admin-sync.lock`) only covers full daily; direct invokes or races bypass it.
- No terminal status guarantee: success update (hana:580, db:112) and except handlers (hana:640, db:160) can be bypassed by `timeout --kill-after` SIGKILL (daily_sync.sh:104), HANA resource exhaustion on overlap, or commit failure in the `create_app()` context inside the script. Process records `process_pid`+`started_at` in summary (hana:490) but exits; later pageviews hit `_mark_stale_running_logs` (app/routes/settings.py:69) → `process_pid and not process_alive` (line 100) writes "Sync process exited without updating its log." and `completed_at=now`.
- Duration (models/settings.py:228) = completed-started therefore reports detection latency (624m, 1322m), not runtime.
- Fixes applied: duplicate guard in hana_sync.py:473 and db_sync.py:71 (abort/reduce overlap within 30m), record `detected_exit_at` + flag in stale marker.

**Bug B — "Datasource to Blue" UnboundLocalError 'time' (100% fail, 0s/0 records):**
- Root: `app/services/blue_sync.py:1007` had `import time` *inside* `if cancelled:` branch of `_import_datasource()`.
- Top-level `import time` at 33 exists, but any local binding makes the name local for the *entire function* at compile time.
- Early uses `time.monotonic()` (line 934 file_start, 1000, 1023, 987 etc.) before the late import line → `UnboundLocalError: cannot access local variable 'time' where it is not associated with a value` (exact Python 3.11+ wording in the prompt error blob).
- Standalone `scripts/push_users_to_blue.py:42` correctly imports only at top.
- Fix: removed the inner import (now uses module binding). Verified with `test_blue_unbound_repro.py` (simulates the old pattern raising, real module now safe).

**Bug C — /settings/blue-datasources TemplateRuntimeError "No filter named 'to_dict'":**
- Root: `app/templates/settings/blue_datasources.html:368` (in `{% block extra_js %}`): `const datasourcesData = {{ datasources | map('to_dict') | list | tojson }};`
- `map('to_dict')` requires a Jinja *filter* named `to_dict` registered on `app.jinja_env.filters`. None existed (no `app.jinja_env.filters['to_dict'] = ...` anywhere).
- `to_dict()` is a Python *method* on `BlueSyncDatasource` (app/models/settings.py:457) and other models.
- View (app/routes/settings.py:1220) passed raw queryset objects.
- Fix (Option 2): view now does `datasources_json = [d.to_dict() for d in datasources]` and passes it; template uses `{{ datasources_json | tojson }}`. Verified with `test_blue_datasources_template_minimal.py` (reproduces exact "No filter named 'to_dict'" pre-fix, clean post-fix).
- Grep for `map\(['"]to_dict` found only this one occurrence.

**Requirement D — Users last + 3min spacing + reuse of 3 bugfixes:**
- Ordering: `IMPORT_ORDER = ['courses', 'instructors', 'students', 'users']` (blue_sync.py:119); `_get_active_datasources` queries `order_by(import_order)` or falls back to list; explicit list paths sort too. Added hard enforcement in `push_all()` (post-605) that always moves legacy_key=='users' / Data144 to end of `to_push`.
- Spacing: `IMPORT_DELAY_SECONDS=300`; `_sleep_with_cancel_checks` + waits after each non-last in loop (722). Added clamp: non-Users always get `max(wait, MIN_NON_USERS_GAP_SECONDS=180)` at the two delay sites.
- 3 bug fixes (replicated, not by calling the .py script — integrated path is separate):
  1. Bug1 (FIRST/LAST → _1): `DATASOURCES['users']['column_renames']`, applied in `_load_csv` (1179).
  2. Bug2 (Prepare timeout): `ACTION_TIMEOUTS['PrepareDataToFinzalizeImportV2']=600` (343), used by `call_soap`.
  3. Bug3 (drop HASH): not present in expected `columns` for users, so `_load_csv` omits it (same effect as standalone `load_users_csv`).
- Manual "Push Selected" (routes/settings.py:910, sync_logs.html form) and scheduled via `blue_sync_cli.py` + daily all go through `push_all`, so now respect order/spacing. Errors are caught per-datasource so a stuck prior won't block users forever (proceed to next including users).
- `scripts/push_users_to_blue.py` left unchanged (per instructions). Tests: `test_requirement_d.py` asserts last+gap+fix presence.
- Policy implemented: continue-on-error + force-users-last.

**Verification performed (local, no HANA/Blue creds):**
- `python3 test_blue_unbound_repro.py` (Bug B)
- `python3 test_blue_datasources_template_minimal.py` (Bug C)
- `python3 test_requirement_d.py` (D)
- py_compile on all edited .py
- Manual review of all call sites, templates, models, and daily_sync flow.

All deterministic bugs (B+C) fully fixed. Bug A root-caused + guarded + diagnosed (cron/lock on server). Requirement D now explicitly enforced + documented.

