# UK TCE Admin System

**Teacher Course Evaluation Administration System**
University of Kentucky

## Overview

This system manages TCE administrators, verification reports, and question banks for the University of Kentucky's Teacher Course Evaluation program.

## Features

### ✅ Implemented (Phase 1 - Admin Management)
- **Super Admin Authentication** - Secure login with fallback credentials
- **Admin Management** - Full CRUD operations with role-based access
  - Three-tier permissions: Super Admin → College Admin → Department Admin
  - Primary contact designation
  - Access flags: Dashboard, Static Reports, Question Bank
- **CSV Import/Export** - Bulk admin management
- **UK Branding** - Consistent brand colors and styling with UK logo

### ✅ Implemented (Phase 2 - Verification Reports)
- **Course Data Sync** - Import from UKDIG CSV files (Courses, Instructors, Students)
- **Verification Reports** - Course listings with TCE status, filtering, statistics
  - Marked/Not Marked/Zero Enrollment status
  - Department & College filtering
  - Student counts and instructor names
  - Course Start/End and TCE Start/End dates
  - Crosslisted course information
  - Export to CSV
- **Visual Indicators** - Bold text for items with questions, yellow glow for child items with questions

### ✅ Implemented (Phase 3 - Question Bank)
- **Question Bank Browser** - Hierarchical view of questions by College → Department → Course → Section
- **Term Filtering** - Filter courses by academic term
- **Question Assignment** - Add existing questions or create new questions
- **Question Types** - Support for Selection (multiple choice) and Comment (open-ended) questions
- **Instructor vs Course Questions** - Separate tabs for each question type
- **Approval Workflow** - Department admins submit changes for college/super admin approval
- **Import/Export** - Upload QB.xlsx and QM.xlsx files, export current mappings

### 🔜 Coming Soon (Phase 4)
- **Azure AD Integration** - University single sign-on (waiting for cloud team)

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Initialize Database & Import Data

```bash
# Create database and super admin
python run.py --create-superadmin

# Import existing administrators from CSV
python run.py --import-admins contacts.csv

# Generate sample course data for testing (optional)
python run.py --generate-sample

# Sync course data from CSV files
python run.py --sync-courses
```

### 3. Run the Application

```bash
python run.py
```

Then open http://127.0.0.1:5000 in your browser.

### Default Login

- **Username:** `tceadmin`
- **Password:** `UK_TCE_2025!`

> ⚠️ Change the default password in production by setting the `SUPER_ADMIN_PASSWORD` environment variable.

## Production Setup (Azure)

Before deploying to production, make sure secrets are provided via environment variables and test data is not committed:

- Set required env vars (see Configuration below); production will fail to start if missing.
- Upload fresh `datasources/` files in production only (CSV/XLSX are ignored in Git).
- Use a managed database (e.g., Azure SQL/Postgres) instead of the default SQLite.
- Disable/remove test accounts in production (`TEST_ACCOUNT*`).

## Course Data Sync

The system imports course data from UKDIG-generated CSV files:

| File | Purpose |
|------|---------|
| `Courses.csv` | Course/section information |
| `Instructor_Course.csv` | Instructor assignments (presence = marked for TCE in SAP) |
| `Student_Course.csv` | Student enrollments (for counting) |

### Sync Methods

**Command Line:**
```bash
# Place CSV files in ./datasources/ directory
python run.py --sync-courses

# Or specify a custom path
python run.py --sync-courses /path/to/csvfiles/
```

**Web Interface:**
1. Login as Super Admin
2. Go to Verification → Sync Data
3. Upload CSV files or sync from existing files

### CSV Column Requirements

**Courses.csv:**

- `SECTION_KEY` - Unique section identifier
- `CLASS` - Course code (e.g., "ACC 201")
- `SECTION_ID` - Section number
- `SECTION_TITLE` - Course title
- `CLASS_COLLEGE_SHORT` - College code
- `CLASS_DEPARTMENT_ID` - Department ID
- `SECTION_BEGIN_DATE`, `SECTION_END_DATE` - Course term dates
- `TCE_INVITE`, `TCE_END_DATE` - Evaluation period dates
- `TCE_R2` - Reminder date
- `CROSSLISTED_ID` - Crosslisted course identifier

**Instructor_Course.csv:**

- `SECTION_KEY` - Links to course
- `USER_ID` - Instructor linkblue (used to lookup name from Users.csv)

**Users.csv:**

- `USER_ID` - Linkblue identifier
- `FIRSTNAME`, `LASTNAME` - Instructor/user name
- `EMAIL` - Email address

**Student_Course.csv:**

- `SECTION_KEY` - Links to course
- `USER_ID` - Student identifier

## Project Structure

```text
tce-admin/
├── app/
│   ├── __init__.py          # Flask app factory
│   ├── config.py            # Configuration settings
│   ├── models/              # SQLAlchemy models
│   ├── routes/              # Flask blueprints
│   ├── services/            # Business logic
│   │   └── course_sync.py   # UKDIG data sync
│   ├── static/              # Static assets
│   │   └── images/          # Logo and images
│   └── templates/           # Jinja2 templates
│       ├── admin/           # Admin management templates
│       ├── questions/       # Question bank templates
│       └── verification/    # Verification report templates
├── datasources/             # CSV/Excel files for sync
│   ├── Courses.csv          # Course data from UKDIG
│   ├── Instructor_Course.csv # Instructor assignments
│   ├── Student_Course.csv   # Student enrollments
│   ├── Users.csv            # User directory
│   ├── QB.xlsx              # Question Bank (optional)
│   └── QM.xlsx              # Question Mapping (optional)
├── instance/                # SQLite database (auto-created)
├── run.py                   # Application entry point
└── requirements.txt         # Python dependencies
```

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `FLASK_ENV` | `development` | Set to `production` for production |
| `SECRET_KEY` | (random) | Flask secret key |
| `SUPER_ADMIN_USERNAME` | `tceadmin` | Super admin username |
| `SUPER_ADMIN_PASSWORD` | `UK_TCE_2025!` | Super admin password |
| `DATABASE_URL` | SQLite | Database connection string |
| `TEST_ACCOUNT_USERNAME` | `testuser` | Test account username (disable in prod) |
| `TEST_ACCOUNT_PASSWORD` | `Test_2025!` | Test account password (disable in prod) |
| `TEST_ACCOUNT2_USERNAME` | `testuser2` | Test account username (disable in prod) |
| `TEST_ACCOUNT2_PASSWORD` | `Test2_2025!` | Test account password (disable in prod) |

## User Roles & Permissions

| Role | View Admins | Add/Edit College Admin | Add/Edit Dept Admin | Export | QB Access |
|------|-------------|------------------------|---------------------|--------|-----------|
| Super Admin | All | ✅ | ✅ | ✅ | ✅ |
| College Admin (Primary) | College | ✅ | ✅ | ❌ | If enabled |
| College Admin | College | ❌ | ✅ | ❌ | If enabled |
| Dept Admin | Department | ❌ | ❌ | ❌ | If enabled |

## Development

### Running in Debug Mode

```bash
FLASK_ENV=development python run.py
```

### Database Reset

```bash
rm instance/tce_admin.db
python run.py --create-superadmin
python run.py --import-admins contacts.csv
python run.py --sync-courses
```

## Deployment

For production deployment on Azure Web App:

1. Set environment variables in Azure App Settings
2. Configure PostgreSQL connection string
3. Select Python 3.11+ runtime
4. Enable Azure AD authentication (when ready)

---

© 2025 University of Kentucky
