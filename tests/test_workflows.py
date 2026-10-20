"""Synthetic, offline workflow tests. No production repository or UI imports."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from threading import RLock

import pytest

from employee_roster.domain import RosterError, current_values, replace_identifier
from employee_roster.excel import XLSX_MIME, export_workbook, parse_workbook
from employee_roster.services.operations import execute
from employee_roster.services import uploads

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def rule(code="BADGE", **overrides):
    return {"id": code, "code": code, "display_name": code, "data_type": "String",
            "uniqueness": "UniqueWithinClient", "required": code == "BADGE",
            "active": True, "regex": "", "display_format": "", **overrides}


def seed():
    return {
        "clients": {client_id: {"id": client_id, "name": name, "active": True, "version": 1,
                                "types": [rule(), rule("NOTE", uniqueness="None")]}
                    for client_id, name in (("1", "First"), ("2", "Second"))},
        "employees": {}, "assignments": {}, "uploads": {}, "audit": [],
        "users": {name: {"admin": name == "ROOT", "active": name != "DISABLED"}
                  for name in ("ROOT", "ALICE", "BOB", "VIEWER", "MANAGER", "OUTSIDER", "DISABLED")},
        "permissions": [{"user": name, "client_id": client_id, "permission": permission}
                        for name, client_id, permission in (
                            ("ALICE", "1", "CAN_EDIT"), ("BOB", "1", "CAN_EDIT"),
                            ("VIEWER", "1", "CAN_VIEW"), ("MANAGER", "1", "CLIENT_ADMIN"),
                            ("DISABLED", "1", "CLIENT_ADMIN"))],
    }


def add_employee(state, domain="JANE.DOE", badge="Alpha", client_id="1", active=True):
    state["employees"].setdefault(domain, {"id": "employee-" + domain, "domain": domain})
    history = replace_identifier([], "BADGE", badge, "2026-10-01", NOW)
    state["assignments"][f"{client_id}:{domain}"] = {
        "id": f"assignment-{client_id}-{domain}", "client_id": client_id, "domain": domain,
        "active": active, "version": 1, "history": history,
    }


def directory(domains):
    return {domain: {"client_id": "1", "active": True} for domain in domains}


class LockedRepository:
    """The real repository must provide this all-or-nothing, global-lock contract."""

    def __init__(self):
        self.state = seed()
        self.lock = RLock()

    def call(self, operation, payload=None, *, actor="ALICE", now=NOW, lookup=directory):
        with self.lock:
            candidate = deepcopy(self.state)
            result = execute(candidate, actor, operation, payload or {}, now, lookup)
            self.state = candidate
            return result


@pytest.fixture
def repo():
    return LockedRepository()


def stage(repo, values=None, domain="JOHN.SMITH", *, rows=None, actor="ALICE", client_id="1", now=NOW,
          lookup=directory, columns=None, issues=None, digest=None):
    rows = rows if rows is not None else [{"row": 2, "domain": domain, "values": values if values is not None else {"BADGE": "Beta"}}]
    parsed = {"rows": rows, "columns": columns if columns is not None else ["DOMAINID", "BADGE", "NOTE"],
              "issues": issues if issues is not None else []}
    digest = digest or sha256(json.dumps(parsed, sort_keys=True).encode()).hexdigest()
    return repo.call("stage", {"client_id": client_id, "parsed": parsed, "sha256": digest}, actor=actor, now=now, lookup=lookup)


def token_payload(review, client_id="1"):
    return {"client_id": client_id, "token": review["token"]}


def edit_payload(values, **overrides):
    return {"client_id": "1", "domain": " jane.doe ", "version": 1,
            "effective": "2026-10-15", "values": values, "active": True, **overrides}


@contextmanager
def error_code(code):
    with pytest.raises(RosterError) as caught:
        yield caught
    assert caught.value.code == code


@pytest.mark.parametrize("actor", ["MISSING", "OUTSIDER", "DISABLED"])
@pytest.mark.parametrize("operation,payload", [
    ("config", {"client_id": "1"}), ("roster", {"client_id": "1"}),
    ("export", {"client_id": "1"}), ("employee", {"client_id": "1", "domain": "JANE.DOE"}),
    ("edit", edit_payload({"BADGE": "Changed"})),
    ("review", {"client_id": "1", "token": "hidden"}),
    ("submit", {"client_id": "1", "token": "hidden"}),
    ("save_type", {"client_id": "1", "version": 1, "rule": rule()}),
])
def test_all_client_operations_require_fresh_authorization(repo, actor, operation, payload):
    before = deepcopy(repo.state)
    with error_code("FORBIDDEN"):
        repo.call(operation, payload, actor=actor)
    assert repo.state == before


def test_clients_permissions_and_detached_results(repo):
    result = repo.call("clients", actor="manager")
    assert result["admin"] is False
    assert len(result["clients"]) == 1
    assert {field: result["clients"][0][field] for field in ("can_view", "can_edit", "client_admin")} == {
        "can_view": True, "can_edit": True, "client_admin": True}
    result["clients"][0]["types"][0]["required"] = False
    assert repo.state["clients"]["1"]["types"][0]["required"] is True
    assert repo.call("clients", actor="OUTSIDER") == {"clients": [], "admin": False}
    assert len(repo.call("clients", actor="ROOT")["clients"]) == 2
    with error_code("FORBIDDEN"):
        repo.call("clients", actor="DISABLED")


def test_viewer_cannot_write_and_editor_cannot_administer(repo):
    assert repo.call("config", {"client_id": "1"}, actor="VIEWER")["id"] == "1"
    with error_code("FORBIDDEN"):
        stage(repo, actor="VIEWER")
    with error_code("FORBIDDEN"):
        repo.call("save_type", {"client_id": "1", "rule": rule(), "version": 1})
    with error_code("FORBIDDEN"):
        repo.call("save_client", {"id": "1", "name": "Renamed", "active": True, "version": 1})


def test_revocation_applies_to_reads_and_uploads(repo):
    review = stage(repo)
    repo.state["permissions"] = []
    for operation, payload in [("review", token_payload(review)), ("submit", token_payload(review)),
                               ("config", {"client_id": "1"}), ("export", {"client_id": "1"})]:
        with error_code("FORBIDDEN"):
            repo.call(operation, payload)


@pytest.mark.parametrize("operation", ["review", "submit"])
def test_token_owner_and_client_gate_even_for_admin(repo, operation):
    review = stage(repo)
    for actor in ("BOB", "ROOT"):
        with error_code("NOT_FOUND"):
            repo.call(operation, token_payload(review), actor=actor)
    repo.state["permissions"].append({"user": "ALICE", "client_id": "2", "permission": "CAN_EDIT"})
    with error_code("NOT_FOUND"):
        repo.call(operation, token_payload(review, "2"))
    with error_code("NOT_FOUND"):
        repo.call(operation, {"client_id": "1", "token": "not-a-real-token"})


def test_dedup_is_private_and_returns_only_current_pending_record(repo):
    first = stage(repo)
    assert stage(repo)["token"] == first["token"]
    assert stage(repo, actor="BOB")["token"] != first["token"]
    assert stage(repo, actor="ROOT", client_id="2")["token"] != first["token"]
    add_employee(repo.state)
    assert stage(repo)["token"] != first["token"]
    stored = repo.state["uploads"][first["token"]]
    assert "changes" not in stored and "bytes" not in stored
    assert stored["validation"] == {field: first[field] for field in ("issues", "changes")}
    assert stored["parsed"]["columns"] == ["DOMAINID", "BADGE", "NOTE"]
    assert stored["effective"] == NOW.date().isoformat()


def test_dedup_does_not_trust_caller_hash_alone(repo):
    first = stage(repo, digest="a" * 64)
    second = stage(repo, values={"BADGE": "Different"}, digest="a" * 64)
    assert first["token"] != second["token"]


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("operation", ["review", "submit"])
def test_exact_expiry_applies_even_after_completion(repo, completed, operation):
    review = stage(repo)
    if completed:
        repo.call("submit", token_payload(review))
    with error_code("EXPIRED"):
        repo.call(operation, token_payload(review), now=NOW + timedelta(minutes=30))
    assert stage(repo, now=NOW + timedelta(minutes=30))["token"] != review["token"]


def test_completed_apply_is_idempotent_and_still_checks_permission(repo):
    review = stage(repo)
    result = repo.call("submit", token_payload(review))
    assert result == {"completed": True, "added": 1, "updated": 0, "unchanged": 0}
    before = deepcopy(repo.state)
    assert repo.call("submit", token_payload(review), lookup=lambda domains: 1 / 0) == result
    assert repo.state == before
    completed = repo.call("review", token_payload(review))
    assert completed["completed"] and completed["result"] == result
    assert completed["changes"] == review["changes"]
    result["added"] = 999
    assert repo.call("submit", token_payload(review))["added"] == 1
    repo.state["users"]["ALICE"]["active"] = False
    with error_code("FORBIDDEN"):
        repo.call("submit", token_payload(review))


@pytest.mark.parametrize("change", ["config", "roster", "global", "recreated"])
def test_stale_review_requires_explicit_restage(repo, change):
    add_employee(repo.state)
    review = stage(repo)
    if change == "config":
        repo.state["clients"]["1"]["version"] += 1
    elif change == "roster":
        repo.state["assignments"]["1:JANE.DOE"]["version"] += 1
    elif change == "global":
        add_employee(repo.state, "JOHN.SMITH", "Other", client_id="2")
    else:
        repo.state["assignments"]["1:JANE.DOE"]["id"] = "replacement-id"
    for operation in ("review", "submit"):
        with error_code("STALE"):
            repo.call(operation, token_payload(review))
    refreshed = stage(repo)
    assert refreshed["token"] != review["token"]
    assert repo.call("submit", token_payload(refreshed))["added"] == 1


def test_current_uniqueness_rechecked_under_lock_even_if_version_not_updated(repo):
    add_employee(repo.state)
    review = stage(repo)
    assignment = repo.state["assignments"]["1:JANE.DOE"]
    assignment["history"] = replace_identifier(assignment["history"], "BADGE", "beta", "2026-10-15", NOW)
    before = deepcopy(repo.state)
    for operation in ("review", "submit"):
        with error_code("STALE"):
            repo.call(operation, token_payload(review))
        assert repo.state == before
    assert "1:JOHN.SMITH" not in repo.state["assignments"]


def test_active_types_rechecked_under_lock(repo):
    review = stage(repo)
    repo.state["clients"]["1"]["types"][0]["active"] = False
    # Even corrupt repositories that fail to bump config version cannot write a disabled type.
    before = deepcopy(repo.state)
    for operation in ("review", "submit"):
        with error_code("STALE"):
            repo.call(operation, token_payload(review))
        assert repo.state == before
    renewed = stage(repo)
    assert renewed["token"] != review["token"]
    assert any("disabled" in item["message"] for item in renewed["issues"])
    assert renewed["changes"][0]["differences"] == []
    assert repo.call("submit", token_payload(renewed))["added"] == 1
    assert current_values(repo.state["assignments"]["1:JOHN.SMITH"]["history"]) == {}


@pytest.mark.parametrize("response", [None, [], {"john.smith": {"client_id": "1", "active": True}},
                                      {"JOHN.SMITH": {"client_id": "1", "active": "yes"}}])
def test_hris_invalid_response_fails_closed(repo, response):
    with error_code("DIRECTORY_UNAVAILABLE"):
        stage(repo, lookup=lambda domains: response)
    assert not repo.state["uploads"]


def test_hris_exception_does_not_leak_details_or_persist_stage(repo):
    def broken(domains):
        raise RuntimeError("secret-database-connection")
    with error_code("DIRECTORY_UNAVAILABLE") as caught:
        stage(repo, lookup=broken)
    assert "secret" not in str(caught.value)
    assert not repo.state["uploads"]


def test_hris_revalidated_at_submit_and_errors_rollback(repo):
    review = stage(repo)
    before = deepcopy(repo.state)
    with error_code("DIRECTORY_UNAVAILABLE"):
        repo.call("submit", token_payload(review), lookup=lambda domains: 1 / 0)
    assert repo.state == before
    with error_code("STALE"):
        repo.call("submit", token_payload(review), lookup=lambda domains: {})
    assert repo.state == before


def test_missing_hris_blocks_add_but_warns_existing(repo):
    missing = stage(repo, lookup=lambda domains: {})
    assert [item["severity"] for item in missing["issues"]] == ["error"]
    add_employee(repo.state)
    existing = stage(repo, domain="JANE.DOE", lookup=lambda domains: {})
    assert [item["severity"] for item in existing["issues"]] == ["warning"]
    assert existing["changes"][0]["kind"] == "Updated"
    assert repo.call("submit", token_payload(existing), lookup=lambda domains: {})["updated"] == 1


def test_hris_inactive_and_wrong_client_warn_without_changing_status(repo):
    add_employee(repo.state, active=False)
    lookup = lambda domains: {domain: {"client_id": "2", "active": False} for domain in domains}
    review = stage(repo, domain="JANE.DOE", lookup=lookup)
    assert len(review["issues"]) == 2 and all(item["severity"] == "warning" for item in review["issues"])
    repo.call("submit", token_payload(review), lookup=lookup)
    assert repo.state["assignments"]["1:JANE.DOE"]["active"] is False
    new = stage(repo, values={"BADGE": "New"}, lookup=lookup)
    repo.call("submit", token_payload(new), lookup=lookup)
    assert repo.state["assignments"]["1:JOHN.SMITH"]["active"] is True


def test_upload_blanks_are_noop_and_absent_employees_are_never_deactivated(repo):
    add_employee(repo.state)
    add_employee(repo.state, "ABSENT.PERSON", "Absent")
    before = deepcopy(repo.state["assignments"])
    review = stage(repo, domain="JANE.DOE", values={"BADGE": "  ", "NOTE": ""})
    assert review["changes"] == []
    assert repo.call("submit", token_payload(review))["unchanged"] == 1
    assert repo.state["assignments"] == before


def test_required_only_on_add_unknown_and_disabled_nonempty_columns_warn(repo):
    repo.state["clients"]["1"]["types"][1]["active"] = False
    review = stage(repo, values={"BADGE": "Beta", "NOTE": "ignored", "UNKNOWN": "ignored", "EMPTY": " "})
    warning = review["issues"][0]
    assert warning["severity"] == "warning"
    assert "UNKNOWN" in warning["message"] and "NOTE" in warning["message"] and "EMPTY" not in warning["message"]
    assert [difference["code"] for difference in review["changes"][0]["differences"]] == ["BADGE"]
    invalid = stage(repo, values={"NOTE": "ignored"})
    assert any(item["severity"] == "error" and item["column"] == "BADGE" for item in invalid["issues"])


def test_duplicate_domains_are_case_insensitive_and_stop_before_hris(repo):
    review = stage(repo, rows=[{"row": 2, "domain": "Jane.Doe", "values": {"BADGE": "one"}},
                              {"row": 3, "domain": " jane.doe ", "values": {"BADGE": "two"}}],
                   lookup=lambda domains: pytest.fail("HRIS must not run after local errors"))
    assert len([item for item in review["issues"] if item["severity"] == "error"]) == 2
    assert review["changes"] == []


def test_upload_unique_values_ignore_case_but_value_changes_preserve_case(repo):
    duplicate = stage(repo, rows=[{"row": 2, "domain": "A.ONE", "values": {"BADGE": "  XyZ "}},
                                 {"row": 3, "domain": "B.TWO", "values": {"BADGE": "xyz"}}])
    assert sum(item["severity"] == "error" for item in duplicate["issues"]) == 2
    add_employee(repo.state)
    conflict = stage(repo, values={"BADGE": " alpha "})
    assert any(item["severity"] == "error" for item in conflict["issues"])
    changed_case = stage(repo, domain="jane.doe", values={"badge": "alpha"})
    assert changed_case["changes"][0]["differences"] == [{"code": "BADGE", "old": "Alpha", "new": "alpha"}]


def test_uniqueness_is_client_scoped_includes_inactive_and_rejects_swaps(repo):
    add_employee(repo.state, active=False)
    assert any(item["severity"] == "error" for item in stage(repo, values={"BADGE": "ALPHA"})["issues"])
    other_client = stage(repo, actor="ROOT", client_id="2", values={"BADGE": "ALPHA"})
    assert not any(item["severity"] == "error" for item in other_client["issues"])
    add_employee(repo.state, "JOHN.SMITH", "Beta")
    swap = stage(repo, rows=[{"row": 2, "domain": "JANE.DOE", "values": {"BADGE": "Beta"}},
                            {"row": 3, "domain": "JOHN.SMITH", "values": {"BADGE": "Alpha"}}])
    assert sum(item["severity"] == "error" for item in swap["issues"]) == 2


def test_staged_effective_day_survives_midnight_and_dedup(repo):
    before_midnight = NOW.replace(hour=23, minute=55)
    review = stage(repo, now=before_midnight)
    after_midnight = before_midnight + timedelta(minutes=10)
    assert stage(repo, now=after_midnight)["token"] == review["token"]
    repo.call("submit", token_payload(review), now=after_midnight)
    assert repo.state["assignments"]["1:JOHN.SMITH"]["history"][0]["start"] == "2026-10-15"


def test_manual_history_backdate_clear_and_noop(repo):
    add_employee(repo.state)
    first = repo.call("edit", edit_payload({"NOTE": "note"}, effective="2026-10-05"))
    assert first["version"] == 2
    second = repo.call("edit", edit_payload({"BADGE": "Beta"}, effective="2026-10-10", version=2))
    assert second["employee"]["history"][0]["end"] == "2026-10-09"
    third = repo.call("edit", edit_payload({"BADGE": "Gamma", "NOTE": None}, effective="2026-10-07", version=3))
    history = third["employee"]["history"]
    assert current_values(history) == {"BADGE": "Gamma"}
    assert next(item for item in history if item["value"] == "Beta")["superseded_at"] == NOW.isoformat()
    assert next(item for item in history if item["value"] == "Alpha")["end"] == "2026-10-06"
    before = deepcopy(repo.state)
    noop = repo.call("edit", edit_payload({"BADGE": " Gamma "}, version=4))
    assert noop["changed"] is False and repo.state == before


@pytest.mark.parametrize("values", [{"BADGE": None}, {"BADGE": " "}, {"UNKNOWN": "x"}])
def test_manual_cannot_clear_required_or_write_unknown(repo, values):
    add_employee(repo.state)
    before = deepcopy(repo.state)
    with error_code("VALIDATION"):
        repo.call("edit", edit_payload(values))
    assert repo.state == before


def test_manual_uses_same_uniqueness_format_and_inactive_type_checks(repo):
    add_employee(repo.state)
    add_employee(repo.state, "JOHN.SMITH", "Beta", active=False)
    with error_code("VALIDATION"):
        repo.call("edit", edit_payload({"BADGE": "beta"}))
    repo.state["clients"]["1"]["types"][1].update(data_type="Int", regex="^[0-9]{3}$")
    with error_code("VALIDATION"):
        repo.call("edit", edit_payload({"NOTE": "12"}))
    repo.state["clients"]["1"]["types"][1]["active"] = False
    with error_code("VALIDATION"):
        repo.call("edit", edit_payload({"NOTE": "123"}))


def test_manual_checks_assignment_scope_version_and_future_date(repo):
    add_employee(repo.state, client_id="2")
    with error_code("NOT_FOUND"):
        repo.call("edit", edit_payload({"BADGE": "Changed"}))
    add_employee(repo.state)
    with error_code("CONFLICT"):
        repo.call("edit", edit_payload({}, version=9))
    for effective in ("2026-10-16", "20261015", "2026-W42-4", "nonsense"):
        with error_code("VALIDATION"):
            repo.call("edit", edit_payload({}, effective=effective))


def test_roster_search_sort_pagination_and_full_export(repo):
    add_employee(repo.state)
    add_employee(repo.state, "ZED.LAST", "Zero", active=False)
    add_employee(repo.state, "AMY.FIRST", "Beta")
    result = repo.call("roster", {"client_id": "1", "sort": "BADGE", "descending": True, "page_size": 1})
    assert result["total"] == 2 and result["rows"][0]["domain"] == "AMY.FIRST"
    assert repo.call("roster", {"client_id": "1", "search": "ALP"})["total"] == 1
    assert repo.call("roster", {"client_id": "1", "page": 2, "page_size": 1})["rows"][0]["domain"] == "JANE.DOE"
    assert repo.call("roster", {"client_id": "1", "include_inactive": True})["total"] == 3
    export = repo.call("export", {"client_id": "1"})
    assert len(export["rows"]) == 3 and export["codes"] == ["BADGE", "NOTE"]
    export["rows"][0]["values"]["BADGE"] = "mutated"
    assert repo.call("employee", {"client_id": "1", "domain": "amy.first"})["values"]["BADGE"] == "Beta"


def test_client_admin_can_rename_but_only_full_admin_can_create_or_change_status(repo):
    with error_code("FORBIDDEN"):
        repo.call("save_client", {"id": "3", "create": True, "name": "New", "active": True}, actor="MANAGER")
    renamed = repo.call("save_client", {"id": "1", "name": "Renamed", "active": True, "version": 1}, actor="MANAGER")
    assert renamed["version"] == 2
    with error_code("FORBIDDEN"):
        repo.call("save_client", {"id": "1", "name": "Renamed", "active": False, "version": 2}, actor="MANAGER")
    disabled = repo.call("save_client", {"id": "1", "name": "Renamed", "active": False, "version": 2}, actor="ROOT")
    assert disabled["active"] is False
    with error_code("INACTIVE"):
        stage(repo)
    assert repo.call("config", {"client_id": "1"})["active"] is False
    new = repo.call("save_client", {"id": "0003", "create": True, "name": "New", "active": True,
                                    "version": None}, actor="ROOT")
    assert new["id"] == "3" and new["version"] == 1
    assert repo.state["clients"]["3"] == new
    with error_code("VALIDATION"):
        repo.call("save_client", {"id": "4", "create": True, "name": " new ", "active": True}, actor="ROOT")


def test_type_update_immutable_code_and_config_version(repo):
    changed = repo.call("save_type", {"client_id": "1", "version": 1, "rule": rule(display_name="Badge number")}, actor="MANAGER")
    assert changed["version"] == 2
    with error_code("CONFLICT"):
        repo.call("save_type", {"client_id": "1", "version": 1, "rule": rule()}, actor="MANAGER")
    with error_code("VALIDATION"):
        repo.call("save_type", {"client_id": "1", "version": 2, "rule": rule(code="RENAMED", id="BADGE")}, actor="MANAGER")
    created = repo.call("save_type", {"client_id": "1", "version": 2, "rule": {field: value for field, value in rule("NEW", required=False).items() if field != "id"}}, actor="MANAGER")
    assert created["types"][-1]["code"] == "NEW"


@pytest.mark.parametrize("restriction", [{"data_type": "Int"}, {"regex": "^[0-9]+$"}])
def test_restrictive_rule_changes_validate_existing_inactive_values(repo, restriction):
    add_employee(repo.state, active=False)
    before = deepcopy(repo.state)
    with error_code("VALIDATION"):
        repo.call("save_type", {"client_id": "1", "version": 1, "rule": rule(**restriction)}, actor="MANAGER")
    assert repo.state == before


def test_required_and_unique_rule_updates_validate_whole_roster(repo):
    add_employee(repo.state)
    add_employee(repo.state, "OTHER.PERSON", "alpha", active=False)
    repo.state["clients"]["1"]["types"][0]["uniqueness"] = "None"
    with error_code("VALIDATION"):
        repo.call("save_type", {"client_id": "1", "version": 1, "rule": rule()}, actor="MANAGER")
    with error_code("VALIDATION"):
        repo.call("save_type", {"client_id": "1", "version": 1, "rule": rule("NOTE", required=True)}, actor="MANAGER")


def test_synthetic_repository_rolls_back_partial_apply(monkeypatch, repo):
    review = stage(repo, rows=[{"row": 2, "domain": "FIRST.PERSON", "values": {"BADGE": "One"}},
                              {"row": 3, "domain": "SECOND.PERSON", "values": {"BADGE": "Two"}}])
    before = deepcopy(repo.state)
    real_replace = uploads.replace_identifier
    calls = 0
    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic persistence failure")
        return real_replace(*args, **kwargs)
    monkeypatch.setattr(uploads, "replace_identifier", fail_second)
    with pytest.raises(RuntimeError):
        repo.call("submit", token_payload(review))
    assert calls == 2 and repo.state == before


def test_global_mutex_serializes_competing_staged_uploads(repo):
    first, second = stage(repo), stage(repo, actor="BOB")
    def apply(item):
        actor, review = item
        try:
            return repo.call("submit", token_payload(review), actor=actor)["added"]
        except RosterError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, [("ALICE", first), ("BOB", second)]))
    assert set(results) == {1, "STALE"}
    assert len(repo.state["employees"]) == len(repo.state["assignments"]) == 1


def test_audit_contains_no_tokens_domains_or_identifier_values(repo):
    review = stage(repo, values={"BADGE": "private-identifier-value"})
    repo.call("submit", token_payload(review))
    serialized = json.dumps(repo.state["audit"])
    for secret in (review["token"], "JOHN.SMITH", "private-identifier-value"):
        assert secret not in serialized
    assert all(set(entry) <= {"actor", "operation", "client_id", "at", "count"} for entry in repo.state["audit"])


@pytest.mark.parametrize("payload", [{"page": 0}, {"page_size": 501}, {"page": True},
                                      {"include_inactive": "true"}, {"descending": 1},
                                      {"sort": "SECRET"}, {"search": "x" * 201}])
def test_roster_payload_bounds(repo, payload):
    with error_code("VALIDATION"):
        repo.call("roster", {"client_id": "1", **payload})


@pytest.mark.parametrize("values", [{"BADGE": "x" * 201}, {"BADGE": 42}, {"BADGE": None},
                                     {"badge": "one", "BADGE": "two"}])
def test_upload_payload_bounds_and_types(repo, values):
    with error_code("VALIDATION"):
        stage(repo, values=values)


def test_import_errors_are_preserved_and_prevent_apply(repo):
    review = stage(repo, issues=[{"severity": "error", "row": 0, "column": "", "message": "Invalid workbook"}],
                   lookup=lambda domains: pytest.fail("Should stop before HRIS"))
    assert review["issues"][0]["message"] == "Invalid workbook"
    with error_code("VALIDATION"):
        repo.call("submit", token_payload(review))


def test_stage_enforces_row_column_cell_and_issue_bounds(repo):
    base = {"client_id": "1", "sha256": "a" * 64,
            "parsed": {"rows": [], "columns": ["DOMAINID"], "issues": []}}
    oversized = [
        {"rows": [None] * 10_001, "columns": ["DOMAINID"], "issues": []},
        {"rows": [], "columns": ["X"] * 101, "issues": []},
        {"rows": [], "columns": ["DOMAINID"], "issues": [None] * 50_001},
    ]
    for parsed in oversized:
        with error_code("VALIDATION"):
            repo.call("stage", {**base, "parsed": parsed})
    values = {f"CODE{number}": "value" for number in range(99)}
    rows = [{"row": number + 2, "domain": f"USER{number}", "values": values} for number in range(5000)]
    with error_code("VALIDATION"):
        repo.call("stage", {**base, "parsed": {"rows": rows, "columns": ["DOMAINID"], "issues": []}})
    with error_code("VALIDATION"):
        repo.call("stage", {**base, "sha256": "not-sha256"})


def test_type_optional_fields_are_guarded_before_domain_validation(repo):
    for field in ("regex", "display_format"):
        with error_code("VALIDATION"):
            repo.call("save_type", {"client_id": "1", "version": 1, "rule": rule(**{field: None})}, actor="MANAGER")


def test_manual_status_change_is_assignment_scoped(repo):
    add_employee(repo.state)
    add_employee(repo.state, client_id="2")
    result = repo.call("edit", edit_payload({}, active=False))
    assert result["employee"]["active"] is False
    assert result["employee"]["values"] == {"BADGE": "Alpha"}
    assert repo.state["assignments"]["2:JANE.DOE"]["active"] is True
    assert repo.call("roster", {"client_id": "1"})["total"] == 0
    assert len(repo.call("export", {"client_id": "1"})["rows"]) == 1


def test_existing_global_employee_is_reused_without_reassignment(repo):
    add_employee(repo.state, "JOHN.SMITH", "Other", client_id="2")
    original = deepcopy(repo.state["employees"]["JOHN.SMITH"])
    other_assignment = deepcopy(repo.state["assignments"]["2:JOHN.SMITH"])
    review = stage(repo)
    repo.call("submit", token_payload(review))
    assert repo.state["employees"]["JOHN.SMITH"] == original
    assert repo.state["assignments"]["2:JOHN.SMITH"] == other_assignment
    assert len(repo.state["employees"]) == 1 and len(repo.state["assignments"]) == 2


@pytest.mark.parametrize("previous,current", [(True, False), (False, True)])
@pytest.mark.parametrize("warning", ["inactive", "wrong_client", "missing"])
def test_changed_hris_warning_requires_new_review_token(repo, previous, current, warning):
    add_employee(repo.state)

    def lookup(enabled):
        if warning == "missing" and enabled:
            return lambda domains: {}
        return lambda domains: {
            domain: {"active": not (warning == "inactive" and enabled),
                     "client_id": "2" if warning == "wrong_client" and enabled else "1"}
            for domain in domains
        }

    reviewed = stage(repo, domain="JANE.DOE", lookup=lookup(previous))
    before = deepcopy(repo.state)
    for operation in ("review", "submit"):
        with error_code("STALE"):
            repo.call(operation, token_payload(reviewed), lookup=lookup(current))
        assert repo.state == before
    renewed = stage(repo, domain="JANE.DOE", lookup=lookup(current))
    assert renewed["token"] != reviewed["token"]
    assert renewed["changes"] == reviewed["changes"]
    assert renewed["issues"] != reviewed["issues"]
    assert stage(repo, domain="JANE.DOE", lookup=lookup(current))["token"] == renewed["token"]
    assert repo.call("review", token_payload(renewed), lookup=lookup(current)) == renewed
    # A confirmation tied to the old token cannot authorize the renewed baseline.
    with error_code("STALE"):
        repo.call("submit", token_payload(reviewed), lookup=lookup(current))
    assert repo.call("submit", token_payload(renewed), lookup=lookup(current))["updated"] == 1


def test_changed_proposal_without_version_bump_is_stale(repo):
    add_employee(repo.state)
    reviewed = stage(repo, domain="JANE.DOE")
    assignment = repo.state["assignments"]["1:JANE.DOE"]
    assignment["history"] = replace_identifier(assignment["history"], "BADGE", "Gamma", "2026-10-15", NOW)
    before = deepcopy(repo.state)
    for operation in ("review", "submit"):
        with error_code("STALE"):
            repo.call(operation, token_payload(reviewed))
        assert repo.state == before
    renewed = stage(repo, domain="JANE.DOE")
    assert renewed["token"] != reviewed["token"]
    assert renewed["changes"][0]["differences"] == [{"code": "BADGE", "old": "Gamma", "new": "Beta"}]


def test_completed_review_replays_original_baseline_despite_later_changes(repo):
    lookup = lambda domains: {domain: {"client_id": "2", "active": False} for domain in domains}
    reviewed = stage(repo, lookup=lookup)
    result = repo.call("submit", token_payload(reviewed), lookup=lookup)
    repo.call("edit", edit_payload({"BADGE": "Later"}, domain="JOHN.SMITH"))
    repo.call("save_type", {"client_id": "1", "version": 1, "rule": rule(display_name="Renamed")},
              actor="MANAGER")
    before = deepcopy(repo.state)
    unavailable = lambda domains: pytest.fail("Completed replay must not query HRIS")
    completed = repo.call("review", token_payload(reviewed), lookup=unavailable)
    assert completed == {**reviewed, "completed": True, "result": result}
    assert repo.call("submit", token_payload(reviewed), lookup=unavailable) == result
    assert repo.state == before
    completed["issues"].clear()
    completed["changes"][0]["differences"][0]["new"] = "tampered"
    assert repo.call("review", token_payload(reviewed), lookup=unavailable)["changes"] == reviewed["changes"]
    for operation in ("review", "submit"):
        with error_code("NOT_FOUND"):
            repo.call(operation, token_payload(reviewed), actor="ROOT", lookup=unavailable)


def test_staged_parsed_input_and_validation_are_detached_json_without_bytes(repo):
    parsed = {"rows": [{"row": 2, "domain": " john.smith ", "values": {"badge": " Beta "}}],
              "columns": ["domainid", "badge"], "issues": []}
    payload = {"client_id": "1", "parsed": parsed, "sha256": "A" * 64}
    reviewed = repo.call("stage", payload)
    baseline = deepcopy(reviewed)
    record = repo.state["uploads"][reviewed["token"]]
    stored = deepcopy(record)
    parsed["rows"][0]["values"]["badge"] = "tampered"
    parsed["columns"].clear()
    reviewed["issues"].append({"severity": "error"})
    reviewed["changes"][0]["differences"][0]["new"] = "tampered"
    assert record == stored
    assert record["sha256"] == "a" * 64
    assert record["parsed"] == {"rows": [{"row": 2, "domain": "JOHN.SMITH", "values": {"BADGE": "Beta"}}],
                                "columns": ["DOMAINID", "BADGE"], "issues": []}
    assert record["validation"] == {field: baseline[field] for field in ("issues", "changes")}
    # Simulate durable JSON persistence; bytes anywhere in the record would fail serialization.
    repo.state["uploads"][baseline["token"]] = json.loads(json.dumps(record))
    assert repo.call("review", token_payload(baseline)) == baseline
    assert repo.call("submit", token_payload(baseline))["added"] == 1


def test_multi_identifier_review_and_submit_survive_canonical_json_roundtrip(repo):
    reviewed = stage(repo, values={"NOTE": "Imported note", "BADGE": "Beta"},
                     columns=["DOMAINID", "NOTE", "BADGE"])
    record = repo.state["uploads"][reviewed["token"]]
    assert list(record["parsed"]["rows"][0]["values"]) == ["NOTE", "BADGE"]

    # Model persistence's sort_keys=True without importing a production repository.
    repo.state = json.loads(json.dumps(repo.state, sort_keys=True))
    restored = repo.state["uploads"][reviewed["token"]]
    assert list(restored["parsed"]["rows"][0]["values"]) == ["BADGE", "NOTE"]
    assert restored["parsed"]["columns"] == ["DOMAINID", "NOTE", "BADGE"]
    assert restored["validation"] == {field: reviewed[field] for field in ("issues", "changes")}

    # Unchanged input must not become STALE merely because JSON reordered its keys.
    assert repo.call("review", token_payload(reviewed)) == reviewed
    assert [item["code"] for item in reviewed["changes"][0]["differences"]] == ["BADGE", "NOTE"]
    assert reviewed["issues"] == []
    assert repo.call("submit", token_payload(reviewed)) == {
        "completed": True, "added": 1, "updated": 0, "unchanged": 0}
    assert current_values(repo.state["assignments"]["1:JOHN.SMITH"]["history"]) == {
        "BADGE": "Beta", "NOTE": "Imported note"}


def test_parser_skipped_blank_domains_preserve_warning_and_domain_column(repo):
    content = export_workbook([
        {"domain": "", "values": {"BADGE": "Skipped"}},
        {"domain": "JOHN.SMITH", "values": {"BADGE": "Beta"}},
    ], ["BADGE"])
    parsed = parse_workbook(content, "roster.xlsx", XLSX_MIME)
    assert parsed["columns"] == ["DOMAINID", "BADGE"]
    assert [row["row"] for row in parsed["rows"]] == [3]
    assert parsed["issues"] == [{"severity": "warning", "row": 2, "column": "DOMAINID",
                                 "message": "Row skipped because DOMAINID is blank."}]
    reviewed = repo.call("stage", {"client_id": "1", "parsed": parsed, "sha256": sha256(content).hexdigest()})
    assert reviewed["issues"] == parsed["issues"]
    assert repo.call("submit", token_payload(reviewed))["added"] == 1
    assert set(repo.state["assignments"]) == {"1:JOHN.SMITH"}


@pytest.mark.parametrize("columns", [[], ["BADGE"], ["DOMAINID", " domainid ", "BADGE"]])
def test_invalid_domain_headers_block_before_hris(repo, columns):
    reviewed = stage(repo, columns=columns, lookup=lambda domains: pytest.fail("Local errors must stop HRIS"))
    assert any(item["severity"] == "error" for item in reviewed["issues"])
    assert reviewed["changes"] == []
    with error_code("VALIDATION"):
        repo.call("submit", token_payload(reviewed))
    assert not repo.state["assignments"]


def test_manual_edit_versions_stale_uploads_and_reject_lost_updates(repo):
    add_employee(repo.state)
    reviewed = stage(repo, domain="JANE.DOE")
    noop = repo.call("edit", edit_payload({"BADGE": "Alpha"}))
    assert noop["changed"] is False and noop["version"] == 1
    assert repo.call("review", token_payload(reviewed)) == reviewed
    changed = repo.call("edit", edit_payload({"NOTE": "Changed"}))
    assert changed["changed"] is True and changed["version"] == 2
    before = deepcopy(repo.state)
    with error_code("CONFLICT"):
        repo.call("edit", edit_payload({"BADGE": "Lost update"}))
    for operation in ("review", "submit"):
        with error_code("STALE"):
            repo.call(operation, token_payload(reviewed))
    assert repo.state == before


def test_manual_unique_values_are_client_scoped_and_nonunique_values_can_repeat(repo):
    add_employee(repo.state)
    add_employee(repo.state, "OTHER.PERSON", "Beta", client_id="2", active=False)
    add_employee(repo.state, "JOHN.SMITH", "Other")
    repo.call("edit", edit_payload({"NOTE": "Shared"}, domain="JOHN.SMITH"))
    changed = repo.call("edit", edit_payload({"BADGE": "Beta", "NOTE": "Shared"}))
    assert changed["employee"]["values"] == {"BADGE": "Beta", "NOTE": "Shared"}
    assert repo.state["assignments"]["2:OTHER.PERSON"]["version"] == 1


def test_required_is_assignment_scoped_and_existing_omissions_are_not_clears(repo):
    add_employee(repo.state)
    repo.state["assignments"]["1:JANE.DOE"]["history"] = []
    reviewed = stage(repo, domain="JANE.DOE", values={"NOTE": "Allowed"})
    assert not reviewed["issues"]
    assert repo.call("submit", token_payload(reviewed))["updated"] == 1
    edited = repo.call("edit", edit_payload({"NOTE": "Manual"}, version=2))
    assert edited["employee"]["values"] == {"NOTE": "Manual"}
    add_employee(repo.state, "JOHN.SMITH", client_id="2")
    missing = stage(repo, values={"NOTE": "New assignment"}, lookup=lambda domains: pytest.fail("Required check is local"))
    assert any(item["column"] == "BADGE" and item["severity"] == "error" for item in missing["issues"])


def test_rule_revalidation_ignores_other_clients(repo):
    add_employee(repo.state)
    add_employee(repo.state, "OTHER.PERSON", "Not an integer", client_id="2", active=False)
    repo.call("edit", edit_payload({"NOTE": "123"}))
    updated = repo.call("save_type", {"client_id": "1", "version": 1,
                                     "rule": rule("NOTE", required=True, data_type="Int")}, actor="MANAGER")
    assert updated["version"] == 2
    assert repo.state["clients"]["2"]["version"] == 1


@pytest.mark.parametrize("client_id,expected", [("3", "3"), ("0000000003", "3"), ("2147483647", "2147483647")])
def test_client_creation_uses_explicit_canonical_hris_id(repo, client_id, expected):
    created = repo.call("save_client", {"id": client_id, "create": True, "name": "New", "active": True,
                                        "version": 0}, actor="ROOT")
    assert created == {"id": expected, "types": [], "version": 1, "name": "New", "active": True}
    assert set(repo.state["clients"]) == {"1", "2", expected}


@pytest.mark.parametrize("client_id", [None, "", "0", "000", "-1", "+3", "3.0", "2147483648",
                                      "00000000003", "abc", "a" * 32, "\uff13", 3, True])
def test_client_creation_rejects_missing_or_invalid_hris_id(repo, client_id):
    before = deepcopy(repo.state)
    with error_code("VALIDATION"):
        repo.call("save_client", {"id": client_id, "create": True, "name": "New", "active": True}, actor="ROOT")
    assert repo.state == before


@pytest.mark.parametrize("client_id", ["1", "0001"])
def test_duplicate_client_id_cannot_upsert_existing_client(repo, client_id):
    before = deepcopy(repo.state)
    with error_code("VALIDATION"):
        repo.call("save_client", {"id": client_id, "create": True, "name": "Replacement", "active": False}, actor="ROOT")
    assert repo.state == before


@pytest.mark.parametrize("create", [False, None])
@pytest.mark.parametrize("actor,code", [("ROOT", "NOT_FOUND"), ("MANAGER", "FORBIDDEN")])
def test_client_update_cannot_implicitly_create_or_upsert(repo, create, actor, code):
    payload = {"id": "3", "name": "New", "active": True, "version": 1}
    if create is not None:
        payload["create"] = create
    before = deepcopy(repo.state)
    with error_code(code):
        repo.call("save_client", payload, actor=actor)
    assert repo.state == before


@pytest.mark.parametrize("create", [None, "true", 1, 0])
def test_client_create_flag_requires_boolean(repo, create):
    with error_code("VALIDATION"):
        repo.call("save_client", {"id": "3", "create": create, "name": "New", "active": True}, actor="ROOT")
    assert set(repo.state["clients"]) == {"1", "2"}


def test_client_rename_requires_current_version_and_client_admin_scope(repo):
    before = deepcopy(repo.state)
    with error_code("FORBIDDEN"):
        repo.call("save_client", {"id": "2", "name": "Unauthorized", "active": True, "version": 1}, actor="MANAGER")
    with error_code("CONFLICT"):
        repo.call("save_client", {"id": "1", "name": "Stale", "active": True, "version": 2}, actor="MANAGER")
    assert repo.state == before


def test_unknown_operation_fields_and_naive_time_rejected(repo):
    with error_code("VALIDATION"):
        repo.call("anything")
    with error_code("VALIDATION"):
        repo.call("clients", {"admin": True})
    with error_code("VALIDATION"):
        repo.call("clients", now=NOW.replace(tzinfo=None))
