"""Project management commands: init, validate, status, context, backup, restore, checkpoint."""

from __future__ import annotations

import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import urlparse

import typer

from havn.cli import _load_config, _resolve_project, _warehouse_exists, app, console


def _resolve_template_url(ref: str) -> str:
    """Resolve a template reference to a downloadable archive URL.

    Accepted forms:
      * A direct URL to a .zip or .tar.gz archive.
      * GitHub shorthand: owner/repo -> main branch tarball.
      * GitHub shorthand with branch: owner/repo@branch.
    """
    parsed = urlparse(ref)
    if parsed.scheme in ("http", "https", "file"):
        return ref
    # GitHub shorthand: owner/repo or owner/repo@branch
    match = re.fullmatch(r"([\w.-]+)/([\w.-]+)(?:@([\w./-]+))?", ref)
    if match:
        owner, repo, branch = match.group(1), match.group(2), match.group(3) or "main"
        return f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.tar.gz"
    raise typer.BadParameter(
        f"Could not interpret --from value {ref!r}. "
        "Expected an http(s) URL to .zip/.tar.gz, or GitHub shorthand like owner/repo[@branch]."
    )


def _download(url: str, dest: Path) -> None:
    """Download url to dest. Raises typer.Exit on network errors."""
    console.print(f"[dim]fetching {url}[/dim]")
    req = urllib.request.Request(url, headers={"User-Agent": "havn-init"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as out:
            shutil.copyfileobj(resp, out)
    except urllib.error.HTTPError as e:
        console.print(f"[red]download failed: HTTP {e.code} {e.reason}[/red]")
        raise typer.Exit(code=1) from e
    except Exception as e:
        console.print(f"[red]download failed: {e}[/red]")
        raise typer.Exit(code=1) from e


def _extract_archive(archive_path: Path, staging: Path) -> Path:
    """Extract archive into staging and return the project root inside.

    If the archive has a single top-level directory (as GitHub archives do),
    return that directory. Otherwise return staging itself.
    """
    name = archive_path.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            # Basic path-traversal defence.
            for member in zf.namelist():
                if member.startswith("/") or ".." in Path(member).parts:
                    raise typer.Exit(code=1)
            zf.extractall(staging)
    elif name.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(archive_path) as tf:
            for member in tf.getmembers():
                mpath = Path(member.name)
                if member.name.startswith("/") or ".." in mpath.parts:
                    raise typer.Exit(code=1)
            tf.extractall(staging)  # noqa: S202 - members validated above
    else:
        console.print(f"[red]Unsupported archive type: {archive_path.name}[/red]")
        console.print("[red]Expected .zip, .tar, or .tar.gz[/red]")
        raise typer.Exit(code=1)

    entries = [p for p in staging.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return staging


def _init_from_remote(name: str, directory: Optional[Path], url: str) -> None:
    """Create a project by extracting a remote archive into the target directory."""
    archive_url = _resolve_template_url(url)
    target = directory or Path.cwd() / name

    if target.exists() and any(target.iterdir()):
        console.print(f"[red]Target {target} is not empty. Refusing to overwrite.[/red]")
        raise typer.Exit(code=1)
    target.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Decide suffix from URL, default to .tar.gz for GitHub shorthand.
        suffix = ".tar.gz"
        parsed = urlparse(archive_url)
        low = parsed.path.lower()
        if low.endswith(".zip"):
            suffix = ".zip"
        elif low.endswith((".tar.gz", ".tgz")):
            suffix = ".tar.gz"
        elif low.endswith(".tar"):
            suffix = ".tar"
        archive_path = tmp_path / f"template{suffix}"
        _download(archive_url, archive_path)

        staging = tmp_path / "staging"
        staging.mkdir()
        project_root = _extract_archive(archive_path, staging)

        for item in project_root.iterdir():
            dest = target / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

    console.print(f"[green]Project '{name}' created at {target} from {url}[/green]")
    readme = target / "README.md"
    if readme.exists():
        console.print()
        console.print(f"[dim]See {readme} for next steps.[/dim]")


@app.command()
def init(
    name: Annotated[str, typer.Argument(help="Project name")] = "my-project",
    directory: Annotated[Optional[Path], typer.Option("--dir", "-d", help="Target directory")] = None,
    empty: Annotated[bool, typer.Option("--empty", help="Create empty project without sample data")] = False,
    backend: Annotated[str, typer.Option("--backend", help="Warehouse backend: duckdb (default) or ducklake")] = "duckdb",
    from_url: Annotated[Optional[str], typer.Option("--from", help="Fetch a remote template (.zip or .tar.gz URL, or owner/repo for GitHub)")] = None,
) -> None:
    """Scaffold a new data platform project.

    By default, creates a local project from built-in templates. Pass --from to
    bootstrap from a remote archive instead — useful for case studies, course
    starters, or team-shared templates.

        havn init fjordbank --from https://example.com/template.tar.gz
        havn init my-proj   --from user/repo                  # GitHub shorthand
    """
    if from_url is not None:
        _init_from_remote(name=name, directory=directory, url=from_url)
        return

    from havn.templates import (
        CLAUDE_MD_TEMPLATE,
        COPILOT_INSTRUCTIONS_TEMPLATE,
        CURSORRULES_TEMPLATE,
        PROJECT_YML_DUCKLAKE_TEMPLATE,
        PROJECT_YML_EMPTY_TEMPLATE,
        PROJECT_YML_TEMPLATE,
        SQLFLUFF_TEMPLATE,
        SAMPLE_BRONZE_SQL,
        SAMPLE_CONTRACTS_YML,
        SAMPLE_EXPLORE_NOTEBOOK,
        SAMPLE_EXPORT_SCRIPT,
        SAMPLE_FULL_REFRESH_JOB,
        SAMPLE_GOLD_REGIONS_SQL,
        SAMPLE_GOLD_SUMMARY_SQL,
        SAMPLE_GOLD_TOP_SQL,
        SAMPLE_INCREMENTAL_JOB,
        SAMPLE_INGEST_NOTEBOOK,
        SAMPLE_MACRO_GEO,
        SAMPLE_SEED_CSV,
        SAMPLE_SILVER_DAILY_SQL,
        SAMPLE_SILVER_EVENTS_SQL,
    )

    if backend not in ("duckdb", "ducklake"):
        console.print(f"[red]Invalid backend: {backend!r}. Must be 'duckdb' or 'ducklake'.[/red]")
        raise typer.Exit(code=1)
    from havn.engine.secrets import ENV_TEMPLATE

    target = directory or Path.cwd() / name
    target.mkdir(parents=True, exist_ok=True)

    dirs = [
        "ingest", "transform/bronze", "transform/silver", "transform/gold",
        "export", "seeds", "contracts", "notebooks", "macros", "orchestration",
    ]
    for d in dirs:
        (target / d).mkdir(parents=True, exist_ok=True)

    # Write project.yml based on backend + empty flags
    if backend == "ducklake":
        (target / "project.yml").write_text(
            PROJECT_YML_DUCKLAKE_TEMPLATE.format(
                name=name, sample="false" if empty else "true"
            )
        )
        (target / ".havn").mkdir(exist_ok=True)
        (target / ".havn" / "data").mkdir(exist_ok=True)
    elif empty:
        (target / "project.yml").write_text(PROJECT_YML_EMPTY_TEMPLATE.format(name=name))
    else:
        (target / "project.yml").write_text(PROJECT_YML_TEMPLATE.format(name=name))

    # Sample data scaffolding (skipped for --empty, regardless of backend)
    if not empty:
        (target / "ingest" / "earthquakes.dpnb").write_text(SAMPLE_INGEST_NOTEBOOK)
        (target / "transform" / "bronze" / "earthquakes.sql").write_text(SAMPLE_BRONZE_SQL)
        (target / "transform" / "silver" / "earthquake_events.sql").write_text(SAMPLE_SILVER_EVENTS_SQL)
        (target / "transform" / "silver" / "earthquake_daily.sql").write_text(SAMPLE_SILVER_DAILY_SQL)
        (target / "transform" / "gold" / "earthquake_summary.sql").write_text(SAMPLE_GOLD_SUMMARY_SQL)
        (target / "transform" / "gold" / "top_earthquakes.sql").write_text(SAMPLE_GOLD_TOP_SQL)
        (target / "transform" / "gold" / "region_risk.sql").write_text(SAMPLE_GOLD_REGIONS_SQL)
        (target / "export" / "earthquake_report.py").write_text(SAMPLE_EXPORT_SCRIPT)
        (target / "macros" / "geo.py").write_text(SAMPLE_MACRO_GEO, encoding="utf-8")
        (target / "seeds" / "magnitude_scale.csv").write_text(SAMPLE_SEED_CSV)
        (target / "contracts" / "quality.yml").write_text(SAMPLE_CONTRACTS_YML)
        (target / "notebooks" / "explore.dpnb").write_text(SAMPLE_EXPLORE_NOTEBOOK)
        # Starter orchestration jobs
        (target / "orchestration" / "full-refresh.yml").write_text(SAMPLE_FULL_REFRESH_JOB)
        (target / "orchestration" / "incremental.yml").write_text(SAMPLE_INCREMENTAL_JOB)

    # Config files (both empty and sample projects)
    (target / ".env").write_text(ENV_TEMPLATE)
    (target / ".gitignore").write_text(
        "warehouse.duckdb\nwarehouse.duckdb.wal\n"
        ".havn/catalog.ducklake\n.havn/catalog.ducklake.wal\n.havn/data/\n"
        "__pycache__/\n*.pyc\n.venv/\n.env\noutput/\n_snapshots/\n"
        ".havn/pr-build/\n"
    )
    # .havn/ holds shareable PR state. .havn/prs/ travels with the repo (commit
    # the JSON files there to share PRs with collaborators); .havn/pr-build/ is
    # a transient worktree directory and is gitignored above.
    havn_dir = target / ".havn"
    havn_dir.mkdir(parents=True, exist_ok=True)
    (havn_dir / "README.md").write_text(
        "# .havn/\n\n"
        "This directory holds havn state that is shared via git.\n\n"
        "- `prs/` — Pull request definitions (JSON). Commit and push these to\n"
        "  share PRs with your team. Each developer runs their own havn\n"
        "  locally; PR state travels with the repository.\n"
        "- `pr-build/` — Transient git worktrees used by `havn pr build`.\n"
        "  Automatically cleaned up after each build. Gitignored.\n"
    )
    (havn_dir / "prs").mkdir(exist_ok=True)
    (havn_dir / "prs" / ".gitkeep").write_text("")
    (target / "CLAUDE.md").write_text(CLAUDE_MD_TEMPLATE.format(name=name))
    (target / ".cursorrules").write_text(CURSORRULES_TEMPLATE)
    # Seed a relaxed sqlfluff config so `havn lint` doesn't bury new
    # projects under RF03/AM05 violations on idiomatic SQL. The linter
    # auto-detects this file at lint time.
    (target / ".sqlfluff").write_text(SQLFLUFF_TEMPLATE)
    (target / ".github").mkdir(parents=True, exist_ok=True)
    (target / ".github" / "copilot-instructions.md").write_text(COPILOT_INSTRUCTIONS_TEMPLATE)

    console.print(f"[green]Project '{name}' created at {target}[/green]")
    console.print()
    console.print("Structure:")
    for d in dirs:
        console.print(f"  {d}/")
    console.print()
    if empty:
        console.print("Quick start:")
        console.print(f"  cd {name}")
        console.print("  havn connect              # connect a data source")
        console.print("  havn serve                # open web UI")
        console.print()
        console.print("[dim]AI assistant ready — CLAUDE.md included for Claude Code, Cursor, and others.[/dim]")
    else:
        console.print("Quick start:")
        console.print(f"  cd {name}")
        console.print("  havn seed                   # load the magnitude_scale reference CSV")
        console.print("  havn jobs run full-refresh  # rebuild every model + export")
        console.print("  havn jobs run incremental   # quick re-run (only changed models)")
        console.print("  havn tables                 # see what was built")
        console.print("  havn macros                 # see Python functions usable in SQL")
        console.print("  havn serve                  # open web UI")
        console.print("  havn contracts              # check data quality")


@app.command()
def validate(
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Validate project structure, config, and SQL model dependencies."""
    from havn.config import load_project
    from havn.engine.transform import build_dag, discover_models

    project_dir = _resolve_project(project_dir)
    errors: list[str] = []
    warnings: list[str] = []

    # 1. Validate project.yml
    try:
        config = load_project(project_dir)
        console.print("[green]project.yml[/green] parsed successfully")
    except Exception as e:
        console.print(f"[red]project.yml[/red] failed to parse: {e}")
        raise typer.Exit(1)

    # 2. Check required directories exist
    for d in ("transform",):
        if not (project_dir / d).exists():
            warnings.append(f"Directory '{d}/' not found")

    # 3. Validate streams reference valid actions
    for name, stream in config.streams.items():
        for step in stream.steps:
            if step.action not in ("ingest", "transform", "export", "seed"):
                errors.append(f"Stream '{name}': unknown action '{step.action}'")

    # 4. Discover and validate SQL models
    transform_dir = project_dir / "transform"
    models = discover_models(transform_dir)
    model_names = {m.full_name for m in models}

    # Check for duplicate model names
    seen: dict[str, str] = {}
    for m in models:
        if m.full_name in seen:
            errors.append(f"Duplicate model: {m.full_name} (in {m.path} and {seen[m.full_name]})")
        seen[m.full_name] = str(m.path)

    # Check depends_on references
    for m in models:
        for dep in m.depends_on:
            # External deps (landing.*) are fine — only flag deps that look like
            # they should be models but aren't
            if dep in model_names:
                continue
            schema = dep.split(".")[0] if "." in dep else ""
            if schema in ("bronze", "silver", "gold"):
                warnings.append(f"Model {m.full_name}: depends on '{dep}' which is not a known model")

    # 5. Check for circular dependencies
    try:
        build_dag(models)
        console.print(f"[green]DAG[/green] {len(models)} models, no circular dependencies")
    except Exception as e:
        errors.append(f"Circular dependency detected: {e}")

    # 6. Check .env variables referenced in config
    import re
    config_lines = (project_dir / "project.yml").read_text().splitlines() if (project_dir / "project.yml").exists() else []
    # Only check non-comment lines for env var references
    active_text = "\n".join(line for line in config_lines if not line.strip().startswith("#"))
    env_refs = set(re.findall(r"\$\{(\w+)\}", active_text))
    if env_refs:
        import os
        missing = [v for v in env_refs if not os.environ.get(v)]
        if missing:
            for v in missing:
                warnings.append(f"Environment variable ${{{v}}} referenced in project.yml but not set")

    # Report
    if warnings:
        console.print()
        for w in warnings:
            console.print(f"  [yellow]warn[/yellow]  {w}")
    if errors:
        console.print()
        for e in errors:
            console.print(f"  [red]error[/red] {e}")
        console.print()
        console.print(f"[red]Validation failed: {len(errors)} error(s), {len(warnings)} warning(s)[/red]")
        raise typer.Exit(1)
    else:
        console.print()
        console.print(f"[green]Validation passed ({len(warnings)} warning(s))[/green]")


@app.command()
def status(
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Show project health: git info, warehouse stats, last run."""
    from havn.config import load_project
    from havn.engine.backends import create_backend
    from havn.engine.database import open_warehouse

    project_dir = _resolve_project(project_dir)
    config = load_project(project_dir)

    console.print(f"[bold]havn project:[/bold] {config.name}")

    # Git info
    try:
        from havn.engine.git import current_branch, is_dirty, is_git_repo, changed_files

        if is_git_repo(project_dir):
            branch = current_branch(project_dir) or "unknown"
            console.print(f"[bold]git branch:[/bold] {branch}")
            dirty = is_dirty(project_dir)
            if dirty:
                files = changed_files(project_dir)
                console.print(f"[bold]git status:[/bold] {len(files)} files modified (uncommitted)")
                for f in files[:10]:
                    console.print(f"  [yellow]modified:[/yellow] {f}")
                if len(files) > 10:
                    console.print(f"  [dim]... and {len(files) - 10} more[/dim]")
            else:
                console.print("[bold]git status:[/bold] [green]clean[/green]")
        else:
            console.print("[dim]git: not a git repository[/dim]")
    except Exception:
        pass

    # Backend info
    backend = create_backend(config.database, project_dir=project_dir)
    st = backend.status()
    if st["backend"] == "duckdb":
        size_mb = st.get("size_bytes", 0) / (1024 * 1024)
        suffix = f" ({size_mb:,.1f} MB)" if st.get("size_bytes") else ""
        console.print(f"[bold]backend:[/bold] duckdb — {st.get('path', '?')}{suffix}")
    else:
        enc = "on" if st.get("encrypted") else "off"
        reachable = "yes" if st.get("catalog_reachable") else "no"
        snap = st.get("snapshot_count", 0)
        console.print(
            f"[bold]backend:[/bold] ducklake — "
            f"catalog={st.get('catalog', '?')}, snapshots={snap}, "
            f"encryption={enc}, reachable={reachable}"
        )
        if not st.get("healthy") and st.get("error"):
            console.print(f"  [red]error:[/red] {st['error']}")

    # Warehouse stats
    if backend.exists():
        conn = open_warehouse(config, project_dir, read_only=True)
        try:
            rows = conn.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('information_schema', '_havn') "
                "AND table_schema NOT LIKE 'pg_%' "
                "AND table_schema NOT LIKE '__ducklake%' "
                "AND table_name NOT LIKE 'ducklake_%'"
            ).fetchall()
            total_tables = len(rows)
            total_rows = 0
            for schema, tname in rows:
                try:
                    count = conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{tname}"').fetchone()[0]
                    total_rows += count
                except Exception:
                    pass
            console.print(f"[bold]warehouse:[/bold] {total_tables} tables, {total_rows:,} rows")

            # Last run (skip if meta tables don't exist yet)
            try:
                last = conn.execute(
                    "SELECT run_type, target, status, started_at, duration_ms "
                    "FROM _havn.run_log ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
            except Exception:
                last = None
            if last:
                import datetime
                run_type, run_target, run_status, started, dur = last
                status_color = "green" if run_status == "success" else "red"
                ago = ""
                if started:
                    try:
                        delta = datetime.datetime.now() - started
                        if delta.days > 0:
                            ago = f"{delta.days}d ago"
                        elif delta.seconds > 3600:
                            ago = f"{delta.seconds // 3600}h ago"
                        elif delta.seconds > 60:
                            ago = f"{delta.seconds // 60}m ago"
                        else:
                            ago = "just now"
                    except Exception:
                        ago = str(started)[:19]
                console.print(
                    f"[bold]last run:[/bold]  {run_type} {run_target} "
                    f"([{status_color}]{run_status}[/{status_color}], {ago})"
                )
        finally:
            conn.close()
    else:
        console.print("[bold]warehouse:[/bold] [yellow]not created yet[/yellow]")


@app.command()
def checkpoint(
    message: Annotated[Optional[str], typer.Option("--message", "-m", help="Custom commit message")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Smart git commit: stages files, auto-generates commit message from changes."""
    import subprocess

    from havn.engine.git import current_branch, is_git_repo

    project_dir = _resolve_project(project_dir)

    if not is_git_repo(project_dir):
        console.print("[red]Not a git repository. Run 'git init' first.[/red]")
        raise typer.Exit(1)

    # Check for .env in staged files and warn
    try:
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if ".env" in (staged.stdout or ""):
            console.print("[yellow]Warning: .env is staged. Unstaging to prevent committing secrets.[/yellow]")
            subprocess.run(["git", "reset", "HEAD", ".env"], cwd=project_dir, capture_output=True)
    except Exception:
        pass

    # Stage everything except .env
    subprocess.run(["git", "add", "--all"], cwd=project_dir, capture_output=True)
    # Unstage .env if it got added
    subprocess.run(["git", "reset", "HEAD", ".env"], cwd=project_dir, capture_output=True, check=False)

    # Check if there's anything to commit
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    staged_files = [f for f in (result.stdout or "").strip().split("\n") if f]
    if not staged_files:
        console.print("[yellow]No changes to commit.[/yellow]")
        return

    # Auto-generate commit message if not provided
    if not message:
        message = _generate_commit_message(staged_files)

    # Commit
    commit_result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        console.print(f"[red]Commit failed: {commit_result.stderr}[/red]")
        raise typer.Exit(1)

    branch = current_branch(project_dir) or "unknown"
    console.print(f"[green]Committed {len(staged_files)} file(s) on branch {branch}[/green]")
    console.print(f"  [dim]{message}[/dim]")


def _generate_commit_message(staged_files: list[str]) -> str:
    """Generate a commit message from staged file paths."""
    parts = []
    models_changed = []
    scripts_changed = []
    config_changed = False

    for f in staged_files:
        if f.startswith("transform/") and f.endswith(".sql"):
            # Extract model name: transform/gold/region_risk.sql -> gold.region_risk
            rel = f[len("transform/"):]
            parts_path = rel.rsplit("/", 1)
            if len(parts_path) == 2:
                schema, name = parts_path
                models_changed.append(f"{schema}.{name.replace('.sql', '')}")
            else:
                models_changed.append(rel.replace(".sql", ""))
        elif f.startswith("ingest/") or f.startswith("export/"):
            scripts_changed.append(f)
        elif f == "project.yml":
            config_changed = True

    if models_changed:
        if len(models_changed) <= 3:
            parts.append("Update " + ", ".join(models_changed))
        else:
            parts.append(f"Update {len(models_changed)} models")
    if scripts_changed:
        if len(scripts_changed) <= 3:
            parts.append("update " + ", ".join(scripts_changed))
        else:
            parts.append(f"update {len(scripts_changed)} scripts")
    if config_changed:
        parts.append("modify pipeline config")

    if parts:
        return "; ".join(parts)
    return f"Update {len(staged_files)} file(s)"


@app.command()
def context(
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Generate a project summary to paste into any AI assistant (ChatGPT, Claude, etc.)."""
    from havn.config import load_project
    from havn.engine.database import open_warehouse
    from havn.engine.transform import discover_models

    project_dir = _resolve_project(project_dir)
    config = load_project(project_dir)

    lines: list[str] = []
    lines.append(f"# havn project: {config.name}")
    lines.append("")
    lines.append("This is a havn data platform project. havn uses DuckDB for analytics,")
    lines.append("plain SQL for transforms, and Python for ingest/export scripts.")
    lines.append("")

    # Project config summary
    lines.append("## Configuration (project.yml)")
    lines.append(f"- Database: {config.database.path}")
    if config.connections:
        lines.append(f"- Connections: {', '.join(config.connections.keys())}")
    if config.streams:
        for name, s in config.streams.items():
            desc = f" — {s.description}" if s.description else ""
            sched = f" (schedule: {s.schedule})" if s.schedule else ""
            lines.append(f"- Stream '{name}'{desc}{sched}")
    lines.append("")

    # SQL models
    transform_dir = project_dir / "transform"
    models = discover_models(transform_dir)
    if models:
        lines.append("## SQL Models")
        for m in models:
            deps = f" (depends on: {', '.join(m.depends_on)})" if m.depends_on else ""
            lines.append(f"- {m.full_name} [{m.materialized}]{deps}")
        lines.append("")

    # Ingest/export scripts
    for script_type in ("ingest", "export"):
        script_dir = project_dir / script_type
        if script_dir.exists():
            py_files = list(script_dir.glob("*.py"))
            nb_files = list(script_dir.glob("*.dpnb"))
            scripts = sorted(f.name for f in py_files + nb_files if not f.name.startswith("_"))
            if scripts:
                lines.append(f"## {script_type.title()} Scripts")
                for s in scripts:
                    lines.append(f"- {script_type}/{s}")
                lines.append("")

    # Warehouse tables
    if _warehouse_exists(config, project_dir):
        conn = open_warehouse(config, project_dir, read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_schema NOT IN ('information_schema', '_havn')
                ORDER BY table_schema, table_name
                """
            ).fetchall()
            if rows:
                lines.append("## Warehouse Tables")
                for schema, name, ttype in rows:
                    lines.append(f"- {schema}.{name} ({ttype.lower()})")
                lines.append("")

            # Recent history (skip if meta tables don't exist yet)
            try:
                history_rows = conn.execute(
                    """
                    SELECT run_type, target, status, started_at, error
                    FROM _havn.run_log
                    ORDER BY started_at DESC
                    LIMIT 10
                    """
                ).fetchall()
                if history_rows:
                    lines.append("## Recent Run History")
                    for rtype, target, rstatus, started, error in history_rows:
                        ts = str(started)[:19] if started else ""
                        err = f" — {error}" if error else ""
                        lines.append(f"- [{rstatus}] {rtype}: {target} ({ts}){err}")
                    lines.append("")
            except Exception:
                pass  # no run history yet
        finally:
            conn.close()

    lines.append("## Available Commands")
    lines.append("- havn transform — build SQL models in dependency order")
    lines.append("- havn transform --force — force rebuild all")
    lines.append("- havn run <script> — run an ingest or export script")
    lines.append("- havn jobs run <name> — run a job (full-refresh, incremental, etc.)")
    lines.append("- havn query \"<sql>\" — run ad-hoc SQL queries")
    lines.append("- havn tables — list warehouse tables")
    lines.append("- havn lint — lint SQL files")
    lines.append("- havn history — show run log")
    lines.append("- havn serve — start the web UI")
    lines.append("")
    lines.append("## How to Help Me")
    lines.append("I'm working on this havn data platform project. You can help me by:")
    lines.append("- Writing SQL transform files (put them in transform/bronze/, silver/, or gold/)")
    lines.append("- Writing Python ingest scripts (put them in ingest/, `db` connection is pre-injected)")
    lines.append("- Debugging failed pipeline runs")
    lines.append("- Writing queries to analyze data in the warehouse")
    lines.append("- Adding new data sources or exports")

    output = "\n".join(lines)
    console.print(output)
    console.print()
    console.print("[dim]---[/dim]")
    console.print("[dim]Copy the text above and paste it into any AI assistant.[/dim]")
    console.print("[dim]Then ask your question about this project.[/dim]")


@app.command()
def backup(
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Backup file path (default: _backups/)")] = None,
    no_verify: Annotated[bool, typer.Option("--no-verify", help="Skip integrity verification")] = False,
    note: Annotated[str, typer.Option("--note", "-n", help="Note to attach to this backup")] = "",
    keep: Annotated[Optional[int], typer.Option("--keep", help="Keep only the N most recent backups")] = None,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Create a verified backup of the warehouse database.

    Flushes the DuckDB WAL, copies the database file, verifies integrity,
    and records the backup in _backups/manifest.json with a SHA-256 checksum.
    """
    from havn.engine.backup import BackupError, cleanup_backups, create_backup

    project_dir = _resolve_project(project_dir)
    from havn.config import load_project

    config = load_project(project_dir)
    if config.database.backend != "duckdb":
        console.print(
            f"[red]havn backup only supports the 'duckdb' backend "
            f"(current: '{config.database.backend}'). "
            f"For DuckLake, use `havn migrate --to duckdb` to produce a portable file.[/red]"
        )
        raise typer.Exit(1)
    if not _warehouse_exists(config, project_dir):
        console.print("[red]No warehouse database found. Nothing to backup.[/red]")
        raise typer.Exit(1)

    db_path = project_dir / config.database.path
    try:
        entry = create_backup(
            project_dir, db_path,
            output=output,
            verify=not no_verify,
            note=note,
        )
    except BackupError as e:
        console.print(f"[red]Backup failed: {e}[/red]")
        raise typer.Exit(1)

    size_mb = entry["size_bytes"] / (1024 * 1024)
    verified = "[green]verified[/green]" if entry["verified"] else "[yellow]not verified[/yellow]"
    console.print(f"[green]Backup created:[/green] {entry['path']} ({size_mb:.1f} MB, {verified})")
    console.print(f"  SHA-256: {entry['sha256'][:16]}...")

    if keep is not None:
        removed = cleanup_backups(project_dir, keep=keep)
        if removed:
            console.print(f"  Cleaned up {len(removed)} old backup(s)")


@app.command()
def restore(
    backup_path: Annotated[Path, typer.Argument(help="Path to the backup file")],
    no_verify: Annotated[bool, typer.Option("--no-verify", help="Skip integrity verification before restore")] = False,
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Restore the warehouse database from a backup.

    Verifies the backup's integrity before restoring.  Removes stale WAL files.
    """
    from havn.engine.backup import BackupError, restore_backup

    project_dir = _resolve_project(project_dir)
    from havn.config import load_project

    config = load_project(project_dir)
    if config.database.backend != "duckdb":
        console.print(
            f"[red]havn restore only supports the 'duckdb' backend "
            f"(current: '{config.database.backend}').[/red]"
        )
        raise typer.Exit(1)

    db_path = project_dir / config.database.path
    if db_path.exists():
        console.print(f"[yellow]Overwriting existing database: {db_path}[/yellow]")

    try:
        result = restore_backup(
            project_dir, db_path, backup_path,
            verify=not no_verify,
        )
    except BackupError as e:
        console.print(f"[red]Restore failed: {e}[/red]")
        raise typer.Exit(1)

    size_mb = result["size_bytes"] / (1024 * 1024)
    console.print(f"[green]Database restored from {backup_path} ({size_mb:.1f} MB)[/green]")


@app.command(name="backup-list")
def backup_list(
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory")] = None,
) -> None:
    """List all tracked backups."""
    from havn.engine.backup import list_backups

    project_dir = _resolve_project(project_dir)
    entries = list_backups(project_dir)

    if not entries:
        console.print("[dim]No backups found.[/dim]")
        return

    from rich.table import Table

    table = Table(title="Backups")
    table.add_column("#", style="dim")
    table.add_column("Timestamp")
    table.add_column("File")
    table.add_column("Size")
    table.add_column("Verified")
    table.add_column("Exists")
    table.add_column("Note")

    for i, entry in enumerate(reversed(entries), 1):
        size_mb = entry.get("size_bytes", 0) / (1024 * 1024)
        verified = "[green]yes[/green]" if entry.get("verified") else "[yellow]no[/yellow]"
        exists = "[green]yes[/green]" if entry.get("exists") else "[red]missing[/red]"
        ts = entry.get("timestamp", "")[:19]
        table.add_row(
            str(i), ts, entry.get("filename", "?"),
            f"{size_mb:.1f} MB", verified, exists,
            entry.get("note", ""),
        )

    console.print(table)


@app.command(name="backup-verify")
def backup_verify(
    backup_path: Annotated[Path, typer.Argument(help="Path to the backup file to verify")],
) -> None:
    """Verify a backup file's integrity and show its contents."""
    from havn.engine.backup import verify_backup

    result = verify_backup(backup_path)

    if result.get("valid"):
        console.print(f"[green]Backup is valid:[/green] {backup_path}")
        size_mb = result.get("size_bytes", 0) / (1024 * 1024)
        console.print(f"  Size: {size_mb:.1f} MB")
        console.print(f"  SHA-256: {result['sha256'][:16]}...")
        console.print(f"  Schemas: {', '.join(result.get('schemas', []))}")
        console.print(f"  Tables: {result.get('table_count', 0)}")
    else:
        console.print(f"[red]Backup is INVALID:[/red] {backup_path}")
        if "error" in result:
            console.print(f"  Error: {result['error']}")
        raise typer.Exit(1)


@app.command()
def clear(
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory (default: current dir)")] = None,
) -> None:
    """Clear sample project files and start fresh with an empty project."""
    import shutil

    from havn.config import load_project

    project_dir = _resolve_project(project_dir)
    config = load_project(project_dir)

    if not config.sample:
        console.print("[red]This is not a sample project (no sample: true in project.yml).[/red]")
        raise typer.Exit(1)

    confirm = typer.confirm("This will delete all sample files and the warehouse database. Continue?")
    if not confirm:
        raise typer.Abort()

    # Delete warehouse database
    if config.database.backend == "duckdb":
        db_path = project_dir / config.database.path
        if db_path.exists():
            db_path.unlink()
            console.print(f"  Deleted {config.database.path}")
        wal_path = Path(str(db_path) + ".wal")
        if wal_path.exists():
            wal_path.unlink()
    else:
        # DuckLake: remove catalog and data_path (if local)
        catalog = config.database.catalog or ""
        if catalog and not catalog.startswith(("postgres:", "s3://")):
            cp = project_dir / catalog if not Path(catalog).is_absolute() else Path(catalog)
            if cp.exists():
                cp.unlink()
                console.print(f"  Deleted {catalog}")
            wp = Path(str(cp) + ".wal")
            if wp.exists():
                wp.unlink()
        data_path = config.database.data_path or ""
        if data_path and not data_path.startswith("s3://"):
            dp = project_dir / data_path if not Path(data_path).is_absolute() else Path(data_path)
            if dp.exists():
                shutil.rmtree(dp)
                console.print(f"  Deleted {data_path}")

    # Delete snapshot/rewind data
    for meta_dir in [".havn", "_snapshots", "output"]:
        meta_path = project_dir / meta_dir
        if meta_path.exists():
            shutil.rmtree(meta_path)

    # Clear content directories (keep the dirs, delete contents)
    for subdir in ["ingest", "transform", "export", "macros", "seeds", "contracts", "notebooks"]:
        dir_path = project_dir / subdir
        if dir_path.exists():
            shutil.rmtree(dir_path)
            dir_path.mkdir(parents=True, exist_ok=True)
            console.print(f"  Cleared {subdir}/")

    # Recreate transform subdirectories
    for layer in ["bronze", "silver", "gold"]:
        (project_dir / "transform" / layer).mkdir(parents=True, exist_ok=True)

    # Rewrite project.yml: remove sample flag and sample-specific config
    _rewrite_project_yml_clean(project_dir, config.name)

    console.print()
    console.print("[green]Sample project cleared. You're starting fresh.[/green]")
    console.print()
    console.print("Next steps:")
    console.print("  havn connect              # connect a data source")
    console.print("  havn serve                # open web UI")


def _rewrite_project_yml_clean(project_dir: Path, name: str) -> None:
    """Rewrite project.yml without sample flag and sample-specific content."""
    from havn.templates import PROJECT_YML_EMPTY_TEMPLATE

    (project_dir / "project.yml").write_text(PROJECT_YML_EMPTY_TEMPLATE.format(name=name))
    console.print("  Updated project.yml")
