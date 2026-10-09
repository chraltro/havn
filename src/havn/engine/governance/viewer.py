"""Who a governed read is for."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Viewer:
    """The identity masking and row policies are evaluated against.

    ``attributes`` are the admin-managed key/value pairs stored with the user
    (``{"region": "north"}``), read by row-policy filters via ``havn_attr``.
    """

    username: str
    role: str
    attributes: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def cache_key(self) -> tuple:
        """Everything that can change what this viewer sees."""
        attrs = tuple(sorted((str(k), repr(v)) for k, v in (self.attributes or {}).items()))
        return (self.username, self.role, attrs)


# The process itself: CLI runs, scheduled jobs, the transform engine. Never
# governed; the local OS user owns the warehouse file.
SYSTEM = Viewer(username="system", role="admin", attributes={})


def viewer_from_user(user: dict | Viewer | None) -> Viewer:
    """Build a Viewer from the dict ``_require_permission`` returns."""
    if isinstance(user, Viewer):
        return user
    if not user:
        return Viewer(username="anonymous", role="viewer", attributes={})
    attrs = user.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    return Viewer(
        username=str(user.get("username") or "anonymous"),
        role=str(user.get("role") or "viewer"),
        attributes=dict(attrs),
    )
