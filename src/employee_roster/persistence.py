"""Single-CALL client and normalized, lock-scoped Snowflake persistence.

No session construction, credentials, UI imports, DDL during operations, or client
transaction state. All interpolated identifiers below come from this manifest.
Only the trusted app owner may call the procedure: actor is an app assertion,
not a SQL security principal. Viewers must never have procedure/table privileges.
"""
from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
import json
import uuid

from .domain import RosterError, key

PREFIX = "DATASTORE.ROSTER."
SCHEMA_VERSION = 1
JSON_LIMIT = 8_388_608
STATEMENT_PARAMS = {"READ_LATEST_WRITES": "TRUE"}
SAFE_MESSAGES = {
    "VALIDATION": "Invalid request; check the supplied values.",
    "FORBIDDEN": "Access denied.",
    "NOT_FOUND": "Record not found.",
    "EXPIRED": "Upload expired; stage it again.",
    "STALE": "Roster or configuration changed; stage the upload again.",
    "CONFLICT": "Record changed or conflicts with existing data; refresh and try again.",
    "INACTIVE": "Client is inactive.",
    "DIRECTORY_UNAVAILABLE": "HRIS lookup failed; no changes were applied.",
    "SCHEMA_MISMATCH": "Roster schema is incompatible; contact an administrator.",
    "INTERNAL": "Roster request failed. Refresh before retrying; contact support with the correlation ID.",
}


def error_envelope(code="INTERNAL", correlation_id=None):
    code = code if code in SAFE_MESSAGES else "INTERNAL"
    return {"ok": False, "error": {"code": code, "message": SAFE_MESSAGES[code],
                                  "correlation_id": correlation_id or uuid.uuid4().hex}}


def encode(value, maximum=JSON_LIMIT):
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > maximum:
        raise RosterError("Serialized data exceeds supported size.")
    return encoded


class SnowflakeRepository:
    """Supply the UI's st.connection Snowpark session; do not wrap call in BEGIN.

    Returns the JSON envelope as a dict, not just its data member. A transport
    failure has unknown commit outcome: never blindly retry a write.
    """
    def __init__(self, session):
        self.session = session

    def call(self, actor: str, operation: str, payload: dict) -> dict:
        try:
            rows = self.session.sql(
                "CALL DATASTORE.ROSTER.ROSTER_EXECUTE(?, ?, ?)",
                params=[actor, operation, encode(payload)],
            ).collect()
            envelope = json.loads(rows[0][0])
            if type(envelope.get("ok")) is not bool:
                raise ValueError("Invalid response")
            if envelope["ok"]:
                if not isinstance(envelope.get("data"), dict):
                    raise ValueError("Invalid response")
                return {"ok": True, "data": envelope["data"]}
            error = envelope["error"]
            correlation_id = str(uuid.UUID(error["correlation_id"]))
            return error_envelope(error["code"], correlation_id)
        except RosterError as exc:
            return error_envelope(exc.code)
        except Exception:
            return error_envelope()


# (column name, SQL type, nullable). No semi-structured hybrid columns.
# JSON is restricted to bounded staging fragments, never entire roster state.
def column(name, sql_type, nullable=False):
    return name, sql_type, nullable


def table(columns, primary, unique=(), foreign=()):
    return {"columns": columns, "primary": tuple(primary), "unique": tuple(unique), "foreign": tuple(foreign)}


TEXT_ID = "VARCHAR(100)"
TABLES = {
    "SCHEMA_VERSION": table([
        column("ID", "NUMBER(38,0)"), column("VERSION", "NUMBER(38,0)"),
        column("SIGNATURE", "VARCHAR(64)"),
    ], ["ID"]),
    "ROSTER_MUTEX": table([column("ID", "NUMBER(38,0)")], ["ID"]),
    "CLIENTS": table([
        column("ID", TEXT_ID), column("NAME", "VARCHAR(100)"), column("NAME_KEY", "VARCHAR(300)"),
        column("ACTIVE", "BOOLEAN"), column("VERSION", "NUMBER(38,0)"),
    ], ["ID"], [("NAME_KEY",)]),
    "IDENTIFIER_TYPES": table([
        column("ID", TEXT_ID), column("CLIENT_ID", TEXT_ID), column("CODE", "VARCHAR(100)"),
        column("DISPLAY_NAME", "VARCHAR(100)"), column("DATA_TYPE", "VARCHAR(20)"),
        column("UNIQUENESS", "VARCHAR(30)"), column("REQUIRED", "BOOLEAN"),
        column("ACTIVE", "BOOLEAN"), column("REGEX", "VARCHAR(500)"),
        column("DISPLAY_FORMAT", "VARCHAR(100)"), column("POSITION", "NUMBER(38,0)"),
    ], ["ID"], [("CLIENT_ID", "CODE")], [(("CLIENT_ID",), "CLIENTS", ("ID",))]),
    "EMPLOYEES": table([
        column("ID", TEXT_ID), column("DOMAIN", "VARCHAR(200)"), column("DOMAIN_KEY", "VARCHAR(600)"),
    ], ["ID"], [("DOMAIN_KEY",)]),
    "ASSIGNMENTS": table([
        column("ID", TEXT_ID), column("CLIENT_ID", TEXT_ID), column("DOMAIN", "VARCHAR(200)"),
        column("DOMAIN_KEY", "VARCHAR(600)"), column("ACTIVE", "BOOLEAN"),
        column("VERSION", "NUMBER(38,0)"),
    ], ["ID"], [("CLIENT_ID", "DOMAIN_KEY")], [
        (("CLIENT_ID",), "CLIENTS", ("ID",)),
        (("DOMAIN_KEY",), "EMPLOYEES", ("DOMAIN_KEY",)),
    ]),
    "IDENTIFIERS": table([
        column("ID", TEXT_ID), column("ASSIGNMENT_ID", TEXT_ID), column("CLIENT_ID", TEXT_ID),
        column("CODE", "VARCHAR(100)"), column("VALUE", "VARCHAR(200)"),
        column("VALUE_KEY", "VARCHAR(600)"), column("START_DATE", "DATE"),
        column("END_DATE", "DATE", True), column("SUPERSEDED_AT", "TIMESTAMP_TZ(9)", True),
        column("SUPERSEDED_BY", TEXT_ID, True),
    ], ["ID"], foreign=[(("ASSIGNMENT_ID",), "ASSIGNMENTS", ("ID",)),
                       (("CLIENT_ID", "CODE"), "IDENTIFIER_TYPES", ("CLIENT_ID", "CODE"))]),
    "ROSTER_USERS": table([
        column("USER_KEY", "VARCHAR(600)"), column("ACTIVE", "BOOLEAN"), column("ADMIN", "BOOLEAN"),
    ], ["USER_KEY"]),
    "PERMISSIONS": table([
        column("USER_KEY", "VARCHAR(600)"), column("CLIENT_ID", TEXT_ID),
        column("PERMISSION", "VARCHAR(30)"), column("ACTIVE", "BOOLEAN"),
    ], ["USER_KEY", "CLIENT_ID", "PERMISSION"], foreign=[
        (("USER_KEY",), "ROSTER_USERS", ("USER_KEY",)), (("CLIENT_ID",), "CLIENTS", ("ID",))]),
    "UPLOADS": table([
        column("TOKEN", "VARCHAR(200)"), column("CLIENT_ID", TEXT_ID), column("ACTOR", "VARCHAR(600)"),
        column("SHA256", "VARCHAR(64)"), column("CREATED_AT", "TIMESTAMP_TZ(9)"),
        column("EXPIRES_AT", "TIMESTAMP_TZ(9)"), column("EFFECTIVE", "DATE"),
        column("CONFIG_VERSION", "NUMBER(38,0)"), column("EXPECTED_JSON", "VARCHAR(8388608)"),
        column("COLUMNS_JSON", "VARCHAR(32768)"), column("COMPLETED", "BOOLEAN"),
        column("RESULT_JSON", "VARCHAR(32768)"), column("COMPLETED_AT", "TIMESTAMP_TZ(9)", True),
        column("VALIDATION_REVISION", "VARCHAR(64)"),
    ], ["TOKEN"], foreign=[(("CLIENT_ID",), "CLIENTS", ("ID",)),
                         (("ACTOR",), "ROSTER_USERS", ("USER_KEY",))]),
    "UPLOAD_ROWS": table([
        column("TOKEN", "VARCHAR(200)"), column("POSITION", "NUMBER(38,0)"),
        column("ROW_NUMBER", "NUMBER(38,0)"), column("DOMAIN", "VARCHAR(200)"),
        column("VALUES_JSON", "VARCHAR(262144)"),
    ], ["TOKEN", "POSITION"], foreign=[(("TOKEN",), "UPLOADS", ("TOKEN",))]),
    "UPLOAD_ISSUES": table([
        column("TOKEN", "VARCHAR(200)"), column("REVISION", "VARCHAR(64)"),
        column("POSITION", "NUMBER(38,0)"), column("ISSUE_JSON", "VARCHAR(262144)"),
    ], ["TOKEN", "REVISION", "POSITION"], foreign=[(("TOKEN",), "UPLOADS", ("TOKEN",))]),
    "UPLOAD_CHANGES": table([
        column("TOKEN", "VARCHAR(200)"), column("REVISION", "VARCHAR(64)"),
        column("POSITION", "NUMBER(38,0)"), column("CHANGE_JSON", "VARCHAR(262144)"),
    ], ["TOKEN", "REVISION", "POSITION"], foreign=[(("TOKEN",), "UPLOADS", ("TOKEN",))]),
    "AUDIT": table([
        column("ID", TEXT_ID), column("ACTOR", "VARCHAR(600)"), column("OPERATION", "VARCHAR(30)"),
        column("CLIENT_ID", TEXT_ID), column("AT", "TIMESTAMP_TZ(9)"),
        column("CHANGE_COUNT", "NUMBER(38,0)", True),
    ], ["ID"]),
}
SCHEMA_SIGNATURE = sha256(encode(TABLES).encode()).hexdigest()


def query(session, sql, params=None):
    return session.sql(sql, params=params).collect(statement_params=STATEMENT_PARAMS)


def records(session, sql, params=None):
    return [dict(row.as_dict()) for row in query(session, sql, params)]


def check_version(session):
    rows = query(session, PREFIX_SELECT_VERSION)
    if len(rows) != 1 or tuple(rows[0]) != (1, SCHEMA_VERSION, SCHEMA_SIGNATURE):
        raise RosterError("Incompatible schema.", "SCHEMA_MISMATCH")


PREFIX_SELECT_VERSION = "SELECT ID, VERSION, SIGNATURE FROM DATASTORE.ROSTER.SCHEMA_VERSION"


def lock(session):
    rows = query(session, "SELECT ID FROM DATASTORE.ROSTER.ROSTER_MUTEX WHERE ID = 1 FOR UPDATE WAIT 30")
    if len(rows) != 1 or rows[0][0] != 1:
        raise RosterError("Missing mutex.", "SCHEMA_MISMATCH")


def _iso(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _db_value(value, sql_type):
    if value is None:
        return None
    if sql_type == "DATE":
        return value if isinstance(value, str) else value.isoformat()
    if sql_type.startswith("TIMESTAMP"):
        return value if isinstance(value, str) else value.isoformat()
    return value


def _placeholder(sql_type):
    # qmark datetime binding otherwise defaults to TIMESTAMP_NTZ in the driver.
    if sql_type.startswith("TIMESTAMP_TZ"):
        return "TO_TIMESTAMP_TZ(?)"
    if sql_type == "DATE":
        return "TO_DATE(?)"
    return "?"


class SnapshotStore:
    """Used only AFTER lock and version check, within the handler transaction.

    Pagination remains in-engine. Selected-client snapshots and row-at-a-time
    deltas need realistic latency/size acceptance before production rollout.
    """
    def __init__(self, session):
        self.session = session

    def _rows(self, name, suffix="", params=None):
        columns = ", ".join('"' + entry[0] + '"' for entry in TABLES[name]["columns"])
        return records(self.session, f"SELECT {columns} FROM {PREFIX}{name}{suffix}", params)

    def load(self, actor, operation, payload):
        state = {"clients": {}, "employees": {}, "assignments": {}, "users": {},
                 "permissions": [], "uploads": {}, "audit": []}
        client_id = payload.get("client_id") if isinstance(payload, dict) else None
        # All client headers are needed for global client-name uniqueness.
        for row in self._rows("CLIENTS"):
            state["clients"][row["ID"]] = dict(id=row["ID"], name=row["NAME"], active=row["ACTIVE"],
                                              version=int(row["VERSION"]), types=[])
        for row in self._rows("ROSTER_USERS", " WHERE USER_KEY = ?", [key(actor)]):
            state["users"][row["USER_KEY"]] = {"active": row["ACTIVE"], "admin": row["ADMIN"]}
        for row in self._rows("PERMISSIONS", " WHERE USER_KEY = ? AND ACTIVE = TRUE", [key(actor)]):
            state["permissions"].append({"user": row["USER_KEY"], "client_id": row["CLIENT_ID"],
                                         "permission": row["PERMISSION"]})
        type_client = payload.get("id") if operation == "save_client" else client_id
        type_suffix = " ORDER BY CLIENT_ID, POSITION" if operation == "clients" else " WHERE CLIENT_ID = ? ORDER BY POSITION"
        for row in self._rows("IDENTIFIER_TYPES", type_suffix, None if operation == "clients" else [type_client]):
            state["clients"][row["CLIENT_ID"]]["types"].append({
                field.lower(): row[field] for field in
                ("ID", "CODE", "DISPLAY_NAME", "DATA_TYPE", "UNIQUENESS", "REQUIRED", "ACTIVE", "REGEX", "DISPLAY_FORMAT")})
        if operation in {"clients", "config", "save_client"}:
            return state
        # Deliberately global: domain identity races span client boundaries.
        for row in self._rows("EMPLOYEES"):
            state["employees"][row["DOMAIN_KEY"]] = {"id": row["ID"], "domain": row["DOMAIN"]}
        by_id = {}
        for row in self._rows("ASSIGNMENTS", " WHERE CLIENT_ID = ?", [client_id]):
            item = {"id": row["ID"], "client_id": row["CLIENT_ID"], "domain": row["DOMAIN"],
                    "active": row["ACTIVE"], "version": int(row["VERSION"]), "history": []}
            state["assignments"][f"{client_id}:{row['DOMAIN_KEY']}"] = item
            by_id[row["ID"]] = item
        for row in self._rows("IDENTIFIERS", " WHERE CLIENT_ID = ? ORDER BY START_DATE, ID", [client_id]):
            by_id[row["ASSIGNMENT_ID"]]["history"].append({
                "id": row["ID"], "code": row["CODE"], "value": row["VALUE"],
                "start": _iso(row["START_DATE"]), "end": _iso(row["END_DATE"]),
                "superseded_at": _iso(row["SUPERSEDED_AT"]), "superseded_by": row["SUPERSEDED_BY"]})
        if operation not in {"stage", "review", "submit"}:
            return state
        suffix, params = " WHERE CLIENT_ID = ? AND ACTOR = ?", [client_id, key(actor)]
        if operation in {"review", "submit"}:
            suffix += " AND TOKEN = ?"
            params.append(payload.get("token"))
        else:
            suffix += " AND SHA256 = ? AND COMPLETED = FALSE AND EXPIRES_AT > CURRENT_TIMESTAMP()"
            params.append(str(payload.get("sha256", "")).lower())
        for row in self._rows("UPLOADS", suffix, params):
            token, revision = row["TOKEN"], row["VALIDATION_REVISION"]
            upload = {"token": token, "client_id": row["CLIENT_ID"], "actor": row["ACTOR"],
                      "sha256": row["SHA256"], "created_at": _iso(row["CREATED_AT"]),
                      "expires_at": _iso(row["EXPIRES_AT"]), "effective": _iso(row["EFFECTIVE"]),
                      "config_version": int(row["CONFIG_VERSION"]), "expected": json.loads(row["EXPECTED_JSON"]),
                      "completed": row["COMPLETED"], "result": json.loads(row["RESULT_JSON"]),
                      "parsed": {"columns": json.loads(row["COLUMNS_JSON"]), "rows": [], "issues": []},
                      "validation": {"issues": [], "changes": []}}
            if row["COMPLETED_AT"] is not None:
                upload["completed_at"] = _iso(row["COMPLETED_AT"])
            for child in self._rows("UPLOAD_ROWS", " WHERE TOKEN = ? ORDER BY POSITION", [token]):
                upload["parsed"]["rows"].append({"row": int(child["ROW_NUMBER"]), "domain": child["DOMAIN"],
                                                  "values": json.loads(child["VALUES_JSON"])})
            for child in self._rows("UPLOAD_ISSUES", " WHERE TOKEN = ? AND REVISION IN (?, ?) ORDER BY POSITION",
                                    [token, "PARSED", revision]):
                target = upload["parsed"] if child["REVISION"] == "PARSED" else upload["validation"]
                target["issues"].append(json.loads(child["ISSUE_JSON"]))
            for child in self._rows("UPLOAD_CHANGES", " WHERE TOKEN = ? AND REVISION = ? ORDER BY POSITION", [token, revision]):
                upload["validation"]["changes"].append(json.loads(child["CHANGE_JSON"]))
            state["uploads"][token] = upload
        return state

    def _delta(self, name, before, after):
        """Insert/update only. Losing history is a bug, not a request to DELETE."""
        if before.keys() - after.keys():
            raise RosterError("Destructive delta refused.", "INTERNAL")
        spec = TABLES[name]
        columns = [entry[0] for entry in spec["columns"]]
        primary = spec["primary"]
        for row_key, row in after.items():
            if before.get(row_key) == row:
                continue
            if row_key not in before:
                sql = f"INSERT INTO {PREFIX}{name} (" + ", ".join(f'"{name}"' for name in columns) + ")"
                params = [_db_value(row[name], kind) for name, kind, _ in spec["columns"]]
                placeholders = [_placeholder(kind) for _, kind, _ in spec["columns"]]
                if name in {"UPLOAD_ISSUES", "UPLOAD_CHANGES"}:
                    # Revisiting a previous content-addressed validation must not
                    # duplicate its archived rows. The global mutex is held.
                    sql += " SELECT " + ", ".join(placeholders)
                    sql += f" WHERE NOT EXISTS (SELECT 1 FROM {PREFIX}{name} WHERE "
                    sql += " AND ".join(f'"{field}" = ?' for field in primary) + ")"
                    params.extend(row[field] for field in primary)
                else:
                    sql += " VALUES (" + ", ".join(placeholders) + ")"
            else:
                updated = [(name, kind) for name, kind, _ in spec["columns"] if name not in primary]
                sql = f"UPDATE {PREFIX}{name} SET " + ", ".join(
                    f'"{name}" = {_placeholder(kind)}' for name, kind in updated)
                sql += " WHERE " + " AND ".join(f'"{name}" = ?' for name in primary)
                params = [_db_value(row[name], kind) for name, kind in updated] + [row[name] for name in primary]
            query(self.session, sql, params)

    def save(self, before, after):
        old, new = flatten(before), flatten(after)
        for name in old:
            if name in {"UPLOAD_ISSUES", "UPLOAD_CHANGES"}:
                # Validation revisions are append-only until expiry cleanup.
                new[name] = {**old[name], **new[name]}
            self._delta(name, old[name], new[name])
        if after["audit"][:len(before["audit"])] != before["audit"]:
            raise RosterError("Audit rewrite refused.", "INTERNAL")
        for item in after["audit"][len(before["audit"]):]:
            row = {"ID": uuid.uuid4().hex, "ACTOR": item["actor"], "OPERATION": item["operation"],
                   "CLIENT_ID": item["client_id"], "AT": item["at"], "CHANGE_COUNT": item.get("count")}
            self._delta("AUDIT", {}, {row["ID"]: row})


def flatten(state):
    """Materialize normalized rows without retaining raw workbook bytes."""
    result = {name: {} for name in ("CLIENTS", "IDENTIFIER_TYPES", "EMPLOYEES", "ASSIGNMENTS", "IDENTIFIERS",
                                   "UPLOADS", "UPLOAD_ROWS", "UPLOAD_ISSUES", "UPLOAD_CHANGES")}
    for client in state["clients"].values():
        result["CLIENTS"][client["id"]] = {"ID": client["id"], "NAME": client["name"],
            "NAME_KEY": key(client["name"]), "ACTIVE": client["active"], "VERSION": client["version"]}
        for position, rule in enumerate(client["types"]):
            result["IDENTIFIER_TYPES"][rule["id"]] = {
                **{field.upper(): rule[field] for field in ("id", "code", "display_name", "data_type", "uniqueness",
                                                            "required", "active", "regex", "display_format")},
                "CLIENT_ID": client["id"], "POSITION": position}
    for employee in state["employees"].values():
        result["EMPLOYEES"][employee["id"]] = {"ID": employee["id"], "DOMAIN": employee["domain"],
                                                 "DOMAIN_KEY": key(employee["domain"])}
    for assignment in state["assignments"].values():
        result["ASSIGNMENTS"][assignment["id"]] = {**{field.upper(): assignment[field] for field in
            ("id", "client_id", "domain", "active", "version")}, "DOMAIN_KEY": key(assignment["domain"])}
        for entry in assignment["history"]:
            result["IDENTIFIERS"][entry["id"]] = {"ID": entry["id"], "ASSIGNMENT_ID": assignment["id"],
                "CLIENT_ID": assignment["client_id"], "CODE": entry["code"], "VALUE": entry["value"],
                "VALUE_KEY": key(entry["value"]), "START_DATE": entry["start"], "END_DATE": entry.get("end"),
                "SUPERSEDED_AT": entry.get("superseded_at"), "SUPERSEDED_BY": entry.get("superseded_by")}
    for token, upload in state["uploads"].items():
        # Staging must persist its validation baseline in services/uploads.py.
        # Fail closed if an older engine omits it; do not synthesize an empty plan.
        validation = upload["validation"]
        revision = sha256(encode(validation).encode()).hexdigest()
        result["UPLOADS"][token] = {"TOKEN": token, **{field.upper(): upload[field] for field in
            ("client_id", "actor", "sha256", "created_at", "expires_at", "effective", "config_version", "completed")},
            "EXPECTED_JSON": encode(upload["expected"]), "COLUMNS_JSON": encode(upload["parsed"]["columns"], 32768),
            "RESULT_JSON": encode(upload["result"], 32768), "COMPLETED_AT": upload.get("completed_at"),
            "VALIDATION_REVISION": revision}
        for position, row in enumerate(upload["parsed"]["rows"]):
            result["UPLOAD_ROWS"][(token, position)] = {"TOKEN": token, "POSITION": position, "ROW_NUMBER": row["row"],
                "DOMAIN": row["domain"], "VALUES_JSON": encode(row["values"], 262144)}
        for issue_revision, issues in (("PARSED", upload["parsed"]["issues"]), (revision, validation["issues"])):
            for position, issue in enumerate(issues):
                result["UPLOAD_ISSUES"][(token, issue_revision, position)] = {"TOKEN": token, "REVISION": issue_revision,
                    "POSITION": position, "ISSUE_JSON": encode(issue, 262144)}
        for position, change in enumerate(validation["changes"]):
            result["UPLOAD_CHANGES"][(token, revision, position)] = {"TOKEN": token, "REVISION": revision,
                "POSITION": position, "CHANGE_JSON": encode(change, 262144)}
    return result
