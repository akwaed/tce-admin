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
