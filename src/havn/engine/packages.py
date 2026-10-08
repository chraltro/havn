"""Installing and locating havn packages.

A package is a directory laid out like a havn project -- ``transform/`` and
``macros/`` -- published so other projects can reuse its models and macros.
``packages:`` in ``project.yml`` declares them, ``havn packages install``
fetches them into ``havn_packages/<name>/``, and ``havn_packages.lock``
records the commit each one resolved to.

The lock is the installed state and is meant to be committed. Everything
downstream of installation (:func:`package_roots`, model discovery, macro
registration) reads the lock rather than the config, so a checkout that has
not run ``install`` yet simply sees no packages instead of half of them.

Git work shells out to the git CLI via subprocess with an argument list, in
the same style as :mod:`havn.engine.git`: no shell, and every user-supplied
argument validated first so it cannot be mistaken for an option.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from havn.config import PackageConfig, ProjectConfig

logger = logging.getLogger(__name__)

PACKAGES_DIRNAME = "havn_packages"
LOCK_FILENAME = "havn_packages.lock"
MANIFEST_FILENAME = "havn_package.yml"

# A revision may be a tag, a branch or a commit. Git's own refname rules are
# wider than this, but this covers every spelling a package would use and
# keeps out anything that could read as an option or a path traversal.
_SAFE_REV_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")

_CLONE_TIMEOUT = 180
_GIT_TIMEOUT = 60

# Directories never copied out of a local-path package: build noise, and the
# package's own .git, which would confuse the outer repository.
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", ".venv", "node_modules", "*.duckdb", "*.duckdb.wal"
)


class PackageError(RuntimeError):
    """A package could not be installed or read."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_rev(rev: str) -> bool:
    """Validate a git revision (tag, branch or commit)."""
    if not rev or len(rev) > 250:
        return False
    if rev.startswith("-") or ".." in rev or rev.endswith(".lock"):
        return False
    return bool(_SAFE_REV_RE.match(rev))


# A ``git:`` source may only speak one of these. Everything else git would
# accept is either an arbitrary-command transport (``ext::sh -c ...``), a way
# to read the server's own disk (``file://``, a bare local path) or a
# cleartext protocol (``git://``, ``http://``).
_ALLOWED_GIT_SCHEMES = ("https://", "ssh://")

# ``git@github.com:owner/repo.git`` -- scp-like syntax, the common SSH remote.
_SCP_LIKE_RE = re.compile(
    r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:(?![/\\])[A-Za-z0-9._/~-]+$"
)

# A test seam, never true in production and deliberately not settable from
# project.yml, the CLI, the API or the environment: havn's own package tests
# clone from bare repositories in a temp directory so they exercise the real
# ``git clone``/``git checkout`` paths without a network. A local directory as
# a package source is supported for users through ``path:``, which is resolved
# against the project directory instead of being handed to git.
_ALLOW_LOCAL_GIT_REMOTES = False


def _validate_git_url(url: str) -> bool:
    """Validate a ``git:`` remote URL.

    Installing a package runs ``git clone`` with this string and then imports
    the result's Python macros, so the transport is part of the trust
    boundary. Only ``https://``, ``ssh://`` and the scp-like ``git@host:path``
    are accepted.

    Everything else git understands is refused on purpose:

    - ``ext::<command>`` makes git run an arbitrary shell command as the
      transport, so a project.yml could execute code on whoever installs it.
    - ``file://`` and a bare local path turn a package entry into a read of
      the server's own filesystem. A local directory is a legitimate source,
      but it belongs under ``path:``, which is resolved against the project
      directory rather than handed to git.
    - ``git://`` and ``http://`` are unauthenticated cleartext, so the code
      that gets imported is whatever the network returned.

    A leading dash (which git reads as an option), embedded NUL or newline,
    and absurd lengths are still rejected before any of that.
    """
    if not url or len(url) > 2000:
        return False
    if url.startswith("-"):
        return False
    if any(c in url for c in ("\x00", "\n", "\r")):
        return False
    lowered = url.lower()
    if lowered.startswith(_ALLOWED_GIT_SCHEMES):
        return True
    # Anything else carrying a transport marker is refused by name, including
    # git's "<transport>::<address>" form.
    if "::" in url or "://" in url:
        return False
    if _SCP_LIKE_RE.match(url):
        return True
    return _ALLOW_LOCAL_GIT_REMOTES and Path(url).is_dir()


def _run_git(cwd: Path, *args: str, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a git command in *cwd*, never through a shell."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise PackageError(
            "git is not installed or not on PATH; havn packages needs it to fetch git sources"
        ) from None
    except subprocess.TimeoutExpired:
        raise PackageError(f"git {' '.join(args[:2])} timed out after {timeout}s") from None


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class PackageManifest:
    """``havn_package.yml`` at a package root. Every field is optional."""

    name: str = ""
    version: str = ""
    requires_havn: str = ""
    description: str = ""
    # Schema overrides: ``{"silver": "silver"}`` keeps the package's silver
    # models in ``silver`` instead of the default ``<pkg>_silver``.
    schemas: dict[str, str] = field(default_factory=dict)


@dataclass
class LockEntry:
    """One package as installed, recorded in ``havn_packages.lock``."""

    name: str
    source: str  # "git" or "path"
    git: str = ""
    rev: str = ""
    commit: str = ""
    path: str = ""

    def to_dict(self) -> dict[str, str]:
        out: dict[str, str] = {"name": self.name, "source": self.source}
        if self.source == "git":
            out["git"] = self.git
            out["rev"] = self.rev
            out["commit"] = self.commit
        else:
            out["path"] = self.path
        return out


@dataclass
class InstallResult:
    """What one package did during an install."""

    name: str
    source: str  # "git" or "path"
    path: Path
    ref: str = ""  # the requested rev, or the source path for local packages
    commit: str = ""  # resolved commit, "" for local packages
    status: str = "installed"  # "installed", "unchanged", "removed" or "error"
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status != "error"


@dataclass
class PackageRoot:
    """An installed package on disk, ready for discovery."""

    name: str
    path: Path
    transform_dir: Path
    macros_dir: Path
    manifest: PackageManifest

    def schema_for(self, schema: str) -> str:
        """The warehouse schema a package schema lands in.

        ``silver`` in package ``crm`` becomes ``crm_silver`` by default, so a
        package can never take a name the project was already using. The
        package's own ``havn_package.yml`` can override a schema to land
        somewhere else -- including back on its own name, which is how a
        package deliberately writes into a shared schema and accepts the
        collision risk that comes with it.
        """
        override = self.manifest.schemas.get(schema)
        if override:
            return override
        return f"{self.name}_{schema}"


# ---------------------------------------------------------------------------
# Paths, lock file and manifest
# ---------------------------------------------------------------------------


def packages_dir(project_dir: Path) -> Path:
    """The directory installed packages live in."""
    return Path(project_dir) / PACKAGES_DIRNAME


def lock_path(project_dir: Path) -> Path:
    """The lock file path, next to ``project.yml``."""
    return Path(project_dir) / LOCK_FILENAME


def read_lock(project_dir: Path) -> dict[str, LockEntry]:
    """Read ``havn_packages.lock``, keyed by package name.

    A missing or unreadable lock is an empty lock: a project that has never
    installed anything must behave exactly as it did before packages existed.
    """
    path = lock_path(project_dir)
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}
    entries: dict[str, LockEntry] = {}
    for item in raw.get("packages", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        if not name:
            continue
        entries[name] = LockEntry(
            name=name,
            source=str(item.get("source", "git")),
            git=str(item.get("git", "")),
            rev=str(item.get("rev", "")),
            commit=str(item.get("commit", "")),
            path=str(item.get("path", "")),
        )
    return entries


def write_lock(project_dir: Path, entries: list[LockEntry]) -> Path:
    """Write ``havn_packages.lock``. Entries are sorted by name for a stable diff."""
    path = lock_path(project_dir)
    payload = {
        "version": 1,
        "packages": [e.to_dict() for e in sorted(entries, key=lambda e: e.name)],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


def read_manifest(package_path: Path) -> PackageManifest:
    """Read ``havn_package.yml`` from a package root, or return the defaults."""
    path = Path(package_path) / MANIFEST_FILENAME
    if not path.is_file():
        return PackageManifest()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return PackageManifest()
    if not isinstance(raw, dict):
        return PackageManifest()
    schemas_raw = raw.get("schemas", {}) or {}
    schemas = (
        {str(k): str(v) for k, v in schemas_raw.items()}
        if isinstance(schemas_raw, dict)
        else {}
    )
    return PackageManifest(
        name=str(raw.get("name", "")),
        version=str(raw.get("version", "")),
        requires_havn=str(raw.get("requires_havn", "")),
        description=str(raw.get("description", "")),
        schemas=schemas,
    )


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", text.strip()):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    return tuple(parts) or (0,)


def check_requires_havn(requirement: str) -> str:
    """Check a manifest's ``requires_havn`` against the running havn.

    Returns a warning string, or "" when satisfied or unparseable. This warns
    rather than fails: a package that declares a floor havn does not meet is
    usually still usable, and refusing to load it would be a worse default
    than letting the author's own SQL fail with a real message.
    """
    req = (requirement or "").strip()
    if not req:
        return ""
    match = re.match(r"^(>=|==|>)\s*v?([0-9][0-9.]*)$", req)
    if not match:
        return ""
    op, wanted = match.group(1), match.group(2)
    from havn import __version__

    have = _version_tuple(__version__)
    need = _version_tuple(wanted)
    if op == ">=":
        ok = have >= need
    elif op == ">":
        ok = have > need
    else:
        ok = have[: len(need)] == need
    if ok:
        return ""
    return f"requires havn {req}, running {__version__}"


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def _resolve_head(repo: Path) -> str:
    result = _run_git(repo, "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else ""


def _head_is_branch(repo: Path) -> bool:
    """True when HEAD points at a branch rather than a detached commit.

    A tag or a commit checks out detached; a branch does not. That is the
    cheapest honest way to tell whether the rev the user pinned actually
    pins anything.
    """
    return _run_git(repo, "symbolic-ref", "-q", "HEAD").returncode == 0


def _clone(url: str, rev: str, dest: Path) -> list[str]:
    """Clone *url* at *rev* into *dest*. Returns warnings.

    Tries a shallow clone of the rev first, which covers tags and branches in
    one round trip. A commit SHA cannot be cloned that way, so the fallback is
    a full clone plus a checkout.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)

    shallow = _run_git(
        dest.parent,
        "clone",
        "--quiet",
        "--depth",
        "1",
        "--branch",
        rev,
        "--",
        url,
        dest.name,
        timeout=_CLONE_TIMEOUT,
    )
    if shallow.returncode != 0:
        if dest.exists():
            shutil.rmtree(dest)
        full = _run_git(
            dest.parent, "clone", "--quiet", "--", url, dest.name, timeout=_CLONE_TIMEOUT
        )
        if full.returncode != 0:
            raise PackageError(
                f"git clone of {url} failed: {full.stderr.strip() or 'unknown error'}"
            )
        checkout = _run_git(dest, "checkout", "--quiet", rev)
        if checkout.returncode != 0:
            raise PackageError(
                f"git checkout of '{rev}' in {url} failed: "
                f"{checkout.stderr.strip() or 'no such revision'}"
            )

    warnings: list[str] = []
    if _head_is_branch(dest):
        warnings.append(
            f"rev '{rev}' is a branch, not a pin: it moves under you. "
            "Use a tag or a commit for a reproducible build."
        )
    return warnings


def _check_local_source(source: Path, dest: Path) -> None:
    """Refuse a ``path:`` package that contains the destination.

    ``path: .`` or ``path: ..`` names a directory that holds
    ``havn_packages/`` itself, so ``copytree`` copies the growing destination
    into itself and recurses until the path length gives out. The comparison
    is on resolved paths, and covers the destination and every ancestor of it.

    This runs before anything is written, so a refusal leaves whatever was
    installed before exactly as it was.
    """
    if not source.is_dir():
        raise PackageError(f"local package path does not exist: {source}")

    dest_resolved = dest.resolve() if dest.exists() else dest.absolute()
    packages_root = dest_resolved.parent
    for inside in (dest_resolved, packages_root):
        if inside == source or source in inside.parents:
            raise PackageError(
                f"local package path {source} contains {packages_root}, so "
                "installing it would copy the package directory into itself. "
                "Point 'path:' at the package itself, not at the project or a "
                "parent of it."
            )


def _copy_local(source: Path, dest: Path) -> None:
    """Copy a ``path:`` package into ``dest``.

    Symlinks are copied as symlinks rather than followed: following one out of
    the package would copy whatever it points at into the checkout, and a
    dangling one raises ``shutil.Error`` part way through the copy.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, ignore=_COPY_IGNORE, symlinks=True)


def _discard_partial(dest: Path) -> None:
    """Remove a checkout an install left half-written."""
    if not dest.exists():
        return
    try:
        shutil.rmtree(dest)
    except OSError as exc:
        logger.warning("Could not remove the partial checkout at %s: %s", dest, exc)


def _install_one(
    project_dir: Path,
    pkg: PackageConfig,
    locked: LockEntry | None,
    *,
    upgrade: bool,
) -> InstallResult:
    dest = packages_dir(project_dir) / pkg.name

    if pkg.path:
        source = Path(pkg.path)
        if not source.is_absolute():
            source = project_dir / source
        source = source.resolve()
        _check_local_source(source, dest)
        try:
            _copy_local(source, dest)
        except (shutil.Error, OSError):
            # A copy that stopped part way leaves a directory that looks
            # installed and is not. Nothing is better than half.
            _discard_partial(dest)
            raise
        return InstallResult(
            name=pkg.name,
            source="path",
            path=dest,
            ref=str(source),
            status="installed",
        )

    url = pkg.git or ""
    rev = pkg.rev or ""
    if not _validate_git_url(url):
        raise PackageError(
            f"package '{pkg.name}': invalid git URL {url!r}. A git source must "
            "be https://, ssh:// or git@host:path. For a package on this "
            "machine use 'path:' instead of 'git:'."
        )
    if not _validate_rev(rev):
        raise PackageError(f"package '{pkg.name}': invalid rev {rev!r}")

    # The lock wins unless --upgrade, so a second install reproduces the first
    # even when the tag has since been moved or the branch has advanced.
    target = rev
    from_lock = False
    if (
        not upgrade
        and locked is not None
        and locked.source == "git"
        and locked.git == url
        and locked.rev == rev
        and locked.commit
    ):
        target = locked.commit
        from_lock = True

    if dest.is_dir() and (dest / ".git").exists():
        head = _resolve_head(dest)
        if head and from_lock and head == target:
            return InstallResult(
                name=pkg.name,
                source="git",
                path=dest,
                ref=rev,
                commit=head,
                status="unchanged",
            )

    try:
        warnings = _clone(url, target, dest)
        commit = _resolve_head(dest)
    except (PackageError, shutil.Error, OSError):
        # A clone that succeeded and a checkout that did not leaves a
        # directory at the wrong revision, which the next run would treat as
        # installed.
        _discard_partial(dest)
        raise
    if not commit:
        _discard_partial(dest)
        raise PackageError(f"package '{pkg.name}': could not resolve the cloned commit")
    return InstallResult(
        name=pkg.name,
        source="git",
        path=dest,
        ref=rev,
        commit=commit,
        status="installed",
        warnings=warnings,
    )


def install_packages(
    project_dir: Path,
    config: ProjectConfig,
    *,
    upgrade: bool = False,
) -> list[InstallResult]:
    """Install every package declared in ``config`` and write the lock file.

    Without ``upgrade`` a git package already recorded in the lock is fetched
    at the locked commit, so two checkouts of the same repository build the
    same SQL. With ``upgrade`` the declared rev is resolved afresh and the
    lock is rewritten.

    A package that fails to install is reported as an error result rather than
    raised, so one bad source does not hide what the others did. The lock
    keeps the previous entry for a failed package.

    A package that the lock still names but ``packages:`` no longer declares
    is removed, checkout and lock entry both, the same as
    ``havn packages remove`` would. Leaving it installed means the DAG keeps
    building models the project has stopped asking for.
    """
    project_dir = Path(project_dir)
    locked = read_lock(project_dir)
    results: list[InstallResult] = []
    new_lock: list[LockEntry] = []

    packages = list(getattr(config, "packages", []) or [])
    if not packages and not lock_path(project_dir).is_file():
        return results

    declared = {pkg.name for pkg in packages}
    for name in sorted(set(locked) - declared):
        dest = packages_dir(project_dir) / name
        existed = dest.is_dir()
        _discard_partial(dest)
        results.append(
            InstallResult(
                name=name,
                source=locked[name].source,
                path=dest,
                ref=locked[name].rev or locked[name].path or "",
                status="removed",
                message=(
                    "no longer declared in project.yml; checkout removed"
                    if existed
                    else "no longer declared in project.yml; lock entry removed"
                ),
            )
        )

    for pkg in packages:
        try:
            result = _install_one(project_dir, pkg, locked.get(pkg.name), upgrade=upgrade)
        except (PackageError, shutil.Error, OSError) as exc:
            # shutil.Error and OSError reach here from a real filesystem: a
            # dangling symlink, an unreadable file, a full disk. They used to
            # escape as a traceback, so the lock was never written and the
            # other packages' results were lost with it. The partial checkout
            # itself is cleaned up by _install_one, which knows what it
            # touched.
            results.append(
                InstallResult(
                    name=pkg.name,
                    source="git" if pkg.git else "path",
                    path=packages_dir(project_dir) / pkg.name,
                    ref=pkg.rev or pkg.path or "",
                    status="error",
                    message=str(exc),
                )
            )
            # Keep whatever the lock already said about this package: a failed
            # fetch must not silently unpin a working install.
            if pkg.name in locked:
                new_lock.append(locked[pkg.name])
            continue

        manifest = read_manifest(result.path)
        if manifest.name and manifest.name != pkg.name:
            result.warnings.append(
                f"package manifest calls itself '{manifest.name}', installed as '{pkg.name}'"
            )
        requires = check_requires_havn(manifest.requires_havn)
        if requires:
            result.warnings.append(f"package '{pkg.name}' {requires}")

        results.append(result)
        if result.source == "git":
            new_lock.append(
                LockEntry(
                    name=pkg.name,
                    source="git",
                    git=pkg.git or "",
                    rev=pkg.rev or "",
                    commit=result.commit,
                )
            )
        else:
            new_lock.append(
                LockEntry(name=pkg.name, source="path", path=str(pkg.path or ""))
            )

    write_lock(project_dir, new_lock)
    return results


def remove_package(project_dir: Path, name: str) -> bool:
    """Delete an installed package and drop it from the lock.

    Returns False when the package was neither on disk nor in the lock. The
    ``packages:`` entry in ``project.yml`` is left alone: editing the user's
    config behind their back would make the next install put it straight back
    without explaining why.
    """
    project_dir = Path(project_dir)
    from havn.engine.utils import validate_identifier

    validate_identifier(name, "package name")

    dest = packages_dir(project_dir) / name
    locked = read_lock(project_dir)
    found = dest.is_dir() or name in locked

    if dest.is_dir():
        shutil.rmtree(dest)
    if name in locked:
        del locked[name]
        write_lock(project_dir, list(locked.values()))
    return found


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def package_roots(project_dir: Path) -> list[PackageRoot]:
    """Every installed package, from the lock file, sorted by name.

    Returns an empty list for a project with no lock file, which is the common
    case and costs one ``stat``.
    """
    project_dir = Path(project_dir)
    entries = read_lock(project_dir)
    if not entries:
        return []
    roots: list[PackageRoot] = []
    for name in sorted(entries):
        path = packages_dir(project_dir) / name
        if not path.is_dir():
            logger.warning(
                "Package '%s' is in %s but not installed; run 'havn packages install'",
                name,
                LOCK_FILENAME,
            )
            continue
        roots.append(
            PackageRoot(
                name=name,
                path=path,
                transform_dir=path / "transform",
                macros_dir=path / "macros",
                manifest=read_manifest(path),
            )
        )
    return roots
