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

Select a college and one of its imported academic terms, enter both dates, give
a reason, and acknowledge the data integrity warning. Start must be on or before
end. Use the same form to replace an existing override. Overrides cover all
courses for that college/term, including courses imported later. Use **Restore
SAP dates** to remove an override and immediately expose the latest source dates.

The existing `courses.tce_start` and `courses.tce_end` database columns continue
to store raw SAP data. The ORM exposes these as `sap_tce_start` / `sap_tce_end`;
`Course.tce_start` / `Course.tce_end` resolve the effective dates in both Python
and SQL, including filtering, sorting, dashboard counts, and exports. New
`college_date_overrides` rows take precedence without rewriting source files.
Incoming SAP refreshes still update the raw dates. The Blue payload replaces
`TCE_INVITE` and `TCE_END_DATE` before column mapping. Reminder and report dates
are not changed; the settings page calls out that their timing needs review.

The current integration reads from SAP/HANA and sends to Blue. No SAP write-back
endpoint exists in this repository; these overrides apply to SAP-sourced data
in the app and Blue payloads, not to SAP itself.

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

The app's existing `db.create_all()` startup creates three additive tables:
`college_policies`, `college_date_overrides`, and `college_policy_audit`. No
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
