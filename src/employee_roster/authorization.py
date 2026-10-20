"""Snapshot authorization; never cache permissions across repository transactions."""
from __future__ import annotations

from .domain import key, require, required_text


def identity(state: dict, actor: str) -> tuple[str, bool]:
    actor = key(required_text(actor, "Actor", 200))
    user = state["users"].get(actor)
    require(user is not None and user.get("active") is True,
            "Access denied.", "FORBIDDEN")
    return actor, user.get("admin") is True


def permissions(state: dict, actor: str, client_id: str) -> dict[str, bool]:
    actor, admin = identity(state, actor)
    granted = {item["permission"] for item in state["permissions"]
               if key(item["user"]) == actor and str(item["client_id"]) == client_id}
    client_admin = admin or "CLIENT_ADMIN" in granted
    edit = client_admin or "CAN_EDIT" in granted
    return {"can_view": edit or "CAN_VIEW" in granted,
            "can_edit": edit, "client_admin": client_admin}


def authorize(state: dict, actor: str, client_id: str, permission: str) -> dict:
    require(permissions(state, actor, client_id)[permission], "Access denied.", "FORBIDDEN")
    client = state["clients"].get(client_id)
    require(client is not None, "Client not found.", "NOT_FOUND")
    return client
