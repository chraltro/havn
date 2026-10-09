"""Verified agent changes.

An agent's proposed edits to model files are held as a *change set* instead
of being written to the project. Before anyone sees "ready to apply", havn
verifies the change set: parse and DAG checks, the bind pass, the unit tests
of affected models, contracts and assertions, and a data diff of every
affected model built into a scratch database attached next to the warehouse.
The real tables and files are untouched until the user applies it.

- :mod:`.store` -- the change set itself: files, persistence, apply/discard.
- :mod:`.overlay` -- a throwaway copy of the project with the changes applied,
  and the agent workspace the sidebar's review mode runs in.
- :mod:`.verify` -- the verification report.
"""

from __future__ import annotations

from havn.engine.changesets.store import (
    ChangeSet,
    ChangeSetConflict,
    ChangeSetError,
    FileChange,
    apply_change_set,
    create_change_set,
    discard_change_set,
    get_change_set,
    list_change_sets,
    revise_change_set,
    save_change_set,
)

__all__ = [
    "ChangeSet",
    "ChangeSetConflict",
    "ChangeSetError",
    "FileChange",
    "apply_change_set",
    "create_change_set",
    "discard_change_set",
    "get_change_set",
    "list_change_sets",
    "revise_change_set",
    "save_change_set",
]
