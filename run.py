#!/usr/bin/env python3
"""
TCE Admin System - Application Entry Point
University of Kentucky

Usage:
    python run.py                       # Run in development mode
    python run.py --import-admins FILE  # Import admins from contacts.csv
    python run.py --create-superadmin   # Create/reset super admin account
    python run.py --sync-courses        # Sync course data from CSV files
    python run.py --generate-sample     # Generate sample course data for testing
"""
import os
import sys
import argparse
from app import create_app, db
from app.models.admin import Admin

app = create_app(os.environ.get('FLASK_ENV', 'development'))


def import_admins_from_csv(filepath):
    """Import administrators from a CSV file"""
    import csv
    
    with app.app_context():
        if not os.path.exists(filepath):
            print(f"Error: File not found: {filepath}")
            return False
        
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            
            imported = 0
            skipped = 0
            errors = []
            
            for row in reader:
                linkblue = row.get('linkblue', '').strip().lower()
                if not linkblue:
                    continue
                
                # Skip if already exists
                existing = Admin.query.filter_by(linkblue=linkblue).first()
                if existing:
                    skipped += 1
                    continue
                
                try:
                    admin = Admin.from_csv_row(row)
                    db.session.add(admin)
                    imported += 1
                except Exception as e:
                    errors.append(f"{linkblue}: {str(e)}")
            
            db.session.commit()
            
            print(f"\n✓ Import complete!")
            print(f"  - Imported: {imported}")
            print(f"  - Skipped (already exist): {skipped}")
            if errors:
                print(f"  - Errors: {len(errors)}")
                for err in errors[:5]:
                    print(f"    • {err}")
            
            return True


def create_superadmin():
    """Create or reset the super admin account"""
    with app.app_context():
        admin = Admin.query.filter_by(linkblue='tceadmin').first()
        
        if admin:
            # Reset password
            admin.set_password(app.config['SUPER_ADMIN_PASSWORD'])
            db.session.commit()
            print("✓ Super admin password reset successfully")
        else:
            # Create new
            admin = Admin(
                linkblue='tceadmin',
                first_name='TCE',
                last_name='Administrator',
                email='tce-admin@uky.edu',
                role='super_admin',
                is_primary_contact=True,
                has_dashboard_access=True,
                has_static_report_access=True,
                has_qb_access=True
            )
            admin.set_password(app.config['SUPER_ADMIN_PASSWORD'])
            db.session.add(admin)
            db.session.commit()
            print("✓ Super admin account created")
        
        print(f"\n  Username: {app.config['SUPER_ADMIN_USERNAME']}")
        print(f"  Password: {app.config['SUPER_ADMIN_PASSWORD']}")


def sync_courses(datasources_path='./datasources'):
    """Sync course data from CSV files"""
    from app.services.course_sync import CourseSyncService
    
    with app.app_context():
        print(f"\n📊 Syncing course data from: {datasources_path}")
        print("-" * 50)
        
        sync = CourseSyncService(datasources_path)
        result = sync.sync_all()
        
        if result['success']:
            print("\n✓ Sync completed successfully!")
            print(f"\n  Statistics:")
            for key, value in result['stats'].items():
                print(f"    • {key.replace('_', ' ').title()}: {value}")
            
            if result['errors']:
                print(f"\n  ⚠️  Warnings ({len(result['errors'])} shown):")
                for err in result['errors']:
                    print(f"    • {err}")
        else:
            print("\n✗ Sync failed!")
            
        return result


def generate_sample_data(output_path='./datasources'):
    """Generate sample course data for testing"""
    from app.services.course_sync import write_sample_csvs
    
    print(f"\n📝 Generating sample data in: {output_path}")
    print("-" * 50)
    
    result = write_sample_csvs(output_path)
    
    print(f"\n✓ Sample data generated!")
    print(f"  • Courses: {result['courses']}")
    print(f"  • Instructors: {result['instructors']}")
    print(f"  • Students: {result['students']}")
    print(f"\nRun 'python run.py --sync-courses' to import this data.")
    
    return result


def main():
    parser = argparse.ArgumentParser(description='TCE Admin System')
    parser.add_argument('--import-admins', type=str, metavar='FILE',
                       help='Import administrators from CSV file')
    parser.add_argument('--create-superadmin', action='store_true',
                       help='Create or reset super admin account')
    parser.add_argument('--sync-courses', nargs='?', const='./datasources', metavar='PATH',
                       help='Sync course data from CSV files (default: ./datasources)')
    parser.add_argument('--generate-sample', nargs='?', const='./datasources', metavar='PATH',
                       help='Generate sample course data for testing')
    parser.add_argument('--host', type=str, default='127.0.0.1',
                       help='Host to bind to (default: 127.0.0.1)')
    parser.add_argument('--port', type=int, default=5000,
                       help='Port to bind to (default: 5000)')
    
    args = parser.parse_args()
    
    if args.import_admins:
        import_admins_from_csv(args.import_admins)
    elif args.create_superadmin:
        create_superadmin()
    elif args.sync_courses:
        sync_courses(args.sync_courses)
    elif args.generate_sample:
        generate_sample_data(args.generate_sample)
    else:
        print("\n" + "="*50)
        print("  UK TCE Admin System")
        print("="*50)
        print(f"\n  Starting server at http://{args.host}:{args.port}")
        print(f"  Super Admin: {app.config['SUPER_ADMIN_USERNAME']} / {app.config['SUPER_ADMIN_PASSWORD']}")
        print("\n  Press Ctrl+C to stop\n")
        app.run(host=args.host, port=args.port, debug=True)


if __name__ == '__main__':
    main()
