# College settings

Super administrators can open **Settings → College settings**. The first college
selected is Pharmacy, when available. All changes require a reason and are
recorded with the actor, time, college, term (for dates), and before/after values.

## Course-number exclusions

Pharmacy defaults to excluding **catalog course numbers greater than 500**.
Course 500 and below remain included. The number comes from `CLASS`, with
`CRS_SECTION` and `SECTION_KEY` as fallbacks. The displayed section and SAP's
`SECTION_ID` object identifier are never compared with the threshold. Courses
whose catalog numbers cannot be parsed remain included.

The rule applies to all of that college's contacts and all terms. It affects
dashboard totals/timelines, verification lists/details/statistics/exports, and
report exports. Super administrators retain access to the source courses.
Other colleges have no default exclusions. Saving an empty threshold explicitly
disables the rule, including Pharmacy's default.

Both scheduled and manual Blue pushes omit excluded course records and matching
student/instructor records. They evaluate the fresh course CSV even when the
local database has not yet synced. The same policy snapshot is used across a
push. Missing policy data or required source fields abort preparation; an empty
payload retains the existing `AbortOnEmpty` behavior. Existing records already
in Blue are subject to Blue's configured import behavior; this change does not
issue a separate deletion request for them.

## TCE date overrides

Super administrators can edit **TCE start**, **TCE end**, and the local **TCE
flag / Blue upload** setting on any course detail page. In **Verification**,
select multiple course checkboxes (or all courses on the current page), choose
the fields to change in **Bulk edit TCE settings**, and provide a reason.
Selections apply only to the current page. Crosslisted sections are independent;
select each section that should change.

Each field defaults to **Keep unchanged**. Start-only edits preserve each
course's existing end date, and end-only edits preserve its start date. The
server validates the resulting dates for every selected course before saving
anything. One invalid course rejects the entire selection. Only super admins
can submit changes; forms include session CSRF protection. Each changed course
gets an audit entry recording the actor, time, reason, and before/after values.

**Remove course override** restores that field's college/term override, if one
exists, or its latest SAP value. Existing college/term overrides remain active
and can be removed in College settings; new edits in the UI target selected
courses instead. Course overrides take precedence independently for each date.

The existing `courses.tce_start` and `courses.tce_end` database columns continue
to store raw SAP data. The ORM exposes these as `sap_tce_start` / `sap_tce_end`;
`Course.tce_start` / `Course.tce_end` resolve the effective dates in both Python
and SQL, including filtering, sorting, dashboard counts, and exports. New
`course_tce_overrides` rows take precedence over existing `college_date_overrides`
without rewriting source files.
Incoming SAP refreshes still update the raw dates. The Blue payload replaces
`TCE_INVITE` and `TCE_END_DATE` before column mapping. Reminder and report dates
are not changed; the settings page calls out that their timing needs review.

The current integration reads from SAP/HANA and sends to Blue. No SAP write-back
endpoint exists in this repository; these overrides apply to SAP-sourced data
in the app and Blue payloads, not to SAP itself.

## Turn TCE off locally

**Turn off — block Blue upload** saves a persistent local block for the selected
section keys. It omits those courses and their student/instructor assignments
from manual and scheduled Blue payloads. It does not change SAP flags, imported
instructor assignments, dates, or source files. Verification shows **Blocked
from Blue**; effective TCE status, counts, filters, and exports treat the course
as not marked. `Course.sap_marked_for_tce` retains the imported flag in the
existing `courses.marked_for_tce` database column.

**Remove local block — use SAP flag** restores effective status from the latest
SAP flag and resumes normal Blue upload rules, including college exclusions.
It does not force an unmarked SAP course to become marked. Date overrides remain
unchanged. Course settings and their audit history survive daily imports and
removal/reimport of the same section key. As with college exclusions, records
already in Blue depend on Blue's configured import behavior; no separate Blue
deletion is issued.

## Verification term default

When `term` is absent from the verification URL, choose the term with the most
courses running today, within the contact's visible scope. Today uses
`America/New_York`; course start/end bounds are inclusive. Ties use the latest
course start, then the term label for deterministic selection. Between terms,
use the nearest upcoming course's term, or the most recently ended course's
term. Without usable dates, use the most populated term. An explicit `term=`
continues to mean **All Terms**, and a manually selected term is preserved.
Verification counts and the CSV export respect the selected term.

## Rollout and checks

See [Production rollout](college-policies-production.md) for server/Azure steps,
policy preview commands, and rollback instructions.

The app's existing `db.create_all()` startup creates the additive tables
`college_policies`, `college_date_overrides`, `college_policy_audit`,
`course_tce_overrides`, and `course_tce_audit`. The last two are new for
course-level controls. No
existing columns or source records are rewritten. Deploy/restart the web and
scheduled-sync processes together. Pharmacy's default exclusion becomes active
when this version runs; no date overrides are seeded without actual dates.

Run `python -m unittest discover -s tests -v` for synthetic automated tests.
For a DB-backed preview with no SOAP calls, run:

```sh
python scripts/blue_sync_cli.py --policy-dry-run --datasource courses --datasource instructors --datasource students
```

The original `--dry-run` remains offline raw-CSV validation and explicitly warns
that it does not include saved college policies. Policy previews write sync logs
but do not push to Blue. Integration tests use synthetic files and an in-memory
SQLite database; a live PostgreSQL/SAP/Blue integration has not been exercised.
