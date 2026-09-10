# Deploy the college settings release

This release adds Pharmacy section exclusions, college/term TCE date overrides,
and the automatic verification term default. No dependency changes are required.
The app creates three new tables; existing SAP date columns retain their data.

## Choose the production target

The repository has two deployment configurations:

- `DEPLOY_CHECKLIST.md` describes `/var/www/tce-admin`, the existing `venv`,
  `tce-admin.service`, and a separate cron sync.
- `.github/workflows/main_tce-admin.yml` automatically deploys pushes to `main`
  to the **Production** slot of Azure App Service **TCE-Admin**.

The release branch is `feature/college-settings-overrides`. Pushing this branch
does not trigger the checked-in production deployment workflow. Merging it into
`main` does. Confirm which environment serves users before merging.

If the web app and scheduled sync run on different hosts, deploy this release
to both. Both must use the same PostgreSQL database so saved overrides are shared.
Do not resume Blue sync while its worker still runs the old code.

## Linux server rollout

1. Schedule a short maintenance window. In the app, check that there is no
   active HANA, DB, or Blue sync. Back up the production database using your usual
   PostgreSQL backup/snapshot process and retain its restore instructions.
   Record the existing cron schedule; temporarily comment out only this app's
   `daily_sync.sh` entry under the account that owns it. Do not stop unrelated cron
   jobs. Let any active sync finish before continuing.

2. SSH into the server and run these commands individually, checking the result
   of each. If `git status` shows local code changes, resolve those before switching
   versions; do not reset or discard them.

   ```bash
   cd /var/www/tce-admin
   git status --short
   git rev-parse HEAD
   git fetch origin
   git log -1 --oneline origin/feature/college-settings-overrides
   ```

   Save the current full SHA from `git rev-parse HEAD` as your rollback revision.
   After verifying the fetched release, stop the web process and install it:

   ```bash
   sudo systemctl stop tce-admin.service
   git switch --detach origin/feature/college-settings-overrides
   ```

   Detached HEAD pins the server to this fetched release without modifying your
   local `main` branch. After the release is merged into `main`, return to your
   normal branch-based deployment process. `.env`, `venv`, and ignored runtime
   datasource files are not replaced by the branch switch.

3. Initialize and verify the new tables once **before** starting multiple web
   workers. Run as the deployment account with the same DB configuration as the
   web service. This reads `.env` when present and refuses to silently create an
   unrelated SQLite database if `DATABASE_URL` is missing.

   ```bash
   FLASK_ENV=production venv/bin/python - <<'PY'
   import os
   from dotenv import load_dotenv
   load_dotenv('.env')
   if not os.environ.get('DATABASE_URL'):
       raise SystemExit('DATABASE_URL is missing. Load the production service environment first.')
   from app import create_app
   from app.models import db
   from sqlalchemy import inspect
   app = create_app('production')
   with app.app_context():
       required = {'college_policies', 'college_date_overrides', 'college_policy_audit'}
       missing = required - set(inspect(db.engine).get_table_names())
       if missing:
           raise SystemExit('Missing tables: ' + ', '.join(sorted(missing)))
       print('College policy tables are ready.')
   PY
   ```

   Stop here if this fails. Use the rollback instructions below to restore service
   while resolving the database issue. No ALTER or data backfill is required for
   this release. Do not drop/recreate the existing database.

4. Start and inspect the web app:

   ```bash
   sudo systemctl start tce-admin.service
   sudo systemctl status tce-admin.service --no-pager
   sudo journalctl -u tce-admin.service -n 80 --no-pager
   ```

5. Sign in as a superadmin and open **Settings → College settings**.

   - Pharmacy: the threshold should be **500**. Numbers strictly greater than
     500 are excluded by default; 500 and below stay included. Verify using the
     Pharmacy contact's account, since superadmins intentionally retain access.
   - Law: choose the intended term, enter the actual TCE start/end dates, enter
     the reason, acknowledge the integrity warning, and save. No Law dates are
     preconfigured by this release. Check a Law course detail page and CSV export.
   - Verification: open the page without a `term` parameter. Verify the selected
     term has the most ongoing courses in the visible scope. Select **All Terms**
     and another term to confirm manual selection still works.

6. Preview the outgoing files with the saved policies. Run this from the same
   directory and production environment used by the sync account:

   ```bash
   FLASK_ENV=production venv/bin/python scripts/blue_sync_cli.py \
     --policy-dry-run \
     --datasource courses --datasource instructors --datasource students
   ```

   This reads the DB and local CSVs, records a sync log, and makes **no SOAP calls**.
   Confirm success for all three datasources and inspect their samples/counts in
   the log. Samples are only the first few rows; a Law record may not appear there.
   A failure must be resolved before enabling the live sync. The older `--dry-run`
   option is raw-file validation and does **not** include saved college policies.

7. After the app and preview checks pass, restore the original cron entry.
   The next daily sync uses the new rules automatically. If you intentionally
   want an immediate live push using the existing CSVs, run the command below
   only when no other sync is running. **This command writes to Blue.**

   ```bash
   FLASK_ENV=production venv/bin/python scripts/blue_sync_cli.py \
     --datasource courses --datasource instructors --datasource students
   ```

   Check **Settings → Sync Logs** for completion. If the source files need a fresh
   SAP pull, use the usual full daily sync instead of this Blue-only command.
   Verify the first subsequent nightly run as well. This release does not change
   the configured cron time.

## Azure App Service rollout

1. Confirm the active web app is the workflow's `TCE-Admin` Production slot.
   Take a database backup and pause the TCE sync scheduler for the rollout.
2. Review the release branch and its changes. Merge it into `main` only when
   ready to deploy; that merge starts the production workflow automatically.
   The separate CI workflow is not currently a prerequisite of the deploy job.
3. Follow the build/deploy workflow in GitHub Actions through both jobs. Check
   the app logs after deployment and confirm startup created the three tables.
   The DB identity needs permission to create them. If startup cannot create
   them, have the deployment operator initialize them once using the app's
   production environment before restarting the web workers.
4. If cron runs on the Linux server, update that checkout to the same release
   before resuming it. An Azure web deployment does not update `/var/www/tce-admin`.
5. Perform the settings, contact-view, and `--policy-dry-run` checks above, then
   resume the scheduler and inspect the next run. Use the Python executable
   configured on the actual sync host; Azure does not necessarily use `venv/bin/python`.

## Rollback

Keep the scheduler paused and let any active import finish. On the Linux server:

```bash
cd /var/www/tce-admin
sudo systemctl stop tce-admin.service
git switch --detach YOUR_RECORDED_PREVIOUS_SHA
sudo systemctl start tce-admin.service
sudo systemctl status tce-admin.service --no-pager
```

Replace `YOUR_RECORDED_PREVIOUS_SHA` with the full revision you recorded. The new
tables are additive; leave them intact. The older code ignores the policies, so
**do not resume its Blue sync** until you have decided how to preserve the
exclusions and date corrections. A code rollback does not undo a completed Blue
import. Restore those external values through a reviewed corrected import if
necessary. On Azure, redeploy the prior approved release through your normal
process and keep the separate sync host coordinated.

## Validation and boundaries

Before upload: 63 automated tests passed, all three existing deploy-check scripts
passed, Python compilation passed, and `pip check` reported no broken requirements.
The new forms were inspected in-browser using synthetic data. Production
PostgreSQL, SAP, and Blue have not been accessed during this implementation.

Overrides apply to data read **from** SAP and sent to Blue; they do not write back
to SAP. Reminder/report dates remain unchanged. Excluding records from new
payloads does not issue a separate deletion request for records already in Blue;
their treatment depends on Blue's configured import behavior.
