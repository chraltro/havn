"""Git detection utilities.

All functions shell out to the git CLI via subprocess. No Python git libraries.
Every function returns None/empty gracefully if not a git repo or git is not installed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Pattern for validating branch names (reject shell metacharacters and git-unsafe chars)
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")


def _validate_branch_name(name: str) -> bool:
    """Validate that a branch name is safe (no shell injection)."""
    if not name or len(name) > 250:
        return False
    if name.startswith("-") or ".." in name or name.endswith(".lock"):
        return False
    return bool(_SAFE_BRANCH_RE.match(name))


def _validate_file_path(path: str) -> bool:
    """Validate that a file path is safe (no shell injection)."""
    if not path or len(path) > 500:
        return False
    if path.startswith("-"):
        return False
    # Reject null bytes and other dangerous characters
    if "\x00" in path:
        return False
    return True


def _git_root(project_dir: Path) -> Path:
    """Resolve the git repository root for a project directory.

    When a havn project lives in a subdirectory of a git repo (e.g.
    ``repo/internal_stress_project/``), git commands must run from the repo root
    so that file paths from ``git status --porcelain`` resolve correctly.
    Falls back to *project_dir* if the root cannot be determined.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return project_dir


def _run_git(project_dir: Path, *args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a git command from the repository root."""
    root = _git_root(project_dir)
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args=["git", *args], returncode=1, stdout="", stderr="")


def is_git_repo(project_dir: Path) -> bool:
    """Check if the directory is inside a git repository."""
    result = _run_git(project_dir, "rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


def current_branch(project_dir: Path) -> str | None:
    """Get the current branch name."""
    if not is_git_repo(project_dir):
        return None
    result = _run_git(project_dir, "rev-parse", "--abbrev-ref", "HEAD")
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch if branch else None


def is_dirty(project_dir: Path) -> bool:
    """Check if there are uncommitted changes."""
    if not is_git_repo(project_dir):
        return False
    result = _run_git(project_dir, "status", "--porcelain")
    return bool(result.stdout.strip())


def changed_files(project_dir: Path, ref: str = "HEAD") -> list[str]:
    """Get files changed since ref (modified, added, deleted)."""
    if not is_git_repo(project_dir):
        return []
    # Combine staged + unstaged + untracked
    files: set[str] = set()

    # Modified/added/deleted vs ref
    result = _run_git(project_dir, "diff", "--name-only", ref)
    if result.returncode == 0:
        files.update(f for f in result.stdout.strip().split("\n") if f)

    # Staged changes
    result = _run_git(project_dir, "diff", "--cached", "--name-only")
    if result.returncode == 0:
        files.update(f for f in result.stdout.strip().split("\n") if f)

    # Untracked files
    result = _run_git(project_dir, "ls-files", "--others", "--exclude-standard")
    if result.returncode == 0:
        files.update(f for f in result.stdout.strip().split("\n") if f)

    return sorted(files)


def last_commit_hash(project_dir: Path) -> str | None:
    """Get the last commit hash."""
    if not is_git_repo(project_dir):
        return None
    result = _run_git(project_dir, "rev-parse", "HEAD")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def last_commit_message(project_dir: Path) -> str | None:
    """Get the last commit message."""
    if not is_git_repo(project_dir):
        return None
    result = _run_git(project_dir, "log", "-1", "--format=%s")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def diff_files_between(project_dir: Path, base_ref: str, head_ref: str) -> list[str]:
    """Get files changed between two refs."""
    if not is_git_repo(project_dir):
        return []
    result = _run_git(project_dir, "diff", "--name-only", f"{base_ref}...{head_ref}")
    if result.returncode != 0:
        # Fall back to two-dot diff
        result = _run_git(project_dir, "diff", "--name-only", base_ref, head_ref)
        if result.returncode != 0:
            return []
    return [f for f in result.stdout.strip().split("\n") if f]


# ---------------------------------------------------------------------------
# Extended git operations
# ---------------------------------------------------------------------------


def git_log(project_dir: Path, limit: int = 20) -> list[dict]:
    """Get commit log with hash, short_hash, message, author, date."""
    if not is_git_repo(project_dir):
        return []
    limit = max(1, min(limit, 500))
    sep = "---GIT_LOG_SEP---"
    fmt = f"%H{sep}%h{sep}%s{sep}%an{sep}%aI"
    result = _run_git(project_dir, "log", f"-{limit}", f"--format={fmt}")
    if result.returncode != 0:
        return []
    entries = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(sep)
        if len(parts) < 5:
            continue
        entries.append({
            "hash": parts[0],
            "short_hash": parts[1],
            "message": parts[2],
            "author": parts[3],
            "date": parts[4],
        })
    return entries


def git_diff(project_dir: Path, file: str | None = None, staged: bool = False) -> str:
    """Get diff text. Optionally for a specific file or staged changes."""
    if not is_git_repo(project_dir):
        return ""
    args = ["diff"]
    if staged:
        args.append("--cached")
    if file is not None:
        if not _validate_file_path(file):
            return ""
        args.append("--")
        args.append(file)
    result = _run_git(project_dir, *args)
    if result.returncode != 0:
        return ""
    return result.stdout


def git_diff_file(project_dir: Path, path: str) -> str:
    """Get diff for a specific file (both staged and unstaged)."""
    if not is_git_repo(project_dir):
        return ""
    if not _validate_file_path(path):
        return ""
    # Show combined diff (unstaged)
    result = _run_git(project_dir, "diff", "--", path)
    output = result.stdout if result.returncode == 0 else ""
    # Also include staged diff
    result_staged = _run_git(project_dir, "diff", "--cached", "--", path)
    if result_staged.returncode == 0 and result_staged.stdout:
        if output:
            output += "\n"
        output += result_staged.stdout
    return output


def git_stage(project_dir: Path, files: list[str]) -> bool:
    """Stage files (git add). Returns True on success."""
    if not is_git_repo(project_dir):
        return False
    if not files:
        return False
    for f in files:
        if not _validate_file_path(f):
            return False
    result = _run_git(project_dir, "add", "--", *files)
    return result.returncode == 0


def git_unstage(project_dir: Path, files: list[str]) -> bool:
    """Unstage files (git reset HEAD). Returns True on success."""
    if not is_git_repo(project_dir):
        return False
    if not files:
        return False
    for f in files:
        if not _validate_file_path(f):
            return False
    result = _run_git(project_dir, "reset", "HEAD", "--", *files)
    return result.returncode == 0


def git_commit(project_dir: Path, message: str) -> dict | None:
    """Create a commit. Returns {hash, message} or None on failure."""
    if not is_git_repo(project_dir):
        return None
    if not message or not message.strip():
        return None
    result = _run_git(project_dir, "commit", "-m", message)
    if result.returncode != 0:
        return None
    # Get the hash of the new commit
    hash_result = _run_git(project_dir, "rev-parse", "HEAD")
    if hash_result.returncode != 0:
        return None
    return {
        "hash": hash_result.stdout.strip(),
        "message": message,
    }


def git_pull(project_dir: Path, remote: str = "origin", branch: str | None = None) -> dict:
    """Pull from remote. Returns {success, output, error}."""
    if not is_git_repo(project_dir):
        return {"success": False, "output": "", "error": "Not a git repository"}
    if not _validate_branch_name(remote):
        return {"success": False, "output": "", "error": "Invalid remote name"}
    args = ["pull", remote]
    if branch is not None:
        if not _validate_branch_name(branch):
            return {"success": False, "output": "", "error": "Invalid branch name"}
        args.append(branch)
    result = _run_git(project_dir, *args, timeout=30)
    return {
        "success": result.returncode == 0,
        "output": result.stdout.strip(),
        "error": result.stderr.strip() if result.returncode != 0 else "",
    }


def git_push(project_dir: Path, remote: str = "origin", branch: str | None = None) -> dict:
    """Push to remote. Returns {success, output, error}."""
    if not is_git_repo(project_dir):
        return {"success": False, "output": "", "error": "Not a git repository"}
    if not _validate_branch_name(remote):
        return {"success": False, "output": "", "error": "Invalid remote name"}
    args = ["push", remote]
    if branch is not None:
        if not _validate_branch_name(branch):
            return {"success": False, "output": "", "error": "Invalid branch name"}
        args.append(branch)
    result = _run_git(project_dir, *args, timeout=30)
    return {
        "success": result.returncode == 0,
        "output": result.stdout.strip(),
        "error": result.stderr.strip() if result.returncode != 0 else "",
    }


def git_branches(project_dir: Path) -> list[dict]:
    """List branches. Returns list of {name, is_current, is_remote}."""
    if not is_git_repo(project_dir):
        return []
    result = _run_git(project_dir, "branch", "-a", "--no-color")
    if result.returncode != 0:
        return []
    branches = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        is_current = line.startswith("*")
        name = line.lstrip("* ").strip()
        # Skip HEAD pointers like "remotes/origin/HEAD -> origin/main"
        if " -> " in name:
            continue
        is_remote = name.startswith("remotes/")
        if is_remote:
            # Strip "remotes/" prefix for display
            name = name[len("remotes/"):]
        branches.append({
            "name": name,
            "is_current": is_current,
            "is_remote": is_remote,
        })
    return branches


def git_create_branch(project_dir: Path, name: str, checkout: bool = True) -> bool:
    """Create a new branch. Optionally check it out. Returns True on success."""
    if not is_git_repo(project_dir):
        return False
    if not _validate_branch_name(name):
        return False
    if checkout:
        result = _run_git(project_dir, "checkout", "-b", name)
    else:
        result = _run_git(project_dir, "branch", name)
    return result.returncode == 0


def git_checkout_branch(project_dir: Path, name: str) -> dict:
    """Checkout a branch. Returns {success, error}."""
    if not is_git_repo(project_dir):
        return {"success": False, "error": "Not a git repository"}
    if not _validate_branch_name(name):
        return {"success": False, "error": "Invalid branch name"}
    result = _run_git(project_dir, "checkout", name)
    return {
        "success": result.returncode == 0,
        "error": result.stderr.strip() if result.returncode != 0 else "",
    }


def git_delete_branch(project_dir: Path, name: str) -> dict:
    """Delete a branch. Returns {success, error}."""
    if not is_git_repo(project_dir):
        return {"success": False, "error": "Not a git repository"}
    if not _validate_branch_name(name):
        return {"success": False, "error": "Invalid branch name"}
    result = _run_git(project_dir, "branch", "-d", name)
    return {
        "success": result.returncode == 0,
        "error": result.stderr.strip() if result.returncode != 0 else "",
    }


def git_stash(project_dir: Path, message: str | None = None) -> dict:
    """Stash changes. Returns {success, output}."""
    if not is_git_repo(project_dir):
        return {"success": False, "output": "Not a git repository"}
    args = ["stash", "push"]
    if message:
        args.extend(["-m", message])
    result = _run_git(project_dir, *args)
    return {
        "success": result.returncode == 0,
        "output": result.stdout.strip(),
    }


def git_stash_pop(project_dir: Path) -> dict:
    """Pop the latest stash. Returns {success, output, error}."""
    if not is_git_repo(project_dir):
        return {"success": False, "output": "", "error": "Not a git repository"}
    result = _run_git(project_dir, "stash", "pop")
    return {
        "success": result.returncode == 0,
        "output": result.stdout.strip(),
        "error": result.stderr.strip() if result.returncode != 0 else "",
    }


def git_stash_list(project_dir: Path) -> list[dict]:
    """List stashes. Returns list of {index, message}."""
    if not is_git_repo(project_dir):
        return []
    result = _run_git(project_dir, "stash", "list")
    if result.returncode != 0:
        return []
    stashes = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        # Format: "stash@{0}: WIP on main: abc1234 message" or "stash@{0}: On main: message"
        match = re.match(r"stash@\{(\d+)\}:\s*(.*)", line)
        if match:
            stashes.append({
                "index": int(match.group(1)),
                "message": match.group(2),
            })
    return stashes


def git_discard_changes(project_dir: Path, files: list[str]) -> bool:
    """Discard working directory changes (git checkout -- files). Returns True on success."""
    if not is_git_repo(project_dir):
        return False
    if not files:
        return False
    for f in files:
        if not _validate_file_path(f):
            return False
    result = _run_git(project_dir, "checkout", "--", *files)
    return result.returncode == 0


def git_remote_url(project_dir: Path) -> str | None:
    """Get the origin remote URL."""
    if not is_git_repo(project_dir):
        return None
    result = _run_git(project_dir, "remote", "get-url", "origin")
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url if url else None


def git_init(project_dir: Path, initial_branch: str = "main") -> dict:
    """Initialize a new git repository in the project directory.

    Returns ``{success, error}``. Refuses to reinitialize if the directory
    already contains a git repository.
    """
    if is_git_repo(project_dir):
        return {"success": False, "error": "Already a git repository"}
    if not _validate_branch_name(initial_branch):
        return {"success": False, "error": "Invalid initial branch name"}
    # Use -b only on git >= 2.28; fall back to init + rename on older versions
    result = subprocess.run(
        ["git", "init", "-b", initial_branch],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        # Retry without -b for older git
        result = subprocess.run(
            ["git", "init"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {
                "success": False,
                "error": result.stderr.strip() or "git init failed",
            }
        # Rename default branch if the old init created "master"
        _run_git(project_dir, "symbolic-ref", "HEAD", f"refs/heads/{initial_branch}")
    return {"success": True, "output": result.stdout.strip()}


def git_status_detailed(project_dir: Path) -> list[dict]:
    """Get detailed file status with staged/unstaged distinction.

    Returns list of {path, status, staged} where status is one of:
    M (modified), A (added), D (deleted), R (renamed), C (copied), U (untracked).
    """
    if not is_git_repo(project_dir):
        return []
    result = _run_git(project_dir, "status", "--porcelain", "-u")
    if result.returncode != 0:
        return []
    files: list[dict] = []
    seen: set[str] = set()
    for line in result.stdout.split("\n"):
        if not line or len(line) < 4:
            continue
        index_status = line[0]
        work_status = line[1]
        path = line[3:]
        # Handle renames: "R  old -> new"
        if " -> " in path:
            path = path.split(" -> ")[-1]
        # Staged changes
        if index_status not in (" ", "?"):
            key = f"staged:{path}"
            if key not in seen:
                seen.add(key)
                files.append({"path": path, "status": index_status, "staged": True})
        # Unstaged changes
        if work_status not in (" ", "?") and index_status != "?":
            key = f"unstaged:{path}"
            if key not in seen:
                seen.add(key)
                files.append({"path": path, "status": work_status, "staged": False})
        # Untracked
        if index_status == "?" and work_status == "?":
            key = f"unstaged:{path}"
            if key not in seen:
                seen.add(key)
                files.append({"path": path, "status": "U", "staged": False})
    return files
