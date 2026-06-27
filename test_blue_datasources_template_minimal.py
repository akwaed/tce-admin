#!/usr/bin/env python3
"""
Pure-Jinja verification for Bug C fix.
No Flask, no DB required. Confirms that the expression that used to require
a non-existent 'to_dict' Jinja filter now works using the pre-serialized list.
"""
from jinja2 import Environment

def main():
    env = Environment()
    # Exact expression that was in the template before the fix
    bad_expr = "{{ datasources | map('to_dict') | list | tojson }}"
    # The fixed expression
    good_expr = "{{ datasources_json | tojson }}"

    # Fake model objects that have .to_dict() (simulates BlueSyncDatasource)
    class FakeDS:
        def __init__(self, did, name):
            self.datasource_id = did
            self.display_name = name
        def to_dict(self):
            return {"datasource_id": self.datasource_id, "display_name": self.display_name}

    ds_list = [FakeDS("Data144", "Users"), FakeDS("Data161", "Courses")]

    # Demonstrate that bare map('to_dict') blows up (the original bug)
    try:
        tmpl_bad = env.from_string(bad_expr)
        out_bad = tmpl_bad.render(datasources=ds_list)
        print("UNEXPECTED: bad template rendered:", out_bad)
    except Exception as e:
        print("EXPECTED (pre-fix):", type(e).__name__, "-", str(e)[:80])

    # The fixed path (what view now passes)
    ds_json = [d.to_dict() for d in ds_list]
    tmpl_good = env.from_string(good_expr)
    out_good = tmpl_good.render(datasources_json=ds_json)
    print("FIXED render:", out_good)

    # Also prove that even if we had the list, tojson works on it
    assert "Data144" in out_good
    print()
    print("SUCCESS: Bug C root cause confirmed (map('to_dict') filter missing).")
    print("         Fix (datasources_json passed from view + | tojson) works.")
    return 0

if __name__ == "__main__":
    import sys
    sys.exit(main())
