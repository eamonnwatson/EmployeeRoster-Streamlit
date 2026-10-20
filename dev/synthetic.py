"""Offline preview repository. Each instance owns its own transactional snapshot."""
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock

from employee_roster.domain import RosterError, key, replace_identifier
from employee_roster.persistence import error_envelope
from employee_roster.services.operations import execute


def directory_lookup(domains: list[str]) -> dict:
    # Missing users stay missing: never invent HRIS records from arbitrary input.
    directory = {
        "ALICE.EXAMPLE": {"client_id": "101", "active": True},
        "BOB.EXAMPLE": {"client_id": "101", "active": False},
        "CAROL.EXAMPLE": {"client_id": "202", "active": True},
        "NEW.EMPLOYEE": {"client_id": "101", "active": True},
    }
    return {key(domain): deepcopy(directory[key(domain)]) for domain in domains if key(domain) in directory}


def seed() -> dict:
    state = {
        "clients": {}, "employees": {}, "assignments": {}, "uploads": {}, "audit": [],
        "users": {actor: {"active": True, "admin": actor == "PREVIEW.ADMIN"} for actor in
                  ("PREVIEW.ADMIN", "PREVIEW.EDITOR", "PREVIEW.VIEWER", "PREVIEW.CLIENTADMIN")},
        "permissions": [{"user": actor, "client_id": client_id, "permission": permission}
                        for actor, permission, client_ids in (
                            ("PREVIEW.EDITOR", "CAN_EDIT", ("101", "202")),
                            ("PREVIEW.VIEWER", "CAN_VIEW", ("101", "202")),
                            ("PREVIEW.CLIENTADMIN", "CLIENT_ADMIN", ("101",)))
                        for client_id in client_ids],
    }
    for client_id, name in (("101", "Example North"), ("202", "Example South")):
        rules = [{"id": f"{client_id}-{code}", "code": code, "display_name": label, "data_type": "String",
                  "uniqueness": "UniqueWithinClient" if code == "BADGE" else "None",
                  "required": code == "BADGE", "active": True, "regex": "", "display_format": ""}
                 for code, label in (("BADGE", "Badge number"), ("NOTE", "Optional note"))]
        state["clients"][client_id] = {"id": client_id, "name": name, "active": True, "version": 1, "types": rules}
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for domain, client_id, badge, active in (
        ("ALICE.EXAMPLE", "101", "00101", True), ("BOB.EXAMPLE", "101", "00102", False),
        ("CAROL.EXAMPLE", "202", "00201", True),
    ):
        state["employees"][domain] = {"id": "employee-" + domain, "domain": domain}
        history = replace_identifier([], "BADGE", badge, "2026-01-01", now)
        history = replace_identifier(history, "NOTE", "Synthetic employee", "2026-01-01", now)
        state["assignments"][f"{client_id}:{domain}"] = {
            "id": f"assignment-{client_id}-{domain}", "client_id": client_id, "domain": domain,
            "active": active, "version": 1, "history": history,
        }
    return state


class SyntheticRepository:
    def __init__(self):
        self._state = seed()
        self._lock = RLock()

    def call(self, actor: str, operation: str, payload: dict) -> dict:
        with self._lock:
            try:
                candidate = deepcopy(self._state)
                data = execute(candidate, actor, operation, deepcopy(payload), datetime.now(timezone.utc), directory_lookup)
                result = {"ok": True, "data": deepcopy(data)}
                self._state = candidate
                return result
            except RosterError as exc:
                return error_envelope(exc.code)
            except Exception:
                return error_envelope()
