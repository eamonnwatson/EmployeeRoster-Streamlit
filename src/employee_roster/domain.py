"""Pure roster rules shared by the UI preview, tests, and Snowflake handler.

Dates are ISO strings; effective ranges have inclusive ends. Superseded segments
remain available for history but never participate in the canonical timeline.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import re
import uuid

import regex


class RosterError(Exception):
    def __init__(self, message: str, code: str = "VALIDATION"):
        super().__init__(message)
        self.code = code


def key(value: str) -> str:
    return value.strip().upper()


def is_domain_alias(value: str) -> bool:
    """Match the domain column's punctuation-insensitive import aliases."""
    return "".join(char for char in value if char.isalnum()).upper() in ("DOMAIN", "DOMAINID")


def require(condition: bool, message: str, code: str = "VALIDATION") -> None:
    if not condition:
        raise RosterError(message, code)


def required_text(value: str, label: str, maximum: int) -> str:
    require(isinstance(value, str), f"{label} must be text.")
    value = value.strip()
    require(bool(value) and len(value) <= maximum, f"{label} is required (maximum {maximum} characters).")
    require(not any(ord(char) < 32 for char in value), f"{label} contains control characters.")
    return value


DATA_TYPES = ("String", "Int", "Long", "Boolean", "DateTime")
DATE_FORMATS = {"{0:yyyy-MM-dd}": "%Y-%m-%d", "{0:MM/dd/yyyy}": "%m/%d/%Y"}


def parse_date_value(value: str) -> datetime:
    # Explicit invariant formats, rather than locale-dependent .NET parsing.
    for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            pass
    raise RosterError("Use YYYY-MM-DD, MM/DD/YYYY, or YYYY-MM-DDTHH:MM:SS.")


def check_rule(rule: dict) -> dict:
    result = deepcopy(rule)
    result["code"] = key(required_text(result.get("code", ""), "Code", 100))
    require(bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", result["code"])), "Code must start with a letter and contain only letters, digits, or underscores.")
    require(not is_domain_alias(result["code"]) and result["code"] not in {"EMPLOYEE_ID", "IS_ACTIVE", "ROW_VERSION"}, "That code is reserved.")
    result["display_name"] = required_text(result.get("display_name", ""), "Display name", 100)
    require(result.get("data_type") in DATA_TYPES, "Unsupported identifier data type.")
    require(result.get("uniqueness") in ("None", "UniqueWithinClient"), "Unsupported uniqueness scope.")
    for flag in ("required", "active"):
        require(type(result.get(flag)) is bool, f"{flag} must be true or false.")
    require(isinstance(result.get("regex", ""), str), "Regex must be text.")
    pattern = result.get("regex", "").strip()
    require(len(pattern) <= 500, "Regex exceeds 500 characters.")
    # Refuse .NET-specific groups/options instead of silently changing semantics.
    require(not any(token in pattern for token in ("(?<", "(?'", "(?(", "(?n", "\\k<", "\\k'", "\\p{", "\\P{")), "This .NET regex construct is unsupported; use a portable pattern.")
    try:
        regex.compile(pattern, regex.VERSION0)
    except regex.error as exc:
        raise RosterError("Invalid regular expression.") from exc
    result["regex"] = pattern
    require(isinstance(result.get("display_format", ""), str), "Display format must be text.")
    display = result.get("display_format", "").strip()
    allowed = display in ("", "{0}")
    allowed |= result["data_type"] == "DateTime" and display in DATE_FORMATS
    allowed |= result["data_type"] in ("Int", "Long") and bool(re.fullmatch(r"\{0:D(?:[1-9]|[1-2][0-9]|30)\}", display))
    require(allowed, "Supported formats: {0}, integer {0:D1} through {0:D30}, date {0:yyyy-MM-dd} or {0:MM/dd/yyyy}.")
    result["display_format"] = display
    return result


def validate_value(rule: dict, raw: str) -> str:
    value = required_text(raw, rule["code"], 200)
    kind = rule["data_type"]
    if kind in ("Int", "Long"):
        require(bool(re.fullmatch(r"[+-]?[0-9]+", value)), f"{rule['code']} must be an integer.")
        bits = 32 if kind == "Int" else 64
        require(-(2 ** (bits - 1)) <= int(value) < 2 ** (bits - 1), f"{rule['code']} is outside the {bits}-bit range.")
    elif kind == "Boolean":
        require(value.lower() in ("true", "false"), f"{rule['code']} must be true or false.")
    elif kind == "DateTime":
        parse_date_value(value)
    elif kind != "String":
        raise RosterError("Unsupported identifier data type.")
    if rule.get("regex"):
        try:
            require(regex.search(rule["regex"], value, timeout=0.05, flags=regex.VERSION0) is not None, f"{rule['code']} does not match its validation rule.")
        except (TimeoutError, regex.error) as exc:
            raise RosterError(f"{rule['code']} regex could not be evaluated safely.") from exc
    return value


def format_value(rule: dict, value: str) -> str:
    pattern = rule.get("display_format", "")
    if pattern in ("", "{0}"):
        return value
    if rule["data_type"] == "DateTime" and pattern in DATE_FORMATS:
        return parse_date_value(value).strftime(DATE_FORMATS[pattern])
    if rule["data_type"] in ("Int", "Long") and re.fullmatch(r"\{0:D\d+\}", pattern):
        number = int(value)
        return ("-" if number < 0 else "") + str(abs(number)).zfill(int(pattern[4:-1]))
    raise RosterError("Unsupported display format.")


def current_values(history: list[dict]) -> dict[str, str]:
    return {item["code"]: item["value"] for item in history if item.get("end") is None and not item.get("superseded_at")}


def replace_identifier(history: list[dict], code: str, value: str | None, effective: str, now: datetime) -> list[dict]:
    try:
        effective_date = date.fromisoformat(effective)
    except (ValueError, TypeError) as exc:
        raise RosterError("A valid effective date is required.") from exc
    require(effective_date.isoformat() == effective, "Effective date must use YYYY-MM-DD.")
    require(effective_date <= now.date(), "Effective date cannot be in the future.")
    result = deepcopy(history)
    timeline = [item for item in result if item["code"] == code and not item.get("superseded_at")]
    overlaps = [item for item in timeline if item["start"] <= effective and (item.get("end") is None or item["end"] >= effective)]
    require(len(overlaps) <= 1, "Identifier history has overlapping dates; administrator review is required.", "CONFLICT")
    overlap = overlaps[0] if overlaps else None
    later = any(item["start"] > effective for item in timeline)
    if overlap and overlap.get("end") is None and overlap["value"] == value and not later:
        return result
    if overlap and overlap["start"] < effective:
        overlap["end"] = (effective_date - timedelta(days=1)).isoformat()
    replacement_id = uuid.uuid4().hex if value is not None else None
    for item in timeline:
        if item["start"] >= effective:
            item["superseded_at"] = now.isoformat()
            item["superseded_by"] = replacement_id
    if value is not None:
        result.append({"id": replacement_id, "code": code, "value": value, "start": effective, "end": None, "superseded_at": None, "superseded_by": None})
    return result
