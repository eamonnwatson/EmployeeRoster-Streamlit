"""Bounded upload validation and staging, evaluated inside the caller's lock.

Pending records contain normalized input and a detached validation baseline,
not a trusted executable change plan. Review and submit reconstruct proposals
using fresh directory data and require exact equality with that baseline.
Apply uses the captured day. A stale record must be staged again for review;
completed records replay their original baseline and result until expiry.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
import re
import secrets
import uuid

from ..domain import RosterError, current_values, key, replace_identifier, require, required_text, validate_value

MAX_ROWS = 10_000
MAX_COLUMNS = 100
MAX_CELLS = 500_000
MAX_ISSUES = 50_000


def text(value, label: str, maximum: int, *, blank: bool = False) -> str:
    require(isinstance(value, str) and len(value) <= maximum,
            f"{label} must be text (maximum {maximum} characters).")
    if blank and not value.strip():
        return ""
    return required_text(value, label, maximum)


def domain_key(value) -> str:
    # The upload reference uses trusted directory usernames, not DomainId.Create's
    # first.last-only UI pattern. HRIS is the authority for newly added usernames.
    value = key(text(value, "Domain", 200))
    require(not any(char.isspace() for char in value) and ":" not in value,
            "Domain cannot contain whitespace or colons.")
    return value


def normalize_values(values, *, allow_clear: bool = False) -> dict:
    require(isinstance(values, dict) and len(values) <= MAX_COLUMNS,
            "Values must be an object with at most 100 identifiers.")
    result = {}
    for raw_code, raw_value in values.items():
        code = key(text(raw_code, "Code", 100))
        require(code not in result, "Duplicate normalized identifier code.")
        if allow_clear and raw_value is None:
            result[code] = None
        else:
            value = text(raw_value, code, 200, blank=True)
            result[code] = (value or None) if allow_clear else value
    return result


def normalize_parsed(parsed) -> dict:
    require(isinstance(parsed, dict), "Parsed workbook must be an object.")
    rows, columns, issues = (parsed.get(name) for name in ("rows", "columns", "issues"))
    require(isinstance(rows, list) and len(rows) <= MAX_ROWS, "Upload exceeds 10000 rows.")
    require(isinstance(columns, list) and len(columns) <= MAX_COLUMNS, "Upload exceeds 100 columns.")
    require(isinstance(issues, list) and len(issues) <= MAX_ISSUES, "Too many import issues.")
    columns = [key(text(code, "Column", 100)) for code in columns]
    normalized_issues = []
    for item in issues:
        require(isinstance(item, dict) and item.get("severity") in ("error", "warning"), "Invalid import issue.")
        number = item.get("row", 0)
        require(type(number) is int and 0 <= number <= MAX_ROWS + 1, "Invalid issue row.")
        normalized_issues.append(issue(item["severity"], number,
                                       text(item.get("column", ""), "Column", 100, blank=True),
                                       text(item.get("message"), "Issue", 2000)))
    cleaned, numbers, cells = [], set(), len(columns)
    for item in rows:
        require(isinstance(item, dict), "Invalid upload row.")
        number = item.get("row")
        require(type(number) is int and 2 <= number <= MAX_ROWS + 1 and number not in numbers,
                "Invalid or duplicate worksheet row number.")
        numbers.add(number)
        values = normalize_values(item.get("values"))
        cells += 1 + len(values)
        require(cells <= MAX_CELLS, "Upload exceeds 500000 cells.")
        cleaned.append({"row": number, "domain": domain_key(item.get("domain")), "values": values})
    return {"rows": cleaned, "columns": columns, "issues": normalized_issues}


def issue(severity: str, row: int, column: str, message: str) -> dict:
    return {"severity": severity, "row": row, "column": column, "message": message}


def expected_state(state: dict, client_id: str, rows: list) -> dict:
    # Include all client rows: an untouched row can acquire a unique value after
    # review. IDs also detect deletion/recreation with a reset version counter.
    roster = {name: {"id": item["id"], "version": item["version"]}
              for name, item in state["assignments"].items() if item["client_id"] == client_id}
    domains = {row["domain"] for row in rows}
    employees = {domain: deepcopy(state["employees"].get(domain)) for domain in sorted(domains)}
    return {"roster": roster, "employees": employees}


def ownership(state: dict, client_id: str, rules: dict) -> dict:
    owners = defaultdict(lambda: defaultdict(set))
    for assignment in state["assignments"].values():
        if assignment["client_id"] != client_id:
            continue
        for code, value in current_values(assignment["history"]).items():
            rule = rules.get(code)
            if rule and rule["uniqueness"] == "UniqueWithinClient":
                owners[code][key(value)].add(key(assignment["domain"]))
    return owners


def directory_records(directory_lookup, domains: list[str]) -> dict:
    try:
        require(callable(directory_lookup), "HRIS lookup is unavailable.", "DIRECTORY_UNAVAILABLE")
        records = directory_lookup(domains)
        require(isinstance(records, dict), "Invalid HRIS response.", "DIRECTORY_UNAVAILABLE")
        for domain, record in records.items():
            require(isinstance(domain, str) and domain == key(domain) and isinstance(record, dict)
                    and type(record.get("active")) is bool
                    and isinstance(record.get("client_id"), (str, int))
                    and not isinstance(record.get("client_id"), bool),
                    "Invalid HRIS response.", "DIRECTORY_UNAVAILABLE")
        return records
    except Exception as exc:
        # Do not expose directory exception messages, credentials, or records.
        raise RosterError("HRIS lookup failed; no changes were applied.", "DIRECTORY_UNAVAILABLE") from exc


def validate(state: dict, client: dict, parsed: dict, directory_lookup) -> dict:
    client_id = client["id"]
    rules = {rule["code"]: rule for rule in client["types"] if rule["active"]}
    issues = deepcopy(parsed["issues"])
    rows = parsed["rows"]
    if not rows:
        issues.append(issue("error", 0, "DOMAINID", "No importable rows were found."))
    if "DOMAINID" not in parsed["columns"]:
        issues.append(issue("error", 1, "DOMAINID", "DOMAINID column is required."))
    if len(set(parsed["columns"])) != len(parsed["columns"]):
        issues.append(issue("error", 1, "", "Duplicate normalized columns."))
    counts = Counter(row["domain"] for row in rows)
    owners = ownership(state, client_id, rules)
    if any(len(domains) > 1 for values in owners.values() for domains in values.values()):
        issues.append(issue("warning", 0, "", "Current roster contains uniqueness conflicts."))
    unknown = set()
    proposed = []
    within_upload = defaultdict(lambda: defaultdict(list))
    for row in rows:
        number, domain = row["row"], row["domain"]
        if counts[domain] > 1:
            issues.append(issue("error", number, "DOMAINID", "Duplicate DOMAINID in upload."))
        existing = state["assignments"].get(f"{client_id}:{domain}")
        values = {}
        for code, raw in sorted(row["values"].items()):
            if not raw:
                continue  # Upload blanks are never clears.
            if code not in rules:
                unknown.add(code)
                continue
            try:
                values[code] = validate_value(rules[code], raw)
            except RosterError as exc:
                issues.append(issue("error", number, code, str(exc)))
                continue
            if rules[code]["uniqueness"] == "UniqueWithinClient":
                within_upload[code][key(values[code])].append(row)
                if owners[code].get(key(values[code]), set()) - {domain}:
                    issues.append(issue("error", number, code, "Value is already used by another employee in this client."))
        if existing is None:
            for code, rule in rules.items():
                if rule["required"] and not values.get(code):
                    issues.append(issue("error", number, code, "This identifier is required for new employees."))
        proposed.append((row, existing, values))
    if unknown:
        issues.append(issue("warning", 0, "", "Unknown or disabled identifier columns were ignored: " + ", ".join(sorted(unknown))))
    for code, entries in within_upload.items():
        for duplicates in entries.values():
            if len({row["domain"] for row in duplicates}) > 1:
                issues.extend(issue("error", row["row"], code, "Duplicate identifier value in upload.") for row in duplicates)
    # Match the source pipeline's stop-before-HRIS boundary.
    if any(item["severity"] == "error" for item in issues):
        return {"issues": issues, "changes": []}
    records = directory_records(directory_lookup, [row["domain"] for row in rows])
    changes = []
    for row, existing, values in proposed:
        domain, number = row["domain"], row["row"]
        record = records.get(domain)
        if record is None:
            issues.append(issue("warning" if existing else "error", number, "DOMAINID",
                                "Employee not found in HRIS."))
            if existing is None:
                continue
        else:
            if not record["active"]:
                issues.append(issue("warning", number, "DOMAINID", "Employee is inactive in HRIS."))
            if str(record["client_id"]) != client_id:
                issues.append(issue("warning", number, "DOMAINID", "Employee is assigned to a different client in HRIS."))
        old = current_values(existing["history"]) if existing else {}
        differences = [{"code": code, "old": old.get(code), "new": value}
                       for code, value in values.items() if old.get(code) != value]
        if existing is None or differences:
            changes.append({"domain": domain, "kind": "Added" if existing is None else "Updated",
                            "differences": differences, "expected_version": existing["version"] if existing else None})
    return {"issues": issues, "changes": changes}


def _fresh(state: dict, client: dict, record: dict) -> None:
    require(record["config_version"] == client["version"]
            and record["expected"] == expected_state(state, client["id"], record["parsed"]["rows"]),
            "Roster or configuration changed; stage the upload again for review.", "STALE")


def _review(record: dict, validation: dict) -> dict:
    return deepcopy({"token": record["token"], "client_id": record["client_id"],
                     "expires_at": record["expires_at"], **validation,
                     "completed": record["completed"], "result": record["result"]})


def stage(state: dict, actor: str, client: dict, payload: dict, now: datetime, directory_lookup) -> dict:
    parsed = normalize_parsed(payload.get("parsed"))
    digest = payload.get("sha256")
    require(isinstance(digest, str) and re.fullmatch(r"[a-fA-F0-9]{64}", digest) is not None,
            "A SHA256 hexadecimal digest is required.")
    digest = digest.lower()
    expected = expected_state(state, client["id"], parsed["rows"])
    validation = validate(state, client, parsed, directory_lookup)
    for record in state["uploads"].values():
        if (record["actor"] == actor and record["client_id"] == client["id"]
                and record["sha256"] == digest and record["config_version"] == client["version"]
                and record["expected"] == expected and record["parsed"] == parsed
                and record["validation"] == validation
                and not record["completed"] and now < datetime.fromisoformat(record["expires_at"])):
            return _review(record, validation)
    token = secrets.token_urlsafe(32)
    record = {"token": token, "client_id": client["id"], "actor": actor, "parsed": parsed,
              "sha256": digest, "created_at": now.isoformat(), "effective": now.date().isoformat(),
              "expires_at": (now + timedelta(minutes=30)).isoformat(),
              "config_version": client["version"], "expected": expected,
              "completed": False, "result": None, "validation": deepcopy(validation)}
    state["uploads"][token] = record
    return _review(record, validation)


def access_record(state: dict, actor: str, client: dict, token, now: datetime) -> dict:
    token = text(token, "Token", 200)
    record = state["uploads"].get(token)
    require(record is not None and record["actor"] == actor and record["client_id"] == client["id"],
            "Upload not found.", "NOT_FOUND")
    require(now < datetime.fromisoformat(record["expires_at"]), "Upload expired; stage it again.", "EXPIRED")
    return record


def review(state: dict, actor: str, client: dict, payload: dict, now: datetime, directory_lookup) -> dict:
    record = access_record(state, actor, client, payload.get("token"), now)
    if record["completed"]:
        return _review(record, record["validation"])
    _fresh(state, client, record)
    validation = validate(state, client, record["parsed"], directory_lookup)
    require(validation == record["validation"], "Validation changed; stage again and review the new proposal.", "STALE")
    return _review(record, record["validation"])


def submit(state: dict, actor: str, client: dict, payload: dict, now: datetime, directory_lookup) -> dict:
    record = access_record(state, actor, client, payload.get("token"), now)
    if record["completed"]:
        return deepcopy(record["result"])
    _fresh(state, client, record)
    validation = validate(state, client, record["parsed"], directory_lookup)
    require(validation == record["validation"], "Validation changed; stage again and review the new proposal.", "STALE")
    require(not any(item["severity"] == "error" for item in validation["issues"]),
            "Upload has blocking validation errors; review it before submitting.")
    added = updated = 0
    for change in validation["changes"]:
        domain = change["domain"]
        assignment_key = f"{client['id']}:{domain}"
        if change["kind"] == "Added":
            if domain not in state["employees"]:
                state["employees"][domain] = {"id": uuid.uuid4().hex, "domain": domain}
            assignment = {"id": uuid.uuid4().hex, "client_id": client["id"], "domain": domain,
                          "active": True, "version": 0, "history": []}
            added += 1
        else:
            assignment = deepcopy(state["assignments"][assignment_key])
            updated += 1
        for difference in change["differences"]:
            assignment["history"] = replace_identifier(assignment["history"], difference["code"],
                                                       difference["new"], record["effective"], now)
        assignment["version"] += 1
        state["assignments"][assignment_key] = assignment
    result = {"completed": True, "added": added, "updated": updated,
              "unchanged": len(record["parsed"]["rows"]) - added - updated}
    record.update(completed=True, result=result, validation=validation, completed_at=now.isoformat())
    state["audit"].append({"actor": actor, "operation": "submit", "client_id": client["id"],
                           "at": now.isoformat(), "count": added + updated})
    return deepcopy(result)
