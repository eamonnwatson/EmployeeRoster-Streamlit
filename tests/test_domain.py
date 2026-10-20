"""Pure domain parity tests plus explicit regressions for user-owned domain.py."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from employee_roster.domain import (
    RosterError, check_rule, current_values, format_value, key, replace_identifier, validate_value,
)

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def rule(**overrides):
    return {"code": "BADGE", "display_name": "Badge", "data_type": "String",
            "uniqueness": "None", "required": False, "active": True,
            "regex": "", "display_format": "", **overrides}


def test_key_normalizes_case_and_outer_whitespace():
    assert key(" jane.doe ") == "JANE.DOE"


def test_rule_is_normalized_without_mutating_input():
    source = rule(code=" badge ", display_name=" Badge ")
    before = deepcopy(source)
    normalized = check_rule(source)
    assert normalized["code"] == "BADGE" and normalized["display_name"] == "Badge"
    assert source == before


@pytest.mark.parametrize("overrides", [
    {"code": "DOMAINID"}, {"code": "DOMAIN"}, {"code": "EMPLOYEE_ID"},
    {"code": "IS_ACTIVE"}, {"code": "ROW_VERSION"}, {"code": "9BAD"},
    {"code": "BAD-GE"}, {"code": ""}, {"display_name": ""}, {"code": "x" * 101},
    {"data_type": "Float"}, {"uniqueness": "Global"}, {"required": 1}, {"active": "true"},
    {"regex": "["}, {"regex": "(?<named>x)"}, {"regex": r"\p{L}"},
    {"regex": "x" * 501}, {"display_format": "Number"},
    {"display_format": "{0:D3}"}, {"data_type": "Int", "display_format": "{0:D31}"},
    {"data_type": "DateTime", "display_format": "{0:dd/MM/yyyy}"},
])
def test_invalid_rule_contract(overrides):
    with pytest.raises(RosterError):
        check_rule(rule(**overrides))


@pytest.mark.parametrize("code", ["DOMAINID", "DOMAIN", "DOMAIN_ID", "DOMAIN__ID", "DOMAIN_",
                                  "D_O_M_A_I_N_I_D", " domain__id ", "domain___"])
def test_rule_rejects_every_domain_alias_without_mutating_input(code):
    source = rule(code=code)
    before = deepcopy(source)
    with pytest.raises(RosterError, match="reserved"):
        check_rule(source)
    assert source == before


@pytest.mark.parametrize("code", ["DOMAIN_ID2", "DOMAIN_NAME", "DOMAIN_IDENTIFIER", "MY_DOMAIN", "DOMAINID_EXTRA"])
def test_rule_allows_distinct_identifier_codes_near_domain_aliases(code):
    assert check_rule(rule(code=code))["code"] == code


@pytest.mark.parametrize("kind,value,expected", [
    ("String", " abc ", "abc"), ("String", "00123", "00123"),
    ("Int", "+00123", "+00123"), ("Int", "-2147483648", "-2147483648"),
    ("Int", "2147483647", "2147483647"),
    ("Long", "-9223372036854775808", "-9223372036854775808"),
    ("Long", "9223372036854775807", "9223372036854775807"),
    ("Boolean", "TRUE", "TRUE"), ("Boolean", "false", "false"),
    ("DateTime", "2024-02-29", "2024-02-29"),
    ("DateTime", "12/31/2024", "12/31/2024"),
    ("DateTime", "2024-01-01T13:14:15", "2024-01-01T13:14:15"),
])
def test_values_match_reference_types_without_changing_lexical_identity(kind, value, expected):
    assert validate_value(rule(data_type=kind), value) == expected


@pytest.mark.parametrize("kind,value", [
    ("String", ""), ("String", " "), ("String", None), ("String", 1),
    ("String", "x" * 201), ("String", "a\nb"),
    ("Int", "2147483648"), ("Int", "-2147483649"), ("Int", "1.0"), ("Int", "1e3"),
    ("Long", "9223372036854775808"), ("Long", "-9223372036854775809"),
    ("Boolean", "1"), ("Boolean", "yes"),
    ("DateTime", "31/12/2024"), ("DateTime", "2023-02-29"), ("Float", "1"),
])
def test_invalid_values_fail_with_domain_error(kind, value):
    with pytest.raises(RosterError):
        validate_value(rule(data_type=kind), value)


def test_regex_and_type_validation_are_both_enforced():
    typed = check_rule(rule(data_type="Int", regex=r"^[0-9]{3}$"))
    assert validate_value(typed, "123") == "123"
    for value in ("12", "abc"):
        with pytest.raises(RosterError):
            validate_value(typed, value)
    # C# Regex.IsMatch is a search, not implicitly a full-string match.
    assert validate_value(rule(regex="abc"), "xabcx") == "xabcx"


@pytest.mark.parametrize("kind,pattern,value,expected", [
    ("String", "", "001", "001"), ("String", "{0}", "abc", "abc"),
    ("Int", "{0:D3}", "7", "007"), ("Long", "{0:D3}", "-7", "-007"),
    ("Int", "{0:D1}", "123", "123"), ("Long", "{0:D30}", "1", "0" * 29 + "1"),
    ("DateTime", "{0:yyyy-MM-dd}", "12/31/2024", "2024-12-31"),
    ("DateTime", "{0:MM/dd/yyyy}", "2024-12-31", "12/31/2024"),
])
def test_safe_display_formats(kind, pattern, value, expected):
    checked = check_rule(rule(data_type=kind, display_format=pattern))
    assert format_value(checked, value) == expected


def test_history_same_value_noop_and_case_only_change():
    original = replace_identifier([], "BADGE", "Alpha", "2026-10-01", NOW)
    assert replace_identifier(original, "BADGE", "Alpha", "2026-10-15", NOW) == original
    changed = replace_identifier(original, "BADGE", "alpha", "2026-10-01", NOW)
    assert len(changed) == 2 and current_values(changed) == {"BADGE": "alpha"}
    assert changed[0]["superseded_by"] == changed[1]["id"]
    assert changed[0]["superseded_at"] == NOW.isoformat()
    assert original[0]["superseded_at"] is None


def test_history_backdate_truncates_inclusive_end_and_supersedes_future_segments():
    history = replace_identifier([], "BADGE", "one", "2026-10-01", NOW)
    history = replace_identifier(history, "BADGE", "two", "2026-10-10", NOW)
    history = replace_identifier(history, "NOTE", "untouched", "2026-10-02", NOW)
    result = replace_identifier(history, "BADGE", "three", "2026-10-05", NOW)
    assert result[0]["end"] == "2026-10-04"
    assert result[1]["superseded_at"] == NOW.isoformat()
    assert result[1]["superseded_by"] == result[-1]["id"]
    assert current_values(result) == {"BADGE": "three", "NOTE": "untouched"}
    assert history[0]["end"] == "2026-10-09"


def test_history_same_value_backdated_before_start_is_not_noop():
    history = replace_identifier([], "BADGE", "same", "2026-10-10", NOW)
    result = replace_identifier(history, "BADGE", "same", "2026-10-01", NOW)
    assert len(result) == 2 and result[-1]["start"] == "2026-10-01"
    assert result[0]["superseded_at"] is not None


@pytest.mark.parametrize("effective", ["2026-10-01", "2026-10-05"])
def test_clear_preserves_history_and_removes_current(effective):
    history = replace_identifier([], "BADGE", "old", "2026-10-01", NOW)
    result = replace_identifier(history, "BADGE", None, effective, NOW)
    assert current_values(result) == {}
    assert len(result) == 1 and result[0]["superseded_by"] is None
    if effective == "2026-10-01":
        assert result[0]["superseded_at"] == NOW.isoformat()
    else:
        assert result[0]["end"] == "2026-10-04"
    assert replace_identifier(result, "BADGE", None, "2026-10-15", NOW) == result


def test_replace_in_gap_supersedes_later_segments():
    history = replace_identifier([], "BADGE", "old", "2026-10-01", NOW)
    history = replace_identifier(history, "BADGE", None, "2026-10-03", NOW)
    history = replace_identifier(history, "BADGE", "later", "2026-10-10", NOW)
    result = replace_identifier(history, "BADGE", "gap", "2026-10-05", NOW)
    assert result[0]["end"] == "2026-10-02"
    assert result[1]["superseded_at"] == NOW.isoformat()
    assert current_values(result) == {"BADGE": "gap"}


@pytest.mark.parametrize("effective", ["2026-10-16", "not-a-date", None])
def test_history_rejects_future_and_invalid_dates(effective):
    with pytest.raises(RosterError):
        replace_identifier([], "BADGE", "value", effective, NOW)


def test_history_detects_overlapping_canonical_segments():
    history = replace_identifier([], "BADGE", "one", "2026-10-01", NOW)
    history += replace_identifier([], "BADGE", "two", "2026-10-02", NOW)
    with pytest.raises(RosterError) as caught:
        replace_identifier(history, "BADGE", "three", "2026-10-05", NOW)
    assert caught.value.code == "CONFLICT"


@pytest.mark.parametrize("effective", ["20261005", "2026-W41-1", "2026-10-05T00:00:00"])
def test_regression_history_must_reject_noncanonical_effective_date(effective):
    history = replace_identifier([], "BADGE", "old", "2026-10-01", NOW)
    before = deepcopy(history)
    with pytest.raises(RosterError) as caught:
        replace_identifier(history, "BADGE", "new", effective, NOW)
    assert caught.value.code == "VALIDATION"
    assert history == before


@pytest.mark.parametrize("field", ["regex", "display_format"])
@pytest.mark.parametrize("value", [None, 42, False, [], {}])
def test_regression_malformed_optional_rule_field_is_domain_error(field, value):
    source = rule(**{field: value})
    before = deepcopy(source)
    with pytest.raises(RosterError) as caught:
        check_rule(source)
    assert caught.value.code == "VALIDATION"
    assert source == before


def test_omitted_optional_rule_text_defaults_to_empty_strings():
    source = rule()
    del source["regex"], source["display_format"]
    checked = check_rule(source)
    assert checked["regex"] == checked["display_format"] == ""
    assert "regex" not in source and "display_format" not in source
