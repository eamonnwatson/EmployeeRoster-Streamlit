-- CONTROLLED SCHEMA OWNER ONLY; NOT executable by app viewers.
-- Viewers receive app USAGE only, NEVER procedure/table privileges. No GRANTs.
-- There is intentionally no persistent cleanup procedure or public operation.
-- Anonymous procedure executes as CALLER: only the controlled owner with actual
-- table privileges can run this file. Do not delegate DML to viewer/app roles.
-- Run outside an ambient transaction; inspect the JSON success/error envelope.
--
-- handler.cleanup uses ONE scoped transaction and the SAME seeded hybrid mutex
-- BEFORE version/read checks; deletes expired UPLOAD_ROWS, UPLOAD_ISSUES,
-- UPLOAD_CHANGES, then UPLOADS against one captured cutoff, all atomically.
-- Never deletes clients/employees/identifier history/audit. Engine enforces
-- expiry on access even if scheduled/manual cleanup has not run.
-- No task scheduling or live execution is performed by this script's author.
WITH CLEANUP_ROSTER AS PROCEDURE ()
RETURNS VARCHAR
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'regex')
IMPORTS = ('@DATASTORE.ROSTER.ROSTER_CODE/employee_roster.zip')
HANDLER = 'employee_roster.handler.cleanup'
CALL CLEANUP_ROSTER();
