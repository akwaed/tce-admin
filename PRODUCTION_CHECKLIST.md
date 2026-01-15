# Production Go-Live Checklist (Azure)

Use this before pushing to GitHub and again before deploying to Azure.

## GitHub Hygiene
- [ ] Confirm `.env` files are not tracked (`.env`, `.env.*`).
- [ ] Remove any hardcoded secrets or test credentials from code/config.
- [ ] Verify `datasources/` files are NOT committed (CSV/XLSX/JSON ignored).
- [ ] Ensure `instance/` is empty (only `instance/.gitkeep` should be tracked).
- [ ] Rotate any credentials that may have been used locally.

## Required Environment Variables (Production)
- [ ] `SECRET_KEY`
- [ ] `DATABASE_URL`
- [ ] `SUPER_ADMIN_USERNAME`
- [ ] `SUPER_ADMIN_PASSWORD`

## Disable Test Accounts (Production)
- [ ] Do not set `TEST_ACCOUNT_USERNAME`, `TEST_ACCOUNT_PASSWORD`
- [ ] Do not set `TEST_ACCOUNT2_USERNAME`, `TEST_ACCOUNT2_PASSWORD`
- [ ] Remove/disable any test admins created in local DBs before prod import.

## Data & Storage
- [ ] Upload fresh production `datasources/` files on Azure:
  - [ ] `Courses.csv`
  - [ ] `Instructor_Course.csv`
  - [ ] `Student_Course.csv`
  - [ ] `Users.csv`
  - [ ] `QB.xlsx` (if applicable)
  - [ ] `QM.xlsx` (if applicable)
- [ ] Confirm `datasources/pending_changes.json` and `datasources/qb_audit_log.json` are clean or rotated for prod.

## Azure Setup & Validation
- [ ] Create the Azure app/service and configure environment variables.
- [ ] Configure managed database (Azure SQL/Postgres) and verify connectivity.
- [ ] Run initial data sync and verify Question Bank loads.
- [ ] Test super admin login and primary college admin flows.
- [ ] Validate audit log writes in `datasources/qb_audit_log.json`.
