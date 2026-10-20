"""Owner-rights entry points; never expose these procedures to app viewers.

The supplied actor is trusted application identity, not CURRENT_USER(). Public
SQL execution would permit impersonation. Bootstrap/cleanup run only through
owner-controlled anonymous procedures; they are not operations of ROSTER_EXECUTE.

Manual acceptance: deploy to an isolated non-production schema/account, confirm
hybrid availability, package resolution, metadata shapes, cross-session latest
reads, competing writers, lock timeout, audit rollback, expiry, and realistic
snapshot/upload latency. Fake-session tests cannot establish real concurrency.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import uuid

from .directory import SnowflakeDirectory
from .domain import RosterError, key, required_text
from .persistence import (JSON_LIMIT, PREFIX, SCHEMA_SIGNATURE, SCHEMA_VERSION, TABLES,
                          SnapshotStore, check_version, encode, error_envelope, lock,
                          query, records)
from .services.operations import execute


def _transaction(session, action):
    """One scoped transaction, including reads. Never DDL or nested CALL here."""
    correlation_id = uuid.uuid4().hex
    begun = False
    try:
        query(session, "BEGIN TRANSACTION")
        begun = True
        lock(session)
        check_version(session)
        data = action()
        response = encode({"ok": True, "data": data})  # Serialization failure also rolls back.
        query(session, "COMMIT")
        begun = False
        return response
    except Exception as exc:
        code = exc.code if isinstance(exc, RosterError) else "INTERNAL"
        if begun:
            try:
                query(session, "ROLLBACK")
            except Exception:
                # Returning ends this SP scope; Snowflake rolls back any still-open
                # scoped transaction. Never claim rollback/commit succeeded here.
                code = "INTERNAL"
        return encode(error_envelope(code, correlation_id))


def main(session, actor: str, operation: str, payload_json: str) -> str:
    def action():
        try:
            if not isinstance(payload_json, str) or len(payload_json.encode()) > JSON_LIMIT:
                raise ValueError("Invalid request size")
            payload = json.loads(payload_json)
            if not isinstance(payload, dict):
                raise ValueError("Invalid request")
            normalized_actor = key(required_text(actor, "Actor", 200))
        except (TypeError, ValueError):
            raise RosterError("Invalid request.") from None
        store = SnapshotStore(session)
        state = store.load(normalized_actor, operation, payload)
        before = deepcopy(state)
        # UTC is an explicit application-day choice, independent of session TIMEZONE.
        now = datetime.now(timezone.utc)
        data = execute(state, normalized_actor, operation, payload, now, SnowflakeDirectory(session).lookup)
        store.save(before, state)
        return data
    return _transaction(session, action)


def bootstrap(session, actor: str, active: bool, admin: bool, client_id: str,
              permission: str, permission_active: bool) -> str:
    """Controlled owner operation; explicit flags, no default administrator.

    Pass empty client/permission to change only the user. Existing permissions
    are retained with ACTIVE=false on revocation, rather than deleting history.
    """
    def action():
        user_key = key(required_text(actor, "Actor", 200))
        if any(type(flag) is not bool for flag in (active, admin, permission_active)):
            raise RosterError("Explicit Boolean flags required.")
        if not isinstance(client_id, str) or not isinstance(permission, str):
            raise RosterError("Explicit client and permission required.")
        if bool(client_id) != bool(permission) or permission not in {"", "CAN_VIEW", "CAN_EDIT", "CLIENT_ADMIN"}:
            raise RosterError("Invalid permission.")
        if client_id:
            required_text(client_id, "Client", 100)
            if not query(session, "SELECT ID FROM DATASTORE.ROSTER.CLIENTS WHERE ID = ?", [client_id]):
                raise RosterError("Client not found.", "NOT_FOUND")
        store = SnapshotStore(session)
        users = store._rows("ROSTER_USERS", " WHERE USER_KEY = ?", [user_key])
        row = {"USER_KEY": user_key, "ACTIVE": active, "ADMIN": admin}
        store._delta("ROSTER_USERS", {user_key: users[0]} if users else {}, {user_key: row})
        if permission:
            prior = store._rows("PERMISSIONS", " WHERE USER_KEY = ? AND CLIENT_ID = ? AND PERMISSION = ?",
                                [user_key, client_id, permission])
            row = {"USER_KEY": user_key, "CLIENT_ID": client_id, "PERMISSION": permission, "ACTIVE": permission_active}
            row_key = (user_key, client_id, permission)
            store._delta("PERMISSIONS", {row_key: prior[0]} if prior else {}, {row_key: row})
        # CURRENT_USER identifies the controlling administrator, not the target user.
        operator = query(session, "SELECT CURRENT_USER()")[0][0]
        audit_id = uuid.uuid4().hex
        store._delta("AUDIT", {}, {audit_id: {"ID": audit_id, "ACTOR": operator, "OPERATION": "bootstrap",
            "CLIENT_ID": client_id, "AT": datetime.now(timezone.utc), "CHANGE_COUNT": 1}})
        return {"saved": True}
    return _transaction(session, action)


def cleanup(session) -> str:
    def action():
        # One timestamp for children and parents; rows cannot expire between DELETEs.
        cutoff = datetime.now(timezone.utc).isoformat()
        for name in ("UPLOAD_ROWS", "UPLOAD_ISSUES", "UPLOAD_CHANGES"):
            query(session, f"DELETE FROM {PREFIX}{name} WHERE TOKEN IN "
                  "(SELECT TOKEN FROM DATASTORE.ROSTER.UPLOADS WHERE EXPIRES_AT <= TO_TIMESTAMP_TZ(?))", [cutoff])
        query(session, "DELETE FROM DATASTORE.ROSTER.UPLOADS WHERE EXPIRES_AT <= TO_TIMESTAMP_TZ(?)", [cutoff])
        return {"cleaned": True}
    return _transaction(session, action)


def _schema_error():
    raise RosterError("Partial, unrecognized, or incompatible roster schema; controlled migration required.",
                      "SCHEMA_MISMATCH")


def _key_groups(session, command):
    grouped = {}
    for row in records(session, command):
        row = {name.lower(): value for name, value in row.items()}
        group = (row["table_name"], row["constraint_name"])
        grouped.setdefault(group, []).append((int(row["key_sequence"]), row["column_name"]))
    return {(name, tuple(column for _, column in sorted(columns)))
            for (name, _), columns in grouped.items()}


def _verify_indexes(session):
    # Constraint metadata can say ENFORCED=YES while old rows failed validation.
    # https://docs.snowflake.com/en/sql-reference/sql/create-hybrid-table#label-hybrid-table-constraint-validation-failure
    # Match the complete manifest, not generated index names or just a row count.
    expected = Counter()
    for name, spec in TABLES.items():
        expected[(name, spec["primary"], "Y")] += 1
        for columns in spec["unique"]:
            expected[(name, columns, "Y")] += 1
        for columns, _, _ in spec["foreign"]:
            expected[(name, columns, "N")] += 1
    actual = Counter()
    for entry in records(session, "SHOW INDEXES IN SCHEMA DATASTORE.ROSTER"):
        row = {field.lower(): value for field, value in entry.items()}
        if (row.get("database_name") != "DATASTORE" or row.get("schema_name") != "ROSTER"
                or row.get("kind") != "KEY_VALUE_INDEX" or row.get("status") != "ACTIVE"):
            _schema_error()
        # SHOW renders identifiers as [COL1, COL2], not a JSON string array.
        # The manifest only contains simple uppercase identifiers.
        columns = row.get("columns")
        if not isinstance(columns, str) or not columns.startswith("[") or not columns.endswith("]"):
            _schema_error()
        signature = (row.get("table"), tuple(column.strip() for column in columns[1:-1].split(",")),
                     row.get("is_unique"))
        actual[signature] += 1
    # No additional secondary indexes are declared by this strict schema contract.
    if actual != expected:
        _schema_error()


def verify_schema(session, *, allow_empty=False):
    """Inspect actual signatures, not just a marker/comment or table existence.

    Run as the schema owner with complete metadata visibility. Schema DDL must
    be quiesced; the runtime mutex cannot serialize concurrent DDL auto-commits.
    """
    existing = records(session, "SELECT TABLE_NAME, IS_HYBRID FROM DATASTORE.INFORMATION_SCHEMA.TABLES "
                       "WHERE TABLE_SCHEMA = 'ROSTER'")
    if not existing and allow_empty:
        return False
    if len(existing) != len(TABLES) or {row["TABLE_NAME"] for row in existing} != set(TABLES):
        _schema_error()
    if any(row["IS_HYBRID"] != "YES" for row in existing):
        _schema_error()
    columns = records(session, "SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE, IS_NULLABLE, "
                      "CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE, "
                      "COLUMN_DEFAULT, IS_IDENTITY "
                      "FROM DATASTORE.INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'ROSTER' "
                      "ORDER BY TABLE_NAME, ORDINAL_POSITION")
    # INFORMATION_SCHEMA.DATETIME_PRECISION and COLLATION_NAME are not applicable.
    # DESCRIBE carries exact timestamp precision and any collation in its type.
    described = {}
    for name in TABLES:
        for entry in records(session, f"DESCRIBE TABLE {PREFIX}{name}"):
            entry = {field.lower(): value for field, value in entry.items()}
            if entry["kind"] != "COLUMN":
                _schema_error()
            described[(name, entry["name"])] = entry["type"]
    actual = {}
    for row in columns:
        if row["COLUMN_DEFAULT"] is not None or row["IS_IDENTITY"] != "NO":
            _schema_error()
        kind = row["DATA_TYPE"]
        if kind in {"TEXT", "VARCHAR"}:
            kind = f"VARCHAR({int(row['CHARACTER_MAXIMUM_LENGTH'])})"
        elif kind == "NUMBER":
            kind = f"NUMBER({int(row['NUMERIC_PRECISION'])},{int(row['NUMERIC_SCALE'])})"
        elif kind.startswith("TIMESTAMP"):
            kind = described.get((row["TABLE_NAME"], row["COLUMN_NAME"]))
        if described.get((row["TABLE_NAME"], row["COLUMN_NAME"])) != kind:
            _schema_error()
        actual.setdefault(row["TABLE_NAME"], []).append((row["COLUMN_NAME"], kind, row["IS_NULLABLE"] == "YES"))
    if actual != {name: spec["columns"] for name, spec in TABLES.items()}:
        _schema_error()
    primary = _key_groups(session, "SHOW PRIMARY KEYS IN SCHEMA DATASTORE.ROSTER")
    unique = _key_groups(session, "SHOW UNIQUE KEYS IN SCHEMA DATASTORE.ROSTER")
    if primary != {(name, spec["primary"]) for name, spec in TABLES.items()}:
        _schema_error()
    if unique != {(name, columns) for name, spec in TABLES.items() for columns in spec["unique"]}:
        _schema_error()
    foreign = {}
    for row in records(session, "SHOW IMPORTED KEYS IN SCHEMA DATASTORE.ROSTER"):
        row = {name.lower(): value for name, value in row.items()}
        if any(row[field] != value for field, value in (("pk_database_name", "DATASTORE"),
                ("pk_schema_name", "ROSTER"), ("fk_database_name", "DATASTORE"), ("fk_schema_name", "ROSTER"))):
            _schema_error()
        group = (row["fk_table_name"], row["fk_name"], row["pk_table_name"])
        foreign.setdefault(group, []).append((int(row["key_sequence"]), row["fk_column_name"], row["pk_column_name"]))
    actual_foreign = {(name, tuple(item[1] for item in sorted(parts)), target,
                       tuple(item[2] for item in sorted(parts))) for (name, _, target), parts in foreign.items()}
    expected_foreign = {(name, columns, target, target_columns) for name, spec in TABLES.items()
                        for columns, target, target_columns in spec["foreign"]}
    if actual_foreign != expected_foreign:
        _schema_error()
    constraints = records(session, "SELECT CONSTRAINT_TYPE, ENFORCED FROM DATASTORE.INFORMATION_SCHEMA.TABLE_CONSTRAINTS "
                          "WHERE TABLE_SCHEMA = 'ROSTER'")
    expected_count = sum(1 + len(spec["unique"]) + len(spec["foreign"]) for spec in TABLES.values())
    if len(constraints) != expected_count or any(row["ENFORCED"] != "YES" or row["CONSTRAINT_TYPE"] not in
           {"PRIMARY KEY", "UNIQUE", "FOREIGN KEY"} for row in constraints):
        _schema_error()
    _verify_indexes(session)
    return True


def install_schema(session) -> str:
    """Owner-only, offline-authored setup, not a runtime operation.

    DDL is not transactional. A failed fresh install deliberately leaves a
    partial schema that subsequent runs reject; never auto-repair unknown data.
    Upload the reviewed zip before invoking, with no concurrent deployment/DDL.
    """
    if verify_schema(session, allow_empty=True):
        # No table modification on existing installs, even for mutex repair.
        check_version(session)
        rows = query(session, "SELECT ID FROM DATASTORE.ROSTER.ROSTER_MUTEX")
        if len(rows) != 1 or rows[0][0] != 1:
            _schema_error()
        return "Compatible schema; no changes."
    for name, spec in TABLES.items():
        definitions = [f'"{column}" {kind}' + ("" if nullable else " NOT NULL")
                       for column, kind, nullable in spec["columns"]]
        quoted = lambda names: ", ".join(f'"{name}"' for name in names)
        definitions.append("PRIMARY KEY (" + quoted(spec["primary"]) + ")")
        definitions.extend("UNIQUE (" + quoted(columns) + ")" for columns in spec["unique"])
        definitions.extend("FOREIGN KEY (" + quoted(columns) + f") REFERENCES {PREFIX}{target} (" +
                           quoted(target_columns) + ")" for columns, target, target_columns in spec["foreign"])
        # Override account/database/schema defaults, including for indexed text.
        # https://docs.snowflake.com/en/sql-reference/sql/create-hybrid-table#label-hybrid-table-collations-disable
        query(session, f"CREATE HYBRID TABLE IF NOT EXISTS {PREFIX}{name} (" + ", ".join(definitions) +
              ") DEFAULT_DDL_COLLATION = ''")
    verify_schema(session)
    begun = False
    try:
        query(session, "BEGIN TRANSACTION")
        begun = True
        query(session, "INSERT INTO DATASTORE.ROSTER.ROSTER_MUTEX (ID) VALUES (1)")
        lock(session)
        query(session, "INSERT INTO DATASTORE.ROSTER.SCHEMA_VERSION (ID, VERSION, SIGNATURE) VALUES (?, ?, ?)",
              [1, SCHEMA_VERSION, SCHEMA_SIGNATURE])
        query(session, "COMMIT")
        begun = False
    finally:
        if begun:
            query(session, "ROLLBACK")
    return "Schema initialized; no users provisioned."
