-- CONTROLLED SCHEMA OWNER ONLY; NOT for the app/viewer role.
-- Viewers receive app USAGE only, NEVER SP USAGE or table DML. No GRANTs here.
-- No stored bootstrap procedure is created, so no viewer-callable admin endpoint
-- exists. This anonymous procedure uses CALLER rights: run as controlled owner.
-- No default administrator. Deliberately UNSET session variables below make this
-- fail closed until the owner explicitly supplies all six reviewed parameters.
-- Do not add hard-coded usernames or defaults to this file.
--
-- Required session variables (not initialized here):
-- ROSTER_BOOTSTRAP_ACTOR: authenticated application username to provision
-- ROSTER_BOOTSTRAP_ACTIVE / ROSTER_BOOTSTRAP_ADMIN: explicit Boolean user flags
-- ROSTER_BOOTSTRAP_CLIENT_ID / ROSTER_BOOTSTRAP_PERMISSION: existing client and
-- CAN_VIEW, CAN_EDIT or CLIENT_ADMIN; both empty strings for user-only updates
-- ROSTER_BOOTSTRAP_PERMISSION_ACTIVE: explicit Boolean (false revokes)
--
-- Run outside an ambient transaction. Handler starts and completes one scoped
-- transaction, locks the same global mutex BEFORE any reads, gates schema version,
-- upserts user/permission, writes non-payload audit and commits; errors rollback.
-- Review return envelope. Provisioning an administrator is a controlled security
-- decision, not an application self-registration flow.
WITH BOOTSTRAP_ROSTER AS PROCEDURE (
    ACTOR VARCHAR, ACTIVE BOOLEAN, ADMIN BOOLEAN, CLIENT_ID VARCHAR,
    PERMISSION VARCHAR, PERMISSION_ACTIVE BOOLEAN
)
RETURNS VARCHAR
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'regex')
IMPORTS = ('@DATASTORE.ROSTER.ROSTER_CODE/employee_roster.zip')
HANDLER = 'employee_roster.handler.bootstrap'
CALL BOOTSTRAP_ROSTER(
    $ROSTER_BOOTSTRAP_ACTOR, $ROSTER_BOOTSTRAP_ACTIVE, $ROSTER_BOOTSTRAP_ADMIN,
    $ROSTER_BOOTSTRAP_CLIENT_ID, $ROSTER_BOOTSTRAP_PERMISSION,
    $ROSTER_BOOTSTRAP_PERMISSION_ACTIVE
);
