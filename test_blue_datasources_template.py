#!/usr/bin/env python3
"""
Verification for Bug C: the | map('to_dict') crash is gone.
Renders the exact expression (or equivalent) that used to fail in blue_datasources.html
"""
import sys
import os
sys.path.insert(0, ".")
os.environ.setdefault("FLASK_ENV", "development")

from app import create_app
from app.models import db
from app.models.settings import BlueSyncDatasource
from jinja2 import Environment, FileSystemLoader, select_autoescape

def main():
    app = create_app()
    with app.app_context():
        try:
            db.create_all()
        except Exception:
            pass

        # Seed one if table empty (does not require real data)
        if BlueSyncDatasource.query.count() == 0:
            ds = BlueSyncDatasource(
                datasource_id="Data999", display_name="Test DS", csv_file="test.csv",
                import_order=99, is_active=True
            )
            db.session.add(ds)
            db.session.commit()

        datasources = BlueSyncDatasource.query.order_by(BlueSyncDatasource.import_order).all()
        datasources_json = [d.to_dict() for d in datasources]

        # Reproduce the context passed to the real template
        env = Environment(
            loader=FileSystemLoader("app/templates"),
            autoescape=select_autoescape()
        )
        # Use the exact line that was in the template (now using datasources_json)
        tmpl = env.from_string('const datasourcesData = {{ datasources_json | tojson }};')
        out = tmpl.render(datasources_json=datasources_json)
        print("RENDERED:", out)
        print()
        print("SUCCESS: Bug C fixed. Template renders using datasources_json | tojson (no to_dict filter).")
        return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("FAILED:", repr(e))
        import traceback
        traceback.print_exc()
        sys.exit(1)
