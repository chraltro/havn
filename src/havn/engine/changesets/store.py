"""Change sets: proposed file edits held apart from the project until applied.

A change set lives in ``.havn/changesets/<id>.json``. Each file entry records
the proposed content and the SHA-256 of the file as it was on disk when the
change was proposed, so applying it can refuse when someone edited the file
in the meantime (the verification ran against the old content and would no
longer describe what is being written).

Only files a model change can involve are accepted: SQL under ``transform/``,
macros, unit tests and ask evals under ``tests/``, contracts and metrics.
Anything else an agent touches is reported, never applied.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from havn.textio import read_project_text

#: Root folder -> the file suffixes a change set may touch inside it.
ALLOWED_ROOTS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("transform",): (".sql",),
    ("macros",): (".py", ".sql"),
    ("contracts",): (".yml", ".yaml"),
    ("metrics",): (".yml", ".yaml"),
    ("tests", "unit"): (".yml", ".yaml", ".csv"),
    ("tests", "ask"): (".yml", ".yaml"),
}

MAX_FILE_BYTES = 1_000_000
MAX_FILES = 100
_KEEP = 200

STATUSES = ("pending", "verifying", "ready", "failed", "applied", "discarded")

_lock = threading.RLock()


class ChangeSetError(ValueError):
    """A change set request is invalid."""


class ChangeSetConflict(ChangeSetError):
    """A file changed on disk after the change set was proposed."""

    def __init__(self, paths: list[str]) -> None:
        super().__init__(
            "These files changed since the change set was proposed: "
            + ", ".join(paths)
            + ". Re-submit or re-verify it against the current files."
        )
        self.paths = paths


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_dir(project_dir: Path) -> Path:
    return Path(project_dir) / ".havn" / "changesets"


def check_path(path: str) -> str:
    """Normalise ``path`` and make sure a change set may touch it.

    Returns the POSIX relative path. Raises ChangeSetError otherwise.
    """
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        raise ChangeSetError("file path is empty")
    p = PurePosixPath(raw)
    if p.is_absolute() or (p.parts and ":" in p.parts[0]):
        raise ChangeSetError(f"{raw}: paths must be relative to the project root")
    if any(part in ("..", "") for part in p.parts) or p.parts[0] == ".":
        raise ChangeSetError(f"{raw}: path may not contain '..'")
    for root, suffixes in ALLOWED_ROOTS.items():
        if tuple(p.parts[: len(root)]) == root and len(p.parts) > len(root):
            if p.suffix.lower() not in suffixes:
                raise ChangeSetError(
                    f"{raw}: only {', '.join(suffixes)} files may change under {'/'.join(root)}/"
                )
            return p.as_posix()
    allowed = ", ".join("/".join(r) + "/" for r in ALLOWED_ROOTS)
    raise ChangeSetError(f"{raw}: change sets may only touch {allowed}")


def is_allowed(path: str) -> bool:
    try:
        check_path(path)
        return True
    except ChangeSetError:
        return False


@dataclass
class FileChange:
    path: str
    action: str  # "create" | "modify" | "delete"
    content: str | None  # proposed text; None for a delete
    base: str | None = None  # text on disk when proposed; None for a create
    base_sha: str | None = None

    def to_dict(self, *, include_content: bool = True) -> dict:
        d = asdict(self)
        if not include_content:
            d.pop("content")
            d.pop("base")
        return d


@dataclass
class ChangeSet:
    id: str
    source: str
    title: str = ""
    status: str = "pending"
    files: list[FileChange] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    report: dict | None = None
    session: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    applied_at: str | None = None
    applied_by: str | None = None
    revision: int = 1

    def to_dict(self, project_dir: Path | None = None, *, include_content: bool = True) -> dict:
        d = {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "status": self.status,
            "files": [f.to_dict(include_content=include_content) for f in self.files],
            "ignored": list(self.ignored),
            "report": self.report,
            "session": self.session,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "applied_at": self.applied_at,
            "applied_by": self.applied_by,
            "revision": self.revision,
        }
        if project_dir is not None and self.status not in ("applied", "discarded"):
            d["stale_files"] = stale_files(project_dir, self)
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> "ChangeSet":
        files = [FileChange(**f) for f in raw.get("files", [])]
        known = {k: raw[k] for k in (
            "id", "source", "title", "status", "ignored", "report", "session",
            "created_at", "updated_at", "applied_at", "applied_by", "revision",
        ) if k in raw}
        return cls(files=files, **known)


def _disk_state(project_dir: Path, rel: str) -> tuple[str | None, str | None]:
    """(text, sha) of the file on disk, or (None, None) when it is absent."""
    path = Path(project_dir) / rel
    if not path.is_file():
        return None, None
    return read_project_text(path), _sha(path.read_bytes())


def build_file_changes(project_dir: Path, proposals: list[dict]) -> list[FileChange]:
    """Turn ``[{path, content}]`` (content None = delete) into FileChanges.

    Proposals that would leave a file exactly as it is are dropped.
    """
    if not isinstance(proposals, list):
        raise ChangeSetError("files must be a list")
    if len(proposals) > MAX_FILES:
        raise ChangeSetError(f"at most {MAX_FILES} files per change set")
    out: list[FileChange] = []
    seen: set[str] = set()
    for p in proposals:
        if not isinstance(p, dict):
            raise ChangeSetError("each file must be an object with path and content")
        rel = check_path(p.get("path", ""))
        if rel.lower() in seen:
            raise ChangeSetError(f"{rel}: listed twice")
        seen.add(rel.lower())
        delete = bool(p.get("delete")) or p.get("content") is None
        content = None if delete else str(p.get("content"))
        if content is not None:
            content = content.replace("\r\n", "\n")
            if len(content.encode("utf-8")) > MAX_FILE_BYTES:
                raise ChangeSetError(f"{rel}: larger than {MAX_FILE_BYTES} bytes")
        base, base_sha = _disk_state(project_dir, rel)
        if delete:
            if base is None:
                continue  # deleting something that is not there: nothing to do
            out.append(FileChange(rel, "delete", None, base, base_sha))
        elif base is None:
            out.append(FileChange(rel, "create", content, None, None))
        elif base != content:
            out.append(FileChange(rel, "modify", content, base, base_sha))
    return out


def _path_for(project_dir: Path, cs_id: str) -> Path:
    if not cs_id or not all(c in "0123456789abcdef" for c in cs_id) or len(cs_id) > 32:
        raise ChangeSetError(f"invalid change set id {cs_id!r}")
    return store_dir(project_dir) / f"{cs_id}.json"


def save_change_set(project_dir: Path, cs: ChangeSet) -> ChangeSet:
    with _lock:
        cs.updated_at = _now()
        path = _path_for(project_dir, cs.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps(cs.to_dict(), indent=1, default=str), encoding="utf-8")
        os.replace(tmp, path)
        _prune(project_dir)
        return cs


def _prune(project_dir: Path) -> None:
    files = sorted(store_dir(project_dir).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def get_change_set(project_dir: Path, cs_id: str) -> ChangeSet:
    path = _path_for(project_dir, cs_id)
    if not path.is_file():
        raise ChangeSetError(f"change set {cs_id} not found")
    return ChangeSet.from_dict(json.loads(read_project_text(path)))


def list_change_sets(project_dir: Path, *, include_closed: bool = True, limit: int = 50) -> list[ChangeSet]:
    d = store_dir(project_dir)
    if not d.is_dir():
        return []
    out = []
    for path in d.glob("*.json"):
        try:
            cs = ChangeSet.from_dict(json.loads(read_project_text(path)))
        except Exception:
            continue
        if not include_closed and cs.status in ("applied", "discarded"):
            continue
        out.append(cs)
    out.sort(key=lambda c: c.updated_at, reverse=True)
    return out[:limit]


def create_change_set(
    project_dir: Path,
    proposals: list[dict],
    *,
    source: str,
    title: str = "",
    session: str | None = None,
    ignored: list[str] | None = None,
) -> ChangeSet:
    files = build_file_changes(project_dir, proposals)
    if not files:
        raise ChangeSetError("the proposed files are identical to the project; nothing to change")
    cs = ChangeSet(
        id=uuid.uuid4().hex[:12],
        source=str(source)[:80],
        title=str(title or "")[:200],
        files=files,
        ignored=list(ignored or []),
        session=session,
    )
    return save_change_set(project_dir, cs)


def revise_change_set(
    project_dir: Path,
    cs_id: str,
    proposals: list[dict],
    *,
    title: str | None = None,
    ignored: list[str] | None = None,
) -> ChangeSet:
    """Replace an open change set's files with a new full proposal."""
    with _lock:
        cs = get_change_set(project_dir, cs_id)
        if cs.status in ("applied", "discarded"):
            raise ChangeSetError(f"change set {cs_id} is {cs.status}; submit a new one")
        files = build_file_changes(project_dir, proposals)
        if not files:
            raise ChangeSetError("the proposed files are identical to the project; nothing to change")
        cs.files = files
        cs.status = "pending"
        cs.report = None
        cs.revision += 1
        if title is not None:
            cs.title = str(title)[:200]
        if ignored is not None:
            cs.ignored = list(ignored)
        return save_change_set(project_dir, cs)


def stale_files(project_dir: Path, cs: ChangeSet) -> list[str]:
    """Files whose disk content no longer matches what the change set saw."""
    out = []
    for f in cs.files:
        _text, sha = _disk_state(project_dir, f.path)
        if sha != f.base_sha:
            out.append(f.path)
    return out


def proposed_files(cs: ChangeSet) -> dict[str, str | None]:
    """``{path: new text or None for delete}``."""
    return {f.path: f.content for f in cs.files}


def apply_change_set(
    project_dir: Path,
    cs_id: str,
    *,
    force: bool = False,
    user: str | None = None,
) -> ChangeSet:
    """Write a change set's files into the project.

    Refuses a change set that has not passed verification unless ``force``,
    and always refuses when a file changed on disk since it was proposed. The
    writes are all-or-nothing: a failure part way restores what was written.
    """
    project_dir = Path(project_dir)
    with _lock:
        cs = get_change_set(project_dir, cs_id)
        if cs.status in ("applied", "discarded"):
            raise ChangeSetError(f"change set {cs_id} is already {cs.status}")
        if cs.status == "verifying":
            raise ChangeSetError(f"change set {cs_id} is still being verified")
        passed = bool(cs.report and cs.report.get("ok"))
        if not passed and not force:
            reason = "has not been verified" if not cs.report else "failed verification"
            raise ChangeSetError(f"change set {cs_id} {reason}; apply it anyway with force")
        stale = stale_files(project_dir, cs)
        if stale:
            raise ChangeSetConflict(stale)

        written: list[tuple[Path, bytes | None]] = []
        try:
            for f in cs.files:
                target = project_dir / f.path
                original = target.read_bytes() if target.is_file() else None
                written.append((target, original))
                if f.action == "delete":
                    if target.exists():
                        target.unlink()
                    continue
                text = f.content or ""
                if original is not None and b"\r\n" in original:
                    text = text.replace("\n", "\r\n")
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:6]}.havn-tmp")
                with open(tmp, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
                os.replace(tmp, target)
        except Exception:
            for target, original in reversed(written):
                try:
                    if original is None:
                        if target.exists():
                            target.unlink()
                    else:
                        target.write_bytes(original)
                except OSError:
                    pass
            raise
        cs.status = "applied"
        cs.applied_at = _now()
        cs.applied_by = user
        return save_change_set(project_dir, cs)


def discard_change_set(project_dir: Path, cs_id: str) -> ChangeSet:
    with _lock:
        cs = get_change_set(project_dir, cs_id)
        if cs.status == "applied":
            raise ChangeSetError(f"change set {cs_id} is already applied")
        cs.status = "discarded"
        return save_change_set(project_dir, cs)


def set_status(project_dir: Path, cs_id: str, status: str, report: dict | None = None) -> ChangeSet:
    with _lock:
        cs = get_change_set(project_dir, cs_id)
        if cs.status in ("applied", "discarded"):
            return cs
        cs.status = status
        if report is not None:
            cs.report = report
        return save_change_set(project_dir, cs)


def summary(cs: ChangeSet) -> dict[str, Any]:
    return {
        "id": cs.id,
        "status": cs.status,
        "files": [f"{f.action} {f.path}" for f in cs.files],
    }
