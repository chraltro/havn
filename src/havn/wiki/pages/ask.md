# Ask the warehouse, and verified agent changes

Two features that keep AI work inside what havn can check.

- **Ask** answers a business question from the semantic layer. The language
  model picks metrics, dimensions, a time grain, filters and a time range from
  `metrics/*.yml`. havn validates that choice, compiles it with the semantic
  layer and runs it read-only. The model never writes the SQL.
- **Verified agent changes**: when a coding agent edits model files, the edits
  become a *change set*. havn verifies it before you can apply it: validation,
  the bind pass, unit tests, contracts and a data diff of a scratch build.

## Ask

```bash
havn ask "revenue by region last quarter"
havn ask -c "now by month"            # refine the previous question
havn ask -c "only Norway"
havn ask "average order value" --save-suggestion
havn ask --eval tests/ask/*.yml       # measure accuracy on your catalog
```

In the web UI: **Data → Ask**. AI agents use the `ask` MCP tool.

### What an answer contains

| Part | What it is |
|---|---|
| Result | The rows, and a chart chosen from the shape of the spec: a number for a single value, a line over a time grain, bars over a dimension. |
| Spec | The structured query: `metrics`, `dimensions`, `grain`, `filters`, `start`, `end`, `order_by`, `limit`. |
| SQL | What the spec compiled to. A single metric compiles exactly as `havn metrics query` would; several metrics are joined on their shared dimensions with `FULL OUTER JOIN`. |
| Metrics | The definitions used: measure, model, always-applied filters, file. |
| Lineage | Each metric model's upstream chain down to its sources. |
| Freshness | The last build of every model in that chain. A model past `alerts.freshness_hours` (default 24) gets a stale warning, the same rule the Quality panel uses. |

The answer is marked **Verified** because it comes from defined metrics only.

### When no metric answers it

havn says so instead of guessing. It lists the closest metrics and, when one of
your models has the columns, suggests a metric definition as YAML. The
suggestion is checked before you see it: valid identifiers, an existing model,
existing columns, no name clash. **Add to metrics/** in the UI (or
`--save-suggestion` on the CLI) writes it to `metrics/<name>.yml`; it never
overwrites a file or an existing metric.

A spec the model gets wrong (an undeclared dimension, an unknown metric) is
sent back once with the errors. If the second attempt is still wrong, the
question is reported as unanswerable with those errors.

### Exploratory SQL (off by default)

With `ai.exploratory_sql: true`, an unanswerable question offers **Try
exploratory SQL**. The model then writes a SELECT from the table and column
list. The answer is labelled *unverified*, and the SQL still goes through the
governed read path: read-only validation, masking, the row cap and the
per-role timeout.

### Governance

Every query Ask runs, including the catalog lookups, goes through
`run_read_query` in `havn/engine/read_path.py`, the same function `POST
/api/query` uses. A user sees what their role sees: a masked column stays
masked in an answer. With `havn serve` running, the CLI and MCP send the
question to the server, so the server's users and policies apply.

## Configuration

```yaml
# project.yml
ai:
  provider: anthropic          # anthropic | openai (any OpenAI-compatible endpoint)
  model: claude-sonnet-5-5
  # api_key_env: ANTHROPIC_API_KEY   # the variable in .env holding the key
  # timeout: 60
  # max_rows: 1000             # rows returned per answer
  exploratory_sql: false
  share_dimension_values: false
  summarize_results: false
```

The key itself never goes in project.yml: put `ANTHROPIC_API_KEY=...` in
`.env`.

### The sidebar's agent: no API key

With `provider: agent`, Ask sends its prompt through the same agent CLI the
agent sidebar uses (Claude Code, Codex or Gemini CLI), which is already signed
in, so no key is needed:

```yaml
ai:
  provider: agent
  agent: claude        # claude | codex | gemini; defaults to the first one installed
  # model: sonnet      # passed to the CLI; its own default otherwise
```

The CLI runs headless, with no tools, in an empty temporary directory, so it
sees only the prompt: the same catalog metadata the API providers get. Its
vendor still receives that prompt. Without any `ai:` section, Ask uses the
Anthropic API when `ANTHROPIC_API_KEY` is set and this provider otherwise.

### A local model: nothing leaves the machine

Any server speaking the OpenAI `/chat/completions` API works: Ollama, LM
Studio, vLLM, llama.cpp.

```yaml
ai:
  provider: openai
  base_url: http://localhost:11434/v1     # Ollama
  model: qwen2.5:14b
```

A server on `localhost` needs no key. A model that follows instructions well
and handles JSON matters more than size; run `havn ask --eval` to see how a
given model does on your catalog before relying on it.

### What is sent to the model

| Sent | When |
|---|---|
| Metric definitions, dimension names and types, `@col` docs, model names, descriptions and column types, today's date | Always (metadata only) |
| Earlier questions and their specs | For a follow-up |
| Distinct values of declared text dimensions (up to 25 each, masked for your role) | Only with `share_dimension_values: true` |
| Up to 50 result rows | Only with `summarize_results: true` and `--summarize` |

With a hosted provider (`anthropic`, or `openai` pointed at a remote URL),
that metadata leaves the machine; row data does not unless you turn on one of
the two options above. With a local model, nothing leaves it.

## Evaluating accuracy

```yaml
# tests/ask/sales.yml
cases:
  - question: Revenue by region last year
    expect:
      metrics: [revenue]
      dimensions: [region]
      start: "2025-01-01"
      end: "2026-01-01"
  - question: Average delivery time
    expect: unanswerable
  - question: now by month
    history:
      - question: Revenue by region
        spec: {metrics: [revenue], dimensions: [region]}
    expect: {metrics: [revenue], grain: month}
```

`havn ask --eval tests/ask/*.yml` plans each question (nothing runs against
the warehouse) and compares only the keys under `expect`. Names, list order and
filter value order do not matter. It exits non-zero below `--min-accuracy`
(default 1.0), so it can gate CI.

## Verified agent changes

### In the agent sidebar

The sidebar's mode button cycles **Review → Auto → Ask**. Review is the
default:

- **Review**: the agent works in a temporary copy of the project (source files
  only: no warehouse, no `.env`, no git history). When it finishes a reply,
  what it changed becomes a change set and is verified. A card in the sidebar
  shows the result per check and the data diff, with **Apply**, **Re-verify**
  and **Discard**. A follow-up message revises the same change set, so the
  agent can fix a failing check.
- **Auto**: the agent writes project files directly (as before).
- **Ask**: the agent can only read.

### From an MCP agent

`submit_change_set` takes `files: [{path, content}]` (`content: null` deletes)
and returns the report. Pass `change_set_id` with the full new file set to
revise it until `ok` is true. `get_change_set` reads the status. MCP never
applies: the user does, from the UI or `havn changes apply`.

### What can be in a change set

`transform/**/*.sql`, `macros/*.py|.sql`, `tests/unit/*`, `tests/ask/*`,
`contracts/*.yml`, `metrics/*.yml`. Anything else the agent touched is listed
as not carried over.

### The checks

| Check | What it does |
|---|---|
| Read-only SQL | Every changed model passes the `/api/query` read-only check, so agent SQL cannot carry a second statement or read files. A model that fails it is not built or tested. |
| Validate | The project still loads, no cycles or duplicates, name-level checks on affected models. |
| Bind | The shadow bind pass, including contract column declarations. |
| Unit tests | Tests of every affected model, plus any changed test. |
| Scratch build | Affected models (changed plus everything downstream) built as tables into a temporary database attached next to the warehouse; references between them point at the scratch copies. |
| Assertions | Each rebuilt model's `@assert` lines, on the scratch copy. |
| Contracts | Contracts on affected models, on the scratch copy. A changed contract on an unaffected model runs on the real table. |
| Data diff | Each scratch copy against the real table: rows added, removed and modified (with `-- havn:primary_key`), schema changes, a few sample rows (masked for your role). |

Nothing is written to the warehouse during verification, and the scratch
database is deleted afterwards. **Ready to apply** appears only when no check
failed; **Apply anyway** is there for a failed one. Apply is all-or-nothing and
refuses when a file changed on disk since the change was proposed.

### CLI

```bash
havn changes                    # open change sets
havn changes show <id> --diff
havn changes verify <id>
havn changes apply <id> [--force]
havn changes discard <id>
```

### Limits

- Incremental and microbatch models are rebuilt in full in scratch, so their
  diff compares a full rebuild with the current table.
- Snapshot (SCD2) models are not rebuilt in scratch.
- The scratch build uses the macros registered on the running warehouse
  connection; unit tests and the bind pass load the change set's own macros.
- In the sidebar, the next message waits until the current change set is
  verified.
- Change sets are kept in `.havn/changesets/` (the newest 200).
