"""
Course Data Sync Service
Imports course data from UKDIG-generated CSV files into the database

CSV Files Expected:
- Courses.csv: Course/section information
- Instructor_Course.csv: Instructor assignments (presence = marked for TCE in SAP)
- Student_Course.csv: Student enrollments (for counting)
"""
import csv
import os
from datetime import datetime
from collections import defaultdict
from app.models import db
from app.models.course import Course, Instructor, College, Department, SyncLog


class CourseSyncService:
    """Service for syncing course data from CSV files to database"""
    
    def __init__(self, datasources_path='./datasources'):
        self.datasources_path = datasources_path
        self.errors = []
        self.stats = {
            'courses_added': 0,
            'courses_updated': 0,
            'instructors_added': 0,
            'colleges_added': 0,
            'departments_added': 0,
            'students_counted': 0
        }
    
    def sync_all(self):
        """Run full sync of all data"""
        log = SyncLog(sync_type='full', status='running')
        db.session.add(log)
        db.session.commit()
        
        try:
            # 1. Load courses first (creates colleges/departments)
            self.sync_courses()
            
            # 2. Load instructor assignments (determines TCE marking)
            self.sync_instructors()
            
            # 3. Count students per course
            self.sync_student_counts()
            
            # Update sync log
            log.status = 'completed'
            log.completed_at = datetime.utcnow()
            log.records_processed = (
                self.stats['courses_added'] + 
                self.stats['courses_updated'] + 
                self.stats['instructors_added']
            )
            if self.errors:
                import json
                log.errors = json.dumps(self.errors[:50])  # Keep first 50 errors
            
            db.session.commit()
            
            return {
                'success': True,
                'stats': self.stats,
                'errors': self.errors[:10]
            }
            
        except Exception as e:
            log.status = 'failed'
            log.errors = str(e)
            db.session.commit()
            raise
    
    def sync_courses(self):
        """Import courses from Courses.csv"""
        filepath = os.path.join(self.datasources_path, 'Courses.csv')
        
        if not os.path.exists(filepath):
            self.errors.append(f"Courses.csv not found at {filepath}")
            return
        
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                try:
                    section_key = row.get('SECTION_KEY', '').strip()
                    if not section_key:
                        continue
                    
                    # Ensure college exists
                    college_code = row.get('CLASS_COLLEGE_SHORT', '').strip()
                    college_name = row.get('CLASS_COLLEGE', '').strip()
                    if college_code:
                        college = College.query.get(college_code)
                        if not college:
                            college = College(code=college_code, name=college_name or college_code)
                            db.session.add(college)
                            self.stats['colleges_added'] += 1
                    
                    # Ensure department exists and is up-to-date
                    dept_id = row.get('CLASS_DEPARTMENT_ID', '').strip()
                    dept_name = row.get('CLASS_DEPARTMENT', '').strip()
                    if dept_id:
                        dept = Department.query.get(dept_id)
                        if not dept:
                            dept = Department(
                                id=dept_id,
                                name=dept_name or dept_id,
                                college_code=college_code
                            )
                            db.session.add(dept)
                            self.stats['departments_added'] += 1
                        else:
                            # Update existing department name and college if changed
                            if dept_name and dept.name != dept_name:
                                dept.name = dept_name
                            if college_code and dept.college_code != college_code:
                                dept.college_code = college_code
                    
                    # Create or update course
                    course = Course.query.get(section_key)
                    is_new = course is None
                    
                    if is_new:
                        course = Course(section_key=section_key)
                    
                    # Update course fields
                    course.class_id = row.get('CLASS_ID', '').strip()
                    course.class_code = row.get('CLASS', '').strip()
                    course.section_id = row.get('SECTION_ID', '').strip()
                    course.crs_section = row.get('CRS_SECTION', '').strip()
                    course.section_title = row.get('SECTION_TITLE', '').strip()
                    course.college_code = college_code
                    course.department_id = dept_id
                    course.crosslisted_id = row.get('CROSSLISTED_ID', '').strip() or None
                    
                    # Parse dates if present (use actual CSV column names)
                    course.course_start = self._parse_date(row.get('SECTION_BEGIN_DATE'))
                    course.course_end = self._parse_date(row.get('SECTION_END_DATE'))
                    course.tce_start = self._parse_date(row.get('TCE_INVITE'))
                    course.tce_end = self._parse_date(row.get('TCE_END_DATE'))
                    course.tce_reminder = self._parse_date(row.get('TCE_R2'))
                    
                    # Extract term from section_key (last 7 chars typically)
                    if len(section_key) >= 7:
                        course.term_code = section_key[-7:]
                    
                    course.last_synced = datetime.utcnow()
                    course.marked_for_tce = False  # Will be set by instructor sync
                    
                    if is_new:
                        db.session.add(course)
                        self.stats['courses_added'] += 1
                    else:
                        self.stats['courses_updated'] += 1
                        
                except Exception as e:
                    self.errors.append(f"Course {row.get('SECTION_KEY', 'unknown')}: {str(e)}")
            
            db.session.commit()
    
    def sync_instructors(self):
        """
        Import instructor assignments from Instructor_Course.csv
        Presence in this file = course is marked for TCE in SAP
        Names are looked up from Users.csv
        """
        # First, load user data from Users.csv for name lookup
        users_data = {}
        users_filepath = os.path.join(self.datasources_path, 'Users.csv')
        if os.path.exists(users_filepath):
            try:
                with open(users_filepath, 'r', encoding='utf-8') as f:
                    user_reader = csv.DictReader(f)
                    for row in user_reader:
                        user_id = row.get('USER_ID', '').strip()
                        if user_id:
                            users_data[user_id] = {
                                'first_name': row.get('FIRSTNAME', '').strip(),
                                'last_name': row.get('LASTNAME', '').strip(),
                                'email': row.get('EMAIL', '').strip()
                            }
            except Exception as e:
                self.errors.append(f"Error loading Users.csv: {str(e)}")

        filepath = os.path.join(self.datasources_path, 'Instructor_Course.csv')

        if not os.path.exists(filepath):
            self.errors.append(f"Instructor_Course.csv not found at {filepath}")
            return

        # Track which courses have instructors
        courses_with_instructors = set()

        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            for row in reader:
                try:
                    section_key = row.get('SECTION_KEY', '').strip()
                    user_id = row.get('USER_ID', '').strip()

                    if not section_key or not user_id:
                        continue

                    # Check if course exists
                    course = Course.query.get(section_key)
                    if not course:
                        # Course not in our system yet - skip
                        continue

                    courses_with_instructors.add(section_key)

                    # Check if instructor already exists for this course
                    existing = Instructor.query.filter_by(
                        section_key=section_key,
                        user_id=user_id
                    ).first()

                    if not existing:
                        # Look up user data from Users.csv
                        user_info = users_data.get(user_id, {})

                        instructor = Instructor(
                            section_key=section_key,
                            user_id=user_id,
                            first_name=user_info.get('first_name', ''),
                            last_name=user_info.get('last_name', ''),
                            email=user_info.get('email', ''),
                            instructor_role=row.get('ROLE', '').strip() or None,
                            last_synced=datetime.utcnow()
                        )
                        db.session.add(instructor)
                        self.stats['instructors_added'] += 1
                    else:
                        # Update existing instructor with user info if available
                        user_info = users_data.get(user_id, {})
                        if user_info:
                            existing.first_name = user_info.get('first_name', existing.first_name)
                            existing.last_name = user_info.get('last_name', existing.last_name)
                            existing.email = user_info.get('email', existing.email)
                            existing.last_synced = datetime.utcnow()

                except Exception as e:
                    self.errors.append(f"Instructor {row.get('USER_ID', 'unknown')}: {str(e)}")

        # Mark courses with instructors as "marked for TCE"
        for section_key in courses_with_instructors:
            course = Course.query.get(section_key)
            if course:
                course.marked_for_tce = True

        db.session.commit()
    
    def sync_student_counts(self):
        """Count students per course from Student_Course.csv"""
        filepath = os.path.join(self.datasources_path, 'Student_Course.csv')
        
        if not os.path.exists(filepath):
            self.errors.append(f"Student_Course.csv not found at {filepath}")
            return
        
        # Count students per section
        student_counts = defaultdict(int)
        
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                section_key = row.get('SECTION_KEY', '').strip()
                if section_key:
                    student_counts[section_key] += 1
                    self.stats['students_counted'] += 1
        
        # Update course student counts
        for section_key, count in student_counts.items():
            course = Course.query.get(section_key)
            if course:
                course.student_count = count
        
        db.session.commit()
    
    def _parse_date(self, date_str):
        """Parse date string to date object"""
        if not date_str:
            return None
        
        date_str = date_str.strip()
        
        # Try common date formats
        formats = [
            '%Y-%m-%d',
            '%m/%d/%Y',
            '%Y/%m/%d',
            '%d-%m-%Y',
            '%Y-%m-%d %H:%M:%S',
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        
        return None


def generate_sample_data():
    """Generate sample course data for testing"""
    
    # Sample colleges
    colleges = [
        ('AS', 'Arts and Sciences'),
        ('EN', 'Engineering'),
        ('BE', 'Business & Economics'),
        ('ED', 'Education'),
        ('AG', 'Ag, Food and Environment'),
        ('MD', 'Medicine'),
        ('NU', 'Nursing'),
        ('PH', 'Public Health'),
    ]
    
    # Sample departments per college
    # NOTE: Using 'SAMPLE_' prefix for IDs to avoid conflicts with real data from Courses.csv
    departments = {
        'AS': [('SAMPLE_BIO', 'Biology'), ('SAMPLE_CHE', 'Chemistry'), ('SAMPLE_ENG', 'English'), ('SAMPLE_HIS', 'History')],
        'EN': [('SAMPLE_CS', 'Computer Science'), ('SAMPLE_EE', 'Electrical Engineering'), ('SAMPLE_ME', 'Mechanical Engineering')],
        'BE': [('SAMPLE_ACC', 'Accountancy'), ('SAMPLE_ECO', 'Economics'), ('SAMPLE_FIN', 'Finance')],
        'ED': [('SAMPLE_CI', 'Curriculum & Instruction'), ('SAMPLE_EL', 'Educational Leadership')],
        'AG': [('SAMPLE_ANI', 'Animal Science'), ('SAMPLE_PLT', 'Plant Science')],
        'MD': [('SAMPLE_IM', 'Internal Medicine'), ('SAMPLE_SUR', 'Surgery')],
        'NU': [('SAMPLE_NUR', 'Nursing')],
        'PH': [('SAMPLE_EPI', 'Epidemiology'), ('SAMPLE_BIO', 'Biostatistics')],
    }
    
    # Sample courses
    courses_data = []
    instructors_data = []
    students_data = []
    
    course_num = 0
    for college_code, college_name in colleges:
        for dept_id, dept_name in departments.get(college_code, []):
            # Generate 3-5 courses per department
            for i in range(1, 5):
                course_num += 1
                prefix = dept_name[:3].upper()
                class_num = 100 + (i * 100)
                section_key = f"{prefix}{class_num}-001-2025010"  # Spring 2025
                
                course = {
                    'SECTION_KEY': section_key,
                    'CLASS_ID': f'CLS{course_num:05d}',
                    'CLASS': f'{prefix} {class_num}',
                    'SECTION_ID': '001',
                    'SECTION_TITLE': f'Introduction to {dept_name} {i}',
                    'CLASS_COLLEGE_SHORT': college_code,
                    'CLASS_COLLEGE': college_name,
                    'CLASS_DEPARTMENT_ID': dept_id,
                    'CLASS_DEPARTMENT': dept_name,
                    'CROSSLISTED_ID': '',
                    'SECTION_BEGIN_DATE': '2025-01-13',
                    'SECTION_END_DATE': '2025-05-02',
                    'TCE_INVITE': '2025-04-14',
                    'TCE_END_DATE': '2025-04-28',
                    'TCE_R2': '2025-04-21',
                }
                courses_data.append(course)
                
                # 80% of courses are marked for TCE (have instructors)
                if course_num % 5 != 0:
                    instructor = {
                        'SECTION_KEY': section_key,
                        'USER_ID': f'inst{course_num:03d}',
                        'FIRST_NAME': f'Professor{course_num}',
                        'LAST_NAME': f'Smith{course_num}',
                        'EMAIL': f'inst{course_num:03d}@uky.edu',
                        'ROLE': 'Primary'
                    }
                    instructors_data.append(instructor)
                    
                    # Add students (10-50 per course, some with 0)
                    if course_num % 7 != 0:  # ~14% zero enrollment
                        num_students = 15 + (course_num % 35)
                        for s in range(num_students):
                            student = {
                                'SECTION_KEY': section_key,
                                'USER_ID': f'stu{course_num:03d}{s:03d}'
                            }
                            students_data.append(student)
    
    return courses_data, instructors_data, students_data


def write_sample_csvs(output_path='./datasources'):
    """Write sample data to CSV files"""
    import os
    
    os.makedirs(output_path, exist_ok=True)
    
    courses, instructors, students = generate_sample_data()
    
    # Write Courses.csv
    if courses:
        with open(os.path.join(output_path, 'Courses.csv'), 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=courses[0].keys())
            writer.writeheader()
            writer.writerows(courses)
    
    # Write Instructor_Course.csv
    if instructors:
        with open(os.path.join(output_path, 'Instructor_Course.csv'), 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=instructors[0].keys())
            writer.writeheader()
            writer.writerows(instructors)
    
    # Write Student_Course.csv
    if students:
        with open(os.path.join(output_path, 'Student_Course.csv'), 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=students[0].keys())
            writer.writeheader()
            writer.writerows(students)
    
    return {
        'courses': len(courses),
        'instructors': len(instructors),
        'students': len(students)
    }
