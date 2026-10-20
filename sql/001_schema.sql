-- CONTROLLED SCHEMA OWNER ONLY. No grants are issued by these scripts.
-- Viewers receive app USAGE only, NEVER table DML/SELECT or procedure USAGE.
-- The app owner is a trusted boundary: actor is asserted by authenticated app
-- code, so public/direct ROSTER_EXECUTE access would allow impersonation.
-- Review inherited/future grants before deployment; use a dedicated owner.
--
-- OFFLINE AUTHORED; NOT EXECUTED OR COMPILED AGAINST A LIVE ACCOUNT.
-- Run with complete schema-owner metadata visibility, AUTOCOMMIT enabled, no
-- ambient transaction, no concurrent deployment/DDL, and stop-on-error enabled.
-- DATASTORE must already be a permanent, controlled database. Hybrid support,
-- regex/Snowpark package availability and account region require acceptance.
--
-- 1. Build employee_roster.zip locally using tools/package_handler.py.
-- 2. Owner may run ONLY the following two namespace/stage statements first.
-- 3. Owner uploads the reviewed ZIP (package at root) to ROSTER_CODE separately.
--    This script intentionally performs no PUT, role creation, or credential work.
-- 4. Run the anonymous installer below after the reviewed archive is present.
--
-- Schema DDL lives in employee_roster.persistence.TABLES (single manifest).
-- handler.install_schema verifies ALL actual column types/lengths/nullability,
-- primary/unique/foreign keys, HYBRID status, version and manifest signature.
-- Any partial/unrecognized/standard-table target fails BEFORE changing tables.
-- Existing compatible schemas are a no-op. Fresh DDL uses only CREATE HYBRID
-- TABLE IF NOT EXISTS; no OR REPLACE TABLE, conversion or implicit migration.
-- DDL auto-commits: a failed fresh install needs controlled manual remediation,
-- not an automatic repair. The marker and singleton mutex are seeded together.
-- All runtime, bootstrap and cleanup writes take mutex ID=1 before any snapshot.
--
-- Documentation checked:
-- https://docs.snowflake.com/en/sql-reference/constructs/for-update
-- https://docs.snowflake.com/en/user-guide/tables-hybrid-limitations
-- https://docs.snowflake.com/en/sql-reference/transactions
-- VARIANT is deliberately not used even where non-indexed types are supported.
-- All JSON fields are bounded VARCHAR; dates and timestamps use native types.
CREATE SCHEMA IF NOT EXISTS DATASTORE.ROSTER;
CREATE STAGE IF NOT EXISTS DATASTORE.ROSTER.ROSTER_CODE;

WITH INSTALL_ROSTER AS PROCEDURE ()
RETURNS VARCHAR
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python', 'regex')
IMPORTS = ('@DATASTORE.ROSTER.ROSTER_CODE/employee_roster.zip')
HANDLER = 'employee_roster.handler.install_schema'
CALL INSTALL_ROSTER();

-- Manual acceptance gates before 002:
-- * Clean install then rerun produces no data/table changes.
-- * Standard-table, missing-table, extra-column, changed-constraint and wrong
--   marker fixtures fail without changing any pre-existing table.
-- * Verify VARCHAR byte limits on realistic 10000-row uploads, metadata SHOW
--   output shapes and TIMESTAMP_TZ precision in the target account.
-- * Confirm no schema-wide future grants expose tables/procedures to viewers.
