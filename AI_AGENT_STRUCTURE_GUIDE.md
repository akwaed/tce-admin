# UK TCE Admin System - Complete App Structure & Documentation for AI Agents

**Purpose:** This document provides a detailed breakdown of the application architecture, database models, routes, services, and HTML templates. Use this as a reference guide when delegating development tasks to AI agents.

---

## Table of Contents
1. [Project Overview](#project-overview)
2. [Directory Structure](#directory-structure)
3. [Core Application Files](#core-application-files)
4. [Database Models](#database-models)
5. [Routes & Blueprints](#routes--blueprints)
6. [Services Layer](#services-layer)
7. [HTML Templates](#html-templates)
8. [Configuration & Environment](#configuration--environment)

---

## Project Overview

**Project Name:** UK TCE Admin System  
**Purpose:** Manage TCE administrators, verification reports, and question banks for University of Kentucky's Teacher Course Evaluation program  
**Stack:** Python/Flask, SQLAlchemy ORM, Jinja2 templates, SQLite/PostgreSQL  
**Authentication:** Fallback username/password (tceadmin) + Azure AD integration (coming soon)  
**Key Features:**
- Admin management with 3-tier role hierarchy
- Course data sync from CSV/XLSX files
- Verification reports with TCE status tracking
- Question bank browser with hierarchical view
- Approval workflow for question mappings

---

## Directory Structure

```
tce-admin/
├── app/                              # Main Flask application package
│   ├── __init__.py                  # App factory (create_app function)
│   ├── config.py                    # Configuration for dev/prod/test
│   │
│   ├── models/                      # SQLAlchemy ORM models
│   │   ├── __init__.py              # Model exports
│   │   ├── admin.py                 # Admin user model, authentication
│   │   ├── course.py                # Course, Department, College models
│   │   ├── question.py              # Question, QuestionBank models
│   │   ├── settings.py              # ApprovalRequest, QuestionChange models
│   │   └── sync_history.py          # SyncLog model
│   │
│   ├── routes/                      # Flask blueprints (URL routing)
│   │   ├── __init__.py              # Blueprint init
│   │   ├── auth.py                  # Login/logout endpoints
│   │   ├── main.py                  # Dashboard, home page
│   │   ├── admin.py                 # Admin CRUD operations
│   │   ├── verification.py          # Course verification & sync
│   │   ├── questions.py             # Question bank browser & API
│   │   ├── tracking.py              # Approval workflow tracking
│   │   ├── settings.py              # Settings, datasources, Blue sync
│   │   ├── reports.py               # Report generation & export
│   │   └── services_qb_service.py   # Question bank service (helper)
│   │
│   ├── services/                    # Business logic layer
│   │   ├── __init__.py
│   │   ├── course_sync.py           # UKDIG CSV sync logic
│   │   ├── blue_sync.py             # Explorance Blue API sync
│   │   ├── blue_discovery.py        # Blue datasource discovery
│   │   ├── backup_service.py        # Backup/restore utilities
│   │   └── sync_control.py          # Sync state management
│   │
│   ├── static/                      # Static assets
│   │   ├── css/                     # Stylesheets
│   │   ├── js/                      # JavaScript files
│   │   ├── images/                  # UK logo, icons
│   │   └── ...
│   │
│   └── templates/                   # Jinja2 HTML templates
│       ├── base.html                # Main layout template
│       ├── dashboard.html           # Home page/dashboard
│       ├── UKsmall.jpg              # UK branding logo
│       │
│       ├── auth/                    # Authentication templates
│       │   └── login.html           # Login form
│       │
│       ├── admin/                   # Admin management templates
│       │   ├── list.html            # List all admins
│       │   ├── add.html             # Add new admin form
│       │   ├── edit.html            # Edit admin form
│       │   ├── import.html          # CSV import form
│       │   ├── copy.html            # Copy admin utilities
│       │   ├── cleanup.html         # Cleanup operations
│       │   ├── fix_departments.html # Department assignment fix
│       │   └── course_assignments.html # Admin to course mapping
│       │
│       ├── verification/            # Verification report templates
│       │   ├── list.html            # Course verification list
│       │   ├── detail.html          # Course detail view
│       │   ├── sync.html            # Data sync interface
│       │   └── user_lookup.html     # User/instructor lookup
│       │
│       ├── questions/               # Question bank templates
│       │   ├── browser.html         # QB browser main interface
│       │   ├── import.html          # QB file import form
│       │   ├── pending.html         # Pending approvals (all)
│       │   └── pending_mine.html    # Pending approvals (user's)
│       │
│       ├── tracking/                # Approval tracking templates
│       │   └── ...                  # Change tracking interfaces
│       │
│       ├── settings/                # Settings & admin panel
│       │   ├── index.html           # Main settings page
│       │   ├── api_key.html         # API key management
│       │   ├── api_key_confirm.html # API key confirmation
│       │   ├── blue_datasources.html # Blue sync settings
│       │   ├── sync_logs.html       # Sync history/logs
│       │   ├── sync_log_detail.html # Detailed sync log view
│       │   ├── changes_index.html   # Change tracking index
│       │   ├── changes_detail.html  # Change detail view
│       │   ├── changes_entity.html  # Entity change view
│       │   ├── ws_url.html          # WebSocket URL config
│       │   └── _file_timeline.html  # Partial: file sync timeline
│       │
│       └── reports/                 # Report templates
│           └── ...
│
├── datasources/                     # CSV/XLSX input files
│   ├── Courses.csv                  # Course data (from UKDIG)
│   ├── Instructor_Course.csv        # Instructor mappings
│   ├── Student_Course.csv           # Student enrollment counts
│   ├── Users.csv                    # User directory (names/emails)
│   ├── QB.xlsx                      # Question Bank export (Blue)
│   └── QM.xlsx                      # Question Mapping export (Blue)
│
├── instance/                        # Auto-created instance folder
│   └── tce_admin.db                 # SQLite database (dev)
│
├── run.py                           # Application entry point
├── requirements.txt                 # Python dependencies
├── .env                             # Environment variables (not in Git)
└── README.md                        # Project documentation
```

---

## Core Application Files

### 1. **app/__init__.py** - App Factory

Creates and configures the Flask application.

**Key Functions:**
- `create_app(config_name='default')` - Application factory
- Database initialization
- Blueprint registration
- Login manager setup
- Context processor for UK branding

**Blueprints Registered:**
```
/                    → main_bp (dashboard, home)
/auth                → auth_bp (login/logout)
/admin               → admin_bp (admin CRUD)
/verification        → verification_bp (course reports)
/questions           → questions_bp (question bank)
/tracking            → tracking_bp (approval workflow)
/settings            → settings_bp (admin panel)
/reports             → reports_bp (export/reports)
```

### 2. **app/config.py** - Configuration

Environment-based configuration management.

**Key Classes:**
- `Config` - Base configuration
- `DevelopmentConfig` - DEBUG=True
- `ProductionConfig` - DEBUG=False, validates SECRET_KEY
- `TestingConfig` - In-memory SQLite

**Key Variables:**
```python
SECRET_KEY              # Flask session signing key
SUPER_ADMIN_USERNAME    # Default: 'tceadmin'
SUPER_ADMIN_PASSWORD    # Default: 'UK_TCE_2025!' (change in production)
DATABASE_URL            # Connection string (defaults to SQLite)
UK_BLUE, UK_WHITE, UK_BLACK  # Brand colors
AZURE_AD_*              # Azure AD OAuth2 config (env vars)
SESSION_TYPE            # Flask-Session type
ITEMS_PER_PAGE          # Pagination size
```

### 3. **run.py** - Entry Point

Standalone script to run the app or perform maintenance tasks.

**Commands:**
```bash
python run.py                          # Run dev server (http://127.0.0.1:5000)
python run.py --create-superadmin      # Create or reset super admin
python run.py --import-admins FILE.csv # Bulk import admins
python run.py --sync-courses [PATH]    # Sync from CSV files
python run.py --generate-sample [PATH] # Generate test data
python run.py --host 0.0.0.0 --port 5000  # Custom host/port
```

---

## Database Models

### 1. **app/models/admin.py** - User/Admin Model

**Admin Class:**
```python
class Admin(UserMixin, db.Model):
    id                      # Primary key (auto-increment)
    linkblue               # UK username (unique)
    email                  # Email address
    password_hash          # Bcrypt hashed password
    first_name             # First name
    last_name              # Last name
    role                   # 'super_admin' | 'college_admin' | 'dept_admin'
    college_code           # College code (e.g., 'EN', 'AS')
    department_id          # FK to Department (if dept_admin)
    contact_type           # 'College' | 'Department'
    is_primary_contact     # Boolean - designated contact for college/dept
    is_active              # Boolean - enable/disable account
    
    # Access flags
    has_dashboard_access   # Boolean
    has_static_report_access  # Boolean
    has_qb_access          # Boolean
    
    # Timestamps
    created_at             # DateTime
    updated_at             # DateTime
    last_login             # DateTime

# Key Methods:
- set_password(password)              # Hash and store password
- check_password(password)            # Verify password
- from_csv_row(row)                   # Create Admin from CSV
- has_role(required_role)             # Role check
- get_accessible_colleges()           # List colleges user can access
- get_accessible_departments()        # List departments user can access
```

### 2. **app/models/course.py** - Course Structure

**College Class:**
```python
class College(db.Model):
    id                  # Primary key
    code               # College code (e.g., 'EN', 'AS', 'MG')
    name               # College name
    # Relationships
    departments        # One-to-many: College → Departments
```

**Department Class:**
```python
class Department(db.Model):
    id                          # Primary key
    college_code               # FK to College
    name                       # Department name
    course_department_id       # UKDIG department identifier
    # Relationships
    college                    # FK to College
    courses                    # One-to-many: Department → Courses
    admins                     # Many-to-many: Admins for this dept
```

**Course Class:**
```python
class Course(db.Model):
    id                  # Primary key
    section_key         # Unique UKDIG section identifier
    course_code         # Course code (e.g., 'ACC 201')
    section_id          # Section number
    title               # Course title
    college_code        # FK to College
    department_id       # FK to Department
    
    # Dates
    start_date          # Course start
    end_date            # Course end
    tce_start           # Evaluation start
    tce_end             # Evaluation end
    
    crosslist_id        # Crosslist group identifier
    student_count       # Enrollment count
    marked_for_tce      # Boolean - instructor marked for eval
    
    # Relationships
    instructors         # Many-to-many: Course ↔ Instructors
    students            # Student enrollments count
    questions           # Question mappings for this course
```

**Instructor Class:**
```python
class Instructor(db.Model):
    id                  # Primary key
    linkblue           # UK username
    first_name         # First name
    last_name          # Last name
    email              # Email address
    # Relationships
    courses            # Many-to-many: Courses taught
```

### 3. **app/models/question.py** - Question Bank Models

**QuestionBank Class:**
```python
class QuestionBank(db.Model):
    id                  # Primary key
    question_id        # Unique question identifier
    question_type      # 'selection' | 'comment'
    question_text      # Question content
    category           # Question category/grouping
    created_at         # DateTime
```

**Question Class:**
```python
class Question(db.Model):
    id                      # Primary key
    section_id              # FK to Course/Section
    question_bank_id        # FK to QuestionBank
    question_type           # 'instructor' | 'course'
    sub_type                # 'selection' | 'comment'
    order_index             # Display order
    # Relationships
    course                  # FK to Course
    approvals               # Pending approval requests
```

### 4. **app/models/settings.py** - Approval & Change Tracking

**ApprovalRequest Class:**
```python
class ApprovalRequest(db.Model):
    id                      # Primary key
    request_type           # 'question_change' | 'question_delete'
    requester_id           # FK to Admin (who submitted)
    approver_id            # FK to Admin (who approves, NULL if pending)
    status                 # 'pending' | 'approved' | 'rejected'
    changes                # JSON blob of changes
    reason                 # Request description
    created_at             # DateTime
    approved_at            # DateTime (when approved)
```

**QuestionChange Class:**
```python
class QuestionChange(db.Model):
    id                      # Primary key
    approval_request_id    # FK to ApprovalRequest
    question_id            # FK to Question
    change_type            # 'add' | 'modify' | 'delete'
    old_value              # Previous value (JSON)
    new_value              # New value (JSON)
```

### 5. **app/models/sync_history.py** - Data Sync Tracking

**SyncLog Class:**
```python
class SyncLog(db.Model):
    id                      # Primary key
    sync_type              # 'csv' | 'blue' | 'manual'
    status                 # 'pending' | 'success' | 'failed'
    file_name              # Source file name
    
    # Counts
    colleges_created       # Number of colleges added
    colleges_updated       # Number of colleges modified
    departments_created
    departments_updated
    courses_created
    courses_updated
    instructors_created
    instructors_updated
    
    # Errors & Warnings
    errors                 # JSON array of error messages
    warnings               # JSON array of warnings
    
    # Timestamps
    started_at             # When sync began
    completed_at           # When sync finished
    duration_seconds       # Total time taken
```

---

## Routes & Blueprints

### Route Structure & Endpoints

#### **auth_bp** (`/auth`) - Authentication
```
GET  /auth/login                    # Display login form
POST /auth/login                    # Process login (redirect to dashboard)
GET  /auth/logout                   # Logout and redirect to login
GET  /auth/unauthorized             # 403 error page
```

**Template:** `auth/login.html`  
**Auth Guard:** Flask-Login (requires @login_required)

---

#### **main_bp** (`/`) - Dashboard & Home
```
GET  /                              # Main dashboard (requires login)
GET  /api/dashboard-stats           # Dashboard data (JSON)
```

**Template:** `dashboard.html`  
**Features:**
- Admin count by role
- Course sync status
- Recent approvals
- Quick action buttons
- UK branding

---

#### **admin_bp** (`/admin`) - Admin Management (Phase 1)
```
GET    /admin/                           # List all admins (paginated)
GET    /admin/<id>                       # View admin detail
GET    /admin/add                        # Add admin form
POST   /admin/add                        # Create new admin
GET    /admin/<id>/edit                  # Edit admin form
POST   /admin/<id>/edit                  # Update admin
POST   /admin/<id>/delete                # Delete admin
GET    /admin/import                     # CSV import form
POST   /admin/import                     # Process CSV import
GET    /admin/export                     # Export admins to CSV
GET    /admin/copy                       # Copy admin utility
POST   /admin/copy                       # Perform copy
GET    /admin/cleanup                    # Cleanup utilities
POST   /admin/cleanup                    # Execute cleanup
GET    /admin/fix-departments            # Fix department assignments
POST   /admin/fix-departments            # Apply fixes
GET    /admin/course-assignments         # Admin ↔ Course mapping
POST   /admin/course-assignments/save    # Save mappings
```

**Templates:**
- `admin/list.html` - Admin listing with filters/search
- `admin/add.html` - Create admin form (role-specific fields)
- `admin/edit.html` - Edit admin with validation
- `admin/import.html` - Bulk CSV import interface
- `admin/copy.html` - Copy/duplicate admin settings
- `admin/cleanup.html` - Cleanup & maintenance tools
- `admin/fix_departments.html` - Department reassignment
- `admin/course_assignments.html` - Admin-to-course mapping

**Permissions:**
- Super Admin: view all, create/edit any role, export/import
- College Admin (Primary): create/edit dept admins in their college
- Dept Admin: view only

---

#### **verification_bp** (`/verification`) - Course Reports (Phase 2)
```
GET    /verification/                    # Course list with filters
GET    /verification/<course_id>         # Course detail view
GET    /verification/sync                # Data sync interface
POST   /verification/sync                # Perform sync from CSV/upload
GET    /verification/export              # Export visible courses to CSV
GET    /verification/user-lookup         # User/instructor search
GET    /api/verification/courses         # Course data (JSON, filtered)
```

**Templates:**
- `verification/list.html` - Course verification with TCE status, filters by college/dept/term
- `verification/detail.html` - Single course details, instructor/student info
- `verification/sync.html` - CSV upload & sync interface
- `verification/user_lookup.html` - Search instructors/students

**Features:**
- Marked / Not Marked / Zero Enrollment status
- Course crosslisting info
- Student count, instructor names
- Date ranges (start/end, TCE start/end)
- CSV export with role-based filtering

---

#### **questions_bp** (`/questions`) - Question Bank Browser (Phase 3)
```
GET    /questions/                       # QB browser with term filter
GET    /questions/import                 # QB/QM file upload form
POST   /questions/import                 # Process QB/QM imports
GET    /questions/api/questions/<type>/<id>  # Get questions for unit (JSON)
GET    /questions/pending                # Pending approvals (all users)
GET    /questions/pending-mine           # Pending approvals (user's)
POST   /questions/approve/<request_id>   # Approve change request
POST   /questions/reject/<request_id>    # Reject change request
```

**Templates:**
- `questions/browser.html` - Hierarchical QB browser (College → Dept → Course → Section)
- `questions/import.html` - QB.xlsx & QM.xlsx file upload
- `questions/pending.html` - All pending approvals (super/college admin view)
- `questions/pending_mine.html` - User's own pending submissions

**Features:**
- Hierarchical collapsible tree view
- Term filtering
- Role-based scope (super sees all, college sees college, dept sees dept)
- Bold text = has questions
- Yellow expanders = children have questions
- Two tabs: Instructor Questions | Course Questions
- Each tab splits: Selection (multiple choice) | Comment (open-ended)
- Approval workflow: dept submits → college approves → super confirms

---

#### **tracking_bp** (`/tracking`) - Approval Workflow Tracking
```
GET    /tracking/                        # Approval request list
GET    /tracking/<request_id>            # Request detail
POST   /tracking/<request_id>/approve    # Approve request
POST   /tracking/<request_id>/reject     # Reject with reason
GET    /tracking/changes                 # View all changes made
```

**Templates:**
- Settings panel templates (see Settings Blueprint)

---

#### **settings_bp** (`/settings`) - Admin Panel & System Settings
```
GET    /settings/                        # Main settings page
GET    /settings/api-key                 # API key management
POST   /settings/api-key/generate        # Generate new API key
POST   /settings/api-key/revoke          # Revoke API key
GET    /settings/api-key-confirm         # Confirm API key action
GET    /settings/blue-datasources        # Blue integration settings
POST   /settings/blue-datasources/test   # Test Blue connection
POST   /settings/blue-datasources/sync   # Trigger Blue sync
GET    /settings/sync-logs               # View sync history
GET    /settings/sync-logs/<log_id>      # Sync log detail
GET    /settings/changes                 # Change tracking index
GET    /settings/changes/<entity_id>     # Entity change history
GET    /settings/ws-url                  # WebSocket URL config (for webhooks)
```

**Templates:**
- `settings/index.html` - Main settings hub (navigation)
- `settings/api_key.html` - API key display/regeneration
- `settings/api_key_confirm.html` - Confirmation modal
- `settings/blue_datasources.html` - Blue sync config & status
- `settings/sync_logs.html` - Paginated sync history with filters
- `settings/sync_log_detail.html` - Detailed log view with error breakdown
- `settings/changes_index.html` - Change tracking overview
- `settings/changes_detail.html` - Detailed change audit trail
- `settings/changes_entity.html` - Entity-specific change log
- `settings/ws_url.html` - Webhook URL configuration
- `settings/_file_timeline.html` - Partial template for file sync timeline

---

#### **reports_bp** (`/reports`) - Reports & Exports
```
GET    /reports/                         # Report dashboard
GET    /reports/admin-summary            # Admin summary report
GET    /reports/course-status            # Course TCE status report
POST   /reports/export-csv               # Export filtered data to CSV
```

---

## Services Layer

### **app/services/course_sync.py** - UKDIG CSV Import

**CourseSyncService Class:**
```python
class CourseSyncService:
    def __init__(self, datasources_path='./datasources')
    
    def sync_all(self)                  # Main sync orchestrator
    def load_courses_csv()              # Parse Courses.csv
    def load_instructors_csv()          # Parse Instructor_Course.csv
    def load_users_csv()                # Parse Users.csv (names/emails)
    def load_students_csv()             # Parse Student_Course.csv (counts)
    def create_or_update_colleges()     # Sync colleges
    def create_or_update_departments()  # Sync departments
    def create_or_update_courses()      # Sync courses & sections
    def create_or_update_instructors()  # Sync instructor records
    def associate_instructors()         # Link instructors to courses
    def count_students()                # Count enrollments per course
    def validate_data()                 # Pre-sync validation
    def write_sample_csvs()             # Generate test data
```

**Expected CSV Columns:**

*Courses.csv:*
```
SECTION_KEY, CLASS, SECTION_ID, SECTION_TITLE, CLASS_COLLEGE_SHORT,
CLASS_DEPARTMENT_ID, SECTION_BEGIN_DATE, SECTION_END_DATE, TCE_INVITE,
TCE_END_DATE, TCE_R2, CROSSLISTED_ID, ACADEMIC_TERM
```

*Instructor_Course.csv:*
```
SECTION_KEY, USER_ID
```

*Users.csv:*
```
USER_ID, FIRSTNAME, LASTNAME, EMAIL
```

*Student_Course.csv:*
```
SECTION_KEY, USER_ID
```

---

### **app/services/blue_sync.py** - Explorance Blue Integration

**BlueSyncService Class:**
```python
class BlueSyncService:
    def __init__(self, api_url, api_token)
    
    def discover_courses()              # Fetch courses from Blue
    def discover_questions()            # Fetch question definitions
    def sync_mappings()                 # Sync question mappings to courses
    def test_connection()               # Validate API credentials
    def get_course_questions()          # Get Q's for a specific course
    def upload_results()                # Send TCE results back to Blue
```

---

### **app/services/blue_discovery.py** - Blue Datasource Discovery

Helper class to discover and validate Blue datasources (files/URLs).

---

### **app/services/backup_service.py** - Database Backups

**BackupService Class:**
```python
def backup_database()                   # Create backup file
def restore_database(backup_file)       # Restore from backup
def list_backups()                      # List available backups
```

---

### **app/services/sync_control.py** - Sync State Management

Manages sync locks and prevents concurrent syncs.

```python
def acquire_sync_lock()                 # Prevent duplicate syncs
def release_sync_lock()                 # Release lock
def is_sync_in_progress()               # Check sync status
```

---

## HTML Templates

All templates use Jinja2 syntax and inherit from `base.html`.

### Base Template (`base.html`)

**Structure:**
```html
<!DOCTYPE html>
<html>
<head>
    <!-- UK Branding CSS -->
    <!-- Bootstrap 5 -->
    <!-- Custom CSS -->
</head>
<body>
    <!-- Navigation Bar -->
    <!-- UK Logo -->
    <!-- Role & User Info -->
    
    <!-- Main Content Area -->
    {% block content %}{% endblock %}
    
    <!-- Footer -->
    <!-- Scripts (Bootstrap, jQuery, custom JS) -->
</body>
</html>
```

**Available Variables (via context processor):**
- `current_user` - Logged-in Admin object
- `UK_BLUE` - Brand color (#0033A0)
- `UK_WHITE` - White (#FFFFFF)
- `UK_BLACK` - Black (#000000)
- `DATA_REFRESH_NOTE` - "Data is refreshed daily at 4:00 AM EST"

**Key Features:**
- Responsive Bootstrap navbar with UK logo
- User dropdown (profile, logout)
- Navigation menu (Dashboard, Admins, Verification, Questions, Settings)
- Breadcrumb trail
- Flash message display (success, error, warning, info)
- Role-based menu visibility

---

### Dashboard Template (`dashboard.html`)

**Purpose:** Home page landing showing system status.

**Sections:**
1. **Welcome Card** - Greeting with user role
2. **Quick Stats**
   - Total admins by role
   - Total courses
   - Last sync time
   - Pending approvals count
3. **Recent Activity**
   - Last 5 syncs with status
   - Pending approvals (user's)
4. **Quick Actions** (buttons)
   - Sync Data
   - Browse Questions
   - Manage Admins
   - View Reports
5. **Access Restrictions Notice** - Role-based message

**Data Source:** `/api/dashboard-stats` (AJAX)

---

### Admin Templates (`admin/`)

#### **admin/list.html** - Admin Directory

**Features:**
- Sortable table: Linkblue, Name, Email, Role, College, Department, Status
- Filters: Role, College, Department, Status
- Search by name/email
- Pagination
- Action buttons: View, Edit, Delete, Copy
- Bulk import/export buttons
- Create new admin button

**Columns:**
```
Linkblue | Name | Email | Role | College | Department | Status | Actions
```

**Permissions:**
- Super Admin: see all, all actions
- College Admin: see college admins & dept admins in college, add dept admins
- Dept Admin: view only, filter to department

---

#### **admin/add.html** - Create Admin Form

**Form Fields:**
- Linkblue (required, unique)
- Email (required, email format)
- First Name (required)
- Last Name (required)
- Role (required) - dropdown: Super Admin | College Admin | Department Admin
- College (conditional) - shown if role = College Admin or Dept Admin
- Department (conditional) - shown if role = Dept Admin
- Primary Contact (checkbox)
- Access Flags:
  - Has Dashboard Access
  - Has Static Report Access
  - Has Question Bank Access
- Status (Active/Inactive)
- Send Welcome Email (checkbox)

**Validation:**
- Linkblue: unique, lowercase, 3-20 chars
- Email: valid format, optional duplicate for different user
- College/Dept: required based on role

**Submit:** Create Admin → Redirect to admin/list

---

#### **admin/edit.html** - Edit Admin Form

**Same fields as add.html, plus:**
- Read-only linkblue display
- Last login timestamp
- Account creation date
- Reset password option
- Recent activity log (last 5 actions)
- Delete button (with confirmation)

**Permissions:**
- Super Admin: edit any admin, change role/college/dept
- College Admin (Primary): edit dept admins in college, can't change own role
- Dept Admin: cannot edit (view-only)

---

#### **admin/import.html** - CSV Import

**Features:**
- File upload (CSV only)
- Expected columns helper (collapsible reference)
- Preview first 5 rows before import
- Dry-run option (no-commit preview)
- Import mode: Create Only | Update Existing | Create & Update
- Error handling & rollback on failure

**Expected CSV Columns:**
```
linkblue, first_name, last_name, email, role, college_code,
department_id, is_primary_contact, has_dashboard_access,
has_static_report_access, has_qb_access, is_active
```

**Upload Process:**
1. Validate file format
2. Parse CSV rows
3. Validate each row
4. Display preview + error summary
5. Confirm import
6. Execute import
7. Show result summary (created, updated, skipped, errors)

---

#### **admin/copy.html** - Copy/Duplicate Admin

**Features:**
- Select source admin to copy
- Select target college/dept
- Choose what to copy:
  - Access flags only
  - Role settings
  - All settings
- Create new admin or update existing

---

#### **admin/cleanup.html** - Maintenance Tools

**Tools:**
1. **Remove Inactive Admins** - Delete inactive for >N days
2. **Fix Department Assignments** - Reassign dept admins to correct dept
3. **Merge Duplicate Admins** - Combine duplicate entries
4. **Reset Passwords** - Bulk password reset

---

#### **admin/fix_departments.html** - Department Fix Utility

**Purpose:** Reassign department admins when department structure changes.

**Features:**
- List department admins with current assignment
- New department selector per admin
- Bulk reassignment
- Preview changes before commit

---

#### **admin/course_assignments.html** - Admin ↔ Course Mapping

**Purpose:** Assign college/dept admins to specific courses (for advanced access control).

**Features:**
- Table: Admin | Assigned Courses | Action
- Add assignment modal
- Remove assignment buttons
- Filter by college/dept

---

### Verification Templates (`verification/`)

#### **verification/list.html** - Course Verification Report

**Purpose:** Display all courses with TCE status, filtering/sorting.

**Features:**
- **Filters:**
  - College (multi-select)
  - Department (multi-select)
  - Academic Term (multi-select, auto-populated from data)
  - TCE Status: Marked | Not Marked | Zero Enrollment
  - Search by course code/title
- **Columns:**
  - Course Code
  - Section ID
  - Title
  - Instructor(s)
  - Student Count
  - College
  - Department
  - Start Date
  - End Date
  - TCE Start
  - TCE End
  - Status (badge: green=marked, yellow=not marked, gray=zero enrollment)
  - Crosslisted ID (if applicable)
  - Actions (view detail, export row)
- **Statistics Section:**
  - Total courses | Marked | Not Marked | Zero Enrollment
  - By College (pie chart)
  - By Department (bar chart)
- **Export Options:**
  - Export visible (CSV)
  - Export all (CSV)
  - Column selection before export
- **Pagination:** 50 per page

**Data Source:** Courses table (with filter logic)

**Permissions:**
- Super Admin: see all
- College Admin: see college only
- Dept Admin: see department only

---

#### **verification/detail.html** - Course Detail View

**Sections:**
1. **Course Info**
   - Code, Title, Section ID
   - Instructor(s) with email
   - Student count
   - College, Department
2. **Date Information**
   - Course start/end
   - TCE invite start/end
   - Reminder date
3. **TCE Status**
   - Current status (Marked/Not Marked/Zero Enrollment)
   - Mark for TCE button (if applicable)
4. **Crosslisted Courses** (if applicable)
   - List of linked courses
5. **Question Mapping**
   - Questions assigned to this course (preview)
6. **Sync History**
   - Last updated timestamp
   - Data source

**Actions:**
- Back to list
- Edit course
- Export detail to CSV

---

#### **verification/sync.html** - Data Sync Interface

**Sections:**
1. **Current Sync Status**
   - Last sync time
   - Sync source
   - Item counts (courses, instructors, students)
2. **CSV Upload**
   - File input: Courses.csv, Instructor_Course.csv, Student_Course.csv, Users.csv
   - Or select from datasources/ folder
3. **Sync Options**
   - Full sync vs. incremental
   - Create missing vs. merge existing
   - Dry-run (preview only)
4. **Sync Progress** (real-time via AJAX/WebSocket)
   - Progress bar
   - Current step (e.g., "Loading courses...", "Creating colleges...")
   - Item counts per step
   - Live error log
5. **Result Summary**
   - Colleges created/updated
   - Departments created/updated
   - Courses created/updated
   - Instructors created/updated
   - Warnings & errors

**Data Source:** `/verification/sync` (POST with files)

---

#### **verification/user_lookup.html** - User/Instructor Search

**Purpose:** Find instructors/students by name or email.

**Features:**
- Search box (name or email)
- Results table: Name, Email, Linkblue, Departments, Courses
- Filter by college/department
- Link to instructor detail (if admin)

---

### Question Templates (`questions/`)

#### **questions/browser.html** - Question Bank Browser

**Purpose:** Hierarchical browse of questions mapped to organizational units.

**Layout:**
1. **Header**
   - Title: "Question Bank Browser"
   - Term filter (multi-select dropdown)
   - Search box (filter tree nodes)
   - Help text
2. **Tree View** (scrollable, collapsible)
   ```
   ▶ College Name
     ▶ Department Name
       ▶ Course Code - Title
         ▶ Section ID
   ```
   - **Bold text** = node has questions
   - **Yellow expander icon** = child nodes have questions
   - Click node to expand/collapse
   - Click course/section to view questions

3. **Question Detail Pane** (right side)
   - **Tabs:** Instructor Questions | Course Questions
   - **Per Tab:**
     - Selection (Multiple Choice) heading + list
     - Comment (Open-Ended) heading + list
     - Each question:
       - Question ID
       - Question text (preview, max 200 chars)
       - Question type icon
       - Actions: View full | Delete | Edit
   - **Approval Status** - badge if pending approval

4. **Question Detail Modal**
   - Full question text
   - Type (Selection/Comment)
   - Options (if Selection)
   - Approval status & history
   - Close button

**Features:**
- **Term Filtering:** Auto-populate from academic_term in courses
- **Role-Based Scope:**
  - Super Admin: see all colleges
  - College Admin: see college only
  - Dept Admin: see department only
- **Search:** Filter tree in real-time by course code/title
- **Load on Demand:** Questions load when section expanded

**Data Source:** AJAX calls to `/questions/api/questions/<type>/<id>`

---

#### **questions/import.html** - File Import

**Purpose:** Upload QB.xlsx and QM.xlsx from Explorance Blue.

**Sections:**
1. **File Upload**
   - Input: QB.xlsx (Question Bank definitions)
   - Input: QM.xlsx (Question Mapping to courses)
   - Or: Select existing files from datasources/
2. **Preview**
   - Show first 5 rows of QB (columns, sample questions)
   - Show first 5 rows of QM (course-to-question mappings)
   - Column mapping reference (what each column represents)
3. **Import Options**
   - Replace existing mappings vs. merge
   - Dry-run option
4. **Import & Result**
   - Questions imported count
   - Mappings created count
   - Errors/warnings summary

---

#### **questions/pending.html** - Pending Approvals (All)

**Purpose:** Approvers view all pending question change requests (Super/College Admin).

**Features:**
- **Filter:** By status (Pending | Approved | Rejected), by submitter, by college/dept
- **Table Columns:**
  - Request ID
  - Submitter (name, college/dept)
  - Change Type (Add | Modify | Delete)
  - Question Summary (text preview)
  - Unit (college/dept/course/section)
  - Status (badge)
  - Submitted Date
  - Actions: View | Approve | Reject
- **Approval Modal:**
  - Show detailed change (before/after)
  - Approve/Reject buttons
  - Optional comment field
- **Result Message:** "Approved by [approver] on [date]" or "Rejected: [reason]"

---

#### **questions/pending_mine.html** - My Pending Approvals

**Purpose:** Users view their own submitted pending requests (Dept Admin).

**Features:**
- **List of User's Pending Requests:**
  - Change description
  - Submitted date
  - Current status
  - Expected approval date (SLA)
  - Actions: View detail | Withdraw request
- **Status History:**
  - Submitted → Pending Approval → Approved/Rejected
  - Timestamps & approver names
- **Message:** "Waiting for college admin approval..." or "Approved!" or "Rejected: reason"

---

### Settings Templates (`settings/`)

#### **settings/index.html** - Settings Hub

**Navigation Menu:**
- API Key Management
- Blue Sync Configuration
- Sync Logs & History
- Change Tracking
- WebSocket URL
- Database Backup/Restore

---

#### **settings/api_key.html** - API Key Management

**Display:**
- Current API key (partially masked)
- Creation date
- Last used date
- Generate new button
- Revoke button
- Copy to clipboard button

---

#### **settings/blue_datasources.html** - Blue Integration Settings

**Sections:**
1. **Connection Settings**
   - Blue API URL (input, help text)
   - API Token (input, masked)
   - Test connection button
2. **Sync Settings**
   - Auto-sync frequency (Off | Daily | Weekly | Manual)
   - Preferred sync time
   - Retry on failure (checkbox)
3. **Status**
   - Last successful sync
   - Next scheduled sync
   - Connection status (indicator: green/red)
4. **Sync Now** button
   - Trigger immediate sync
   - Show real-time progress

---

#### **settings/sync_logs.html** - Sync History

**Features:**
- **Filters:** Date range, sync type (CSV | Blue | Manual), status (Success | Failed)
- **Table Columns:**
  - Sync Date
  - Sync Type
  - Source File
  - Status (badge)
  - Items processed (colleges, depts, courses, instructors)
  - Duration (seconds)
  - Actions: View detail | Download log
- **Pagination:** 25 per page
- **Export:** Download all visible logs as CSV

---

#### **settings/sync_log_detail.html** - Detailed Sync Log

**Sections:**
1. **Summary**
   - Sync ID
   - Date/time
   - Duration
   - Status
   - Source file
2. **Counts**
   - Colleges: created/updated
   - Departments: created/updated
   - Courses: created/updated
   - Instructors: created/updated
3. **Errors** (expandable)
   - Error ID
   - Type (validation | system | data)
   - Message
   - Affected record (if applicable)
4. **Warnings** (expandable)
   - Minor issues noted
5. **Changes** (expandable)
   - What changed (old value → new value)
   - Per entity type
6. **Actions**
   - Rollback sync (if applicable)
   - Download full log (JSON/CSV)

---

#### **settings/changes_index.html** - Change Tracking Overview

**Sections:**
- Timeline of recent changes (by date)
- Entity type filters (Admin | Course | Department | College | Question)
- Change type (Created | Modified | Deleted)

---

#### **settings/changes_detail.html** - Audit Trail for Entity

**Display:**
- Entity name/ID
- Full history of changes (reverse chronological)
- Per change:
  - Change date/time
  - Changed by (admin name)
  - Change type (Created/Modified/Deleted)
  - Fields changed (old → new)

---

### Authentication Templates (`auth/`)

#### **auth/login.html** - Login Form

**Form:**
- Branding: UK logo, title
- Username/Linkblue input
- Password input
- "Remember me" checkbox
- Login button
- Help text: "Contact [admin email] if you forgot password"
- Note: "Coming Soon: UK Single Sign-On (LinkBlue)"

**Styling:**
- Centered card layout
- UK blue theme
- Responsive (mobile-friendly)
- Link to forgot password (if implemented)

---

## Configuration & Environment

### Environment Variables

Set in `.env` file or system environment:

```bash
# Flask
FLASK_ENV=development              # development | production
SECRET_KEY=your-secret-key         # Random string for session signing

# Database
DATABASE_URL=postgresql://user:pass@host/db  # Leave blank for SQLite

# Authentication
SUPER_ADMIN_USERNAME=tceadmin
SUPER_ADMIN_PASSWORD=UK_TCE_2025!
TEST_ACCOUNT_USERNAME=testuser
TEST_ACCOUNT_PASSWORD=Test_2025!
TEST_ACCOUNT2_USERNAME=testuser2
TEST_ACCOUNT2_PASSWORD=Test2_2025!

# Azure AD (coming soon)
AZURE_AD_TENANT_ID=your-tenant-id
AZURE_AD_CLIENT_ID=your-client-id
AZURE_AD_CLIENT_SECRET=your-client-secret
AZURE_AD_REDIRECT_URI=https://app.com/auth/callback

# Blue API (Explorance)
BLUE_API_URL=https://blue.youruni.edu/api
BLUE_API_TOKEN=your-api-token

# Data Sync
UKDIG_SYNC_HOUR=4                  # 4 AM daily sync

# Session
SESSION_TYPE=filesystem            # filesystem | redis
```

### Key Configuration Points

**In `app/config.py`:**
- `UK_BLUE`, `UK_WHITE`, `UK_BLACK` - Brand colors
- `ITEMS_PER_PAGE=50` - Pagination default
- `PERMANENT_SESSION_LIFETIME` - Session timeout (8 hours)
- `SQLALCHEMY_DATABASE_URI` - Database connection

---

## AI Agent Task Guidelines

When delegating tasks to an AI agent, specify:

1. **Scope:** Which route/model/template to modify
2. **Requirements:**
   - User story or feature description
   - Input data (CSV columns, API payloads)
   - Output format (JSON, HTML, redirect)
   - Validation rules
3. **Permissions:** Which roles can access (Super | College | Dept)
4. **Error Handling:** What to do on failure (flash message, log error, etc.)
5. **Testing:** Expected test data, edge cases

---

## Example Task Prompts for AI

**Task 1: Add a new filter to the course list**
> "Add a 'Crosslisted' filter to `/verification/list.html` that shows only courses with a non-null crosslist_id. The filter should be a checkbox in the filters panel and persist in the URL query string."

**Task 2: Create an export function**
> "Add a 'Download as CSV' button to `/admin/list.html` that exports the currently visible admin list (after filters applied). Use the column headers: Linkblue, Name, Email, Role, College, Department, Status. Name the file 'admins-[date].csv'."

**Task 3: Modify a model**
> "Add an `is_archived` boolean field to the `Course` model, default False. Create a migration (if using Alembic) or just update `course.py`. Update the verification list to exclude archived courses by default, with an option to show them."

**Task 4: Add a new template**
> "Create a new template `questions/edit.html` that allows a dept admin to edit a selected question. Form fields: question text, question type (dropdown), options (for selection type). Add validation: question text min 10 chars. On submit, create a new ApprovalRequest record and redirect to `/questions/pending-mine` with a success message."

---

## Summary

This document provides:
- ✅ Complete directory structure
- ✅ All database models with fields
- ✅ All routes with URL patterns & methods
- ✅ HTML template descriptions & content
- ✅ Data flow & relationships
- ✅ Permissions & access control
- ✅ Configuration reference

Use this as the reference guide for all future AI agent tasks on this project.
