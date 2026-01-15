"""
Question Bank Service
Handles loading and processing of Question Bank data from Excel files
"""
import os
import pandas as pd
from collections import defaultdict
import json


class QuestionBankService:
    """
    Service for loading and processing Question Bank data
    """
    
    def __init__(self, datafiles_path='./datasources'):
        self.datafiles_path = datafiles_path
        self.questions = {}
        self.question_mapping = defaultdict(dict)
        self.hierarchy = {}
        self.terms = []
        
    def load_courses(self, filter_terms=None, admin_units=None):
        """
        Load course hierarchy from Courses.csv with optional filtering
        
        Args:
            filter_terms: List of ACADEMIC_TERM values to filter by
            admin_units: Dict with keys like 'CLASS_COLLEGE_SHORT' or 'CLASS_DEPARTMENT_ID' 
                        containing lists of allowed unit IDs
        
        Returns:
            dict: Hierarchical structure of courses
        """
        courses_file = os.path.join(self.datafiles_path, 'Courses.csv')
        if not os.path.exists(courses_file):
            return {}
        
        df = pd.read_csv(courses_file, low_memory=False)
        
        # Get unique terms for filter dropdown
        self.terms = sorted(df['ACADEMIC_TERM'].dropna().unique().tolist())
        
        # Apply term filter
        if filter_terms:
            df = df[df['ACADEMIC_TERM'].isin(filter_terms)]
        
        # Apply admin unit filter
        if admin_units:
            if 'CLASS_COLLEGE_SHORT' in admin_units and admin_units['CLASS_COLLEGE_SHORT']:
                df = df[df['CLASS_COLLEGE_SHORT'].isin(admin_units['CLASS_COLLEGE_SHORT'])]
            elif 'CLASS_DEPARTMENT_ID' in admin_units and admin_units['CLASS_DEPARTMENT_ID']:
                # Convert to string for comparison
                df['CLASS_DEPARTMENT_ID'] = df['CLASS_DEPARTMENT_ID'].astype(str)
                admin_units['CLASS_DEPARTMENT_ID'] = [str(x) for x in admin_units['CLASS_DEPARTMENT_ID']]
                df = df[df['CLASS_DEPARTMENT_ID'].isin(admin_units['CLASS_DEPARTMENT_ID'])]
        
        # Build hierarchy: College -> Department -> Course -> Section
        hierarchy = {}
        
        for _, row in df.iterrows():
            college_id = str(row.get('CLASS_COLLEGE_SHORT', ''))
            college_name = str(row.get('CLASS_COLLEGE', ''))
            dept_id = str(row.get('CLASS_DEPARTMENT_ID', ''))
            dept_name = str(row.get('CLASS_DEPARTMENT', ''))
            class_code = str(row.get('CLASS', ''))
            section_key = str(row.get('SECTION_KEY', ''))
            section_title = str(row.get('SECTION_TITLE', ''))
            section_id = str(row.get('CRS_SECTION', ''))
            
            if not college_id or college_id == 'nan':
                continue
            
            # College level
            if college_name not in hierarchy:
                hierarchy[college_name] = {
                    'ID': college_id,
                    'Units': {}
                }
            
            # Department level
            if dept_name and dept_name != 'nan':
                if dept_name not in hierarchy[college_name]['Units']:
                    hierarchy[college_name]['Units'][dept_name] = {
                        'ID': dept_id,
                        'Units': {}
                    }
                
                # Course level (CLASS)
                if class_code and class_code != 'nan':
                    if class_code not in hierarchy[college_name]['Units'][dept_name]['Units']:
                        hierarchy[college_name]['Units'][dept_name]['Units'][class_code] = {
                            'ID': class_code,
                            'Units': {}
                        }
                    
                    # Section level
                    if section_key and section_key != 'nan':
                        section_display = f"{section_id}" if section_id else section_key
                        hierarchy[college_name]['Units'][dept_name]['Units'][class_code]['Units'][section_display] = {
                            'ID': section_key,
                            'Description': section_title
                        }
        
        self.hierarchy = self._sort_hierarchy(hierarchy)
        return self.hierarchy
    
    def _sort_hierarchy(self, tree):
        """Recursively sort hierarchy by keys"""
        if not isinstance(tree, dict):
            return tree
        
        sorted_tree = {}
        for key in sorted(tree.keys()):
            sorted_tree[key] = tree[key]
            if isinstance(tree[key], dict) and 'Units' in tree[key]:
                sorted_tree[key]['Units'] = self._sort_hierarchy(tree[key]['Units'])
        
        return sorted_tree
    
    def load_question_mapping(self, mapping_file=None):
        """
        Load question mappings from Excel file
        
        Args:
            mapping_file: Path to Question Mapping Excel file
        
        Returns:
            dict: Question mappings by type and unit ID
        """
        if mapping_file is None:
            # Look for question mapping files in datafiles_path
            for filename in ['QM.xlsx', 'Question_Mapping.xlsx']:
                test_path = os.path.join(self.datafiles_path, filename)
                if os.path.exists(test_path):
                    mapping_file = test_path
                    break
        
        if not mapping_file or not os.path.exists(mapping_file):
            return {}
        
        try:
            xlsx = pd.ExcelFile(mapping_file)
            
            # Try to find the mappings sheet
            sheet_name = None
            for name in xlsx.sheet_names:
                if 'mapping' in name.lower():
                    sheet_name = name
                    break
            
            if not sheet_name:
                sheet_name = xlsx.sheet_names[0]
            
            df = pd.read_excel(xlsx, sheet_name=sheet_name, header=None)
            
            # First row contains column headers (placeholders like Dept_Crs_Sel_001)
            # First column is Type (DEPARTMENT, COURSE, SECTION)
            # Second column is ID
            
            # Get header row (row 0)
            headers = df.iloc[0].tolist()
            
            # Process data rows starting from row 1
            for idx in range(1, len(df)):
                row = df.iloc[idx]
                mapping_type = str(row.iloc[0]).upper() if pd.notna(row.iloc[0]) else ''
                unit_id = str(row.iloc[1]) if pd.notna(row.iloc[1]) else ''
                
                if not mapping_type or not unit_id or mapping_type == 'NAN':
                    continue
                
                # Get question mappings for this unit (columns 2 onwards)
                question_values = []
                for col_idx in range(2, len(row)):
                    val = row.iloc[col_idx]
                    question_values.append(val if pd.notna(val) else None)
                
                if mapping_type not in self.question_mapping:
                    self.question_mapping[mapping_type] = {}
                
                self.question_mapping[mapping_type][unit_id] = question_values
            
            return dict(self.question_mapping)
            
        except Exception as e:
            print(f"Error loading question mapping: {e}")
            return {}
    
    def load_question_bank(self, qb_file=None):
        """
        Load question bank from Excel file
        
        Args:
            qb_file: Path to Question Bank Excel file
        
        Returns:
            dict: Questions indexed by question ID
        """
        if qb_file is None:
            for filename in ['QB.xlsx', 'QuestionBank.xlsx']:
                test_path = os.path.join(self.datafiles_path, filename)
                if os.path.exists(test_path):
                    qb_file = test_path
                    break
        
        if not qb_file or not os.path.exists(qb_file):
            return {}
        
        try:
            xlsx = pd.ExcelFile(qb_file)
            
            # Try to find the questions sheet
            sheet_name = None
            for name in xlsx.sheet_names:
                if 'question' in name.lower():
                    sheet_name = name
                    break
            
            if not sheet_name:
                sheet_name = xlsx.sheet_names[0]
            
            df = pd.read_excel(xlsx, sheet_name=sheet_name)
            
            # Expected columns: Question ID, Type Definition ID, Question Title, etc.
            for _, row in df.iterrows():
                q_id = row.get('Question ID') or row.iloc[0] if len(row) > 0 else None
                if pd.isna(q_id):
                    continue
                
                q_id = str(q_id)
                
                # Try to find question text in various possible column names
                q_text = None
                for col in ['Question Title', 'Question Text', 'Title', 'Text']:
                    if col in row.index and pd.notna(row[col]):
                        q_text = str(row[col])
                        break
                
                if q_text is None and len(row) > 3:
                    q_text = str(row.iloc[3]) if pd.notna(row.iloc[3]) else ''
                
                q_type = row.get('Type Definition ID') or row.iloc[1] if len(row) > 1 else ''
                
                self.questions[q_id] = {
                    'id': q_id,
                    'type': str(q_type) if pd.notna(q_type) else '',
                    'text': q_text or ''
                }
            
            return self.questions
            
        except Exception as e:
            print(f"Error loading question bank: {e}")
            return {}
    
    def get_available_terms(self):
        """Get list of available ACADEMIC_TERM values"""
        return self.terms
    
    def get_questions_for_unit(self, unit_type, unit_id):
        """
        Get questions mapped to a specific organizational unit
        
        Args:
            unit_type: 'DEPARTMENT', 'COURSE', or 'SECTION'
            unit_id: The ID of the unit
        
        Returns:
            dict: Questions organized by category (course/instructor, selection/comment)
        """
        # Mapping level indices matching the JS implementation
        # Format: {'LEVEL': {'CRS': [sel_start, sel_end, com_start, com_end], 'INS': [...]}}
        mapping_indices = {
            'DEPARTMENT': {'CRS': [0, 14, 14, 19], 'INS': [57, 71, 71, 76]},
            'COURSE': {'CRS': [19, 33, 33, 38], 'INS': [76, 90, 90, 95]},
            'SECTION': {'CRS': [38, 52, 52, 57], 'INS': [95, 109, 109, 114]}
        }
        
        result = {
            'course': {'selection': [], 'comment': []},
            'instructor': {'selection': [], 'comment': []}
        }
        
        unit_type = unit_type.upper()
        if unit_type not in self.question_mapping:
            return result
        
        if unit_id not in self.question_mapping[unit_type]:
            return result
        
        mappings = self.question_mapping[unit_type][unit_id]
        indices = mapping_indices.get(unit_type)
        
        if not indices:
            return result
        
        # Course selection questions
        for i in range(indices['CRS'][0], min(indices['CRS'][1], len(mappings))):
            q_id = mappings[i]
            if q_id and q_id in self.questions:
                result['course']['selection'].append(self.questions[q_id])
        
        # Course comment questions
        for i in range(indices['CRS'][2], min(indices['CRS'][3], len(mappings))):
            q_id = mappings[i]
            if q_id and q_id in self.questions:
                result['course']['comment'].append(self.questions[q_id])
        
        # Instructor selection questions
        for i in range(indices['INS'][0], min(indices['INS'][1], len(mappings))):
            q_id = mappings[i]
            if q_id and q_id in self.questions:
                result['instructor']['selection'].append(self.questions[q_id])
        
        # Instructor comment questions
        for i in range(indices['INS'][2], min(indices['INS'][3], len(mappings))):
            q_id = mappings[i]
            if q_id and q_id in self.questions:
                result['instructor']['comment'].append(self.questions[q_id])
        
        return result
    
    def get_units_with_questions(self):
        """
        Get a set of all unit IDs that have questions mapped
        
        Returns:
            dict: Keys are unit types (DEPARTMENT, COURSE, SECTION), values are sets of unit IDs
        """
        result = {}
        for unit_type, units in self.question_mapping.items():
            result[unit_type] = set()
            for unit_id, questions in units.items():
                # Check if any questions are actually mapped
                if any(q for q in questions if q):
                    result[unit_type].add(unit_id)
        return result
    
    def generate_hierarchy_html(self, hierarchy=None, level=0):
        """
        Generate HTML for the course hierarchy tree
        
        Args:
            hierarchy: The hierarchy dict to render (uses self.hierarchy if None)
            level: Current nesting level
        
        Returns:
            str: HTML string for the tree
        """
        if hierarchy is None:
            hierarchy = self.hierarchy
        
        if not hierarchy:
            return '<p class="text-muted">No courses found matching your filters.</p>'
        
        level_names = ['COLLEGE', 'DEPARTMENT', 'COURSE', 'SECTION']
        units_with_questions = self.get_units_with_questions()
        
        html = ''
        if level == 0:
            html = '<ul class="courseTree">\n'
        
        for name, details in hierarchy.items():
            unit_id = details.get('ID', '')
            has_children = 'Units' in details and details['Units']
            description = details.get('Description', '')
            
            # Determine if this unit or any children have questions
            current_level = level_names[min(level, 3)]
            has_questions = current_level in units_with_questions and unit_id in units_with_questions[current_level]
            child_has_questions = self._check_children_have_questions(details.get('Units', {}), level + 1, units_with_questions, level_names)
            
            q_class = ' has-questions' if has_questions else ''
            expander_class = ' child-has-questions' if child_has_questions else ''
            
            if has_children:
                html += f'<li class="unit-li">'
                html += f'<span class="expander{expander_class}">+</span>'
                html += f'<span id="{unit_id}" class="unit-root{q_class}" data-level="{current_level}">{name}</span>\n'
                html += '<ul class="nested-unit">\n'
                html += self.generate_hierarchy_html(details['Units'], level + 1)
                html += '</ul>\n</li>\n'
            else:
                desc_html = f' ({description})' if description else ''
                html += f'<li id="{unit_id}" class="section{q_class}" data-level="{current_level}">{name}{desc_html}</li>\n'
        
        if level == 0:
            html += '</ul>\n'
        
        return html
    
    def _check_children_have_questions(self, children, level, units_with_questions, level_names):
        """Check if any children in the hierarchy have questions"""
        if not children or level > 3:
            return False
        
        current_level = level_names[min(level, 3)]
        
        for name, details in children.items():
            unit_id = details.get('ID', '')
            if current_level in units_with_questions and unit_id in units_with_questions[current_level]:
                return True
            if 'Units' in details and self._check_children_have_questions(details['Units'], level + 1, units_with_questions, level_names):
                return True
        
        return False
    
    def to_json(self):
        """Export data as JSON for JavaScript"""
        return {
            'questions': self.questions,
            'mappings': dict(self.question_mapping),
            'terms': self.terms
        }
