"""Changing a user's YAML file (project.yml, job files) without wrecking it.

havn edits a few hand-written files on the user's behalf: adding a
connector, saving alert or database settings, editing a job. Loading them
with PyYAML and dumping the dict back threw away every comment and reflowed
the file. This module does the read-modify-write in ruamel.yaml's round-trip
mode instead, so comments, key order, blank lines, quoting and the style of
untouched values survive, and writes the result atomically.

Use :func:`edit_yaml`::

    from havn import yamlio

    with yamlio.edit_yaml(path) as doc:
        conns = yamlio.ensure_map(doc, "connections")
        yamlio.set_value(conns, name, params)

Three rules the rest of havn depends on:

- The rest of havn reads these files with PyYAML (YAML 1.1), so this module
  resolves plain scalars exactly as PyYAML does, both when reading
  (``enabled: yes`` is ``True`` here too) and when writing (a new string
  ``"on"`` or ``"0 6 * * *"`` is quoted when PyYAML would read it back as
  something else). Nothing changes type across a rewrite.
- The file is written to a temp file in the same directory and moved over
  the target with ``os.replace``: a crash leaves the old file or the new one,
  never half of it. UTF-8, no BOM, the file's own newline convention.
- Edits to one path are serialised by a per-path lock (within this process;
  a CLI and a server editing the same file at once are not coordinated).

Limits of the round trip (ruamel.yaml's, observed with 0.18): the indentation
of block mappings and sequences is normalised to one style per file (guessed
from the file); YAML 1.1 boolean spellings other than true/false (``yes``,
``On``) are written back as ``true``/``false``, sexagesimal numbers as plain
integers and ``null``/``~`` as an empty value; all mean the same to PyYAML.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Iterator

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import CommentMark, YAMLError
from ruamel.yaml.nodes import ScalarNode
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.resolver import VersionedResolver
from ruamel.yaml.scalarint import OctalInt
from ruamel.yaml.scalarstring import ScalarString
from ruamel.yaml.tokens import CommentToken

from havn.textio import read_project_text

__all__ = [
    "YamlLoadError",
    "edit_yaml",
    "ensure_map",
    "set_value",
    "delete_key",
    "sync_mapping",
    "dumps",
    "atomic_write_text",
]


# ---------------------------------------------------------------------------
# Resolver: plain scalars mean what they mean to PyYAML
# ---------------------------------------------------------------------------

_PYYAML_RULES: dict[Any, list[tuple[str, Any]]] | None = None


def _pyyaml_rules() -> dict[Any, list[tuple[str, Any]]]:
    """PyYAML's implicit-resolver table, keyed by first character.

    ruamel's own YAML 1.1 table differs in small ways (it reads ``y`` and
    ``n`` as booleans, PyYAML does not), so the table is taken from PyYAML
    itself rather than approximated.
    """
    global _PYYAML_RULES
    if _PYYAML_RULES is None:
        import yaml

        rules: dict[Any, list[tuple[str, Any]]] = {}
        for first, entries in yaml.resolver.Resolver.yaml_implicit_resolvers.items():
            rules[first] = list(entries)
        rules.pop(None, None)
        _PYYAML_RULES = rules
    return _PYYAML_RULES


class _PyYamlResolver(VersionedResolver):
    """Round-trip resolver with PyYAML's YAML 1.1 rules.

    The version is pinned to 1.1 so ruamel's constructors parse ``012`` as
    octal and ``1:30`` as sexagesimal (as PyYAML does), without the
    ``%YAML 1.1`` directive that setting ``YAML.version`` would add to the
    output.
    """

    def __init__(self, version: Any = None, loader: Any = None, loadumper: Any = None) -> None:
        super().__init__(version=(1, 1), loader=loader, loadumper=loadumper)

    @property
    def versioned_resolver(self) -> Any:
        # ruamel's resolve() does ``resolvers += table.get(None, [])`` on the
        # list it got back; hand out copies so the shared table never grows.
        return {k: list(v) for k, v in _pyyaml_rules().items()}


class _Representer(RoundTripRepresenter):
    """Writes YAML 1.1 octal (``012``) back as written, not as ``!!int '0o12'``.

    ruamel picks the ``0o`` prefix unless the document carries a
    ``%YAML 1.1`` directive, which this module deliberately does not emit.
    """

    def represent_octal_int(self, data: Any) -> ScalarNode:
        width = getattr(data, "_width", None)
        s = f"{data:0{width}o}" if width is not None else format(data, "o")
        anchor = data.yaml_anchor(any=True)
        return self.insert_underscore("0", s, data._underscore, anchor=anchor)


_Representer.add_representer(OctalInt, _Representer.represent_octal_int)


def _make_yaml(text: str) -> YAML:
    y = YAML()  # round-trip
    y.Resolver = _PyYamlResolver
    y.Representer = _Representer
    y.preserve_quotes = True
    y.width = 2**31 - 1  # never re-wrap long strings
    mapping, sequence, offset = _guess_indent(text)
    y.indent(mapping=mapping, sequence=sequence, offset=offset)
    if re.match(r"\A(?:%[^\n]*\n)*---", text):
        y.explicit_start = True
    return y


_KEY_ONLY = re.compile(r"^(\s*)(?:-\s+)?[^#'\"\s][^#]*:\s*(?:#.*)?$")


def _guess_indent(text: str) -> tuple[int, int, int]:
    """Guess (mapping indent, sequence indent, dash offset) from the file.

    ruamel writes one indentation style for the whole file; taking it from
    the first nested mapping and the first block sequence keeps a file that
    is consistently indented looking the way it did. PyYAML's style (what
    havn wrote before) is the default: two spaces, dashes flush with the key.
    """
    mapping: int | None = None
    sequence: int | None = None
    offset: int | None = None
    prev_key_col: int | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        col = len(raw) - len(raw.lstrip(" "))
        if prev_key_col is not None:
            if stripped.startswith("- ") or stripped == "-":
                if offset is None and col >= prev_key_col:
                    offset = col - prev_key_col
                    rest = raw[col + 1:]
                    content = col + 1 + (len(rest) - len(rest.lstrip(" "))) if rest.strip() else col + 2
                    sequence = content - prev_key_col
            elif mapping is None and col > prev_key_col:
                mapping = col - prev_key_col
        if mapping is not None and offset is not None:
            break
        m = _KEY_ONLY.match(raw)
        if m:
            # Column of the key itself, past any "- " of a sequence item.
            body = raw[col:]
            if body.startswith("-"):
                body_rest = body[1:]
                prev_key_col = col + 1 + (len(body_rest) - len(body_rest.lstrip(" ")))
            else:
                prev_key_col = col
        else:
            prev_key_col = None
    mapping = mapping if mapping and mapping > 0 else 2
    if offset is None or sequence is None or sequence < offset + 2:
        offset, sequence = 0, mapping
        if sequence < 2:
            sequence = 2
    return mapping, sequence, offset


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

class YamlLoadError(ValueError):
    """The file is not valid YAML (or has a duplicate key); it was not changed.

    Duplicate keys are refused rather than resolved: PyYAML keeps the last
    value, ruamel would keep the first, and a rewrite must not change which
    one havn sees.
    """


#: The document being edited, so helpers can find a mapping's parents.
_ROOT: ContextVar[CommentedMap | None] = ContextVar("havn_yamlio_root", default=None)

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def _newline_of(path: Path) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "\n"
    return "\r\n" if b"\r\n" in data else "\n"


def atomic_write_text(path: str | Path, text: str, *, newline: str = "\n") -> None:
    """Write ``text`` to ``path`` through a temp file and ``os.replace``.

    UTF-8 without a BOM; ``"\\n"`` in ``text`` becomes ``newline``. The temp
    file sits next to the target (same filesystem, so the replace is atomic)
    and is removed if anything fails. The file mode of an existing target is
    kept, and the new file's mtime is made to differ from the old one so
    mtime-keyed caches notice the change even on coarse-grained filesystems.
    """
    path = Path(path)
    data = text.replace("\r\n", "\n")
    if newline != "\n":
        data = data.replace("\n", newline)
    payload = data.encode("utf-8")
    try:
        old = path.stat()
    except FileNotFoundError:
        old = None
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if old is not None:
            try:
                os.chmod(tmp, old.st_mode & 0o7777)
            except OSError:
                pass
        else:
            umask = os.umask(0)
            os.umask(umask)
            try:
                os.chmod(tmp, 0o666 & ~umask)
            except OSError:
                pass
        _replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if old is not None:
        try:
            new = path.stat()
            if new.st_mtime_ns <= old.st_mtime_ns:
                bumped = old.st_mtime_ns + 10_000_000  # 10 ms: survives float rounding
                os.utime(path, ns=(new.st_atime_ns, bumped))
        except OSError:
            pass


def _replace(src: Path, dst: Path) -> None:
    # On Windows a reader holding the target open (an editor, a virus
    # scanner) makes the replace fail with a sharing violation for a moment.
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


# ---------------------------------------------------------------------------
# Edit session
# ---------------------------------------------------------------------------


def dumps(doc: Any, *, like: str = "") -> str:
    """Serialise a round-trip document, in the indentation style of ``like``."""
    y = _make_yaml(like)
    buf = StringIO()
    y.dump(doc, buf)
    return buf.getvalue()


@contextmanager
def edit_yaml(
    path: str | Path,
    *,
    create: Callable[[], dict] | None = None,
) -> Iterator[CommentedMap]:
    """Load ``path`` for editing; write it back when the block exits cleanly.

    Yields the top-level mapping (a ``CommentedMap``, which behaves as a
    dict). An empty or comment-only file yields an empty mapping and its
    comments are kept at the top of the result. If the file does not exist,
    ``create()`` supplies the initial content, or ``FileNotFoundError`` is
    raised. If the block raises, the file is not touched. Nothing is written
    when the result is identical to the file.
    """
    path = Path(path)
    with _lock_for(path):
        exists = path.exists()
        if exists:
            text = read_project_text(path)
            newline = _newline_of(path)
        elif create is not None:
            text = ""
            newline = "\n"
        else:
            raise FileNotFoundError(str(path))

        y = _make_yaml(text)
        try:
            doc = y.load(text) if text.strip() else None
        except YAMLError as e:
            raise YamlLoadError(f"{path.name}: {e}") from e
        header = ""
        if doc is None:
            # Empty, only comments, or an explicit null: ruamel returns None
            # and drops the comments, so they are carried over as text.
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if lines and all(ln.lstrip().startswith("#") for ln in lines):
                header = text if text.endswith("\n") else text + "\n"
            doc = CommentedMap()
            if not exists and create is not None:
                for k, v in create().items():
                    doc[k] = to_roundtrip(v)
        elif not isinstance(doc, CommentedMap):
            raise ValueError(f"{path.name}: the top level is not a mapping")

        root_token = _ROOT.set(doc)
        try:
            yield doc
        finally:
            _ROOT.reset(root_token)

        if len(doc) == 0 and exists and header:
            return  # comment-only file, nothing added: leave it alone
        body = dumps(doc, like=text) if len(doc) else ""
        if body.strip() == "{}":
            body = ""
        out = header + body
        if exists and out == text:
            return
        atomic_write_text(path, out, newline=newline)


# ---------------------------------------------------------------------------
# Structural helpers that keep comments where they belong
# ---------------------------------------------------------------------------


def to_roundtrip(value: Any) -> Any:
    """Turn plain dicts/lists into ruamel containers (block style)."""
    if isinstance(value, (CommentedMap, CommentedSeq)):
        return value
    if isinstance(value, dict):
        m = CommentedMap()
        for k, v in value.items():
            m[k] = to_roundtrip(v)
        return m
    if isinstance(value, (list, tuple)):
        s = CommentedSeq()
        for v in value:
            s.append(to_roundtrip(v))
        return s
    return value


def _get_token(container: Any, key: Any) -> CommentToken | None:
    entry = container.ca.items.get(key)
    if not entry:
        return None
    idx = 2 if isinstance(container, CommentedMap) else 0
    return entry[idx] if len(entry) > idx else None


def _set_token(container: Any, key: Any, token: CommentToken | None) -> None:
    entry = container.ca.items.setdefault(key, [None, None, None, None])
    idx = 2 if isinstance(container, CommentedMap) else 0
    entry[idx] = token


def _is_flow(node: Any) -> bool:
    try:
        return bool(node.fa.flow_style())
    except AttributeError:
        return False


def _leaf(container: Any, key: Any) -> tuple[Any, Any]:
    """The (container, key) whose comment token follows this entry's text.

    ruamel attaches the comment lines that follow an entry to the deepest,
    last scalar inside it, so that is where to look for (or put) them.
    """
    value = container[key]
    if isinstance(value, CommentedMap) and len(value) and not _is_flow(value):
        return _leaf(value, list(value.keys())[-1])
    if isinstance(value, CommentedSeq) and len(value) and not _is_flow(value):
        return _leaf(value, len(value) - 1)
    return container, key


def _split(token: CommentToken | None) -> tuple[str, str]:
    """(end-of-line part, following full lines) of a comment token."""
    if token is None:
        return "", ""
    value = token.value
    nl = value.find("\n")
    if nl < 0:
        return value, ""
    return value[: nl + 1], value[nl + 1:]


def _take_trailing(container: Any, key: Any) -> str:
    """Detach and return the full comment lines that follow an entry."""
    c, k = _leaf(container, key)
    token = _get_token(c, k)
    head, tail = _split(token)
    if not tail:
        return ""
    if head.strip():
        token.value = head  # keep the entry's own end-of-line comment
    else:
        _set_token(c, k, None)
    return tail


def _append_after(container: Any, key: Any, lines: str) -> None:
    """Put comment ``lines`` after an entry's text (after its own comments)."""
    c, k = _leaf(container, key)
    token = _get_token(c, k)
    if token is None:
        _set_token(c, k, CommentToken("\n" + lines, CommentMark(0), None))
    else:
        value = token.value if token.value.endswith("\n") else token.value + "\n"
        token.value = value + lines


def _append_before_first(parent: Any, parent_key: Any, mapping: CommentedMap, lines: str) -> None:
    """Put comment ``lines`` before the first key of ``mapping``."""
    if parent is None:
        # Top level: the file's header comments.
        if mapping.ca.comment is None:
            mapping.ca.comment = [None, []]
        if mapping.ca.comment[1] is None:
            mapping.ca.comment[1] = []
        mapping.ca.comment[1].append(CommentToken(lines, CommentMark(0), None))
        return
    token = _get_token(parent, parent_key)
    if token is None:
        token = CommentToken("\n" + lines, CommentMark(0), None)
        _set_token(parent, parent_key, token)
    else:
        value = token.value if token.value.endswith("\n") else token.value + "\n"
        token.value = value + lines
    # The emitter reads the same slot through the mapping's own comment.
    if mapping.ca.comment is None:
        mapping.ca.comment = [token, None]
    else:
        mapping.ca.comment[0] = token


def ensure_map(doc: CommentedMap, *path: str) -> CommentedMap:
    """Return the mapping at ``path``, creating it when missing or null.

    A key that is present with a null value (``connections:``) is given an
    empty mapping in place, keeping its position and comments. A value that
    is present but is not a mapping raises ``TypeError``.
    """
    node: Any = doc
    for key in path:
        value = node.get(key) if key in node else None
        if key not in node or value is None:
            set_value(node, key, CommentedMap())
            value = node[key]
        elif not isinstance(value, dict):
            raise TypeError(f"'{key}' is not a mapping")
        if not isinstance(value, CommentedMap):
            value = to_roundtrip(value)
            node[key] = value
        node = value
    return node


def set_value(mapping: CommentedMap, key: str, value: Any) -> None:
    """Set ``mapping[key]``, keeping comments and the old value's quoting.

    - An existing key keeps its position and comments. If the old value was
      a quoted string and the new one is a string, it stays quoted the same
      way. An equal value is left exactly as written.
    - A new key goes at the end; the comment lines that trailed the old last
      entry (often a commented-out example, or a paragraph about the next
      section) move down so they still follow the block.
    - dicts and lists become block YAML.
    """
    if not isinstance(mapping, CommentedMap):
        mapping[key] = value
        return
    if key in mapping:
        old = mapping[key]
        if _same(old, value):
            return
        if (
            isinstance(old, CommentedMap) and isinstance(value, dict) and value
            and not _is_flow(old)
        ):
            _sync(old, value)
            return
        if isinstance(old, ScalarString) and isinstance(value, str) and not isinstance(value, ScalarString):
            value = type(old)(value)
        trailing = "" if _is_flow(mapping) else _take_trailing(mapping, key)
        mapping[key] = to_roundtrip(value)
        if trailing:
            _reattach(mapping, key, trailing)
        return
    trailing = ""
    if len(mapping) and not _is_flow(mapping):
        trailing = _take_trailing(mapping, list(mapping.keys())[-1])
    if _is_flow(mapping):
        mapping.fa.set_block_style()
    mapping[key] = to_roundtrip(value)
    if trailing:
        _reattach(mapping, key, trailing)


def _renders_empty(value: Any) -> bool:
    return isinstance(value, (CommentedMap, CommentedSeq, dict, list)) and not value


def _reattach(mapping: CommentedMap, key: Any, lines: str) -> None:
    """Put comment lines after the entry ``mapping[key]``."""
    if _renders_empty(mapping[key]):
        # Written as ``key: {}``: a comment on the entry would land before
        # the ``{}``, so the lines go in front of whatever follows instead.
        _place_before_next([*_chain(mapping), (mapping, key)], lines)
    else:
        _append_after(mapping, key, lines)


def delete_key(doc: CommentedMap, *path: str) -> bool:
    """Delete the key at ``path``; return whether it was there.

    The entry's own comments (on its lines, and inside it) go with it. The
    full comment lines that followed it belong to whatever comes next, so
    they are kept: moved after the previous entry, or, for the first entry,
    to just before the new first entry (or in front of the next key when the
    mapping is left empty).
    """
    if not path:
        raise ValueError("empty path")
    node: Any = doc
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    last = path[-1]
    if not isinstance(node, dict) or last not in node:
        return False
    token = _ROOT.set(_ROOT.get() or doc)
    try:
        _delete(node, last)
    finally:
        _ROOT.reset(token)
    return True


def _delete(node: Any, key: Any) -> None:
    if not isinstance(node, CommentedMap):
        del node[key]
        return
    keys = list(node.keys())
    idx = keys.index(key)
    trailing = _take_trailing(node, key) if not _is_flow(node) else ""
    del node[key]
    node.ca.items.pop(key, None)
    if not trailing:
        return
    if idx > 0:
        _append_after(node, keys[idx - 1], trailing)
        return
    chain = _chain(node)
    if len(node) or not chain:
        parent, parent_key = chain[-1] if chain else (None, None)
        _append_before_first(parent, parent_key, node, trailing)
    else:
        _place_before_next(chain, trailing)


def _chain(target: Any) -> list[tuple[Any, Any]]:
    """(container, key) pairs from the edited document down to ``target``."""
    root = _ROOT.get()
    if root is None or target is root:
        return []

    def walk(node: Any, trail: list[tuple[Any, Any]]) -> list[tuple[Any, Any]] | None:
        if isinstance(node, CommentedMap):
            items = list(node.items())
        elif isinstance(node, CommentedSeq):
            items = list(enumerate(node))
        else:
            return None
        for k, v in items:
            step = [*trail, (node, k)]
            if v is target:
                return step
            found = walk(v, step)
            if found is not None:
                return found
        return None

    return walk(root, []) or []


def _place_before_next(chain: list[tuple[Any, Any]], lines: str) -> None:
    """Put comment lines before the first key that follows ``chain[-1]``."""
    for parent, parent_key in reversed(chain):
        if not isinstance(parent, CommentedMap):
            break
        keys = list(parent.keys())
        i = keys.index(parent_key)
        if i + 1 < len(keys):
            entry = parent.ca.items.setdefault(keys[i + 1], [None, None, None, None])
            if entry[1] is None:
                entry[1] = []
            entry[1].insert(0, CommentToken(lines, CommentMark(0), None))
            return
    # Nothing follows: end-of-document comments.
    doc = chain[0][0] if chain else _ROOT.get()
    if not isinstance(doc, CommentedMap):
        return
    if doc.ca.comment is None:
        doc.ca.comment = [None, None]
    doc.yaml_end_comment_extend([CommentToken(lines, CommentMark(0), None)])


def sync_mapping(mapping: CommentedMap, data: dict) -> None:
    """Make ``mapping`` equal to ``data``, touching only what differs.

    Keys whose value is unchanged are left exactly as written; nested
    mappings are synced recursively; new keys are appended in ``data``'s
    order; keys missing from ``data`` are deleted last (comments handled as
    in :func:`delete_key`), so a mapping is never emptied on the way.
    """
    token = _ROOT.set(_ROOT.get() or mapping)
    try:
        _sync(mapping, data)
    finally:
        _ROOT.reset(token)


def _sync(mapping: CommentedMap, data: dict) -> None:
    for key, value in data.items():
        set_value(mapping, key, value)
    for key in [k for k in mapping.keys() if k not in data]:
        _delete(mapping, key)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is bool and type(b) is bool and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return list(a.keys()) == list(b.keys()) and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, (dict, list, tuple)) or isinstance(b, (dict, list, tuple)):
        return False
    if a is None or b is None:
        return a is None and b is None
    # ruamel's ScalarInt/ScalarFloat/ScalarString subclass the builtins.
    for kind in (str, int, float):
        if isinstance(a, kind) or isinstance(b, kind):
            return isinstance(a, kind) and isinstance(b, kind) and a == b
    return type(a) is type(b) and a == b
