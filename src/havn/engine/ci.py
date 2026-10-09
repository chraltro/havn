"""GitHub Actions CI integration.

Generates the workflows that put a data diff on every pull request, and posts
diff results as PR comments.

Two workflows, because a pull request needs something to diff against:

- ``havn-base.yml`` runs on every push to the main branch, builds the base
  warehouse and uploads it as the ``havn-base`` artifact.
- ``havn-ci.yml`` runs on every pull request. It downloads the newest base
  artifact, builds only what the PR changed into a branch warehouse that
  defers everything else to the base (``havn branch build``), renders the
  row-level diff (``havn branch diff --markdown``) and posts it as a comment
  that later pushes update in place (``havn ci comment``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from havn.textio import read_project_text

WORKFLOW_TEMPLATE = """\
name: havn CI
on:
  pull_request:
    branches: [__MAIN__]

permissions:
  contents: read
  actions: read
  pull-requests: write

jobs:
  diff:
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{ github.token }}
      GITHUB_TOKEN: ${{ github.token }}
      # The PR checkout is a detached merge commit; this names the branch
      # so havn resolves its branch warehouse.
      HAVN_BRANCH: ${{ github.head_ref }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install havn
        run: pip install havn

      - name: Download the base warehouse
        run: |
          RUN_ID=$(gh run list --workflow havn-base.yml --branch __MAIN__ --status success \\
            --limit 1 --json databaseId --jq '.[0].databaseId')
          if [ -z "$RUN_ID" ]; then
            echo "::error::No successful 'havn base' run on __MAIN__ yet. Run that workflow first."
            exit 1
          fi
          gh run download "$RUN_ID" --name havn-base --dir .havn/ci-base

      - name: Build what the pull request changed
        run: havn branch build --base .havn/ci-base/__BASE_FILE__

      - name: Data diff
        run: havn branch diff --base .havn/ci-base/__BASE_FILE__ --markdown --output havn-data-diff.md

      - name: Post the data diff
        if: always() && hashFiles('havn-data-diff.md') != ''
        run: havn ci comment --markdown havn-data-diff.md
"""

BASE_WORKFLOW_TEMPLATE = """\
name: havn base
on:
  push:
    branches: [__MAIN__]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  base:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install havn
        run: pip install havn

      # Pull requests are diffed against this warehouse. Build it the way
      # production is built (ingest + transform), or replace this step with
      # one that copies a recent backup of production to __BASE_PATH__.
      - name: Build the base warehouse
        run: __BUILD__

      - uses: actions/upload-artifact@v4
        with:
          name: havn-base
          path: __BASE_PATH__
          retention-days: 30
"""


def _workflow_settings(project_dir: Path) -> dict[str, str]:
    """The main branch, base warehouse and build command for this project."""
    from havn.config import load_project

    main, base_path, base_env = "main", "warehouse.duckdb", None
    try:
        cfg = load_project(project_dir, use_branches=False)
        base_env = cfg.branches.base
        main = (cfg.branches.main or cfg.branch.main_branches or [main])[0]
        raw_db = (cfg._raw.get("database") or {}) if cfg._raw else {}
        base_path = raw_db.get("path") or base_path
        if base_env and base_env in cfg.environments:
            base_path = (cfg.environments[base_env].database or {}).get("path") or base_path
    except Exception:
        pass
    env_flag = f" --env {base_env}" if base_env else ""
    if (project_dir / "orchestration" / "full-refresh.yml").exists():
        build = f"havn jobs run full-refresh{env_flag}"
    else:
        build = f"havn transform{env_flag}"
    return {
        "__MAIN__": main or "main",
        "__BASE_PATH__": Path(base_path).as_posix(),
        "__BASE_FILE__": Path(base_path).name,
        "__BUILD__": build,
    }


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


def generate_workflow(project_dir: Path) -> dict:
    """Write .github/workflows/havn-ci.yml and havn-base.yml in the project root."""
    workflows_dir = project_dir / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    values = _workflow_settings(project_dir)

    workflow_path = workflows_dir / "havn-ci.yml"
    workflow_path.write_text(_render(WORKFLOW_TEMPLATE, values), encoding="utf-8")
    base_path = workflows_dir / "havn-base.yml"
    base_path.write_text(_render(BASE_WORKFLOW_TEMPLATE, values), encoding="utf-8")

    return {
        "path": workflow_path.relative_to(project_dir).as_posix(),
        "base_path": base_path.relative_to(project_dir).as_posix(),
        "main": values["__MAIN__"],
        "base_warehouse": values["__BASE_PATH__"],
        "build": values["__BUILD__"],
    }


def _github_target(repo: str | None, pr: int | None) -> tuple[str | None, int | None]:
    """Fill repo and PR number in from the GitHub Actions environment."""
    if not repo:
        repo = os.environ.get("GITHUB_REPOSITORY")
    if not pr:
        # refs/pull/123/merge on a pull_request event
        ref = os.environ.get("GITHUB_REF", "")
        if "/pull/" in ref:
            try:
                pr = int(ref.split("/pull/")[1].split("/")[0])
            except (ValueError, IndexError):
                pass
    if not pr:
        # GITHUB_EVENT_PATH holds the event payload, PR number included.
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if event_path:
            try:
                event = json.loads(read_project_text(Path(event_path)))
                pr = int((event.get("pull_request") or {}).get("number") or 0) or None
            except (OSError, ValueError, TypeError):
                pass
    return repo, pr


def _github_request(method: str, url: str, token: str, payload: dict | None = None) -> Any:
    from urllib.request import Request, urlopen

    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.github+json",
    })
    with urlopen(req, timeout=30) as resp:
        body = resp.read()
    return json.loads(body) if body else None


def post_markdown_comment(
    markdown_path: str,
    repo: str | None = None,
    pr: int | None = None,
    *,
    marker: str | None = None,
    request: Callable[..., Any] | None = None,
) -> dict:
    """Post a markdown file as a PR comment, or update havn's earlier one.

    The comment havn posted before is found by ``marker`` (by default the
    first line of the file when it is an HTML comment, which ``havn branch
    diff --markdown`` always writes) and edited in place, so a PR that is
    pushed ten times has one data-diff comment rather than ten.

    ``request(method, url, token, payload)`` performs the HTTP call; the
    default talks to the GitHub API. Requires GITHUB_TOKEN.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return {"error": "GITHUB_TOKEN environment variable not set"}
    try:
        body = read_project_text(Path(markdown_path))
    except FileNotFoundError:
        return {"error": f"Markdown file not found: {markdown_path}"}
    repo, pr = _github_target(repo, pr)
    if not repo:
        return {"error": "Could not determine GitHub repository. Use --repo flag."}
    if not pr:
        return {"error": "Could not determine PR number. Use --pr flag."}
    if marker is None:
        first = body.lstrip().splitlines()[0] if body.strip() else ""
        marker = first if first.startswith("<!--") else None

    call = request or _github_request
    api = f"https://api.github.com/repos/{repo}"
    try:
        existing = None
        if marker:
            page = 1
            while existing is None and page <= 10:
                comments = call("GET", f"{api}/issues/{pr}/comments?per_page=100&page={page}", token) or []
                existing = next((c for c in comments if marker in (c.get("body") or "")), None)
                if len(comments) < 100:
                    break
                page += 1
        if existing is not None:
            call("PATCH", f"{api}/issues/comments/{existing['id']}", token, {"body": body})
            return {"pr": pr, "repo": repo, "updated": True, "comment_id": existing["id"]}
        created = call("POST", f"{api}/issues/{pr}/comments", token, {"body": body}) or {}
        return {"pr": pr, "repo": repo, "updated": False, "comment_id": created.get("id")}
    except Exception as e:
        return {"error": f"Failed to post comment: {e}"}


def _format_diff_comment(diff_data: list[dict] | dict) -> str:
    """Format diff results as a markdown PR comment."""
    lines = ["## havn data diff", ""]

    # Handle both list of DiffResults and snapshot diff format
    if isinstance(diff_data, dict):
        # Snapshot diff format
        table_changes = diff_data.get("table_changes", [])
        if table_changes:
            lines.append("| Table | Status | Snapshot Rows | Current Rows |")
            lines.append("|-------|--------|--------------|-------------|")
            for tc in table_changes:
                lines.append(
                    f"| {tc['table']} | {tc['status']} "
                    f"| {tc.get('snapshot_rows', '-')} "
                    f"| {tc.get('current_rows', '-')} |"
                )
        else:
            lines.append("No data changes detected.")

        file_changes = diff_data.get("file_changes", {})
        if any(file_changes.get(k) for k in ("added", "removed", "modified")):
            lines.extend(["", "### File changes"])
            for f in file_changes.get("added", []):
                lines.append(f"- :heavy_plus_sign: {f}")
            for f in file_changes.get("removed", []):
                lines.append(f"- :heavy_minus_sign: {f}")
            for f in file_changes.get("modified", []):
                lines.append(f"- :pencil2: {f}")
    elif isinstance(diff_data, list):
        # List of DiffResult dicts
        has_changes = any(
            r.get("added", 0) or r.get("removed", 0) or r.get("modified", 0)
            or r.get("schema_changes") or r.get("is_new") or r.get("error")
            for r in diff_data
        )
        if not has_changes:
            lines.append("No data changes detected.")
        else:
            lines.append("| Model | Before | After | Added | Removed | Modified | Schema |")
            lines.append("|-------|--------|-------|-------|---------|----------|--------|")
            for r in diff_data:
                if r.get("error"):
                    lines.append(f"| {r['model']} | | | | | | ERROR |")
                    continue
                before = "NEW" if r.get("is_new") else str(r.get("total_before", 0))
                after = str(r.get("total_after", 0))
                added = f"+{r.get('added', 0)}" if r.get("added") else "0"
                removed = str(r.get("removed", 0))
                modified = str(r.get("modified", 0))
                sc = r.get("schema_changes", [])
                schema_label = f"{len(sc)} change(s)" if sc else "\u2014"
                lines.append(
                    f"| {r['model']} | {before} | {after} "
                    f"| {added} | {removed} | {modified} | {schema_label} |"
                )

            # Sample rows in details
            for r in diff_data:
                if r.get("sample_added") or r.get("sample_removed") or r.get("sample_modified"):
                    lines.extend(["", f"<details><summary>Sample changed rows for {r['model']}</summary>", ""])
                    if r.get("sample_added"):
                        lines.append(f"**Added ({r.get('added', 0)} rows):**")
                        lines.append("")
                        lines.append(_dict_list_to_md_table(r["sample_added"]))
                    if r.get("sample_removed"):
                        lines.append(f"**Removed ({r.get('removed', 0)} rows):**")
                        lines.append("")
                        lines.append(_dict_list_to_md_table(r["sample_removed"]))
                    if r.get("sample_modified"):
                        lines.append(f"**Modified ({r.get('modified', 0)} rows):**")
                        lines.append("")
                        lines.append(_dict_list_to_md_table(r["sample_modified"]))
                    lines.append("</details>")

    return "\n".join(lines)


def _dict_list_to_md_table(rows: list[dict]) -> str:
    """Convert a list of dicts to a markdown table."""
    if not rows:
        return ""
    cols = list(rows[0].keys())
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for row in rows[:10]:  # Cap at 10 for readability
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in cols) + " |")
    if len(rows) > 10:
        lines.append(f"| ... {len(rows) - 10} more rows ... |")
    return "\n".join(lines)


def post_diff_comment(
    json_path: str,
    repo: str | None = None,
    pr: int | None = None,
) -> dict:
    """Post a formatted diff comment to a GitHub PR.

    Requires GITHUB_TOKEN env var. Uses repo and pr from env if not provided.
    """
    from urllib.request import Request, urlopen

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return {"error": "GITHUB_TOKEN environment variable not set"}

    # Read diff data
    try:
        with open(json_path, encoding="utf-8") as f:
            diff_data = json.load(f)
    except FileNotFoundError:
        return {"error": f"Diff results file not found: {json_path}"}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in {json_path}: {e}"}

    # Resolve repo and PR from GitHub Actions environment
    if not repo:
        repo = os.environ.get("GITHUB_REPOSITORY")
    if not pr:
        # Try to get from GITHUB_REF (refs/pull/123/merge)
        ref = os.environ.get("GITHUB_REF", "")
        if "/pull/" in ref:
            try:
                pr = int(ref.split("/pull/")[1].split("/")[0])
            except (ValueError, IndexError):
                pass

    if not repo:
        return {"error": "Could not determine GitHub repository. Use --repo flag."}
    if not pr:
        return {"error": "Could not determine PR number. Use --pr flag."}

    # Format comment
    comment_body = _format_diff_comment(diff_data)

    # Post comment via GitHub API
    url = f"https://api.github.com/repos/{repo}/issues/{pr}/comments"
    payload = json.dumps({"body": comment_body}).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.github.v3+json",
    }

    try:
        req = Request(url, data=payload, headers=headers)
        urlopen(req, timeout=30)
        return {"pr": pr, "repo": repo}
    except Exception as e:
        return {"error": f"Failed to post comment: {e}"}
