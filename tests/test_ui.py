"""Offline UI and boundary tests. No Snowflake session or live queries."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from dev.synthetic import SyntheticRepository, directory_lookup
from employee_roster import ui
from employee_roster.excel import MAX_COMPRESSED_BYTES, XLSX_MIME, export_workbook, parse_workbook
from employee_roster.persistence import error_envelope

ADMIN = "PREVIEW.ADMIN"
EDITOR = "PREVIEW.EDITOR"
VIEWER = "PREVIEW.VIEWER"
CLIENT_ADMIN = "PREVIEW.CLIENTADMIN"
# Cold imports on local Windows runners can exceed 15 seconds.
APP_TIMEOUT = 60


@pytest.fixture(autouse=True)
def no_snowflake(monkeypatch):
    connection = Mock(side_effect=AssertionError("Tests must never connect to Snowflake"))
    monkeypatch.setattr(st, "connection", connection)
    show_error = Mock(wraps=ui.show_error)
    monkeypatch.setattr(ui, "show_error", show_error)
    yield connection
    connection.assert_not_called()
    assert all(
        (invocation.args[0] if invocation.args else {}).get("code", "INTERNAL") != "INTERNAL"
        for invocation in show_error.call_args_list
    ), "An unexpected exception was hidden by the UI's safe error handler"


def assert_healthy(app, *, allow_error=False):
    assert not app.exception
    if not allow_error:
        assert not app.error
    else:
        # A safe generic error can hide a UI exception caught by render_app.
        assert not any(error_envelope()["error"]["message"] in item.value for item in app.error)
    assert [item.value for item in app.warning] == [
        "Local synthetic preview only. Changes stay in this browser session; no Snowflake connection is used."
    ]


def preview(actor=ADMIN, *, allow_error=False):
    app = AppTest.from_file(str(ROOT / "dev/preview.py"), default_timeout=APP_TIMEOUT)
    app.session_state["_preview_actor"] = actor
    app.run()
    assert_healthy(app, allow_error=allow_error)
    return app


def go(app, page):
    app.switch_page(f"../app_pages/{page}.py").run()
    assert not app.exception
    return app


def click(app, label):
    next(button for button in app.button if button.label == label).click().run()
    assert not app.exception
    return app


def call(repo, operation, payload=None, actor=ADMIN):
    response = repo.call(actor, operation, payload or {})
    assert response["ok"], response
    return response["data"]


def employee(repo):
    return call(repo, "employee", {"client_id": "101", "domain": "ALICE.EXAMPLE"})


def edit(repo, **changes):
    payload = {"client_id": "101", "domain": "ALICE.EXAMPLE", "version": employee(repo)["version"],
               "effective": date.today().isoformat(), "values": {}, "active": True, **changes}
    return call(repo, "edit", payload)


def staged(repo, domain="new.employee", badge="00999", actor=ADMIN):
    parsed = {"columns": ["DOMAINID", "BADGE"], "issues": [],
              "rows": [{"row": 2, "domain": domain, "values": {"BADGE": badge}}]}
    return call(repo, "stage", {"client_id": "101", "parsed": parsed, "sha256": sha256(repr(parsed).encode()).hexdigest()}, actor)


@pytest.mark.parametrize("identity", [None, "", "  ", 101])
def test_production_missing_identity_denies_before_connection(monkeypatch, identity):
    monkeypatch.setattr(st, "user", SimpleNamespace(user_name=identity, email="fallback@example.test", is_logged_in=True))
    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=APP_TIMEOUT).run()
    assert not app.exception
    assert app.error and "identity is unavailable" in app.error[0].value
    assert any("Correlation ID:" in item.value for item in app.text)
    assert not app.selectbox


def test_production_absent_user_name_never_uses_email(monkeypatch):
    monkeypatch.setattr(st, "user", SimpleNamespace(email="fallback@example.test"))
    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=APP_TIMEOUT).run()
    assert not app.exception
    assert app.error


def test_roster_search_sort_paging_and_inactive_controls():
    app = preview(EDITOR)
    assert app.dataframe[0].value["Domain"].tolist() == ["ALICE.EXAMPLE"]
    app.checkbox(key="roster_inactive").check().run()
    assert len(app.dataframe[0].value) == 2
    app.selectbox(key="roster_sort").select("BADGE").run()
    app.checkbox(key="roster_descending").check().run()
    assert app.dataframe[0].value["Domain"].tolist() == ["BOB.EXAMPLE", "ALICE.EXAMPLE"]
    app.text_input(key="roster_search").set_value("00101").run()
    assert app.dataframe[0].value["Domain"].tolist() == ["ALICE.EXAMPLE"]
    app.selectbox(key="roster_size").select(25).run()
    app.number_input(key="roster_page").set_value(2).run()
    assert any("No employees match" in item.value for item in app.info)
    app.text_input(key="roster_search").set_value("").run()
    assert app.number_input(key="roster_page").value == 1
    assert not app.error and not app.exception


def test_client_switch_discards_token_confirmation_and_widget_values():
    app = preview()
    app.session_state["upload_token"] = "old-token"
    app.session_state["upload_confirm_old-token"] = True
    app.session_state["employee_version"] = 99
    app.text_input(key="roster_search").set_value("Alice").run()
    app.selectbox(key=ui.CLIENT_KEY).select("202").run()
    state = dict(app.session_state)
    assert ui.TOKEN_KEY not in state
    assert "upload_confirm_old-token" not in state
    assert "employee_version" not in state
    assert app.text_input(key="roster_search").value == ""
    assert app.dataframe[0].value["Domain"].tolist() == ["CAROL.EXAMPLE"]
    assert not app.exception and not app.error


def test_preview_sessions_are_isolated():
    first, second = preview(), preview()
    first_repo, second_repo = first.session_state["_preview_repository"], second.session_state["_preview_repository"]
    assert first_repo is not second_repo
    edit(first_repo, values={"NOTE": "First session only"})
    assert employee(second_repo)["values"]["NOTE"] == "Synthetic employee"
    first.run()
    second.run()
    assert first.dataframe[0].value["NOTE"].tolist() == ["First session only"]
    assert second.dataframe[0].value["NOTE"].tolist() == ["Synthetic employee"]


def test_viewer_employee_is_read_only_and_admin_pages_are_guarded():
    app = preview(VIEWER)
    click(app, "Open employee")
    assert any("Read-only access" in item.value for item in app.info)
    assert "Save employee" not in [button.label for button in app.button]
    click(app, "Close employee")
    go(app, "upload")
    assert app.error and "do not have access" in app.error[0].value
    assert app.page_link[0].label == "Home"
    go(app, "identifiers")
    assert app.error and not app.text_input
    go(app, "clients")
    assert "Create client" not in [button.label for button in app.button]
    assert "Save client" not in [button.label for button in app.button]


def test_employee_manual_clear_and_status():
    app = preview(EDITOR)
    repo = app.session_state["_preview_repository"]
    click(app, "Open employee")
    app.checkbox(key="employee_clear_NOTE").check()
    app.checkbox(key="employee_active").uncheck()
    click(app, "Save employee")
    saved = employee(repo)
    assert saved["active"] is False
    assert "NOTE" not in saved["values"]
    assert saved["values"]["BADGE"] == "00101"
    assert saved["version"] == 2
    assert "employee_open" not in dict(app.session_state)


@pytest.mark.parametrize("explicit_selection", [False, True])
def test_employee_same_value_backdating_only_updates_selected_identifiers(explicit_selection):
    app = preview(EDITOR)
    repo = app.session_state["_preview_repository"]
    before = employee(repo)
    click(app, "Open employee")
    app.date_input(key="employee_effective").set_value(date(2025, 12, 1))
    if explicit_selection:
        app.multiselect(key="employee_apply_effective").select("BADGE")
    click(app, "Save employee")
    assert_healthy(app)
    saved = employee(repo)
    assert saved["values"] == before["values"]
    assert [item for item in saved["history"] if item["code"] == "NOTE"] == [
        item for item in before["history"] if item["code"] == "NOTE"
    ]
    if explicit_selection:
        badge = [item for item in saved["history"] if item["code"] == "BADGE"]
        assert len(badge) == 2
        assert badge[0]["superseded_at"]
        assert badge[1]["start"] == "2025-12-01"
        assert badge[1]["value"] == "00101"
        assert badge[1]["end"] is None
    else:
        assert saved["history"] == before["history"]


def test_employee_original_version_survives_submit_rerun():
    app = preview(EDITOR)
    repo = app.session_state["_preview_repository"]
    click(app, "Open employee")
    assert app.session_state["employee_version"] == 1
    edit(repo, values={"NOTE": "Concurrent change"})
    app.text_input(key="employee_value_NOTE").set_value("Stale edit")
    click(app, "Save employee")
    assert app.session_state["employee_version"] == 1
    assert app.error and "changed after you opened" in app.error[0].value
    assert employee(repo)["values"]["NOTE"] == "Concurrent change"


def test_employee_reopen_fetches_fresh_values():
    app = preview()
    repo = app.session_state["_preview_repository"]
    click(app, "Open employee")
    click(app, "Close employee")
    edit(repo, values={"NOTE": "Fresh value"})
    click(app, "Open employee")
    assert app.text_input(key="employee_value_NOTE").value == "Fresh value"
    assert app.session_state["employee_version"] == 2


def test_client_admin_rename_but_no_create_or_status_change():
    app = preview(CLIENT_ADMIN)
    assert len(app.selectbox(key=ui.CLIENT_KEY).options) == 1
    go(app, "clients")
    assert app.checkbox(key="client_edit_active").disabled
    assert "Create client" not in [button.label for button in app.button]
    app.text_input(key="client_edit_name").set_value("Renamed North")
    click(app, "Save client")
    assert call(app.session_state["_preview_repository"], "config", {"client_id": "101"})["name"] == "Renamed North"
    go(app, "identifiers")
    assert not app.error


def test_full_admin_disable_client_filters_and_write_gate():
    app = preview()
    go(app, "clients")
    app.checkbox(key="client_edit_active").uncheck()
    click(app, "Save client")
    assert "101" not in app.dataframe[0].value["ID"].tolist()
    app.checkbox(key="clients_disabled").check().run()
    assert "101" in app.dataframe[0].value["ID"].tolist()
    go(app, "upload")
    assert app.error and "inactive" in app.error[0].value.lower()


def test_create_client_uses_explicit_numeric_hris_contract():
    # Deliberately catches the old UUID API; main is updating the engine contract.
    app = preview()
    go(app, "clients")
    app.text_input(key="client_new_id").set_value("303")
    app.text_input(key="client_new_name").set_value("Example West")
    click(app, "Create client")
    assert not app.error
    created = call(app.session_state["_preview_repository"], "config", {"client_id": "303"})
    assert created["id"] == "303" and created["name"] == "Example West"


def test_identifier_code_is_immutable_and_config_version_is_captured():
    app = preview(CLIENT_ADMIN)
    go(app, "identifiers")
    app.selectbox(key="identifier_selection").select("BADGE").run()
    assert app.text_input(key="identifier_form_code").disabled
    repo = app.session_state["_preview_repository"]
    call(repo, "save_client", {"id": "101", "name": "Concurrent rename", "active": True, "version": 1})
    app.text_input(key="identifier_form_display_name").set_value("New label")
    click(app, "Save identifier")
    assert app.session_state["identifier_form_version"] == 1
    assert app.error and "changed after you opened" in app.error[0].value
    click(app, "Reload identifier form")
    app.text_input(key="identifier_form_display_name").set_value("New label")
    click(app, "Save identifier")
    config = call(repo, "config", {"client_id": "101"})
    assert config["types"][0]["display_name"] == "New label"
    assert config["types"][0]["code"] == "BADGE"


def test_identifier_invalid_regex_is_rejected_by_service():
    app = preview()
    go(app, "identifiers")
    app.selectbox(key="identifier_selection").select("NOTE").run()
    app.text_input(key="identifier_form_regex").set_value("[")
    click(app, "Save identifier")
    assert app.error
    config = call(app.session_state["_preview_repository"], "config", {"client_id": "101"})
    assert config["types"][1]["regex"] == ""


def test_upload_review_confirm_submit_replay_and_no_cached_proposal():
    app = preview()
    repo = app.session_state["_preview_repository"]
    review = staged(repo)
    app.session_state[ui.TOKEN_KEY] = review["token"]
    go(app, "upload")
    submit = next(button for button in app.button if button.label == "Submit validated upload")
    assert submit.disabled
    app.checkbox(key="upload_confirm_" + review["token"]).check().run()
    click(app, "Submit validated upload")
    assert any("1 added" in item.value for item in app.success)
    before = deepcopy(repo._state)
    click(app, "Replay submission result")
    assert repo._state == before
    state = dict(app.session_state)
    assert state[ui.TOKEN_KEY] == review["token"]
    assert not {"proposal", "parsed", "review", "changes", "export_bytes"} & state.keys()


def test_uploaded_file_validate_review_submit_clears_raw_widget_bytes():
    app = preview(EDITOR)
    go(app, "upload")
    content = export_workbook([{"domain": "new.employee", "values": {"BADGE": "00999"}}], ["BADGE"])
    original_key = app.file_uploader[0].key
    assert app.button(key="upload_validate").disabled
    app.file_uploader[0].upload("roster.xlsx", content, XLSX_MIME).run()
    assert_healthy(app)
    assert not app.button(key="upload_validate").disabled
    click(app, "Validate workbook")
    assert_healthy(app)
    state = dict(app.session_state)
    token = state[ui.TOKEN_KEY]
    assert original_key not in state
    assert app.file_uploader[0].key != original_key
    assert app.file_uploader[0].value is None
    assert app.button(key="upload_validate").disabled
    assert not any(isinstance(value, (bytes, BytesIO)) for value in state.values())
    assert not {"proposal", "parsed", "review", "changes"} & state.keys()
    assert app.button(key="upload_submit").disabled
    app.checkbox(key="upload_confirm_" + token).check().run()
    click(app, "Submit validated upload")
    assert_healthy(app)
    assert any("1 added" in item.value for item in app.success)
    assert app.file_uploader[0].value is None
    assert app.session_state[ui.TOKEN_KEY] == token
    assert call(app.session_state["_preview_repository"], "employee", {
        "client_id": "101", "domain": "NEW.EMPLOYEE"
    })["values"]["BADGE"] == "00999"


def test_invalid_uploaded_file_is_cleared_and_reports_safe_validation_error():
    app = preview(EDITOR)
    go(app, "upload")
    original_key = app.file_uploader[0].key
    app.file_uploader[0].upload("roster.xlsx", b"not a workbook", XLSX_MIME).run()
    click(app, "Validate workbook")
    assert not app.exception
    assert len(app.error) == 1
    assert "Check required fields" in app.error[0].value
    assert "not a workbook" not in app.error[0].value
    assert original_key not in app.session_state
    assert app.file_uploader[0].value is None
    assert ui.TOKEN_KEY not in app.session_state
    assert "Submit validated upload" not in [button.label for button in app.button]


@pytest.mark.parametrize("replacement_badge", ["00999", "00888"])
def test_new_file_upload_discards_previous_token_and_confirmation(replacement_badge):
    app = preview(EDITOR)
    go(app, "upload")
    content = export_workbook([{"domain": "new.employee", "values": {"BADGE": "00999"}}], ["BADGE"])
    app.file_uploader[0].upload("roster.xlsx", content, XLSX_MIME).run()
    click(app, "Validate workbook")
    token = app.session_state[ui.TOKEN_KEY]
    app.checkbox(key="upload_confirm_" + token).check().run()
    replacement = export_workbook([
        {"domain": "new.employee", "values": {"BADGE": replacement_badge}}
    ], ["BADGE"])
    app.file_uploader[0].upload("replacement.xlsx", replacement, XLSX_MIME).run()
    assert_healthy(app)
    assert ui.TOKEN_KEY not in app.session_state
    assert "upload_confirm_" + token not in app.session_state
    assert "Submit validated upload" not in [button.label for button in app.button]
    click(app, "Validate workbook")
    assert_healthy(app)
    new_token = app.session_state[ui.TOKEN_KEY]
    if replacement_badge != "00999":
        assert new_token != token
    # Identical content can reuse the service's token, but never the confirmation.
    assert not app.checkbox(key="upload_confirm_" + new_token).value
    assert app.button(key="upload_submit").disabled


def test_upload_new_token_resets_confirmation_and_review_is_fresh():
    app = preview()
    repo = app.session_state["_preview_repository"]
    first = staged(repo)
    app.session_state[ui.TOKEN_KEY] = first["token"]
    go(app, "upload")
    app.checkbox(key="upload_confirm_" + first["token"]).check().run()
    second = staged(repo, badge="different")
    app.session_state[ui.TOKEN_KEY] = second["token"]
    app.run()
    assert not app.checkbox(key="upload_confirm_" + second["token"]).value
    edit(repo, values={"NOTE": "Changed after validation"})
    app.run()
    assert app.error and "Re-upload" in app.error[0].value
    assert "Submit validated upload" not in [button.label for button in app.button]


def test_expired_upload_requires_reupload():
    app = preview()
    repo = app.session_state["_preview_repository"]
    review = staged(repo)
    with repo._lock:
        repo._state["uploads"][review["token"]]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    app.session_state[ui.TOKEN_KEY] = review["token"]
    go(app, "upload")
    assert app.error and "expired" in app.error[0].value
    assert "Submit validated upload" not in [button.label for button in app.button]


def test_review_revocation_shows_friendly_error_without_stale_diff():
    app = preview(EDITOR)
    repo = app.session_state["_preview_repository"]
    review = staged(repo, actor=EDITOR)
    app.session_state[ui.TOKEN_KEY] = review["token"]
    go(app, "upload")
    with repo._lock:
        repo._state["permissions"] = [item for item in repo._state["permissions"] if item["user"] != EDITOR]
    app.run()
    assert app.error and not app.dataframe
    assert any("Correlation ID:" in item.value for item in app.text)


class Uploaded(BytesIO):
    def __init__(self, content, mime=XLSX_MIME, name="roster.xlsx"):
        super().__init__(content)
        self.size, self.type, self.name = len(content), mime, name


def test_upload_adapter_uses_actual_mime_bounded_bytes_and_digest():
    content = export_workbook([{"domain": "new.employee", "values": {"BADGE": "00999"}}], ["BADGE"])
    repo = Mock()
    repo.call.return_value = {"ok": True, "data": {"token": "opaque"}}
    assert ui.stage_file(repo, ADMIN, "101", Uploaded(content))["ok"]
    args = repo.call.call_args.args
    assert args[:2] == (ADMIN, "stage")
    assert args[2]["sha256"] == sha256(content).hexdigest()
    assert args[2]["parsed"] == parse_workbook(content, "roster.xlsx", XLSX_MIME)
    repo.reset_mock()
    for uploaded in (Uploaded(content, mime="text/plain"), Uploaded(content, name="roster.xls"),
                     Uploaded(b"x" * (MAX_COMPRESSED_BYTES + 1))):
        response = ui.stage_file(repo, ADMIN, "101", uploaded)
        assert not response["ok"]
    repo.call.assert_not_called()


def test_deferred_download_detection_rejects_eager_only_runtime(monkeypatch):
    def eager_download(label: str, data: bytes):
        raise AssertionError("Capability detection must not render a widget")

    monkeypatch.setattr(st, "download_button", eager_download)
    assert not ui.supports_deferred_download()


def test_export_widget_registers_callable_without_generating_bytes(monkeypatch):
    assert ui.supports_deferred_download()
    generate = Mock(side_effect=AssertionError("Export must not run during page rendering"))
    factory = Mock(return_value=generate)
    monkeypatch.setattr(ui, "download_callback", factory)
    app = preview(VIEWER)
    assert len(app.download_button) == 1
    assert app.download_button[0].label == "Export full client roster"
    factory.assert_called_with(app.session_state["_preview_repository"], VIEWER, "101")
    generate.assert_not_called()
    assert not any(isinstance(value, bytes) for value in dict(app.session_state).values())


def test_download_is_deferred_and_reauthorizes_each_invocation():
    repo = SyntheticRepository()
    generate = ui.download_callback(repo, VIEWER, "101")
    first = parse_workbook(generate(), "roster.xlsx", XLSX_MIME)
    assert len(first["rows"]) == 2  # Includes inactive Bob, not just visible Alice.
    edit(repo, values={"NOTE": "Fresh export"})
    second = parse_workbook(generate(), "roster.xlsx", XLSX_MIME)
    assert second["rows"][0]["values"]["NOTE"] == "Fresh export"
    with repo._lock:
        repo._state["users"][VIEWER]["active"] = False
    with pytest.raises(RuntimeError, match="Access denied.*Correlation ID"):
        generate()


def test_unsupported_runtime_blocks_export_without_weaker_fallback(monkeypatch):
    monkeypatch.setattr(ui, "supports_deferred_download", lambda: False)
    generate = Mock(side_effect=AssertionError("Unsupported runtimes must not generate exports"))
    monkeypatch.setattr(ui, "export_workbook", generate)
    app = preview(allow_error=True)
    assert len(app.error) == 1
    assert "Deferred (callable) downloads are required" in app.error[0].value
    repo = app.session_state["_preview_repository"]
    with repo._lock:
        repo._state["users"][ADMIN]["active"] = False
    for _ in range(2):
        assert not app.download_button
        assert "Generate fresh export" not in [button.label for button in app.button]
        assert "export_bytes" not in dict(app.session_state)
        app.run()
        assert not app.exception
    generate.assert_not_called()


def test_synthetic_directory_is_predetermined_not_fabricated():
    result = directory_lookup(["alice.example", "bob.example", "carol.example", "new.employee", "missing", "arbitrary.user"])
    assert set(result) == {"ALICE.EXAMPLE", "BOB.EXAMPLE", "CAROL.EXAMPLE", "NEW.EMPLOYEE"}
    review = staged(SyntheticRepository(), domain="missing")
    assert any(item["severity"] == "error" and "not found" in item["message"] for item in review["issues"])


@pytest.mark.parametrize("actor,operation,payload", [
    (VIEWER, "edit", {"client_id": "101", "domain": "ALICE.EXAMPLE", "version": 1, "effective": "2026-01-01", "values": {}}),
    (VIEWER, "stage", {"client_id": "101"}),
    (EDITOR, "save_type", {"client_id": "101"}),
    (EDITOR, "save_client", {"id": "101", "name": "Changed", "version": 1, "active": True}),
    (CLIENT_ADMIN, "save_client", {"id": "101", "name": "Changed", "version": 1, "active": False}),
    (CLIENT_ADMIN, "config", {"client_id": "202"}),
])
def test_synthetic_permissions_are_engine_enforced(actor, operation, payload):
    repo = SyntheticRepository()
    before = deepcopy(repo._state)
    response = repo.call(actor, operation, payload)
    assert response["ok"] is False
    assert response["error"]["code"] == "FORBIDDEN"
    assert repo._state == before


def test_synthetic_rolls_back_and_sanitizes_unexpected_failures(monkeypatch):
    from dev import synthetic
    repo = SyntheticRepository()
    before = deepcopy(repo._state)
    def broken(state, *args):
        state["clients"].clear()
        raise RuntimeError("private secret raw exception")
    monkeypatch.setattr(synthetic, "execute", broken)
    result = repo.call(ADMIN, "clients", {})
    assert result["error"]["code"] == "INTERNAL"
    assert "private secret" not in str(result)
    assert repo._state == before


def test_transport_and_error_envelopes_never_expose_raw_exception():
    repo = Mock()
    repo.call.side_effect = RuntimeError("credentials=private")
    assert "private" not in str(ui.invoke(repo, ADMIN, "clients", {}))
    repo.call.side_effect = None
    response = error_envelope("FORBIDDEN")
    response["error"]["message"] = "credentials=private"
    repo.call.return_value = response
    assert "private" not in str(ui.invoke(repo, ADMIN, "clients", {}))
