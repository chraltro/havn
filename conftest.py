"""Ensure tests use the worktree's source tree, not the globally installed package."""

from __future__ import annotations

import sys
from pathlib import Path

# Insert the worktree's src directory at the front of sys.path so that imports
# resolve to this worktree's code rather than the editable-installed main repo.
_worktree_src = str(Path(__file__).parent / "src")
if _worktree_src not in sys.path:
    sys.path.insert(0, _worktree_src)
