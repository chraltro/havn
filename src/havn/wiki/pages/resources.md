# Resource Manager

havn runs every heavy DuckDB operation through a small, per-category
resource governor. This prevents one runaway transform or query from
eating the whole warehouse, and surfaces live task info in the web UI so
you know what's actually running.

## Categories

Four categories, each with independent memory, thread, and concurrency budgets:

| Category   | Covers                                                                |
|------------|-----------------------------------------------------------------------|
| transform  | SQL model builds                                                      |
| query      | ad-hoc SQL (Query panel, dashboards, `/v1/sql`, Flight), notebooks    |
| streaming  | webhook flush, CDC consumers, API pollers, **ingest scripts**         |
| system     | sentinel, diff, quality, backup, compaction, **export scripts**       |

Budgets are set in `project.yml` under the optional `resources:` key:

```yaml
resources:
  transform:
    memory_gb: 4
    threads: 4
    max_concurrent: 2
  query:
    memory_gb: 2
    threads: 2
    max_concurrent: 8
  streaming:
    memory_gb: 1
    threads: 2
    max_concurrent: 4
  system:
    memory_gb: 1
    threads: 2
    max_concurrent: 2
```

Defaults are safe on a laptop. Tune upward as you give havn more headroom.

## HTTP surface

| Endpoint                                    | Purpose                                 |
|---------------------------------------------|-----------------------------------------|
| `GET  /api/resources`                       | One-shot snapshot                       |
| `GET  /api/resources/stream`                | Server-Sent Events, emits every 2s      |
| `POST /api/resources/cancel/{task_id}`      | Cancel a running task                   |
| `PUT  /api/resources/allocation`            | Update a category's budget (persists to project.yml) |

## In the web UI

`Observe → Resources` shows:

- **Capacity bar** — the total memory budget, sliced by category.
- **Category cards** — live active/max, utilization, and editable dials.
- **Active tasks** — sortable list with a Cancel button per row.
- **Recent tasks** — last 20 completions with duration and status.

## Cancelling tasks

Every task registered with the manager exposes its DuckDB connection's
`interrupt()` as a cancellation callback. Clicking Cancel in the UI (or
hitting `POST /api/resources/cancel/{task_id}`) raises
`duckdb.InterruptException` inside the statement, unwinding cleanly and
marking the task as `cancelled` in the recent list.
