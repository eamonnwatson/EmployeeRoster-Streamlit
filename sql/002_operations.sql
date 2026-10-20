-- CONTROLLED OWNER ONLY. Viewers receive app USAGE only, never procedure USAGE
-- or table privileges. No guessed roles or actual GRANT statements are included.
-- ROSTER_EXECUTE trusts the app-supplied actor; direct public SQL access is UNSAFE.
-- The trusted app supplies its existing Snowpark session and makes exactly one
-- parameterized CALL. Do not open a transaction or share client transaction state.
-- Run 001 first. Do not enable caller access until owner-reviewed least-privilege
-- app wiring and inherited/future grants have passed manual acceptance.
-- Versioned zip updates require an explicit controlled procedure release: this
-- IF NOT EXISTS setup intentionally does not replace an existing procedure.
-- No live execution/compilation was performed during authoring.
CREATE PROCEDURE IF NOT EXISTS DATASTORE.ROSTER.ROSTER_EXECUTE(
    ACTOR VARCHAR, OPERATION VARCHAR, PAYLOAD_JSON VARCHAR
)
RETURNS VARCHAR
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'regex')
IMPORTS = ('@DATASTORE.ROSTER.ROSTER_CODE/employee_roster.zip')
HANDLER = 'employee_roster.handler.main'
EXECUTE AS OWNER;

-- Handler: BEGIN TRANSACTION -> singleton hybrid FOR UPDATE WAIT 30 -> schema
-- version/signature gate -> fresh permissions/snapshot -> engine + HRIS -> delta
-- DML and audit -> serialize response -> COMMIT; any exception -> ROLLBACK.
-- READ_LATEST_WRITES is passed on each persistence statement (no ALTER SESSION).
-- No DDL/nested CALL in the transaction. Response is JSON VARCHAR:
-- {"ok":true,"data":{...}} or
-- {"ok":false,"error":{"code":"...","message":"...","correlation_id":"..."}}
-- Unexpected/driver messages and payloads are never operationally logged.
-- Transport failures can have unknown commit outcome: refresh, never blind retry.
--
-- Acceptance: real separate-session competing submit/edit/config/bootstrap/
-- cleanup operations, lock timeout, injected rollback after first DML, audit
-- atomicity, fresh revocation, package imports and sanitized SQL failures.
-- Reads materializing snapshots also acquire the same lock. In-handler filtering
-- implements engine pagination; global employee loading and row-at-a-time delta
-- persistence still need realistic scale/latency/memory acceptance.
