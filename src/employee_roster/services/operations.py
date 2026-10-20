"""Application entry point for a mutable aggregate snapshot.

execute must run under one repository transaction/global mutex, including HRIS
validation. The repository MUST discard the entire snapshot on any exception.
No UI, database, clock, identity-provider, or directory implementation lives here.
Page numbers are one-based; edit values omitted are unchanged, None/blank clears
only optional active identifiers. Rule IDs are stable and codes are immutable.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import uuid

from ..authorization import authorize, identity, permissions
from ..domain import check_rule, current_values, key, replace_identifier, require, validate_value
from . import uploads

_FIELDS = {
    "clients": set(), "config": {"client_id"},
    "roster": {"client_id", "search", "include_inactive", "sort", "descending", "page", "page_size"},
    "export": {"client_id"}, "employee": {"client_id", "domain"},
    "stage": {"client_id", "parsed", "sha256"}, "review": {"client_id", "token"},
    "submit": {"client_id", "token"},
    "edit": {"client_id", "domain", "version", "effective", "values", "active"},
    "save_client": {"id", "name", "active", "version", "create"},
    "save_type": {"client_id", "rule", "version"},
}


def _integer(value, label: str, minimum: int, maximum: int) -> int:
    require(type(value) is int and minimum <= value <= maximum,
            f"{label} must be an integer between {minimum} and {maximum}.")
    return value


def _boolean(value, label: str) -> bool:
    require(type(value) is bool, f"{label} must be true or false.")
    return value


def _version(payload: dict, item: dict) -> None:
    version = _integer(payload.get("version"), "Version", 1, 2**63 - 1)
    require(version == item["version"], "Record changed; refresh and try again.", "CONFLICT")


def _audit(state: dict, actor: str, operation: str, client_id: str, now: datetime) -> None:
    # Never copy payloads, tokens, domains, or identifier values to audit.
    state["audit"].append({"actor": actor, "operation": operation,
                           "client_id": client_id, "at": now.isoformat()})


def _row(assignment: dict, codes: set[str]) -> dict:
    return {"domain": assignment["domain"], "values": {
        code: value for code, value in current_values(assignment["history"]).items() if code in codes},
        "active": assignment["active"], "version": assignment["version"], "id": assignment["id"]}


def _roster(state: dict, client: dict, payload: dict, *, export: bool = False) -> dict:
    codes = [rule["code"] for rule in client["types"] if rule["active"]]
    search = uploads.text(payload.get("search", ""), "Search", 200, blank=True).casefold()
    include_inactive = True if export else _boolean(payload.get("include_inactive", False), "Include inactive")
    rows = [_row(assignment, set(codes)) for assignment in state["assignments"].values()
            if assignment["client_id"] == client["id"] and (include_inactive or assignment["active"])]
    rows = [row for row in rows if not search or search in row["domain"].casefold()
            or any(search in value.casefold() for value in row["values"].values())]
    sort = uploads.text(payload.get("sort", "domain"), "Sort", 100)
    require(sort in ("domain", "active") or key(sort) in codes, "Unknown roster sort column.")
    descending = _boolean(payload.get("descending", False), "Descending")
    rows.sort(key=lambda row: key(row["domain"]))
    if sort == "active":
        rows.sort(key=lambda row: row["active"], reverse=descending)
    elif sort == "domain":
        rows.sort(key=lambda row: key(row["domain"]), reverse=descending)
    else:
        rows.sort(key=lambda row: row["values"].get(key(sort), "").casefold(), reverse=descending)
    if export:
        require(len(rows) <= uploads.MAX_ROWS and (len(rows) + 1) * (len(codes) + 1) <= uploads.MAX_CELLS,
                "Roster exceeds workbook export limits.")
        return {"rows": rows, "codes": codes}
    page = _integer(payload.get("page", 1), "Page", 1, 1_000_000)
    page_size = _integer(payload.get("page_size", 50), "Page size", 1, 500)
    start = (page - 1) * page_size
    return {"rows": rows[start:start + page_size], "total": len(rows)}


def _edit(state: dict, actor: str, client: dict, payload: dict, now: datetime) -> dict:
    domain = uploads.domain_key(payload.get("domain"))
    assignment_key = f"{client['id']}:{domain}"
    existing = state["assignments"].get(assignment_key)
    require(existing is not None, "Employee assignment not found.", "NOT_FOUND")
    _version(payload, existing)
    effective = uploads.text(payload.get("effective"), "Effective date", 10)
    try:
        effective_date = date.fromisoformat(effective)
    except ValueError:
        require(False, "A valid effective date is required.")
    # domain.replace_identifier compares stored ISO strings; enforce canonical form.
    require(effective_date.isoformat() == effective and effective_date <= now.date(),
            "Effective date must be YYYY-MM-DD and cannot be in the future.")
    values = uploads.normalize_values(payload.get("values"), allow_clear=True)
    active = _boolean(payload.get("active", existing["active"]), "Active")
    rules = {rule["code"]: rule for rule in client["types"] if rule["active"]}
    owners = uploads.ownership(state, client["id"], rules)
    assignment = deepcopy(existing)
    for code, value in values.items():
        require(code in rules, "Unknown or inactive identifier type.")
        rule = rules[code]
        if value is None:
            require(not rule["required"], "Required identifiers cannot be cleared.")
        else:
            value = validate_value(rule, value)
            require(not owners[code].get(key(value), set()) - {domain},
                    "Identifier value is already used by another employee in this client.")
        assignment["history"] = replace_identifier(assignment["history"], code, value, effective, now)
    assignment["active"] = active
    changed = assignment != existing
    if changed:
        assignment["version"] += 1
        state["assignments"][assignment_key] = assignment
        _audit(state, actor, "edit", client["id"], now)
    return {"saved": True, "changed": changed, "version": assignment["version"],
            "employee": {**deepcopy(assignment), "values": current_values(assignment["history"])}}


def _save_client(state: dict, actor: str, admin: bool, payload: dict, now: datetime) -> dict:
    client_id = payload.get("id")
    create = _boolean(payload.get("create", client_id is None), "Create")
    if create:
        require(admin, "Only full administrators can create clients.", "FORBIDDEN")
        client_id = uploads.text(client_id, "HRIS client ID", 10)
        require(client_id.isascii() and client_id.isdecimal() and 0 < int(client_id) <= 2147483647,
                "HRIS client ID must be a positive 32-bit integer.")
        client_id = str(int(client_id))
        require(client_id not in state["clients"], "Client ID already exists.")
        require(payload.get("version") is None or type(payload.get("version")) is int and payload["version"] == 0,
                "New clients must not specify an existing version.")
        client = {"id": client_id, "types": [], "version": 0}
    else:
        client_id = uploads.text(client_id, "Client ID", 100)
        client = authorize(state, actor, client_id, "client_admin")
        _version(payload, client)
    active = _boolean(payload.get("active"), "Active")
    if client.get("active", active) != active:
        require(admin, "Only full administrators can change client status.", "FORBIDDEN")
    name = uploads.text(payload.get("name"), "Client name", 100)
    require(not any(other["id"] != client_id and key(other["name"]) == key(name)
                    for other in state["clients"].values()), "Client name already exists.")
    result = deepcopy(client)
    result.update(name=name, active=active)
    if result != client:
        result["version"] += 1
        state["clients"][client_id] = result
        _audit(state, actor, "save_client", client_id, now)
    return deepcopy(result)


def _save_type(state: dict, actor: str, client: dict, payload: dict, now: datetime) -> dict:
    _version(payload, client)
    raw = payload.get("rule")
    require(isinstance(raw, dict), "Rule must be an object.")
    require(set(raw) <= {"id", "code", "display_name", "data_type", "uniqueness", "required", "active", "regex", "display_format"},
            "Unknown rule field; identifier codes cannot be renamed.")
    prepared = deepcopy(raw)
    for field, maximum in (("code", 100), ("display_name", 100), ("data_type", 20), ("uniqueness", 30)):
        prepared[field] = uploads.text(prepared.get(field), field, maximum)
    for field, maximum in (("regex", 500), ("display_format", 100)):
        prepared[field] = uploads.text(prepared.get(field, ""), field, maximum, blank=True)
    rule = check_rule(prepared)
    existing = next((item for item in client["types"] if item["code"] == rule["code"]), None)
    if raw.get("id") is not None:
        rule_id = uploads.text(raw["id"], "Rule ID", 100)
        by_id = next((item for item in client["types"] if item.get("id") == rule_id), None)
        require(by_id is not None and by_id["code"] == rule["code"],
                "Identifier codes cannot be renamed or moved between clients.")
        existing = by_id
    rule["id"] = existing.get("id", uuid.uuid4().hex) if existing else uuid.uuid4().hex
    require(existing is not None or len(client["types"]) < uploads.MAX_COLUMNS - 1,
            "A client supports at most 99 identifier types.")
    # Validate every current value, including inactive assignments, when changing
    # validation or activating a rule. Labels/status-only relaxations need not
    # repair legacy values. Historical segments are never rewritten.
    validation_fields = ("data_type", "regex", "uniqueness", "required")
    revalidate = existing is None or (rule["active"] and not existing["active"]) or any(
        rule[field] != existing[field] for field in validation_fields)
    seen = set()
    for assignment in state["assignments"].values() if revalidate else ():
        if assignment["client_id"] != client["id"]:
            continue
        value = current_values(assignment["history"]).get(rule["code"])
        if value is None:
            require(not (rule["active"] and rule["required"]),
                    "Existing employees are missing this required identifier.")
            continue
        value = validate_value(rule, value)
        if rule["uniqueness"] == "UniqueWithinClient":
            require(key(value) not in seen, "Existing values violate the proposed uniqueness rule.")
            seen.add(key(value))
    result = deepcopy(client)
    if existing == rule:
        return result
    if existing:
        result["types"][client["types"].index(existing)] = rule
    else:
        result["types"].append(rule)
    result["version"] += 1
    state["clients"][client["id"]] = result
    _audit(state, actor, "save_type", client["id"], now)
    return deepcopy(result)


def execute(state: dict, actor: str, operation: str, payload: dict, now: datetime, directory_lookup: callable) -> dict:
    """Execute one authorized operation; raises RosterError on expected failures.

    Pass an aware, consistent application-timezone datetime. Returned structures
    are detached from state. Errors require transaction rollback by the caller.
    """
    actor, admin = identity(state, actor)
    require(isinstance(operation, str) and operation in _FIELDS, "Unknown operation.")
    require(isinstance(payload, dict) and set(payload) <= _FIELDS[operation], "Invalid operation payload.")
    require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None,
            "An aware application timestamp is required.")
    if operation == "clients":
        visible = []
        for client_id, client in state["clients"].items():
            rights = permissions(state, actor, client_id)
            if rights["can_view"]:
                visible.append({**deepcopy(client), **rights})
        return {"clients": sorted(visible, key=lambda item: (key(item["name"]), item["id"])), "admin": admin}
    if operation == "save_client":
        return _save_client(state, actor, admin, payload, now)
    client_id = uploads.text(payload.get("client_id"), "Client ID", 100)
    permission = "client_admin" if operation == "save_type" else (
        "can_edit" if operation in {"edit", "stage", "review", "submit"} else "can_view")
    client = authorize(state, actor, client_id, permission)
    if operation in {"edit", "stage", "review", "submit", "save_type"}:
        require(client["active"], "Client is inactive.", "INACTIVE")
    if operation == "config":
        return deepcopy(client)
    if operation in {"roster", "export"}:
        return _roster(state, client, payload, export=operation == "export")
    if operation == "employee":
        domain = uploads.domain_key(payload.get("domain"))
        assignment = state["assignments"].get(f"{client_id}:{domain}")
        require(assignment is not None, "Employee assignment not found.", "NOT_FOUND")
        return {**deepcopy(assignment), "values": current_values(assignment["history"])}
    if operation == "edit":
        return _edit(state, actor, client, payload, now)
    if operation == "save_type":
        return _save_type(state, actor, client, payload, now)
    return {"stage": uploads.stage, "review": uploads.review, "submit": uploads.submit}[operation](
        state, actor, client, payload, now, directory_lookup)
