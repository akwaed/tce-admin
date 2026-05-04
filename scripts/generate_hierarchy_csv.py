#!/usr/bin/env python3
"""
Generate an Explorance hierarchy CSV directly from Courses.csv.

Output columns:
    Node Id, Node Caption, Parent Node Id, Parent Node Caption, Level, CourseNo

Hierarchy levels:
    1. University
    2. CLASS_COLLEGE_SHORT
    3. CLASS_DEPARTMENT_ID
    4. CLASS_ID

Usage:
    python3 scripts/generate_hierarchy_csv.py Courses.csv hierarchy_export.csv
    python3 scripts/generate_hierarchy_csv.py datasources/Courses.csv
"""
import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path


HEADER = [
    'Node Id',
    'Node Caption',
    'Parent Node Id',
    'Parent Node Caption',
    'Level',
    'CourseNo',
]


def clean(value):
    return str(value).strip() if value is not None else ''


def read_courses(path):
    with path.open('r', encoding='utf-8-sig', newline='') as csvfile:
        return list(csv.DictReader(csvfile))


def build_hierarchy_rows(course_rows, university_id='University', university_caption='University'):
    colleges = {}
    departments = {}
    course_candidates = defaultdict(set)
    course_sections = defaultdict(set)
    flattened_departments = set()
    warnings = []

    for row_number, row in enumerate(course_rows, start=2):
        section_key = clean(row.get('SECTION_KEY'))
        college_code = clean(row.get('CLASS_COLLEGE_SHORT'))
        college_name = clean(row.get('CLASS_COLLEGE')) or college_code
        department_id = clean(row.get('CLASS_DEPARTMENT_ID'))
        department_name = clean(row.get('CLASS_DEPARTMENT')) or department_id
        class_id = clean(row.get('CLASS_ID'))
        class_code = clean(row.get('CLASS'))
        section_title = clean(row.get('SECTION_TITLE'))

        row_label = section_key or f'CSV row {row_number}'

        if college_code:
            colleges[college_code] = college_name or college_code
        else:
            warnings.append(f'Missing CLASS_COLLEGE_SHORT for {row_label}')

        course_parent_id = ''
        course_parent_name = ''
        if department_id:
            if department_id == college_code:
                # Explorance Node Ids are globally unique, so do not emit the
                # same value as both a college node and department node.
                course_parent_id = college_code
                course_parent_name = college_name or college_code
                if department_id not in flattened_departments:
                    warnings.append(
                        'CLASS_DEPARTMENT_ID matches CLASS_COLLEGE_SHORT; '
                        f'using the college node for {department_id}'
                    )
                    flattened_departments.add(department_id)
            else:
                department = {
                    'name': department_name or department_id,
                    'college_code': college_code,
                    'college_name': college_name or college_code,
                }
                existing_department = departments.get(department_id)
                if existing_department and existing_department != department:
                    warnings.append(
                        'Conflicting details for CLASS_DEPARTMENT_ID '
                        f'{department_id}; using {existing_department["college_code"]}'
                    )
                    department = existing_department
                else:
                    departments[department_id] = department

                course_parent_id = department_id
                course_parent_name = department['name']
        else:
            warnings.append(f'Missing CLASS_DEPARTMENT_ID for {row_label}')

        if class_id and course_parent_id:
            course_candidates[class_id].add((
                course_parent_id,
                course_parent_name,
                class_code,
                section_title or class_code or class_id,
            ))
            course_sections[class_id].add(section_key)
        elif not class_id:
            warnings.append(f'Missing CLASS_ID for {row_label}')
        else:
            warnings.append(f'Missing hierarchy parent for CLASS_ID {class_id}')

    hierarchy_rows = [
        [university_id, university_caption, '', '', 1, '']
    ]

    for college_code, college_name in sorted(colleges.items(), key=lambda item: (item[1], item[0])):
        hierarchy_rows.append([
            college_code,
            college_name,
            university_id,
            university_caption,
            2,
            '',
        ])

    for department_id, department in sorted(
        departments.items(),
        key=lambda item: (item[1]['college_name'], item[1]['name'], item[0]),
    ):
        hierarchy_rows.append([
            department_id,
            department['name'],
            department['college_code'],
            department['college_name'],
            3,
            '',
        ])

    course_rows_out = []
    for class_id, candidates in course_candidates.items():
        sorted_candidates = sorted(candidates, key=lambda item: (item[1], item[0], item[2], item[3]))
        parent_ids = {candidate[0] for candidate in sorted_candidates}
        if len(parent_ids) > 1:
            warnings.append(
                f'CLASS_ID {class_id} appears under multiple parents; '
                f'exporting one node from {len(course_sections[class_id])} sections'
            )

        parent_id, parent_name, class_code, caption = sorted_candidates[0]
        captions = {candidate[3] for candidate in sorted_candidates if candidate[3]}
        if len(captions) > 1 and class_code:
            caption = class_code

        course_rows_out.append([
            class_id,
            caption or class_code or class_id,
            parent_id,
            parent_name,
            4,
            class_code,
        ])

    hierarchy_rows.extend(sorted(course_rows_out, key=lambda row: (row[3], row[5], row[1], row[0])))

    node_ids = set()
    duplicates = set()
    for row in hierarchy_rows:
        node_id = row[0]
        if node_id in node_ids:
            duplicates.add(node_id)
        node_ids.add(node_id)

    if duplicates:
        warnings.append(f'Duplicate hierarchy Node Ids generated: {", ".join(sorted(duplicates)[:10])}')

    for index, row in enumerate(hierarchy_rows, start=1):
        parent_id = row[2]
        if parent_id and parent_id not in node_ids:
            warnings.append(
                f'Parent node missing before export: row {index} parent {parent_id} for node {row[0]}'
            )

    return hierarchy_rows, warnings


def write_hierarchy(path, rows):
    with path.open('w', encoding='utf-8', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(HEADER)
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description='Generate hierarchy_export.csv from Courses.csv')
    parser.add_argument('courses_csv', help='Path to Courses.csv')
    parser.add_argument(
        'output_csv',
        nargs='?',
        default='hierarchy_export.csv',
        help='Output CSV path. Defaults to hierarchy_export.csv',
    )
    parser.add_argument('--university-id', default='University')
    parser.add_argument('--university-caption', default='University')
    args = parser.parse_args()

    input_path = Path(args.courses_csv)
    output_path = Path(args.output_csv)

    if not input_path.exists():
        print(f'ERROR: input file not found: {input_path}', file=sys.stderr)
        return 1

    courses = read_courses(input_path)
    rows, warnings = build_hierarchy_rows(
        courses,
        university_id=args.university_id,
        university_caption=args.university_caption,
    )
    write_hierarchy(output_path, rows)

    print(f'Wrote {len(rows):,} hierarchy rows to {output_path}')
    if warnings:
        print(f'Warnings: {len(warnings):,}', file=sys.stderr)
        for warning in warnings[:25]:
            print(f' - {warning}', file=sys.stderr)
        if len(warnings) > 25:
            print(f' - ... {len(warnings) - 25:,} more warnings not shown', file=sys.stderr)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
