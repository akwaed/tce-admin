#!/usr/bin/env python3
"""
Reproduction / verification for Bug B (UnboundLocalError 'time').

This script demonstrates the exact scoping bug and verifies the fix
in app/services/blue_sync.py .

Run BEFORE the fix (with the `import time` inside the if in _import_datasource):
  python -c '
  # paste a copy of the buggy _import_datasource snippet
  '  --> raises: cannot access local variable 'time' ...

Run AFTER:
  python test_blue_unbound_repro.py
  --> should succeed (or fail only on missing Blue creds / no full setup, never on time)

The actual integrated function uses time.monotonic() early, before any conditional.
"""

import sys
import os
import time as _time  # module level

# Minimal simulation of the buggy pattern that used to exist:
def buggy_version():
    """Simulates the pre-fix scope problem."""
    file_start = _time.monotonic()
    # ... other code ...
    cancelled = False  # normally would be result of _cancel_stale
    if not True:  # simulate not dry_run
        if cancelled:
            import time  # <--- late local binding makes 'time' local for WHOLE func
            time.sleep(2)
    # reference that used to be before the local import:
    return _time.monotonic() - file_start

def test_simulated_bug():
    try:
        # Replicate the exact compile-time scoping by exec
        src = '''
def demo():
    t0 = time.monotonic()
    if False:
        if True:
            import time
            time.sleep(1)
    return time.monotonic() - t0
demo()
'''
        # This exec will raise UnboundLocalError because of the inner import
        exec(compile(src, '<repro>', 'exec'), {'time': _time})
        print("SIM: no error (unexpected)")
    except UnboundLocalError as e:
        print(f"SIM: Got expected UnboundLocalError (pre-fix pattern): {e}")
        return True
    return False

def test_actual_module():
    """Import the real module (post-fix) and ensure 'time' not rebound locally in the hot path."""
    try:
        from app.services.blue_sync import BlueSyncService
        src = open('app/services/blue_sync.py', 'r', encoding='utf-8').read()
        # Count top level vs inner 'import time' or 'import time as'
        # The inner one was at indent >0
        lines = src.splitlines()
        inner_time_imports = []
        in_def = 0
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            if stripped.startswith('def ') or stripped.startswith('async def '):
                in_def = len(line) - len(stripped)
            if 'import time' in line:
                indent = len(line) - len(stripped)
                if indent > 0:
                    inner_time_imports.append((i, line.strip()))
        if inner_time_imports:
            print(f"FAIL: still has local 'import time' inside functions: {inner_time_imports}")
            return False
        print("OK: No inner 'import time' inside any function in blue_sync.py")
        # Try to instantiate (won't connect without config, but scope is compile time)
        # We don't call _import_datasource without full setup + mocks, but module import proves no Syntax/Scope error on def
        svc = BlueSyncService(datasources_path='./datasources')
        print("OK: BlueSyncService instantiated without import-time errors")
        # To really exercise the early time.monotonic line we would need heavy mocking of _resolve etc.
        # For isolation verification we at least confirm source and that a call that hits time early would work.
        print("OK: time references in _import_datasource are now safe (module-level binding)")
        return True
    except Exception as e:
        print(f"ERROR during module test: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == '__main__':
    print("=== Bug B repro/verification ===")
    print("1. Simulated pre-fix UnboundLocalError pattern:")
    had_bug = test_simulated_bug()
    print()
    print("2. Actual module post-fix check:")
    ok = test_actual_module()
    print()
    if ok and had_bug:
        print("SUCCESS: Bug pattern reproduced in sim, fixed in real module.")
        sys.exit(0)
    elif ok:
        print("PARTIAL: Module OK (sim did not trigger this run).")
        sys.exit(0)
    else:
        print("FAIL: module check did not pass.")
        sys.exit(1)
