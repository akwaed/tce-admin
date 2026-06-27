# TCE Admin — Deploy Checklist (Server /var/www/tce-admin)

Use this checklist when deploying changes (especially Blue sync pipeline fixes) to the live server.

**Target environment (as of 2026-06):**
- Path: `/var/www/tce-admin`
- Python: `venv/bin/python` (Python 3.12)
- Web app: managed via `tce-admin.service` (systemd)
- Nightly sync: cron `0 3 * * *` running `scripts/daily_sync.sh`
- Logs: `/var/log/tce-sync.log` (and journalctl for the service)
- User: typically `ofa-user` or similar

---

## 1. Pre-Deploy (on your machine / CI)

- [ ] Run all verification scripts that cover the changes:
  ```bash
  python3 test_blue_unbound_repro.py
  python3 test_blue_datasources_template_minimal.py
  python3 test_requirement_d.py
  ```
- [ ] Confirm syntax:
  ```bash
  python3 -m py_compile app/services/blue_sync.py \
                      app/routes/settings.py \
                      scripts/hana_sync.py \
                      scripts/db_sync.py
  ```
- [ ] `git status` is clean (or only intended files are staged).
- [ ] No secrets or local DBs committed.
- [ ] Review the specific changes for this release:
  - Blue sync UnboundLocalError fix (`blue_sync.py`)
  - Template crash fix (`blue_datasources.html` + view)
  - Users.csv last + 3-min gap enforcement
  - Duplicate scheduled run guards (hana + db scripts)
- [ ] Pull latest on server first? (decide merge strategy)

---

## 2. Deploy the Code

Typical flow (adjust to your actual process — git pull, rsync, etc.):

```bash
# On the server
cd /var/www/tce-admin

# 1. Stop the web app (important before overwriting files)
sudo systemctl stop tce-admin.service

# 2. Backup current code (optional but recommended)
sudo tar czf /tmp/tce-admin-backup-$(date +%Y%m%d-%H%M).tar.gz \
    --exclude=venv --exclude=instance --exclude=flask_session .

# 3. Pull / receive the new code
git fetch origin
git checkout main
git pull origin main

# 4. Make sure scripts are executable
chmod +x scripts/*.py scripts/*.sh

# 5. (Rarely needed) Update venv if requirements changed
# source venv/bin/activate
# pip install -r requirements.txt --upgrade
```

---

## 3. Restart & Validate Web App

```bash
# Restart the Flask app
sudo systemctl start tce-admin.service
sudo systemctl status tce-admin.service --no-pager

# Or use the helper
sudo ./scripts/tce-admin-service.sh restart
sudo ./scripts/tce-admin-service.sh status
sudo ./scripts/tce-admin-service.sh logs
```

Post-restart checks:

- [ ] App responds (login page loads).
- [ ] Super admin can log in.
- [ ] Load the fixed page (this was crashing):
  ```
  https://.../tce-admin/settings/blue-datasources
  ```
  → Should load without TemplateRuntimeError.
- [ ] Go to Sync Logs: `/tce-admin/settings/sync-logs`
  - Trigger a manual "Datasource to Blue" for a small datasource (e.g. just "courses" or "students").
  - It should no longer fail instantly with `UnboundLocalError`.
- [ ] Check that "Push Selected to Blue" with multiple datasources works (respecting order).

---

## 4. Verify Blue Sync Pipeline Fixes (Critical for this release)

1. **Manual Datasource to Blue test (Bug B + D)**
   - From Sync Logs page, select 2–3 datasources (e.g. courses + students + users) and push.
   - Watch the live progress / sync log detail.
   - Confirm:
     - No `time` UnboundLocalError.
     - Users is processed **last**.
     - There are ~5 minute (300s) waits printed between non-Users datasources.
   - Check the final summary blob in the log — `datasources_success` / `datasources_failed`.

2. **blue-datasources page**
   - Confirm the page loads and the edit modals work (the JS that was using the broken `datasourcesData`).

3. **Run verification scripts on the server (optional but good)**
   ```bash
   source venv/bin/activate
   python3 test_blue_unbound_repro.py
   python3 test_requirement_d.py
   ```

---

## 5. Bug A — Duplicate Scheduled Run Protection (HANA to Datasource)

This is the hardest to fully verify without waiting for 3am.

- [ ] **Check crontab for duplicates** (very important)
  ```bash
  crontab -l
  # Look for multiple lines that call daily_sync.sh or hana_sync.py around 3am
  # Expected (example):
  # 0 3 * * * /var/www/tce-admin/scripts/daily_sync.sh >> /var/log/tce-sync.log 2>&1
  ```

- [ ] Check for other timers / systemd timers:
  ```bash
  systemctl list-timers --all | grep -E 'tce|sync|hana|blue'
  ls /etc/cron.* /etc/systemd/system/*sync* 2>/dev/null || true
  ```

- [ ] Review recent sync logs in the UI for overlapping "HANA to Datasource" entries with near-identical start times.

- [ ] After next cron run (or force one), verify only **one** HANA log + one DB log appear, and both reach terminal status (completed or failed cleanly).

- [ ] Optional: temporarily trigger two overlapping runs (if safe) and confirm the guard messages appear:
  - `WARNING: Another scheduled HANA sync appears to be running...`

---

## 6. Nightly Sync & Logs

- [ ] Check cron is still correct for the deploy user.
- [ ] Ensure log rotation is in place:
  ```bash
  cat /etc/logrotate.d/tce-sync || echo "Consider adding logrotate"
  ```
- [ ] After deploy, either:
  - Wait for the next 3am run, **or**
  - Manually run (as the cron user):
    ```bash
    sudo -u ofa-user /var/www/tce-admin/scripts/daily_sync.sh
    ```
    (or just the Blue portion via `blue_sync_cli.py --scheduled`)

- [ ] Tail the sync log:
  ```bash
  tail -f /var/log/tce-sync.log
  ```

- [ ] In the UI Sync History, confirm the latest "HANA to Datasource" and "Datasource to Blue" entries have:
  - Reasonable durations (not 600m+)
  - Proper `pipeline_phase` = `done` / `complete` or clean `failed`
  - No "exited without updating its log" unless a real crash occurred

---

## 7. Rollback Plan (if something goes wrong)

```bash
cd /var/www/tce-admin
sudo systemctl stop tce-admin.service

# Restore from the backup you took
sudo tar xzf /tmp/tce-admin-backup-YYYYMMDD-HHMM.tar.gz

sudo systemctl start tce-admin.service
```

Or revert the specific commit:
```bash
git revert <commit>
# then repeat deploy steps
```

---

## 8. Post-Deploy Smoke Test Summary

| Area                        | Test                                      | Expected                          |
|-----------------------------|-------------------------------------------|-----------------------------------|
| UI                          | /settings/blue-datasources                | Loads without crash               |
| Blue manual push            | Push "students" + "courses"               | No UnboundLocalError, finishes    |
| Ordering & spacing          | Push all (or multiple)                    | Users last, >=3min gaps (non-users) |
| Sync log detail             | Look at recent entries                    | Clean status, sensible durations  |
| Cron / scheduled            | Next 3am or manual daily_sync             | Single HANA + DB log, both complete |
| No duplicate crons          | `crontab -l`                              | Only one daily entry              |

---

## Quick Commands Reference

```bash
# Service
sudo ./scripts/tce-admin-service.sh restart
sudo systemctl status tce-admin.service
journalctl -u tce-admin.service -n 100 -f

# Manual syncs
source venv/bin/activate
python scripts/hana_sync.py --output datasources --scheduled
python scripts/db_sync.py --datasources datasources --scheduled
python scripts/blue_sync_cli.py --scheduled

# Check current running syncs
# (via UI or by querying the DB)
```

---

**Last updated:** 2026-06-26 (after Blue sync + UI crash fixes)

If you need a shorter "one-pager" version or this added to the existing PRODUCTION_CHECKLIST.md, let me know.
