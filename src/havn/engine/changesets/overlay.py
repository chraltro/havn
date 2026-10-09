"""Copies of the project: verification overlays and agent workspaces.

Both are a copy of the project's source files (SQL, YAML, Python, small CSVs),
never its warehouse, ``.env``, git history or run state:

- an *overlay* is a temporary copy with a change set applied, which discovery,
  the bind pass and unit tests run against exactly as they would against the
  real project (:func:`overlay_project`);
- an *agent workspace* is where the sidebar's review mode points a coding
  agent, so its edits land in the copy and come back as a change set
  (:class:`AgentWorkspace`).
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

from havn.engine.changesets.store import is_allowed
from havn.textio import read_project_text

logger = logging.getLogger("havn.changesets")

_COPY_SUFFIXES = {
    ".sql", ".yml", ".yaml", ".py", ".lock", ".md", ".dpnb", ".csv", ".json", ".txt", ".toml",
}
_SKIP_DIRS = {
    "node_modules", "__pycache__", "venv", "_snapshots", "output", "dist", "build",
}
_SKIP_FILES = {".env", ".havn-env"}
_MAX_COPY_BYTES = 2_000_000


def _skip_dir(name: str) -> bool:
    return name.startswith(".") or name in _SKIP_DIRS


def iter_source_files(root: Path) -> Iterator[Path]:
    """Relative paths of the files a copy of the project carries."""
    root = Path(root)
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if not _skip_dir(entry.name):
                    stack.append(entry)
                continue
            if entry.name in _SKIP_FILES or entry.name.startswith(".env"):
                continue
            if entry.suffix.lower() not in _COPY_SUFFIXES:
                continue
            try:
                if entry.stat().st_size > _MAX_COPY_BYTES:
                    continue
            except OSError:
                continue
            yield entry.relative_to(root)


def copy_source_tree(src: Path, dst: Path) -> None:
    for rel in iter_source_files(src):
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, target)


def write_files(root: Path, files: dict[str, str | None]) -> None:
    """Apply ``{path: text or None}`` (None deletes) inside ``root``."""
    for rel, content in files.items():
        target = Path(root) / rel
        if content is None:
            if target.exists():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)


@contextlib.contextmanager
def overlay_project(project_dir: Path, files: dict[str, str | None]) -> Iterator[Path]:
    """A temporary copy of the project with ``files`` applied; removed on exit."""
    tmp = Path(tempfile.mkdtemp(prefix="havn-verify-"))
    try:
        copy_source_tree(Path(project_dir), tmp)
        write_files(tmp, files)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class AgentWorkspace:
    """A per-session copy of the project that a coding agent edits.

    :meth:`sync` makes it match the project plus the session's still-open
    change set (so an agent iterating on a failing change sees its own last
    proposal); :meth:`diff` lists what the agent changed relative to the
    project, split into what a change set may carry and what it may not.
    """

    def __init__(self, project_dir: Path, root: Path | None = None) -> None:
        self.project_dir = Path(project_dir)
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="havn-agent-"))
        self.root.mkdir(parents=True, exist_ok=True)

    def sync(self, pending: dict[str, str | None] | None = None) -> None:
        for rel in list(iter_source_files(self.root)):
            try:
                (self.root / rel).unlink()
            except OSError:
                pass
        copy_source_tree(self.project_dir, self.root)
        if pending:
            write_files(self.root, pending)

    def diff(self) -> tuple[list[dict], list[str]]:
        """``(proposals, ignored)``: ``[{path, content}]`` and other changed paths."""
        project_files = {p.as_posix(): p for p in iter_source_files(self.project_dir)}
        work_files = {p.as_posix(): p for p in iter_source_files(self.root)}
        proposals: list[dict] = []
        ignored: list[str] = []
        for rel in sorted(set(project_files) | set(work_files)):
            in_project = rel in project_files
            in_work = rel in work_files
            if in_project and in_work:
                try:
                    before = read_project_text(self.project_dir / rel)
                    after = read_project_text(self.root / rel)
                except OSError:
                    continue
                if before == after:
                    continue
                change = {"path": rel, "content": after}
            elif in_work:
                change = {"path": rel, "content": read_project_text(self.root / rel)}
            else:
                change = {"path": rel, "content": None}
            if is_allowed(rel):
                proposals.append(change)
            else:
                ignored.append(rel)
        return proposals, ignored

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
