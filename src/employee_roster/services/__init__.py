"""UI-independent workflows; callers own locking, persistence, and rollback."""

from .operations import execute

__all__ = ["execute"]
