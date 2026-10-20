"""Offline contract tests only; these do NOT prove Snowflake lock/concurrency behavior.

Run in an already installed uv project environment. No credentials or live SQL.
Manual account acceptance gates are in sql/001_schema.sql and sql/002_operations.sql.
"""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from employee_roster import handler
from employee_roster.directory import DIRECTORY_SQL, SnowflakeDirectory
from employee_roster.domain import RosterError
from employee_roster.persistence import (SCHEMA_SIGNATURE, SCHEMA_VERSION, TABLES, SnapshotStore,
                                         SnowflakeRepository, encode, flatten)

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


class Row(dict):
    def __getitem__(self, index):
        if isinstance(index, int):
            return list(self.values())[index]
        return super().__getitem__(index)

    def __iter__(self):
        return iter(self.values())

    def as_dict(self):
        return dict(self.items())


class FakeSession:
    def __init__(self, respond=None, failure=None, version=SCHEMA_VERSION):
        self.version = version
        self.events = []
        self.respond = respond
        self.failure = failure
        self.writes = []
        self.pending = []
        self.transaction = False

    def sql(self, sql, params=None):
        session = self

        class Frame:
            def collect(self, statement_params=None):
                session.events.append((sql, params, statement_params))
                if session.failure and session.failure(sql):
                    raise RuntimeError("secret-driver-sql-and-credential")
                if sql == "BEGIN TRANSACTION":
                    assert not session.transaction
                    session.transaction = True
                    session.pending = []
                elif sql == "COMMIT":
                    session.writes.extend(session.pending)
                    session.pending = []
                    session.transaction = False
                elif sql == "ROLLBACK":
                    session.pending = []
                    session.transaction = False
                elif sql.startswith(("INSERT", "UPDATE", "DELETE")):
                    assert session.transaction
                    session.pending.append((sql, params))
                elif "FOR UPDATE" in sql:
                    assert session.transaction
                    return [Row(ID=1)]
                elif sql.startswith("SELECT ID, VERSION, SIGNATURE"):
                    return [Row(ID=1, VERSION=session.version, SIGNATURE=SCHEMA_SIGNATURE)]
                if session.respond:
                    return session.respond(sql, params)
                return []
        return Frame()


def seed():
    return {"clients": {"1": {"id": "1", "name": "First", "active": True, "version": 1, "types": []}},
            "employees": {}, "assignments": {}, "users": {"ALICE": {"active": True, "admin": True}},
            "permissions": [], "uploads": {}, "audit": []}


def upload(validation=None):
    return {"token": "token", "client_id": "1", "actor": "ALICE", "sha256": "a" * 64,
            "created_at": NOW.isoformat(), "expires_at": "2026-10-15T12:30:00+00:00",
            "effective": "2026-10-15", "config_version": 1,
            "expected": {"roster": {}, "employees": {"JANE.DOE": None}},
            "parsed": {"columns": ["DOMAINID"], "rows": [{"row": 2, "domain": "JANE.DOE", "values": {}}],
                       "issues": [{"severity": "warning", "row": 1, "column": "", "message": "Parsed issue"}]},
            "validation": validation or {"issues": [], "changes": []}, "completed": False, "result": None}


def test_client_is_one_bound_call_and_returns_envelope():
    payload = {"client_id": "' ); DELETE FROM CLIENTS; --"}
    response = {"ok": True, "data": {"rows": []}}
    session = FakeSession(lambda sql, params: [Row(RESULT=json.dumps(response))])
    assert SnowflakeRepository(session).call("O'BRIEN", "roster", payload) == response
    assert session.events == [("CALL DATASTORE.ROSTER.ROSTER_EXECUTE(?, ?, ?)",
                               ["O'BRIEN", "roster", encode(payload)], None)]


@pytest.mark.parametrize("response", [None, '{"ok":true}', '{"ok":"yes"}',
    '{"ok":false,"error":{"code":"UNSAFE","message":"secret","correlation_id":"invalid"}}'])
def test_client_sanitizes_transport_and_bad_responses(response):
    session = FakeSession(lambda sql, params: [Row(RESULT=response)],
                          failure=(lambda sql: True) if response is None else None)
    result = SnowflakeRepository(session).call("ALICE", "clients", {})
    assert result["ok"] is False
    assert result["error"]["code"] == "INTERNAL"
    assert "secret" not in json.dumps(result)
    assert len(session.events) == 1


def test_handler_order_lock_before_permissions_and_snapshot(monkeypatch):
    session = FakeSession()
    state = seed()

    def load(self, actor, operation, payload):
        assert [event[0] for event in session.events][:2] == [
            "BEGIN TRANSACTION", "SELECT ID FROM DATASTORE.ROSTER.ROSTER_MUTEX WHERE ID = 1 FOR UPDATE WAIT 30"]
        assert "SCHEMA_VERSION" in session.events[2][0]
        session.events.append(("SNAPSHOT_PERMISSIONS", None, None))
        return state

    monkeypatch.setattr(SnapshotStore, "load", load)
    result = json.loads(handler.main(session, "alice", "clients", "{}"))
    assert result["ok"] is True
    assert result["data"]["admin"] is True
    assert session.events[-1][0] == "COMMIT"
    assert not session.writes
    assert all(event[2] == {"READ_LATEST_WRITES": "TRUE"}
               for event in session.events if event[0] != "SNAPSHOT_PERMISSIONS")


def test_mid_delta_failure_rolls_back_everything_including_audit(monkeypatch):
    session = FakeSession(failure=lambda sql: sql.startswith("INSERT INTO DATASTORE.ROSTER.AUDIT"))
    monkeypatch.setattr(SnapshotStore, "load", lambda *args: seed())
    result = json.loads(handler.main(session, "ALICE", "save_client", encode({
        "id": "1", "name": "Changed", "active": True, "version": 1})))
    assert result["ok"] is False
    assert result["error"]["code"] == "INTERNAL"
    assert "secret" not in json.dumps(result)
    assert any(event[0].startswith("UPDATE DATASTORE.ROSTER.CLIENTS") for event in session.events)
    assert session.events[-1][0] == "ROLLBACK"
    assert session.writes == session.pending == []
    assert not any(event[0] == "COMMIT" for event in session.events)


def test_engine_error_and_fresh_revocation_rollback(monkeypatch):
    session = FakeSession()
    state = seed()
    state["users"]["ALICE"]["active"] = False
    monkeypatch.setattr(SnapshotStore, "load", lambda *args: state)
    result = json.loads(handler.main(session, "ALICE", "clients", "{}"))
    assert result["error"]["code"] == "FORBIDDEN"
    assert session.events[-1][0] == "ROLLBACK"


@pytest.mark.parametrize("failure", ["BEGIN TRANSACTION", "FOR UPDATE", "SCHEMA_VERSION"])
def test_failure_before_snapshot_never_loads(monkeypatch, failure):
    session = FakeSession(failure=lambda sql: failure in sql)
    monkeypatch.setattr(SnapshotStore, "load", lambda *args: pytest.fail("Snapshot must not be read"))
    result = json.loads(handler.main(session, "ALICE", "clients", "{}"))
    assert result["ok"] is False
    assert "secret" not in json.dumps(result)
    assert (session.events[-1][0] == "ROLLBACK") == (failure != "BEGIN TRANSACTION")


def test_schema_mismatch_rolls_back_before_snapshot(monkeypatch):
    session = FakeSession(version=999)
    monkeypatch.setattr(SnapshotStore, "load", lambda *args: pytest.fail("No snapshot"))
    result = json.loads(handler.main(session, "ALICE", "clients", "{}"))
    assert result["error"]["code"] == "SCHEMA_MISMATCH"
    assert "secret" not in json.dumps(result)
    assert session.events[-1][0] == "ROLLBACK"


def test_deltas_preserve_original_and_normalized_values_and_history():
    before = seed()
    after = deepcopy(before)
    after["employees"]["JANE.DOE"] = {"id": "employee", "domain": "Jane.Doe"}
    after["assignments"]["1:JANE.DOE"] = {"id": "assignment", "client_id": "1", "domain": "Jane.Doe",
        "active": True, "version": 1, "history": [{"id": "identifier", "code": "BADGE", "value": "MiXeD",
        "start": "2026-01-01", "end": None, "superseded_at": None, "superseded_by": None}]}
    rows = flatten(after)
    assert rows["EMPLOYEES"]["employee"]["DOMAIN"] == "Jane.Doe"
    assert rows["EMPLOYEES"]["employee"]["DOMAIN_KEY"] == "JANE.DOE"
    assert rows["IDENTIFIERS"]["identifier"]["VALUE_KEY"] == "MIXED"
    session = FakeSession()
    session.transaction = True
    store = SnapshotStore(session)
    store.save(before, after)
    assert all(not event[0].startswith("DELETE") for event in session.events)
    identifier_insert = next(event for event in session.events if event[0].startswith(
        "INSERT INTO DATASTORE.ROSTER.IDENTIFIERS"))
    assert "TO_DATE(?)" in identifier_insert[0]
    assert "TO_TIMESTAMP_TZ(?)" in identifier_insert[0]
    removed = deepcopy(after)
    removed["assignments"]["1:JANE.DOE"]["history"] = []
    with pytest.raises(RosterError):
        store.save(after, removed)


def test_staging_round_trip_includes_parsed_issues_validation_and_changes():
    state = seed()
    validation = {"issues": [{"severity": "warning", "message": "Different client"}],
                  "changes": [{"domain": "JANE.DOE", "kind": "Added", "differences": []}]}
    state["uploads"]["token"] = upload(validation)
    tables = flatten(state)
    tables["ROSTER_USERS"] = {"ALICE": {"USER_KEY": "ALICE", "ACTIVE": True, "ADMIN": True}}
    tables["PERMISSIONS"] = {}

    def respond(sql, params):
        for name, rows in tables.items():
            if f" FROM DATASTORE.ROSTER.{name}" in sql:
                values = list(rows.values())
                if name in {"UPLOAD_ISSUES", "UPLOAD_CHANGES"}:
                    values = sorted(values, key=lambda item: item["POSITION"])
                return [Row(**item) for item in values]
        return []
    loaded = SnapshotStore(FakeSession(respond)).load("ALICE", "review", {"client_id": "1", "token": "token"})
    assert loaded["uploads"]["token"] == state["uploads"]["token"]


def test_validation_revisions_append_instead_of_delete():
    before = seed()
    before["uploads"]["token"] = upload({"issues": [{"message": "Old"}], "changes": []})
    after = deepcopy(before)
    after["uploads"]["token"]["validation"] = {"issues": [{"message": "New"}], "changes": []}
    session = FakeSession()
    session.transaction = True
    SnapshotStore(session).save(before, after)
    assert any(event[0].startswith("INSERT INTO DATASTORE.ROSTER.UPLOAD_ISSUES") for event in session.events)
    assert all(not event[0].startswith("DELETE") for event in session.events)


def test_missing_staging_baseline_fails_closed():
    state = seed()
    state["uploads"]["token"] = upload()
    del state["uploads"]["token"]["validation"]
    with pytest.raises(KeyError):
        flatten(state)


def test_directory_batches_binds_and_preserves_filters():
    session = FakeSession(lambda sql, params: [])
    domains = [f"employee.{index}" for index in range(1001)] + ["O'BRIEN,NAME"]
    assert SnowflakeDirectory(session).lookup(domains) == {}
    assert [len(json.loads(event[1][0])) for event in session.events] == [1000, 2]
    assert all(event[0] == DIRECTORY_SQL for event in session.events)
    assert "O'BRIEN" not in DIRECTORY_SQL
    assert "IFNULL(E.TERMINATIONDATE, '9999-12-31') >= '2020-01-01'" in DIRECTORY_SQL
    assert "TRY_TO_NUMBER(E.CLIENTCODE) IS NOT NULL" in DIRECTORY_SQL
    assert "LOWER(TRIM(E.DOMAINID)) = D.DOMAINID" in DIRECTORY_SQL
    assert "DENSE_RANK()" in DIRECTORY_SQL and "E.HIREDATE DESC NULLS LAST" in DIRECTORY_SQL


@pytest.mark.parametrize("second", [{"CLIENTID": 2, "ISACTIVE": True}, {"CLIENTID": 1, "ISACTIVE": False}])
def test_directory_rejects_differing_latest_ties(second):
    session = FakeSession(lambda sql, params: [Row(DOMAINID="jane.doe", CLIENTID=1, ISACTIVE=True),
                                              Row(DOMAINID="Jane.Doe", **second)])
    with pytest.raises(RosterError) as caught:
        SnowflakeDirectory(session).lookup(["JANE.DOE"])
    assert caught.value.code == "DIRECTORY_UNAVAILABLE"
    assert "jane" not in str(caught.value).lower()


def test_directory_accepts_identical_ties_and_no_stub_fallback():
    row = Row(DOMAINID="jane.doe", CLIENTID=1, ISACTIVE=False)
    session = FakeSession(lambda sql, params: [row, row])
    assert SnowflakeDirectory(session).lookup(["JANE.DOE"]) == {"JANE.DOE": {"client_id": "1", "active": False}}
    assert SnowflakeDirectory(FakeSession()).lookup([]) == {}
    with pytest.raises(RosterError):
        SnowflakeDirectory(FakeSession(failure=lambda sql: True)).lookup(["JANE.DOE"])


def test_cleanup_locks_before_deleting_children_then_parent():
    session = FakeSession()
    assert json.loads(handler.cleanup(session))["ok"] is True
    sql = [event[0] for event in session.events]
    assert "FOR UPDATE" in sql[1]
    assert [text.split()[2] for text in sql if text.startswith("DELETE")] == [
        "DATASTORE.ROSTER.UPLOAD_ROWS", "DATASTORE.ROSTER.UPLOAD_ISSUES",
        "DATASTORE.ROSTER.UPLOAD_CHANGES", "DATASTORE.ROSTER.UPLOADS"]
    assert len({event[1][0] for event in session.events if event[0].startswith("DELETE")}) == 1
    assert sql[-1] == "COMMIT"


def test_cleanup_partial_failure_rolls_back():
    session = FakeSession(failure=lambda sql: sql.startswith("DELETE FROM DATASTORE.ROSTER.UPLOAD_CHANGES"))
    assert json.loads(handler.cleanup(session))["ok"] is False
    assert session.events[-1][0] == "ROLLBACK"
    assert session.writes == session.pending == []


def test_bootstrap_uses_mutex_and_explicit_user_flags():
    session = FakeSession(lambda sql, params: [Row(USER="CONTROLLED_OWNER")] if sql == "SELECT CURRENT_USER()" else [])
    result = json.loads(handler.bootstrap(session, "alice", True, False, "", "", False))
    assert result["ok"] is True
    assert "FOR UPDATE" in session.events[1][0]
    insert = next(event for event in session.events if event[0].startswith("INSERT INTO DATASTORE.ROSTER.ROSTER_USERS"))
    assert insert[1] == ["ALICE", True, False]
    assert session.events[-1][0] == "COMMIT"


@pytest.mark.parametrize("existing_user", [False, True], ids=["insert", "update"])
def test_bootstrap_user_only_commits_without_client_or_permission_access(existing_user):
    def respond(sql, params):
        if sql == "SELECT CURRENT_USER()":
            return [Row(USER="CONTROLLED_OWNER")]
        if " FROM DATASTORE.ROSTER.ROSTER_USERS" in sql and existing_user:
            return [Row(USER_KEY="ALICE", ACTIVE=False, ADMIN=True)]
        return []

    session = FakeSession(respond)
    result = json.loads(handler.bootstrap(session, "alice", True, False, "", "", False))
    assert result == {"ok": True, "data": {"saved": True}}
    statements = [event[0] for event in session.events]
    assert statements[-1] == "COMMIT"
    assert "ROLLBACK" not in statements
    assert not any("DATASTORE.ROSTER.CLIENTS" in sql or "DATASTORE.ROSTER.PERMISSIONS" in sql
                   for sql in statements)
    assert len(session.writes) == 2
    user_write, audit_write = session.writes
    assert user_write[0].startswith("UPDATE" if existing_user else "INSERT")
    assert "DATASTORE.ROSTER.ROSTER_USERS" in user_write[0]
    assert audit_write[0].startswith("INSERT INTO DATASTORE.ROSTER.AUDIT")
    assert audit_write[1][1:4] == ["CONTROLLED_OWNER", "bootstrap", ""]


def metadata_session(*, missing=None, standard=None, bad_column=False, bad_keys=False,
                     transform=None, fresh=False):
    def respond(sql, params):
        if "INFORMATION_SCHEMA.TABLES " in sql:
            if fresh and not any(event[0].startswith("CREATE HYBRID TABLE") for event in session.events):
                return []
            return [Row(TABLE_NAME=name, IS_HYBRID="NO" if name == standard else "YES")
                    for name in TABLES if name != missing]
        if "INFORMATION_SCHEMA.COLUMNS " in sql:
            result = []
            for name, spec in sorted(TABLES.items()):
                for position, (column, kind, nullable) in enumerate(spec["columns"], 1):
                    result.append(Row(TABLE_NAME=name, COLUMN_NAME=column, ORDINAL_POSITION=position,
                        DATA_TYPE=kind.split("(")[0], IS_NULLABLE="YES" if nullable else "NO",
                        CHARACTER_MAXIMUM_LENGTH=int(kind[8:-1]) if kind.startswith("VARCHAR") else None,
                        NUMERIC_PRECISION=38, NUMERIC_SCALE=0, DATETIME_PRECISION=None,
                        COLUMN_DEFAULT=None, IS_IDENTITY="NO", COLLATION_NAME=None))
            if bad_column:
                result[0]["COLUMN_NAME"] = "UNRECOGNIZED"
            return result
        if sql.startswith("DESCRIBE TABLE "):
            name = sql.rsplit(".", 1)[1]
            return [Row(name=column, type=kind, kind="COLUMN") for column, kind, _ in TABLES[name]["columns"]]
        if sql.startswith(("SHOW PRIMARY", "SHOW UNIQUE")):
            output = []
            for name, spec in TABLES.items():
                groups = [spec["primary"]] if sql.startswith("SHOW PRIMARY") else spec["unique"]
                for index, columns in enumerate(groups):
                    for sequence, column in enumerate(columns, 1):
                        output.append(Row(table_name=name, constraint_name=f"key_{index}",
                                          key_sequence=sequence, column_name=column))
            return output[1:] if bad_keys else output
        if sql.startswith("SHOW IMPORTED"):
            return [Row(fk_database_name="DATASTORE", fk_schema_name="ROSTER", fk_table_name=name,
                        fk_name=f"fk_{index}", pk_database_name="DATASTORE", pk_schema_name="ROSTER",
                        pk_table_name=target, key_sequence=sequence, fk_column_name=column, pk_column_name=target_column)
                    for name, spec in TABLES.items() for index, (columns, target, target_columns) in enumerate(spec["foreign"])
                    for sequence, (column, target_column) in enumerate(zip(columns, target_columns), 1)]
        if "TABLE_CONSTRAINTS" in sql:
            return [Row(CONSTRAINT_TYPE=kind, ENFORCED="YES")
                    for spec in TABLES.values()
                    for kind in ["PRIMARY KEY"] + ["UNIQUE"] * len(spec["unique"]) +
                    ["FOREIGN KEY"] * len(spec["foreign"])]
        if sql == "SHOW INDEXES IN SCHEMA DATASTORE.ROSTER":
            return [Row(name=f"index_{index}", kind="KEY_VALUE_INDEX", table=name,
                        database_name="DATASTORE", schema_name="ROSTER", is_unique=is_unique,
                        columns="[" + ", ".join(columns) + "]", included_columns="[]", status="ACTIVE")
                    for name, spec in TABLES.items()
                    for index, (columns, is_unique) in enumerate(
                        [(spec["primary"], "Y")] + [(columns, "Y") for columns in spec["unique"]] +
                        [(columns, "N") for columns, _, _ in spec["foreign"]])]
        if sql == "SELECT ID FROM DATASTORE.ROSTER.ROSTER_MUTEX":
            return [Row(ID=1)]
        return []
    def wrapped(sql, params):
        rows = respond(sql, params)
        return transform(sql, rows) if transform else rows

    session = FakeSession(wrapped)
    return session


@pytest.mark.parametrize("settings", [{"missing": "EMPLOYEES"}, {"standard": "CLIENTS"},
                                      {"bad_column": True}, {"bad_keys": True}])
def test_installer_rejects_incompatible_targets_before_any_mutation(settings):
    session = metadata_session(**settings)
    with pytest.raises(RosterError) as caught:
        handler.install_schema(session)
    assert caught.value.code == "SCHEMA_MISMATCH"
    assert all(not event[0].startswith(("INSERT", "UPDATE", "CREATE", "DELETE")) for event in session.events)


def test_compatible_installer_is_noop():
    session = metadata_session()
    assert handler.install_schema(session) == "Compatible schema; no changes."
    assert all(not event[0].startswith(("INSERT", "UPDATE", "CREATE", "DELETE")) for event in session.events)


@pytest.mark.parametrize("index_kind", ["primary", "unique", "foreign"])
@pytest.mark.parametrize("status", ["SUSPENDED", "BUILD FAILURE", "BUILD IN PROGRESS",
                                     "BUILD VALIDATION FAILURE", "UNKNOWN", None, ""])
def test_installer_rejects_nonactive_indexes_despite_enforced_keys(index_kind, status):
    target = {"primary": ("EMPLOYEES", "[ID]"), "unique": ("EMPLOYEES", "[DOMAIN_KEY]"),
              "foreign": ("ASSIGNMENTS", "[CLIENT_ID]")}[index_kind]

    def transform(sql, rows):
        if sql.startswith("SHOW INDEXES"):
            row = next(row for row in rows if (row["table"], row["columns"]) == target)
            row["status"] = status
        return rows

    session = metadata_session(transform=transform)
    with pytest.raises(RosterError) as caught:
        handler.install_schema(session)
    assert caught.value.code == "SCHEMA_MISMATCH"
    assert any("TABLE_CONSTRAINTS" in event[0] for event in session.events)
    assert session.events[-1][0] == "SHOW INDEXES IN SCHEMA DATASTORE.ROSTER"
    assert all(not event[0].startswith(("INSERT", "UPDATE", "CREATE", "DELETE", "BEGIN"))
               for event in session.events)


@pytest.mark.parametrize("change", ["empty", "missing", "duplicate", "extra", "wrong_table", "wrong_columns",
                                    "reordered_columns", "wrong_unique", "wrong_database", "wrong_schema",
                                    "wrong_kind", "missing_status", "malformed_columns", "null_columns"])
def test_installer_requires_exact_index_coverage(change):
    def transform(sql, rows):
        if not sql.startswith("SHOW INDEXES"):
            return rows
        if change == "empty":
            return []
        if change == "missing":
            return rows[1:]
        if change == "duplicate":
            rows[0] = Row(**rows[1])  # Correct count, but one required index is absent.
        elif change == "extra":
            rows.append(Row(**dict(rows[0], name="extra", columns="[SIGNATURE]", is_unique="N")))
        elif change == "reordered_columns":
            row = next(row for row in rows if row["columns"] == "[CLIENT_ID, DOMAIN_KEY]")
            row["columns"] = "[DOMAIN_KEY, CLIENT_ID]"
        elif change == "missing_status":
            del rows[0]["status"]
        else:
            field, value = {
                "wrong_table": ("table", "UNRELATED"), "wrong_columns": ("columns", "[VERSION]"),
                "wrong_unique": ("is_unique", "N"), "wrong_database": ("database_name", "OTHER"),
                "wrong_schema": ("schema_name", "OTHER"), "wrong_kind": ("kind", "UNKNOWN"),
                "malformed_columns": ("columns", "ID"), "null_columns": ("columns", None),
            }[change]
            rows[0][field] = value
        return rows

    session = metadata_session(transform=transform)
    with pytest.raises(RosterError) as caught:
        handler.install_schema(session)
    assert caught.value.code == "SCHEMA_MISMATCH"
    assert all(not event[0].startswith(("INSERT", "UPDATE", "CREATE", "DELETE", "BEGIN"))
               for event in session.events)


def test_index_metadata_names_and_result_order_do_not_define_compatibility():
    def transform(sql, rows):
        if sql.startswith("SHOW INDEXES"):
            for position, row in enumerate(rows):
                row["name"] = f"renamed_{position}"
            return [Row(**{field.upper(): value for field, value in row.items()}) for row in reversed(rows)]
        return rows

    assert handler.install_schema(metadata_session(transform=transform)) == "Compatible schema; no changes."


@pytest.mark.parametrize("change", ["default", "identity", "timestamp_precision", "collation"])
def test_installer_rejects_column_semantic_drift(change):
    def transform(sql, rows):
        if "INFORMATION_SCHEMA.COLUMNS " in sql:
            if change == "default":
                rows[0]["COLUMN_DEFAULT"] = "'unexpected'"
            elif change == "identity":
                next(row for row in rows if row["TABLE_NAME"] == "ROSTER_MUTEX")["IS_IDENTITY"] = "YES"
        if sql == "DESCRIBE TABLE DATASTORE.ROSTER.AUDIT" and change == "timestamp_precision":
            next(row for row in rows if row["name"] == "AT")["type"] = "TIMESTAMP_TZ(3)"
        if sql == "DESCRIBE TABLE DATASTORE.ROSTER.CLIENTS" and change == "collation":
            next(row for row in rows if row["name"] == "NAME")["type"] = "VARCHAR(100) COLLATE 'de'"
        return rows

    session = metadata_session(transform=transform)
    with pytest.raises(RosterError) as caught:
        handler.install_schema(session)
    assert caught.value.code == "SCHEMA_MISMATCH"
    assert all(not event[0].startswith(("INSERT", "UPDATE", "CREATE", "DELETE", "BEGIN"))
               for event in session.events)


def test_fresh_installer_pins_no_collation_and_verifies_before_seeding():
    session = metadata_session(fresh=True)
    assert handler.install_schema(session) == "Schema initialized; no users provisioned."
    statements = [event[0] for event in session.events]
    creates = [sql for sql in statements if sql.startswith("CREATE HYBRID TABLE")]
    assert len(creates) == len(TABLES)
    for name, sql in zip(TABLES, creates):
        assert sql.startswith(f"CREATE HYBRID TABLE IF NOT EXISTS DATASTORE.ROSTER.{name} (")
        assert sql.endswith(") DEFAULT_DDL_COLLATION = ''")
    assert statements.index("SHOW INDEXES IN SCHEMA DATASTORE.ROSTER") < statements.index("BEGIN TRANSACTION")
    assert statements[-1] == "COMMIT"
    assert len(session.writes) == 2
    assert not any("SHOW PARAMETERS" in sql or sql.startswith("ALTER") for sql in statements)


def test_fresh_installer_does_not_seed_if_post_ddl_indexes_fail_validation():
    def transform(sql, rows):
        if sql.startswith("SHOW INDEXES"):
            rows[0]["status"] = "BUILD VALIDATION FAILURE"
        return rows

    session = metadata_session(fresh=True, transform=transform)
    with pytest.raises(RosterError) as caught:
        handler.install_schema(session)
    assert caught.value.code == "SCHEMA_MISMATCH"
    assert sum(event[0].startswith("CREATE HYBRID TABLE") for event in session.events) == len(TABLES)
    assert not any(event[0].startswith(("INSERT", "BEGIN")) for event in session.events)


def test_manifest_constraints_and_types():
    assert TABLES["EMPLOYEES"]["unique"] == (("DOMAIN_KEY",),)
    assert TABLES["ASSIGNMENTS"]["unique"] == (("CLIENT_ID", "DOMAIN_KEY"),)
    assert all(kind not in {"VARIANT", "ARRAY", "OBJECT"} for spec in TABLES.values()
               for _, kind, _ in spec["columns"])
    with pytest.raises(RosterError):
        encode({"too_large": "x" * 100}, maximum=10)


def test_local_package_has_only_allowlisted_modules(tmp_path):
    spec = importlib.util.spec_from_file_location("package_handler", ROOT / "tools" / "package_handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.build(tmp_path / "employee_roster.zip")
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == {"employee_roster/" + name for name in module.PACKAGE_FILES}
        assert "employee_roster/handler.py" in archive.namelist()
        assert not any(token in name for name in archive.namelist() for token in ("excel", "streamlit", "demo", "tests"))
    with pytest.raises(FileExistsError):
        module.build(output)


def test_sql_owner_boundary_and_fail_closed_bootstrap():
    operations = (ROOT / "sql" / "002_operations.sql").read_text()
    bootstrap = (ROOT / "sql" / "003_bootstrap.sql").read_text()
    cleanup = (ROOT / "sql" / "004_cleanup.sql").read_text()
    assert "EXECUTE AS OWNER" in operations
    assert "@DATASTORE.ROSTER.ROSTER_CODE/employee_roster.zip" in operations
    assert "$ROSTER_BOOTSTRAP_ACTOR" in bootstrap
    assert "SET ROSTER_BOOTSTRAP_ACTOR" not in bootstrap
    assert "CREATE PROCEDURE" not in cleanup
    assert "WITH CLEANUP_ROSTER AS PROCEDURE" in cleanup
