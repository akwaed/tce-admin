# Question Bank Browser Module

This module adds Question Bank browser functionality to the TCE Admin System, allowing administrators to view questions mapped to organizational units (departments, courses, sections) with term filtering.

## Files Included

### Routes
- `routes_questions.py` - Replace/merge with `app/routes/questions.py`

### Services (Optional)
- `services_qb_service.py` - Standalone QuestionBankService class (already included in routes file)

### Templates
- `templates/questions/browser.html` - Main QB Browser template
- `templates/questions/import.html` - File import template

## Installation

### 1. Update Routes File

Copy `routes_questions.py` to `app/routes/questions.py`, replacing the existing file.

### 2. Add Templates

Create the templates directory if it doesn't exist:
```bash
mkdir -p app/templates/questions
```

Copy the template files:
```bash
cp templates/questions/browser.html app/templates/questions/
cp templates/questions/import.html app/templates/questions/
```

### 3. Create Datasources Directory

Ensure the datasources directory exists:
```bash
mkdir -p datasources
```

### 4. Upload Data Files

Place the following files in the `datasources/` directory:
- `Courses.csv` - Course data from UKDIG
- `QB.xlsx` - Question Bank file from Explorance Blue (optional)
- `QM.xlsx` - Question Mapping file from Explorance Blue (optional)

**Note:** The Question Mapping file you provided (`Question_Mapping_25_Fall_2025.xlsx`) can be renamed to `QM.xlsx` or the code will search for it automatically.

## Features

### Term Filtering
- Administrators can filter the course hierarchy by academic term (Fall 2025, Spring 2026, etc.)
- Terms are automatically detected from the `ACADEMIC_TERM` column in `Courses.csv`

### Admin Scope Filtering
- Super admins see all courses
- College admins only see courses in their college
- Department admins only see courses in their department

### Question Display
- Hierarchical tree view of courses (College → Department → Course → Section)
- Bold items indicate units with mapped questions
- Yellow expanders indicate children with mapped questions
- Two tabs: Instructor Questions and Course Questions
- Each tab shows Selection and Comment questions separately

### Question Mapping Structure

The system expects question mappings to follow this column structure:
- Columns 0-13: Department Course Selection Questions (Dept_Crs_Sel_001-014)
- Columns 14-18: Department Course Comment Questions (Dept_Crs_Com_001-005)
- Columns 19-32: Course Course Selection Questions (Crs_Crs_Sel_001-014)
- Columns 33-37: Course Course Comment Questions (Crs_Crs_Com_001-005)
- Columns 38-51: Section Course Selection Questions (Sec_Crs_Sel_001-014)
- Columns 52-56: Section Course Comment Questions (Sec_Crs_Com_001-005)
- Columns 57-70: Department Instructor Selection Questions (Dept_Ins_Sel_001-014)
- Columns 71-75: Department Instructor Comment Questions (Dept_Ins_Com_001-005)
- And so on for Course and Section instructor questions...

## API Endpoints

### GET /questions/
Main Question Bank Browser page with term filtering.

Query Parameters:
- `term` (multiple) - Filter by academic term(s)

### GET /questions/api/questions/<unit_type>/<unit_id>
Get questions for a specific unit.

Parameters:
- `unit_type` - DEPARTMENT, COURSE, or SECTION
- `unit_id` - The unit's ID

Returns JSON:
```json
{
    "course": {
        "selection": [{"id": "...", "type": "...", "text": "..."}],
        "comment": [...]
    },
    "instructor": {
        "selection": [...],
        "comment": [...]
    }
}
```

### GET /questions/import
File upload form for super admins.

### POST /questions/import
Handle file uploads (QB.xlsx and QM.xlsx).

## Required Dependencies

The module requires these Python packages (already in requirements.txt):
- pandas
- openpyxl (for Excel file reading)

## Notes

- The code handles both the standard Blue export format and custom variations
- Search functionality allows filtering the course tree in real-time
- All data loading happens from CSV/Excel files; no database storage required for the browser
- The existing database models (QuestionBank, Question, etc.) are still available for future phases that need persistence
