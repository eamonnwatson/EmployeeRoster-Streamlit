"""Native UI over fresh, authorized service operations; no cached business data."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import inspect
from pathlib import Path
import uuid

import streamlit as st

from .domain import DATA_TYPES, RosterError, format_value
from .excel import MAX_COMPRESSED_BYTES, XLSX_MIME, export_workbook, parse_workbook
from .persistence import error_envelope

ROOT = Path(__file__).resolve().parents[2]
CONTEXT_KEY = "_roster_context"
CLIENT_KEY = "roster_client"
TOKEN_KEY = "upload_token"


@dataclass(frozen=True)
class Context:
    repository: object
    actor: str


def clear_state(*prefixes: str) -> None:
    for name in list(st.session_state):
        if any(name.startswith(prefix) for prefix in prefixes):
            del st.session_state[name]


def clear_client_state() -> None:
    clear_state("roster_", "employee_", "upload_", "identifier_", "client_edit_")


def _client_changed() -> None:
    selected = st.session_state[CLIENT_KEY]
    clear_client_state()
    st.session_state[CLIENT_KEY] = selected


def _reset_page() -> None:
    st.session_state["roster_page"] = 1
    clear_state("roster_employee", "employee_")


def _safe_error(error: dict) -> dict:
    code = error.get("code", "INTERNAL")
    try:
        correlation = str(uuid.UUID(str(error.get("correlation_id"))))
    except (ValueError, TypeError, AttributeError):
        correlation = uuid.uuid4().hex
    return error_envelope(code, correlation)["error"]


def show_error(error: dict | None = None, *, home: bool = True) -> None:
    safe = _safe_error(error or {})
    messages = {
        "FORBIDDEN": "You do not have access to this action. Contact your roster administrator.",
        "VALIDATION": "The input could not be validated. Check required fields, identifier formats and workbook structure, then try again.",
        "NOT_FOUND": "This record is no longer available. Return Home and select it again.",
        "EXPIRED": "This upload has expired. Re-upload the workbook and validate it again.",
        "STALE": "The roster or configuration changed. Re-upload and review a new validation before submitting.",
        "CONFLICT": "This record changed after you opened it. Reopen or reload the form before saving again.",
    }
    st.error(messages.get(safe["code"], safe["message"]))
    st.text(f"Correlation ID: {safe['correlation_id']}")
    if home and CONTEXT_KEY in st.session_state:
        st.page_link(str(ROOT / "app_pages/roster.py"), label="Home")


def context() -> Context:
    value = st.session_state.get(CONTEXT_KEY)
    if not isinstance(value, Context):
        show_error({"code": "FORBIDDEN"}, home=False)
        st.stop()
    return value


def invoke(repository, actor: str, operation: str, payload: dict) -> dict:
    """Also used from deferred download threads, without Streamlit commands."""
    try:
        result = repository.call(actor, operation, payload)
        if isinstance(result, dict) and result.get("ok") is True and isinstance(result.get("data"), dict):
            return result
        if isinstance(result, dict) and result.get("ok") is False and isinstance(result.get("error"), dict):
            return {"ok": False, "error": _safe_error(result["error"])}
    except Exception:
        pass
    return error_envelope()


def request(operation: str, payload: dict | None = None) -> dict | None:
    current = context()
    response = invoke(current.repository, current.actor, operation, payload or {})
    if response["ok"]:
        return response["data"]
    show_error(response["error"])
    return None


def required_request(operation: str, payload: dict | None = None) -> dict:
    data = request(operation, payload)
    if data is None:
        st.stop()
    return data


def selected_client(permission: str = "can_view") -> dict:
    # Permissions are deliberately not stored in the navigation context.
    clients = required_request("clients")["clients"]
    selected = st.session_state.get(CLIENT_KEY)
    client = next((item for item in clients if item["id"] == selected), None)
    if client is None or not client.get(permission):
        show_error({"code": "FORBIDDEN"})
        st.stop()
    return client


def render_app(repository, actor: str, synthetic: bool = False) -> None:
    previous = st.session_state.get(CONTEXT_KEY)
    if previous is not None and previous.actor != actor:
        clear_client_state()
        clear_state("clients_", "client_new_")
    st.session_state[CONTEXT_KEY] = Context(repository, actor)
    pages = [
        st.Page(str(ROOT / "app_pages/roster.py"), title="Roster", default=True),
        st.Page(str(ROOT / "app_pages/upload.py"), title="Upload"),
        st.Page(str(ROOT / "app_pages/clients.py"), title="Clients"),
        st.Page(str(ROOT / "app_pages/identifiers.py"), title="Identifiers"),
    ]
    page = st.navigation(pages)
    if st.session_state.get("_ui_page") != page.title:
        clear_state("employee_", "client_edit_", "identifier_form_")
        st.session_state["_ui_page"] = page.title
    if synthetic:
        st.warning("Local synthetic preview only. Changes stay in this browser session; no Snowflake connection is used.")
    st.sidebar.text(f"Signed in as {actor}")
    try:
        listing = required_request("clients")
        clients = {item["id"]: item for item in listing["clients"]}
        if st.session_state.get(CLIENT_KEY) not in clients:
            clear_client_state()
        if clients:
            st.sidebar.selectbox(
                "Client", list(clients), key=CLIENT_KEY, on_change=_client_changed,
                format_func=lambda value: f"{clients[value]['name']} ({value})" + (" - disabled" if not clients[value]["active"] else ""),
            )
        else:
            st.sidebar.info("No assigned clients. Contact your administrator.")
        page.run()
    except Exception:
        show_error()


def download_callback(repository, actor: str, client_id: str):
    def generate() -> bytes:
        response = invoke(repository, actor, "export", {"client_id": client_id})
        if not response["ok"]:
            error = response["error"]
            raise RuntimeError(f"{error['message']} Correlation ID: {error['correlation_id']}") from None
        try:
            return export_workbook(response["data"]["rows"], response["data"]["codes"])
        except Exception:
            error = error_envelope()["error"]
            raise RuntimeError(f"{error['message']} Correlation ID: {error['correlation_id']}") from None
    return generate


def supports_deferred_download() -> bool:
    try:
        # Newer Streamlit uses a postponed type alias rather than spelling out Callable.
        annotation = inspect.get_annotations(inspect.unwrap(st.download_button), eval_str=True)["data"]
    except (KeyError, NameError, TypeError, ValueError):
        return False
    return "Callable" in str(annotation)


def render_export(client_id: str) -> None:
    current = context()
    generate = download_callback(current.repository, current.actor, client_id)
    st.caption("Export includes the entire client roster, including inactive employees, regardless of filters or page.")
    if supports_deferred_download():
        st.download_button("Export full client roster", data=generate, file_name=f"roster-{client_id}.xlsx",
                           mime=XLSX_MIME, key="roster_export", on_click="ignore")
    else:
        st.error("Roster export is unavailable on this runtime. Deferred (callable) downloads are required "
                 "to recheck access on every download. Ask your administrator to use a supported Streamlit "
                 "runtime, such as the tested version 1.66.0. No export has been generated.")


def render_roster() -> None:
    st.title("Employee roster")
    client = selected_client()
    config = required_request("config", {"client_id": client["id"]})
    rules = [rule for rule in config["types"] if rule["active"]]
    if not client["active"]:
        st.info("This client is disabled. Its roster remains read-only.")
    st.text_input("Search domain or identifier", max_chars=200, key="roster_search", on_change=_reset_page)
    st.checkbox("Include inactive employees", key="roster_inactive", on_change=_reset_page)
    columns = st.columns(4)
    sorts = ["domain", "active", *[rule["code"] for rule in rules]]
    if st.session_state.get("roster_sort") not in sorts:
        st.session_state.pop("roster_sort", None)
    columns[0].selectbox("Server sort", sorts, key="roster_sort", on_change=_reset_page)
    columns[1].checkbox("Descending", key="roster_descending", on_change=_reset_page)
    columns[2].selectbox("Rows per page", [25, 50, 100], index=1, key="roster_size", on_change=_reset_page)
    columns[3].number_input("Page number", min_value=1, max_value=1_000_000, step=1, key="roster_page")
    payload = {"client_id": client["id"], "search": st.session_state.roster_search,
               "include_inactive": st.session_state.roster_inactive, "sort": st.session_state.roster_sort,
               "descending": st.session_state.roster_descending, "page": st.session_state.roster_page,
               "page_size": st.session_state.roster_size}
    result = required_request("roster", payload)
    st.caption(f"{result['total']} matching employees. Page {payload['page']} of {max(1, (result['total'] + payload['page_size'] - 1) // payload['page_size'])}. Column-header sorting affects only this page; use Server sort for the whole roster.")
    rows = []
    for employee in result["rows"]:
        row = {"Domain": employee["domain"], "Active": employee["active"]}
        for rule in rules:
            value = employee["values"].get(rule["code"], "")
            try:
                row[rule["code"]] = format_value(rule, value) if value else ""
            except RosterError:
                row[rule["code"]] = value
        rows.append(row)
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
        domains = [row["domain"] for row in result["rows"]]
        if st.session_state.get("roster_employee") not in domains:
            st.session_state.pop("roster_employee", None)
        domain = st.selectbox("Employee details", domains, key="roster_employee")
        if st.button("Open employee", key="roster_open"):
            clear_state("employee_")
            st.session_state["employee_open"] = (client["id"], domain)
    else:
        st.info("No employees match this page and these filters.")
    render_export(client["id"])
    if st.session_state.get("employee_open"):
        employee_dialog(*st.session_state.employee_open)


def _dismiss_employee() -> None:
    clear_state("employee_")


@st.dialog("Employee details", width="large", on_dismiss=_dismiss_employee)
def employee_dialog(client_id: str, domain: str) -> None:
    try:
        _employee_details(client_id, domain)
    except Exception:
        show_error()


def _employee_details(client_id: str, domain: str) -> None:
    client = selected_client()
    if client["id"] != client_id:
        show_error({"code": "NOT_FOUND"})
        return
    employee = required_request("employee", {"client_id": client_id, "domain": domain})
    config = required_request("config", {"client_id": client_id})
    st.session_state.setdefault("employee_version", employee["version"])
    version = st.session_state["employee_version"]
    st.text(f"Domain: {employee['domain']}")
    st.text(f"Assignment: {employee['id']} | Originally opened version: {version}")
    if version != employee["version"]:
        st.warning("This employee changed while this dialog was open. Close and reopen it to refresh; saving will use your original version.")
    editable = client["can_edit"] and config["active"]
    if not editable:
        st.info("Read-only access. Identifier values and history cannot be changed.")
    st.subheader("Current identifiers")
    st.dataframe([{"Code": code, "Value": value} for code, value in employee["values"].items()],
                 hide_index=True, width="stretch")
    st.subheader("History")
    history = [{"Code": item["code"], "Value": item["value"], "Start": item["start"], "End": item.get("end"),
                "Status": "Superseded" if item.get("superseded_at") else ("Current" if item.get("end") is None else "Historical"),
                "Superseded at": item.get("superseded_at")} for item in employee["history"]]
    st.dataframe(history, hide_index=True, width="stretch")
    if editable:
        with st.form("employee_form"):
            st.session_state.setdefault("employee_effective", date.today())
            effective = st.date_input("Effective date", max_value=date.today(), key="employee_effective")
            st.session_state.setdefault("employee_active", employee["active"])
            active = st.checkbox("Active employee assignment", key="employee_active")
            apply_effective = st.multiselect(
                "Also apply effective date to unchanged identifiers",
                [rule["code"] for rule in config["types"] if rule["active"]],
                key="employee_apply_effective",
                help="Choose only identifiers whose history you intend to correct. Later history may be superseded.",
            )
            values = {}
            for rule in config["types"]:
                if not rule["active"]:
                    continue
                code = rule["code"]
                widget_key = "employee_value_" + code
                st.session_state.setdefault(widget_key, employee["values"].get(code, ""))
                value = st.text_input(f"{rule['display_name']} ({code})", max_chars=200, key=widget_key)
                clear = False
                if not rule["required"]:
                    clear = st.checkbox(f"Clear {code}", key="employee_clear_" + code)
                if clear:
                    values[code] = None
                elif value.strip() and (value != employee["values"].get(code, "") or code in apply_effective):
                    values[code] = value
            st.caption("Blank inputs leave values unchanged. Use Clear for optional identifiers. Status changes are manual and apply only to this client.")
            save = st.form_submit_button("Save employee")
        if save:
            result = request("edit", {"client_id": client_id, "domain": domain, "version": version,
                                      "effective": effective.isoformat(), "values": values, "active": active})
            if result is not None:
                clear_state("employee_")
                st.rerun()
    if st.button("Close employee", key="employee_close"):
        clear_state("employee_")
        st.rerun()


def stage_file(repository, actor: str, client_id: str, uploaded) -> dict:
    """Use actual uploader metadata and bounded bytes, never a substituted MIME."""
    try:
        if uploaded.size > MAX_COMPRESSED_BYTES:
            return error_envelope("VALIDATION")
        uploaded.seek(0)
        content = uploaded.read(MAX_COMPRESSED_BYTES + 1)
        parsed = parse_workbook(content, uploaded.name, uploaded.type)
        return invoke(repository, actor, "stage", {"client_id": client_id, "parsed": parsed,
                                                   "sha256": sha256(content).hexdigest()})
    except RosterError as exc:
        return error_envelope(exc.code)
    except Exception:
        return error_envelope()


def _forget_review() -> None:
    clear_state("upload_token", "upload_confirm")


def render_upload() -> None:
    st.title("Upload roster")
    client = selected_client("can_edit")
    if not client["active"]:
        show_error({"code": "INACTIVE"})
        return
    st.caption("Upload an .xlsx workbook, at most 5 MB, with DOMAINID and active identifier codes. Blank cells do not clear values. Missing employees are never deactivated.")
    generation = st.session_state.setdefault("upload_generation", 0)
    uploaded = st.file_uploader("Roster workbook", type=["xlsx"], key=f"upload_file_{generation}",
                                max_upload_size=5, on_change=_forget_review)
    error = st.session_state.pop("upload_error", None)
    if error is not None:
        show_error(error)
    if st.button("Validate workbook", disabled=uploaded is None, key="upload_validate"):
        _forget_review()
        current = context()
        response = stage_file(current.repository, current.actor, client["id"], uploaded)
        if response["ok"]:
            st.session_state[TOKEN_KEY] = response["data"]["token"]
        else:
            st.session_state["upload_error"] = response["error"]
        # Recreate the uploader so raw workbook bytes do not remain in widget state.
        # Retain only the staged token (or a safe error), never the parsed proposal.
        st.session_state["upload_generation"] = generation + 1
        st.rerun()
    token = st.session_state.get(TOKEN_KEY)
    if not token:
        return
    review = request("review", {"client_id": client["id"], "token": token})
    if review is None:
        return
    st.subheader("Review validation")
    st.text(f"Expires at: {review['expires_at']}")
    issues = review["issues"]
    errors = [item for item in issues if item["severity"] == "error"]
    warnings = [item for item in issues if item["severity"] == "warning"]
    st.caption(f"{len(errors)} blocking errors; {len(warnings)} warnings; {len(review['changes'])} proposed employee changes.")
    if issues:
        st.dataframe(issues, hide_index=True, width="stretch")
    differences = []
    for change in review["changes"]:
        for difference in change["differences"] or [{"code": "", "old": None, "new": None}]:
            differences.append({"Domain": change["domain"], "Action": change["kind"], "Code": difference["code"],
                                "Before": difference["old"], "After": difference["new"]})
    if differences:
        st.dataframe(differences, hide_index=True, width="stretch")
    if review["completed"]:
        _submit_summary(review["result"])
        if st.button("Replay submission result", key="upload_replay"):
            result = request("submit", {"client_id": client["id"], "token": token})
            if result is not None:
                _submit_summary(result)
        return
    confirmed = st.checkbox("I have reviewed the changes and warnings", key="upload_confirm_" + token)
    if st.button("Submit validated upload", disabled=bool(errors) or not confirmed, key="upload_submit"):
        result = request("submit", {"client_id": client["id"], "token": token})
        if result is not None:
            # Keep only the token: the next run retrieves the committed result.
            st.rerun()


def _submit_summary(result: dict) -> None:
    st.success(f"Submission complete: {result['added']} added, {result['updated']} updated, {result['unchanged']} unchanged.")


def render_clients() -> None:
    st.title("Clients")
    listing = required_request("clients")
    search = st.text_input("Search clients", key="clients_search", max_chars=100).casefold()
    disabled = st.checkbox("Include disabled clients", key="clients_disabled")
    clients = [item for item in listing["clients"] if (disabled or item["active"])
               and (search in item["name"].casefold() or search in item["id"])]
    if clients:
        st.dataframe([{"ID": item["id"], "Name": item["name"], "Active": item["active"]} for item in clients],
                     hide_index=True, width="stretch")
    else:
        st.info("No clients match your filters.")
    editable = {item["id"]: item for item in clients if item["client_admin"]}
    if editable:
        if st.session_state.get("clients_edit_id") not in editable:
            st.session_state.pop("clients_edit_id", None)
            clear_state("client_edit_")
        selected = st.selectbox("Client to edit", list(editable), key="clients_edit_id",
                                format_func=lambda value: f"{editable[value]['name']} ({value})",
                                on_change=clear_state, args=("client_edit_",))
        current = required_request("config", {"client_id": selected})
        st.session_state.setdefault("client_edit_version", current["version"])
        if st.button("Reload client form", key="clients_reload"):
            clear_state("client_edit_")
            st.rerun()
        with st.form("client_edit_form"):
            st.session_state.setdefault("client_edit_name", current["name"])
            st.session_state.setdefault("client_edit_active", current["active"])
            name = st.text_input("Client name", max_chars=100, key="client_edit_name")
            active = st.checkbox("Client active", key="client_edit_active", disabled=not listing["admin"])
            save = st.form_submit_button("Save client")
        if save:
            result = request("save_client", {"id": selected, "name": name, "active": active,
                                              "version": st.session_state.client_edit_version})
            if result is not None:
                clear_state("client_edit_")
                st.rerun()
    else:
        st.info("Client administration is available only to assigned client administrators or full administrators.")
    if listing["admin"]:
        st.subheader("Create client")
        with st.form("client_new_form", clear_on_submit=False):
            client_id = st.text_input("Numeric HRIS client ID", max_chars=100, key="client_new_id")
            name = st.text_input("New client name", max_chars=100, key="client_new_name")
            active = st.checkbox("New client active", value=True, key="client_new_active")
            create = st.form_submit_button("Create client")
        if create:
            if not client_id or not client_id.isascii() or not client_id.isdecimal():
                st.error("Enter the numeric HRIS client ID using digits only.")
            else:
                result = request("save_client", {"id": client_id, "name": name, "active": active, "create": True})
                if result is not None:
                    clear_state("client_new_")
                    st.rerun()


def render_identifiers() -> None:
    st.title("Identifier configuration")
    client = selected_client("client_admin")
    config = required_request("config", {"client_id": client["id"]})
    if not config["active"]:
        show_error({"code": "INACTIVE"})
        return
    if config["types"]:
        st.dataframe([{field: rule[field] for field in ("code", "display_name", "data_type", "uniqueness", "required", "active", "regex", "display_format")}
                      for rule in config["types"]], hide_index=True, width="stretch")
    rules = {rule["code"]: rule for rule in config["types"]}
    options = ["New identifier", *rules]
    if st.session_state.get("identifier_selection") not in options:
        clear_state("identifier_")
    selection = st.selectbox("Identifier to configure", options, key="identifier_selection",
                             on_change=clear_state, args=("identifier_form_",))
    rule = rules.get(selection)
    st.session_state.setdefault("identifier_form_version", config["version"])
    if st.button("Reload identifier form", key="identifier_reload"):
        clear_state("identifier_form_")
        st.rerun()
    defaults = {"code": "", "display_name": "", "data_type": "String", "uniqueness": "None",
                "required": False, "active": True, "regex": "", "display_format": ""}
    for field, value in (rule or defaults).items():
        st.session_state.setdefault("identifier_form_" + field, value)
    with st.form("identifier_form"):
        code = st.text_input("Code", key="identifier_form_code", max_chars=100, disabled=rule is not None)
        display_name = st.text_input("Display name", key="identifier_form_display_name", max_chars=100)
        data_type = st.selectbox("Data type", DATA_TYPES, key="identifier_form_data_type")
        uniqueness = st.selectbox("Uniqueness", ["None", "UniqueWithinClient"], key="identifier_form_uniqueness")
        required = st.checkbox("Required for new employees", key="identifier_form_required")
        active = st.checkbox("Identifier active", key="identifier_form_active")
        pattern = st.text_input("Validation regex", key="identifier_form_regex", max_chars=500)
        display_format = st.text_input("Display format", key="identifier_form_display_format", max_chars=100)
        st.caption("Codes are immutable. Rules, regex safety, display formats and existing values are validated by the service when saved.")
        save = st.form_submit_button("Save identifier")
    if save:
        proposed = {"code": code, "display_name": display_name, "data_type": data_type, "uniqueness": uniqueness,
                    "required": required, "active": active, "regex": pattern, "display_format": display_format}
        if rule:
            proposed["id"] = st.session_state.identifier_form_id
        result = request("save_type", {"client_id": client["id"], "rule": proposed,
                                        "version": st.session_state.identifier_form_version})
        if result is not None:
            clear_state("identifier_form_")
            st.rerun()
