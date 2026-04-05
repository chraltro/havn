# Pull Requests

havn includes a local pull request system designed for data teams. PRs are JSON files stored in `.havn/prs/` and committed to git alongside your SQL models, so review history travels with the repository without needing a hosted service.

The key capability beyond standard git review is **build-in-worktree**: havn clones your warehouse into an isolated git worktree, runs the PR branch's transforms against real data, and produces a row-level data diff before anyone merges. Reviewers see what actually changes in the warehouse, not just which SQL lines changed.

## Concepts

- **PR JSON files** -- `.havn/prs/<id>.json` stores the PR state (title, branch refs, comments, approvals). Committed to git; shared by pushing the branch.
- **Build results** -- `_havn.pr_builds` in the warehouse stores the worktree diff output. Local to each developer's machine.
- **AI review** -- The review prompt is sent to the developer's own connected agent (Claude Code, Codex, or Gemini) via the agent sidebar. No server-side API keys are used.

## Creating a PR

### Via CLI

```bash
havn pr create \
  --branch feature/new-revenue-model \
  --title "Add revenue_summary gold model" \
  --description "Aggregates orders by month and region" \
  --base main
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--branch, -b` | required | The PR's source branch (head) |
| `--title, -t` | required | Short title |
| `--description, -d` | `""` | Longer description |
| `--base` | `"main"` | Branch to merge into |
| `--author, -a` | `"local"` | Author name |
| `--no-approval` | off | Skip the approval requirement |

After creation, commit and push `.havn/prs/<id>.json` to share the PR with teammates.

### Via Web UI

Go to **Develop -> Git -> Reviews** and click **New PR**. Fill in the title, description, and branch, then save.

## Building a PR

Building runs the PR branch's SQL models in an isolated environment and diffs every table against main.

```bash
havn pr build <pr-id>
```

What happens:

1. A git worktree is created at `.havn/pr-build/<pr-id>/` pinned to the PR branch's commit.
2. The main warehouse is cloned into the worktree so landing tables (raw data) are available.
3. `havn transform` runs inside the worktree using the PR branch's SQL.
4. Every table in the warehouse is compared: row counts, added rows, removed rows, schema changes.
5. Results are stored in `_havn.pr_builds` and the worktree is removed.

Sample output:

```
Building PR feature-revenue...
success -- 3420ms

Data diff:
  modified   gold.revenue_summary: 1234 -> 1289 (+55)
  unchanged  silver.orders
  added      gold.revenue_by_region: 0 -> 142
```

The worktree directory (`.havn/pr-build/`) is gitignored. It is always cleaned up after the build, even on failure.

## Reviewing a PR

### Listing and Showing

```bash
havn pr                         # List all PRs
havn pr --status open           # Filter by status (open / merged / closed)
havn pr show <pr-id>            # Full detail: status, build diff, comments
```

### Comments

```bash
havn pr comment <pr-id> "Looks good, one question about the join key"
```

### Approving

```bash
havn pr approve <pr-id> --reviewer alice
```

### Requesting Changes

```bash
havn pr request-changes <pr-id> --reviewer bob --reason "Missing index on customer_id"
```

### AI Review

```bash
# Print the review prompt (includes PR metadata and build diff)
havn pr review <pr-id>

# Pipe directly to your agent
havn pr review <pr-id> --ai | claude
```

In the web UI, click the **AI Review** button on any PR. The prompt is sent to the connected agent in the sidebar. The AI response is posted as a comment on the PR.

### File Diff

```bash
havn pr diff <pr-id>
```

Shows the list of files changed between the PR branch and its base branch.

## Merging a PR

```bash
havn pr merge <pr-id> --user alice
```

Preconditions checked before merge:

1. PR status is `open`.
2. If `require_approval` is true, at least one reviewer has approved.
3. No reviewer has an outstanding change request.
4. `git merge-tree` reports no conflicts (virtual merge, does not touch the working tree).
5. The working tree must be clean.

If all checks pass:

- A pre-merge version snapshot is created (see [Versioning](versioning)) for rollback.
- `git merge` runs and produces a merge commit.
- The PR status is set to `merged`.

### Can-Merge Check

havn uses `git merge-tree` to test mergeability without modifying any files. It uses the 2-arg form on git >= 2.38 and falls back to the 3-arg form on older versions. The working tree is never modified.

## Closing a PR

```bash
havn pr close <pr-id> --user alice
```

Marks the PR as `closed` without merging. The branch is not deleted.

## Web UI (Reviews Panel)

In **Develop -> Git**, the **Reviews** sub-tab shows all PRs with:

- Status badges (open, merged, closed)
- Approval and change-request state
- Latest build result (row counts, modified tables)
- Comment thread

Click a PR row to expand the comment thread, build diff, and action buttons (Approve, Request Changes, Build, AI Review, Merge, Close).

If the project is not yet a git repository, both the **Status** and **Reviews** sub-tabs show an **Initialize git** button.

## Storage Layout

```
.havn/
  prs/
    <pr-id>.json    -- PR state (commit this to share)
  pr-build/         -- Transient worktrees (gitignored)
```

Inside `warehouse.duckdb`:

```
_havn.pr_builds     -- Build results per PR (local only)
```

## Full CLI Reference

| Command | Description |
|---------|-------------|
| `havn pr` | List all PRs |
| `havn pr create` | Create a new PR |
| `havn pr show <id>` | Show PR detail and latest build |
| `havn pr comment <id> <body>` | Add a comment |
| `havn pr approve <id>` | Approve the PR |
| `havn pr request-changes <id>` | Request changes |
| `havn pr build <id>` | Build in worktree and diff |
| `havn pr review <id>` | Print AI review prompt |
| `havn pr merge <id>` | Merge into base branch |
| `havn pr close <id>` | Close without merging |
| `havn pr diff <id>` | Show changed files |

## Related Pages

- [Versioning](versioning) -- Pre-merge snapshots and restore
- [Lineage](lineage) -- Column-level impact analysis included in build output
- [Git](index) -- Git integration overview
