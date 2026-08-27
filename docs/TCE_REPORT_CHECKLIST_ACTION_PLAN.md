# TCE Report Checklist — Action Plan

## Objective

Add a **TCE Checklist** tab to the TCE Admin application so super administrators can create, complete, verify, copy, and audit report checklists by semester.

The online checklist should replace the working process in `Checklist for TCE Reports - 2025 Spring.xlsx` while preserving the useful report-specific instructions. It should be easier for several super administrators to update, safer to reuse for a new semester, and reliable as an audit record later.

## What the baseline contains

The Spring 2025 workbook has 23 report checklist tabs:

- 21 active report types, including verification, participation, individual, crosslisted, department, college, university, means, distance learning, merged, and website export reports.
- 2 reference-only report types for completed programs: FYE Program Rollover and UK Smart Campus Initiative.
- Repeated workflow sections across most reports: report setup, Info, Content, Export, Filters, Groups or Subjects, Viewers, Distribution, and Publish.
- Report-level fields for published status, creation date, and verification date.
- Item-level creator and verifier columns, with a few sheets containing parallel completion columns for different report audiences.

The new feature should preserve the report-specific differences without copying the spreadsheet's layout directly.

## Recommended product structure

### 1. Super-admin navigation tab

Add a top-level **TCE Checklist** navigation item visible only when `current_user.is_super_admin()` is true. Protect every checklist route on the server with the existing super-admin authorization pattern; hiding the link alone is not sufficient.

### 2. Checklist dashboard

The landing page should show:

- A semester selector and a **Create semester checklist** button.
- One card or row per semester with overall progress, status, last update, and owner.
- Filters for semester, status, report type, assignee, and incomplete or blocked items.
- Clear actions: **Open**, **Copy to new semester**, **Audit history**, and **Archive**.
- A template management link for authorized super administrators.

Recommended semester states: `Draft`, `In progress`, `Ready for verification`, `Verified`, `Published`, and `Archived`.

### 3. Semester checklist workspace

Opening a semester should display all selected report checklists with progress counts. Each report can expand into its sections and tasks.

Each task should support:

- Status: `Not started`, `In progress`, `Complete`, `Blocked`, or `Not applicable`.
- Creator or assignee.
- Verifier.
- Creator completion timestamp and verifier signoff timestamp.
- Notes.
- Optional evidence link, report URL, or reference link.
- Last updated by and last updated time.

The report header should retain the workbook's published status, creation date, and verification date. A report should not become `Verified` until all required tasks are complete or marked not applicable and the verifier signs off.

Use inline controls with small automatic saves so several super administrators can continue the work without editing a large form. Show a visible save result and warn when another user has changed the same task since it was opened.

### 4. Template library

Treat every report type as a reusable template. A template contains ordered sections and task instructions, plus whether the report is active or reference-only.

Template edits should apply only to checklist runs created after the edit. When a semester is created, copy the template title, section names, task text, order, and requirement flags into the semester run. This snapshot prevents a later template edit from rewriting historical audit records.

Seed the library with:

- The 21 active workbook tabs as active templates.
- FYE Program Rollover and UK Smart Campus Initiative as inactive, reference-only templates.
- A source note identifying the Spring 2025 workbook as the initial baseline.

Before seeding production, correct obvious spelling errors and ask the TCE process owner to confirm ambiguous or outdated operational instructions. Preserve original meaning during cleanup.

### 5. Copy to a new semester

The **Copy to new semester** workflow should ask for the new semester, project or term code, and applicable dates.

By default, copying should:

- Copy the checklist structure and instruction snapshots.
- Reset task status, creator and verifier signoffs, timestamps, notes, evidence links, published status, and completion dates.
- Keep a link to the source semester for traceability.
- Use the latest active templates when the user chooses **Start from templates**.
- Use the historical instruction snapshots when the user chooses **Copy prior semester exactly**.

This distinction supports both a clean recurring workflow and a faithful repeat of a special prior-semester setup.

### 6. Audit history and archival

Record every meaningful change with the acting administrator, timestamp, entity, action, and old and new values. Include semester creation, copying, task edits, signoffs, template changes, archiving, and reopening.

Archived semester checklists should be read-only by default. Allow a super administrator to reopen one with a required reason, and record that action in the audit log. Do not permanently delete a checklist that has activity; archive it instead.

## Suggested data model

| Model | Purpose | Key fields |
|---|---|---|
| `ReportChecklistTemplate` | One reusable report type | title, slug, description, version, active, reference_only, source_note |
| `ReportChecklistTemplateItem` | Ordered template sections and tasks | template_id, item_type, section_name, instruction, sort_order, required |
| `SemesterChecklist` | One semester-level checklist run | term_code, display_name, project_name, status, source_checklist_id, created_by_id, timestamps, archived_at |
| `SemesterReportChecklist` | One report instance within a semester | semester_checklist_id, template_id, template_version, title_snapshot, published_status, creation_date, verification_date, status, sort_order |
| `SemesterChecklistItem` | Frozen task instance and its work state | report_checklist_id, section_snapshot, instruction_snapshot, required, status, creator_id, verifier_id, notes, evidence_url, completed_at, verified_at, updated_at, version |
| `ChecklistAuditLog` | Append-only change history | actor_id, entity_type, entity_id, action, old_values_json, new_values_json, reason, timestamp |

Use foreign keys to the existing `Admin` model for creators, verifiers, and audit actors. Add indexes for term code, checklist status, item status, template, and update time.

The application currently creates tables through SQLAlchemy. Production rollout should still use an explicit migration or deployment script that creates and seeds the checklist tables without resetting the existing database.

## Routes and services

Create a dedicated `checklists` blueprint and register it at `/checklists`.

Minimum route set:

| Route | Purpose |
|---|---|
| `GET /checklists` | Semester checklist dashboard |
| `GET, POST /checklists/new` | Create from current templates or copy a prior semester |
| `GET /checklists/<id>` | Semester workspace |
| `POST /checklists/<id>/archive` | Archive with confirmation |
| `POST /checklists/<id>/reopen` | Reopen with a recorded reason |
| `POST /checklists/items/<id>/update` | Save status, ownership, notes, evidence, or signoff |
| `GET /checklists/<id>/audit` | Filtered audit history |
| `GET /checklists/templates` | Template library |
| `GET, POST /checklists/templates/<id>/edit` | Edit and version a template |

Put creation, copying, progress calculation, verification rules, template versioning, and audit logging in a checklist service rather than duplicating them across route handlers.

## User interface direction

Use the application's existing Bootstrap and UK branding, with a cleaner checklist layout:

- Progress bars and compact counts at semester and report levels.
- Collapsible report and section panels so 21 active report types remain manageable.
- Status badges with accessible text and color.
- Sticky filters or a compact report navigator on long pages.
- Clear creator and verifier columns rather than initials typed into cells.
- Responsive controls that work on typical laptop widths.
- Confirmation dialogs for signoff, archive, reopen, and template changes.
- A print-friendly read-only view for audits.

Do not rely on color alone to communicate status. Use labels and icons as well.

## Delivery phases

### Phase 1 — Normalize and approve the baseline

1. Convert the 23 workbook tabs into a structured seed file.
2. Identify shared sections and report-specific tasks.
3. Mark the 21 active and 2 reference-only templates.
4. Correct clear typographical errors without changing meaning.
5. Have the TCE process owner approve task wording, active report types, and default copy behavior.

**Deliverable:** reviewed template seed data and a field mapping from the workbook to the online feature.

### Phase 2 — Database and checklist service

1. Add the checklist models and relationships.
2. Add a safe schema migration or deployment script.
3. Add the idempotent template seed process.
4. Implement create, copy, reset, progress, signoff, archive, reopen, and audit rules.
5. Ensure template changes cannot alter existing semester snapshots.

**Deliverable:** tested checklist domain layer with seeded templates.

### Phase 3 — Super-admin pages

1. Register the `checklists` blueprint.
2. Add the super-admin-only navigation tab.
3. Build the dashboard, new semester workflow, semester workspace, and audit page.
4. Add inline task updates with visible save feedback and concurrency checks.
5. Build template list and edit pages.

**Deliverable:** complete end-to-end checklist workflow in the application.

### Phase 4 — Testing and data validation

Test at least the following:

- Non-super administrators cannot view or change checklist data, even by direct URL.
- A semester can be created from all active templates.
- A previous semester can be copied with completion data reset.
- A template edit affects new semesters but not existing ones.
- Required-task progress and report-level progress are accurate.
- Creator and verifier signoffs record the correct users and timestamps.
- Conflicting updates do not silently overwrite another administrator's changes.
- Every supported change creates an audit entry.
- Archived checklists are read-only until explicitly reopened.
- The two retired templates do not appear in a normal new-semester run.

Run the existing application test suite as well as the new checklist tests.

**Deliverable:** automated test coverage and a completed manual acceptance checklist.

### Phase 5 — Pilot and rollout

1. Create a test semester and compare every online report checklist with the Spring 2025 workbook.
2. Ask at least two super administrators to complete a short collaboration test.
3. Correct wording, ordering, or usability problems found in the pilot.
4. Back up the production database before deployment.
5. Deploy the tables, seed templates, and feature.
6. Keep the workbook available as read-only reference during the first live semester.

**Deliverable:** production release with an approved initial template set.

## MVP acceptance criteria

The first release is ready when:

- Only super administrators can access the TCE Checklist tab and its endpoints.
- A super administrator can create a checklist for a named semester from the 21 active report templates.
- A super administrator can copy a prior semester without carrying forward completion or signoff data.
- Multiple super administrators can update tasks and see who changed each item and when.
- Tasks support status, creator, verifier, notes, evidence link, and timestamps.
- Each report and semester displays accurate completion progress.
- Existing semester instructions remain unchanged after template edits.
- Archived semester checklists remain readable for audits.
- Audit history captures creation, edits, signoffs, copies, template changes, archive, and reopen actions.
- The online checklist has been reconciled against all 23 workbook tabs.

## Later enhancements

After the core workflow is stable, consider:

- Email or in-app reminders for overdue and blocked tasks.
- Due dates calculated from semester or project dates.
- CSV, Excel, or PDF export for external audit packages.
- File attachments if evidence links are not sufficient.
- Report dependencies and automatic readiness checks.
- A term-over-term completion dashboard.

These should not delay the first usable online checklist.

## Working assumptions requiring owner confirmation

- Access is limited to super administrators in the first release.
- The workbook's two completed research-program tabs remain searchable reference templates but are excluded from new semesters.
- Creator and verifier must be different people for final verification.
- Copying resets all work state by default.
- Historical semester runs are audit records and should never change because a template was edited.
