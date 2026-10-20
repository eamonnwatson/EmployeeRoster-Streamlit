# Manual deployment and acceptance

**Preparation only: not deploy-ready or live-verified.** No account discovery,
Snowflake connection, live SQL compilation, grants, deployment, or migration is
performed by the preparation tool. The tracked
`C:/Development/Python/EmployeeRosterStreamlit/snowflake.yml` is an intentionally
incomplete draft: **NOT DEPLOYABLE**. Its warehouse, compute pool and external
access integration fields are deliberately absent, not fake resource names.

The fixed target is `DATASTORE.ROSTER.EMPLOYEE_ROSTER`, using
`SYSTEM$ST_CONTAINER_RUNTIME_PY3_11`. Database/schema references in the handler
are fixed too. Changing just a manifest does not retarget the data layer. Use a
separately approved non-production account with the same namespace for acceptance;
never point synthetic or destructive acceptance fixtures at production.

## Local Python 3.11 checks and synthetic preview

Python 3.11 and uv have been provisioned for local verification. Use `python -m uv`
because the standalone uv executable may not be on PATH. Run these PowerShell commands:

```powershell
python -m uv sync --project "C:/Development/Python/EmployeeRosterStreamlit" --python 3.11 --group dev
python -m uv run --project "C:/Development/Python/EmployeeRosterStreamlit" --python 3.11 python --version
python -m uv run --project "C:/Development/Python/EmployeeRosterStreamlit" --python 3.11 pytest "C:/Development/Python/EmployeeRosterStreamlit/tests/test_packaging.py" -q
python -m uv run --project "C:/Development/Python/EmployeeRosterStreamlit" --python 3.11 pytest "C:/Development/Python/EmployeeRosterStreamlit/tests" -q
python -m uv run --project "C:/Development/Python/EmployeeRosterStreamlit" --python 3.11 streamlit run "C:/Development/Python/EmployeeRosterStreamlit/dev/preview.py" --server.address 127.0.0.1
```

The preview uses only synthetic in-memory records and a synthetic role selector;
it does not establish Snowflake identity or test real permissions, transactions,
packages or concurrency. Stop it with Ctrl+C. Do not deploy the preview. Local
production entry-point execution deliberately fails closed without trusted
`st.user.user_name`; do not add a local identity override to production code.

Python is constrained to `~=3.11.0`; Streamlit to `>=1.66,<2` (tested with 1.66.0).
Deferred downloads recheck authorization when invoked; no eager-download fallback is allowed. The project explicitly
includes the Snowflake connector, Snowpark, openpyxl, defusedxml and regex.
There is no wheel/build backend: source is shipped using explicit artifact paths.
pytest uses both the source root and `src` on its path. pytest, PyYAML and ruff are
development dependencies, not runtime requirements. The standard-library-only
preparation script itself does not need these development dependencies.

These are dependency ranges, not a reproducible resolved release. uv may create a
local lockfile during sync; do not mistake it for a hosted-runtime lock (it is not
in the artifact list). Record and review the actual resolved package versions for
each release, including hosted PyPI resolution and the separate SQL `PACKAGES`
resolution. No tooling installation, sync, or test execution is implied by this
document.

## Explicit resources and offline bundle

Obtain the following from the responsible administrator, without auto-discovery:

- Existing query warehouse and compatible compute pool.
- Existing, enabled PyPI external access integration (EAI) with approved package
  repository network access. Presence of the shipped pyproject triggers package
  resolution; the EAI is required, even for already installed dependencies.
- Dedicated trusted app owner role and a different dedicated viewer role.
- Explicit local Snowflake CLI connection alias. No CLI alias has been verified;
  manually confirm the intended account and alias before supplying it.

Resource names must be single ASCII unquoted identifiers, starting with a letter
or underscore, followed by letters, digits, underscores or dollar signs (maximum
255 characters). They are normalized to uppercase. Quoted/qualified identifiers,
reserved/limited SQL keywords and built-in administrative/PUBLIC roles are
rejected. Owner and viewer must differ after normalization. This validates syntax
only, not existence or permission inheritance. Connection aliases allow spaces
and are recorded verbatim; never supply passwords, connection strings or tokens.

The following prompts deliberately have no resource defaults. Choose a **new**
bundle name every time. These commands only prepare local files:

```powershell
$Root = "C:/Development/Python/EmployeeRosterStreamlit"
$Warehouse = Read-Host "Approved query warehouse"
$Pool = Read-Host "Approved compute pool"
$Eai = Read-Host "Approved PyPI EAI"
$Owner = Read-Host "Dedicated app owner role"
$Viewer = Read-Host "Dedicated viewer role (different from owner)"
$Connection = Read-Host "Verified local CLI connection alias"
$Bundle = Read-Host "Absolute NEW bundle path under the deployment directory"
New-Item -ItemType Directory -Force "$Root/deployment" | Out-Null
python -m uv run --project "$Root" --python 3.11 python "$Root/tools/prepare_deployment.py" `
  --output "$Bundle" --warehouse "$Warehouse" --compute-pool "$Pool" `
  --pypi-eai "$Eai" --owner-role "$Owner" --viewer-role "$Viewer" `
  --connection "$Connection"
```

The output parent must exist. Inside the source tree only `deployment/` or `dist/`
outputs are allowed. The generator refuses an existing destination; it never
replaces source files or imports/executes application modules. Missing, unsafe,
symlinked, syntactically invalid or unlisted production Python files stop
preparation before creating output. Copying is allowlisted, not recursive.

The bundle contains production entry point, all four pages, the complete
`src/employee_roster` Python package, `.streamlit/config.toml`, and pyproject,
plus a generated complete `snowflake.yml`. Local review files
`deployment.json`, `grants_owner.sql` and `grants_viewer.sql` are **not** app
artifacts. The JSON records all supplied resources, including the owner role and
connection to use explicitly at manual deployment; these are not invented
manifest fields. SQL, tools, tests, development preview, handler ZIP, environment
files and local credentials are not uploaded as app artifacts. Bootstrap stays
in its existing separately reviewed SQL file.

An allowlist prevents accidental inclusion of unrelated secret files; it cannot
guarantee there are no credentials hard-coded into a listed production source.
Review the actual bundle before release. Never broaden artifacts to `.` or ship
`.streamlit/secrets.toml`. Hosted connections use Snowflake's embedded identity.

## Security boundary and manual grants

The app uses default owner-rights `st.connection("snowflake")`, not a caller-rights
connection. It passes the authenticated viewer's `st.user.user_name` as the actor
to one parameterized `ROSTER_EXECUTE` call. The procedure trusts that assertion.
**Anyone who can call the procedure directly can assert another actor.** Treat
app/procedure source modification, procedure USAGE and owner-role access as
privileged administrative capabilities.

Two owner responsibilities must be distinguished:

- The **controlled schema/procedure owner** runs the existing SQL installation,
  bootstrap and cleanup scripts. It needs full metadata visibility and the
  private roster-table/stage privileges required by those scripts. Its
  owner-rights procedure needs access to the exact HRIS relation
  `ENGAGEHR.ENGAGEHR.GETEMPLOYEES`, with USAGE on its parent database/schema and
  SELECT on that relation, plus the execution warehouse. The relation's actual
  object type, access and policy behavior must be checked manually by its admin;
  the generator does not guess a TABLE/VIEW grant or assign this role.
- The **dedicated app owner** deploys and owns the Streamlit object. Generated
  `grants_owner.sql` supplies explicit DATASTORE/ROSTER USAGE, CREATE STREAMLIT,
  warehouse/compute pool/EAI USAGE, and USAGE on the exact
  `ROSTER_EXECUTE(VARCHAR, VARCHAR, VARCHAR)` signature. It does not need private
  table DML to call an owner-rights procedure. Source-stage privileges, if the
  chosen CLI upload path requires an existing stage, must be reviewed for that
  exact stage by the administrator, not widened to all stages.

Do not run schema/bootstrap scripts as the minimally privileged app role and then
solve resulting errors by giving it broad data access. If the same controlled
role is deliberately used for both ownership responsibilities, document and
approve the wider trust boundary. The generated grants neither grant ownership
nor transfer existing objects. In managed-access schemas the authorized schema
owner/grant administrator must issue the grants.

Generated `grants_viewer.sql` grants **only USAGE on DATASTORE, DATASTORE.ROSTER
and DATASTORE.ROSTER.EMPLOYEE_ROSTER**. Under default owner-rights operation,
viewers are not granted warehouse, compute pool, EAI, table, stage or procedure
access. There are no FUTURE/ALL object grants, role-to-role grants, user grants,
or self-registration. Never grant the app owner to the viewer role or to ordinary
users. A controlled administrator handles any role assignments separately.

Before sharing, review effective access including PUBLIC, secondary roles,
recursive role inheritance, current and future grants. Checking only the two
supplied role names is insufficient. A viewer must not acquire procedure access,
private table access, app edit rights or privileged owner access indirectly.
Application bootstrap permissions are separate from Snowflake viewer-role access:
both are required. Revocation must be effective at the next operation, not cached.

## Manual release sequence

Stop on every SQL error or returned `ok: false` envelope. SQL execution succeeding
is not proof that a JSON-returning handler operation succeeded. Use AUTOCOMMIT
and no ambient transaction for the installation/bootstrap/cleanup entry points.
No part of the following sequence is executed by the generator.

1. Review source and package/test results. In an approved isolated account, confirm
   permanent DATASTORE/ROSTER support, hybrid availability, quotas, metadata
   visibility, current schema state, and HRIS semantics. Freeze concurrent DDL.
   Do not use a transient database/schema, silently switch to standard tables, or
   change the runtime to warehouse mode to bypass a failed prerequisite.
2. Build the **separate** handler ZIP with the existing tool. Its parent must exist
   and an existing archive is refused. Use a fresh local release directory with
   basename `employee_roster.zip` for the initial SQL's import path:

   ```powershell
   $HandlerDirectory = Read-Host "Absolute existing fresh handler release directory"
   python -m uv run --project "C:/Development/Python/EmployeeRosterStreamlit" --python 3.11 python "C:/Development/Python/EmployeeRosterStreamlit/tools/package_handler.py" --output "$HandlerDirectory/employee_roster.zip"
   ```

   Inspect its allowlisted `employee_roster/` package at ZIP root. The ZIP does not
   bundle third-party wheels; SQL `PACKAGES` must independently resolve Snowpark
   and regex under Python 3.11.
3. As controlled schema owner, run **only the namespace/stage statements** in
   `C:/Development/Python/EmployeeRosterStreamlit/sql/001_schema.sql` first, if
   necessary. DATASTORE must already exist. Manually upload the reviewed archive
   without compression to `@DATASTORE.ROSTER.ROSTER_CODE/employee_roster.zip`.
   Confirm the exact stage filename/import path. Do not execute the installer
   before staging the archive or overwrite an active release blindly.
4. Run the remaining installer in
   `C:/Development/Python/EmployeeRosterStreamlit/sql/001_schema.sql`. It checks
   actual columns, keys, hybrid status, schema signature and version. A compatible
   installation is a no-op. A partial, unrecognized or standard-table schema must
   fail before modification; investigate rather than auto-repair. A fresh approved
   empty namespace may be installed explicitly here only. The app never creates
   new empty tables at startup and never migrates old data automatically.
5. Run `C:/Development/Python/EmployeeRosterStreamlit/sql/002_operations.sql` as
   the controlled procedure owner. Confirm the staged handler imports correctly
   and the exact owner-rights signature is installed. `IF NOT EXISTS` does **not**
   upgrade an existing procedure or prove it uses new code. Upgrades require a
   separately reviewed versioned archive/import-path and procedure release plan.
6. Run `C:/Development/Python/EmployeeRosterStreamlit/sql/003_bootstrap.sql` under
   the controlled schema owner, explicitly setting all six documented session
   variables in the same SQL session. Provision only the approved authenticated
   username; set active/admin flags deliberately. Empty client and permission
   strings mean a user-only update. Client-scoped permissions require an existing
   client and one of CAN_VIEW, CAN_EDIT or CLIENT_ADMIN. There is no default
   administrator. Inspect the return envelope and audit result. Initial approved
   app admin provisioning precedes app use; additional client grants follow
   controlled client creation. Do not put bootstrap identities in source files.
7. Have the authorized grant administrator review and apply the bundle's
   `grants_owner.sql`. Preserve the separate controlled owner's private grants.
   Confirm the intended app owner can deploy without relying on an administrative
   secondary role. For existing apps confirm ownership before any replacement.
8. Review bundle files again. Check Snowflake CLI **3.14.0 or later** locally with
   `snow --version`. Only after explicit release approval, manually deploy from
   the generated bundle directory as the recorded owner and connection:

   ```powershell
   $Settings = Get-Content -Raw "$Bundle/deployment.json" | ConvertFrom-Json
   Push-Location "$Bundle"
   try {
       snow streamlit deploy employee_roster --connection "$($Settings.connection)" --role "$($Settings.owner_role)"
       if ($LASTEXITCODE -ne 0) { throw "Deployment failed; stop and inspect." }
   } finally {
       Pop-Location
   }
   ```

   For an approved replacement only, add `--replace`; do not replace by default.
   Never run the deployment command from the source directory's draft manifest.
9. Manually verify the exact Streamlit object's database/schema/name, owner,
   runtime, query warehouse, compute pool, EAI and deployed source artifacts with
   Snowsight or administrator-reviewed SHOW/DESCRIBE checks. A clean CLI exit is
   insufficient. Open the app as the owner for the smoke test, then under a
   controlled test-viewer role for identity and access acceptance. Only after
   sign-off apply `grants_viewer.sql` for intended production viewers. Recheck
   sharing grants after a replacement; do not assume they were preserved.

Routine expired-upload cleanup uses only the separately reviewed
`C:/Development/Python/EmployeeRosterStreamlit/sql/004_cleanup.sql` under the
controlled schema owner. No task scheduling or automatic cleanup deployment is
included. Cleanup does not remove employee/client/identifier/audit history.

## Mandatory live acceptance gates

Offline packaging checks and fake-session tests cannot establish these gates:

- **Identity and trust:** validate real `st.user.user_name` for different signed-in
  viewers, normalized identity mapping, and fail-closed missing identity. Never
  substitute CURRENT_USER (owner execution context), a form value, query string,
  environment variable or fallback username. Verify default owner-rights execution
  and that direct viewer procedure calls/table queries/app edits are denied.
- **Network and runtime:** verify the selected Python 3.11 container runtime,
  Streamlit APIs, all four pages, source imports, PyPI EAI/package resolution and
  stored-procedure Snowpark/regex packages. Confirm organizational network policy,
  PrivateLink/private connectivity, DNS and browser endpoint support for this
  exact runtime/account before release. Private-link compatibility has not been
  verified here; do not assume warehouse-runtime connectivity settings suffice
  or bypass a private-only policy with a public URL.
- **Two real writers:** use independent Snowflake sessions for concurrent edits,
  duplicate submits, cross-client employee creation, config changes, revocation,
  bootstrap and cleanup. Confirm one consistent winner/stale-conflict outcome,
  no lost updates, no duplicate identifiers/audit, latest cross-session reads,
  expired-token rejection, and bounded lock timeout behavior.
- **Rollback:** in an isolated approved fixture, inject failure after initial DML
  and verify roster, upload completion and audit all roll back together. Test
  serialization/SQL failures too. A transport failure can have unknown commit
  outcome: refresh authoritative state before retrying; never blindly replay a
  mutation. Remove fault injection before release.
- **Schema and recovery:** verify clean install/rerun and rejection of partial,
  standard-table, altered-key/column and incompatible-version fixtures without
  overwriting data. DDL auto-commits; a failed fresh install is not automatically
  transactional or repairable. Retain the marker and singleton mutex; never
  recreate them to hide incompatibility.
- **Performance:** every read snapshot and write uses the same hybrid mutex row
  (`ID=1`, `FOR UPDATE WAIT 30`) before checking state. This globally serializes
  operations. Snapshots include selected-client history and global employees;
  pagination is in-engine, not a guarantee of bounded SQL scanning. Delta writes
  issue per-row DML. Measure realistic roster/history and 10,000-row upload
  latency, memory, contention, warehouse cost and timeout behavior before rollout.
  A larger compute pool does not remove the global serialization bottleneck.

## Rollback and recovery restrictions

Keep prior reviewed app bundles, handler archives, procedure definitions, package
versions and grant records outside active release paths. To roll back code,
quiesce access/writers and have the owner restore a compatible app/procedure pair
under a reviewed release plan. Re-check ownership, runtime, grants and identity.
Do not restore only UI code against an incompatible schema or assume overwriting
a staged ZIP refreshes an existing procedure.

Do not use DROP/CREATE, empty-table replacement, automatic migration, or a standard
table conversion as a recovery shortcut. Hybrid tables have no Fail-safe,
replication or UNDROP TABLE recovery. Hybrid Time Travel has limitations (notably
TIMESTAMP-based AT queries rather than BEFORE/OFFSET/STATEMENT), and cloning is
subject to separate hybrid-specific support constraints. Do not assume familiar
standard-table backup/clone/undrop procedures work. Before production, the data
owner must establish and rehearse an account-supported, consistent backup/restore
and retention plan covering data, constraints, audit, permissions, marker and
mutex. No backup/recovery plan has been live-verified by this preparation work.

## Data compatibility boundaries

This implementation is not an automatic .NET regex/date/Excel migration:

- Identifier types are String, Int (32-bit), Long (64-bit), Boolean (`true`/`false`)
  and DateTime. DateTime parsing accepts the explicit invariant patterns
  `YYYY-MM-DD`, `MM/DD/YYYY`, and `YYYY-MM-DDTHH:MM:SS` (also a space instead of T).
  No locale-dependent parsing, timezone suffixes or arbitrary .NET format strings
  are promised. Effective dates require canonical `YYYY-MM-DD` and use UTC's
  application day, not the Snowflake session timezone.
- Display formats are empty or `{0}`, integers `{0:D1}` through `{0:D30}`, and
  dates `{0:yyyy-MM-dd}` or `{0:MM/dd/yyyy}`. Review unsupported existing formats
  before onboarding.
- Regex uses Python's `regex` package with VERSION0/search semantics, a 500-character
  pattern limit and a 50-ms evaluation timeout. Anchor explicitly for whole-value
  validation. .NET-specific named/balancing/conditional groups, explicit-capture
  options, named backreferences and Unicode-property escapes are rejected by the
  current guard. This is a supported subset, not full .NET compatibility; test
  each existing rule with representative values.
- Uploads are strict `.xlsx` OOXML with matching MIME/ZIP checks: maximum 5 MiB
  compressed, 50 MiB expanded, 10,000 data rows, 100 columns, 500,000 cells and
  2,048 archive entries. Formula-bearing workbooks (including hidden/unused sheets
  and defined-name formulas), external links, macros, embedded objects, encrypted
  or malformed archives and unsafe XML are rejected. Formulas are never evaluated
  or accepted using cached results. Numeric cells retain stored lexical values,
  not Excel display formatting; use text for leading-zero identifiers and
  invariant date text rather than assuming numeric date serial conversion.
- Blank identifier cells are omissions, not delete instructions. Review warnings,
  HRIS enrichment and the staged validation baseline before submit. Configuration,
  roster or directory changes may require restaging; do not bypass stale checks.
  Exports force text cells, including formula-looking values.

## Documentation references

Public product guidance was consulted; no account was inspected. Recheck current
support and organizational policy at release time:

- [Owner-rights Streamlit](https://docs.snowflake.com/en/developer-guide/streamlit/object-management/owners-rights)
- [Create/view privileges and managed-access sharing](https://docs.snowflake.com/en/developer-guide/streamlit/object-management/privileges)
- [Runtime limitations](https://docs.snowflake.com/en/developer-guide/streamlit/limitations)
- [Package dependency management](https://docs.snowflake.com/en/developer-guide/streamlit/app-development/dependency-management)
- [Hybrid restrictions and recovery limitations](https://docs.snowflake.com/en/user-guide/tables-hybrid-limitations)
- [Hybrid cloning support](https://docs.snowflake.com/en/user-guide/tables-hybrid-clone)
