"""Scaffold templates for `havn init`.

These are large string constants used to create sample projects.
Separated from config.py to keep configuration logic focused.
"""

from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# project.yml
# ---------------------------------------------------------------------------

PROJECT_YML_TEMPLATE = """\
name: {name}
description: "Earthquake analytics pipeline — a havn sample project"
sample: true

database:
  path: warehouse.duckdb
  memory_limit: 75%
  threads: 4

connections: {{}}
  # Example: connect to a PostgreSQL database
  # prod_postgres:
  #   type: postgres
  #   host: localhost
  #   port: 5432
  #   database: production
  #   user: ${{POSTGRES_USER}}
  #   password: ${{POSTGRES_PASSWORD}}

# Pipelines live in orchestration/ as individual YAML job files. Edit them
# in the web UI (Develop -> Orchestration) or directly on disk. The starter
# jobs in this sample project run the earthquake pipeline end-to-end.

# alerts:
#   slack:
#     webhook_url: ${{SLACK_WEBHOOK}}
#     events: [pipeline_failure, assertion_failed, anomaly]
#   webhook:
#     url: ${{ALERT_WEBHOOK_URL}}
#     events: [pipeline_failure]

lint:
  dialect: duckdb
"""

PROJECT_YML_EMPTY_TEMPLATE = """\
name: {name}

database:
  path: warehouse.duckdb

connections: {{}}
  # Example: connect to a PostgreSQL database
  # prod_postgres:
  #   type: postgres
  #   host: localhost
  #   port: 5432
  #   database: production
  #   user: ${{POSTGRES_USER}}
  #   password: ${{POSTGRES_PASSWORD}}

# Pipelines live in orchestration/ as individual YAML job files.
# Create them via the web UI (Develop -> Orchestration) or add YAML
# files directly. Each job picks targets from your DAG; schedules can
# be cron expressions (e.g. "0 6 * * *") or intervals (e.g. "every 2 weeks").

lint:
  dialect: duckdb
"""

PROJECT_YML_DUCKLAKE_TEMPLATE = """\
name: {name}
description: "A havn sample project (DuckLake backend)"
sample: {sample}

database:
  backend: ducklake
  catalog: .havn/catalog.ducklake
  data_path: .havn/data
  encrypted: false
  memory_limit: 75%
  threads: 4

connections: {{}}

# Pipelines live in orchestration/ as individual YAML job files.

lint:
  dialect: duckdb
"""

# ---------------------------------------------------------------------------
# Ingest notebook — fetches USGS earthquake data, falls back to sample data
# ---------------------------------------------------------------------------

# Build the ingest cell source — this is the core data loading logic
_INGEST_FETCH_SOURCE = """\
import json
from pathlib import Path
from urllib.request import urlopen

FEED_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_month.geojson"

try:
    print("Fetching live earthquake data from USGS...")
    with urlopen(FEED_URL, timeout=15) as resp:
        data = json.loads(resp.read())
    features = data.get("features", [])
    print(f"Fetched {len(features)} earthquakes (M2.5+, last 30 days)")
except Exception as e:
    print(f"USGS API unavailable ({type(e).__name__}), using sample data...")
    features = [
        {"id":"us7000m1a1","properties":{"mag":7.1,"place":"154km SSW of Kainantu, Papua New Guinea","time":1735776000000,"updated":1735862400000,"felt":120,"tsunami":0,"sig":776,"magType":"mww","type":"earthquake","status":"reviewed","detail":""},"geometry":{"coordinates":[146.49,-6.45,90.2]}},
        {"id":"us7000m2b2","properties":{"mag":6.4,"place":"67km SW of Dili, Timor Leste","time":1735862400000,"updated":1735948800000,"felt":30,"tsunami":1,"sig":450,"magType":"mb","type":"earthquake","status":"reviewed","detail":""},"geometry":{"coordinates":[125.30,-9.02,45.0]}},
        {"id":"us7000m3c3","properties":{"mag":5.8,"place":"12km SE of Ridgecrest, CA","time":1735948800000,"updated":1736035200000,"felt":892,"tsunami":0,"sig":518,"magType":"mww","type":"earthquake","status":"reviewed","detail":""},"geometry":{"coordinates":[-117.58,35.60,8.0]}},
    ]
    print(f"Loaded {len(features)} sample earthquakes")\
"""

_INGEST_LOAD_SOURCE = """\
import pandas as pd

rows = [
    {**f["properties"], "id": f["id"],
     "latitude": f["geometry"]["coordinates"][1],
     "longitude": f["geometry"]["coordinates"][0],
     "depth_km": f["geometry"]["coordinates"][2]}
    for f in features
]

df = pd.DataFrame(rows)
db.execute("CREATE SCHEMA IF NOT EXISTS landing")
db.execute(
    "CREATE OR REPLACE TABLE landing.earthquakes AS "
    "SELECT * REPLACE ("
    "  epoch_ms(time::BIGINT) AS time,"
    "  epoch_ms(updated::BIGINT) AS updated"
    ") FROM df"
)
print(f"Loaded {len(rows)} earthquakes into landing.earthquakes")\
"""

SAMPLE_INGEST_NOTEBOOK = json.dumps({
    "title": "Earthquake Ingestion",
    "cells": [
        {
            "id": "cell_1",
            "type": "markdown",
            "source": (
                "# Earthquake Data Ingestion\n\n"
                "Fetches M2.5+ earthquakes from the past 30 days via the "
                "[USGS GeoJSON API](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php).\n\n"
                "If the API is unavailable (offline, firewall, timeout), the notebook "
                "falls back to 25 sample earthquakes so the pipeline always works."
            ),
        },
        {
            "id": "cell_2",
            "type": "code",
            "source": _INGEST_FETCH_SOURCE,
            "outputs": [],
        },
        {
            "id": "cell_3",
            "type": "markdown",
            "source": "## Load into landing table\n\nCreate `landing.earthquakes` and insert the raw data.",
        },
        {
            "id": "cell_4",
            "type": "code",
            "source": _INGEST_LOAD_SOURCE,
            "outputs": [],
        },
        {
            "id": "cell_5",
            "type": "markdown",
            "source": "## Preview\n\nTop 10 earthquakes by magnitude.",
        },
        {
            "id": "cell_6",
            "type": "code",
            "source": 'db.execute("SELECT id, mag AS magnitude, place FROM landing.earthquakes ORDER BY magnitude DESC LIMIT 10")',
            "outputs": [],
        },
    ],
}, indent=2) + "\n"

# ---------------------------------------------------------------------------
# SQL transform models — bronze / silver / gold with assertions & docs
# ---------------------------------------------------------------------------

SAMPLE_BRONZE_SQL = """\
@config materialized=table, schema=bronze
@depends_on landing.earthquakes
@description Cleaned earthquake records with proper types and readable column names
@assert row_count > 0
@assert no_nulls(event_id)

SELECT
    id AS event_id,
    mag AS magnitude,
    magType AS mag_type,
    place,
    latitude,
    longitude,
    depth_km,
    sig AS significance,
    type AS event_type,
    status,
    time AS event_time,
    updated AS updated_at,
    coalesce(felt, 0) AS felt_reports,
    tsunami = 1 AS tsunami_alert
FROM landing.earthquakes
WHERE mag IS NOT NULL
"""

SAMPLE_SILVER_EVENTS_SQL = """\
@config materialized=table, schema=silver, incremental_strategy=delete+insert, unique_key=event_id
@depends_on bronze.earthquakes
@description Enriched earthquake events with magnitude class, region, and depth classification
@assert row_count > 0
@assert unique(event_id)
@assert accepted_values(magnitude_class, ['Minor', 'Light', 'Moderate', 'Strong', 'Major', 'Great'])
@assert accepted_values(depth_class, ['Shallow', 'Intermediate', 'Deep'])

SELECT
    event_id,
    event_time,
    magnitude,
    mag_type,
    place,
    latitude,
    longitude,
    depth_km,
    felt_reports,
    tsunami_alert,
    significance,
    cast(event_time AS DATE) AS event_date,
    classify_magnitude(magnitude) AS magnitude_class,  -- Python macro (see macros/geo.py)
    CASE
        WHEN place LIKE '% of %'
            THEN trim(split_part(place, ' of ', 2))
        ELSE place
    END AS region,
    CASE
        WHEN depth_km < 70 THEN 'Shallow'
        WHEN depth_km < 300 THEN 'Intermediate'
        ELSE 'Deep'
    END AS depth_class,
    hour(event_time) AS event_hour
FROM bronze.earthquakes
"""

SAMPLE_SILVER_DAILY_SQL = """\
@config materialized=table, schema=silver
@depends_on silver.earthquake_events
@description Daily earthquake aggregates for dashboard and trend analysis
@assert row_count > 0
@assert unique(event_date)
@assert total_events > 0

SELECT
    event_date,
    count(*) AS total_events,
    round(avg(magnitude), 2) AS avg_magnitude,
    max(magnitude) AS max_magnitude,
    round(avg(depth_km), 1) AS avg_depth_km,
    sum(
        CASE
            WHEN
                magnitude_class IN (
                    'Strong', 'Major', 'Great'
                )
                THEN 1
            ELSE 0
        END
    ) AS significant_count,
    sum(
        CASE WHEN tsunami_alert THEN 1 ELSE 0 END
    ) AS tsunami_alerts,
    sum(felt_reports) AS total_felt_reports,
    arg_max(place, magnitude) AS strongest_location
FROM silver.earthquake_events
GROUP BY event_date
ORDER BY event_date DESC
"""

SAMPLE_GOLD_SUMMARY_SQL = """\
@config materialized=table, schema=gold
@depends_on silver.earthquake_events, silver.earthquake_daily
@description Daily earthquake dashboard with magnitude class breakdown
@col event_date: Calendar date
@col total_events: Number of earthquakes recorded
@col significant_count: Events with magnitude >= 6.0 (Strong, Major, Great)
@assert row_count > 0
@assert unique(event_date)

SELECT
    d.event_date,
    d.total_events,
    d.avg_magnitude,
    d.max_magnitude,
    d.avg_depth_km,
    d.significant_count,
    d.tsunami_alerts,
    d.total_felt_reports,
    d.strongest_location,
    sum(CASE
        WHEN e.magnitude_class = 'Minor' THEN 1
        ELSE 0
    END) AS minor_count,
    sum(CASE
        WHEN e.magnitude_class = 'Light' THEN 1
        ELSE 0
    END) AS light_count,
    sum(CASE
        WHEN e.magnitude_class = 'Moderate' THEN 1
        ELSE 0
    END) AS moderate_count,
    sum(CASE
        WHEN e.magnitude_class = 'Strong' THEN 1
        ELSE 0
    END) AS strong_count,
    sum(CASE
        WHEN e.magnitude_class = 'Major' THEN 1
        ELSE 0
    END) AS major_count,
    sum(CASE
        WHEN e.magnitude_class = 'Great' THEN 1
        ELSE 0
    END) AS great_count
FROM silver.earthquake_daily AS d
INNER JOIN silver.earthquake_events AS e
    ON d.event_date = e.event_date
GROUP BY ALL
ORDER BY d.event_date DESC
"""

SAMPLE_GOLD_TOP_SQL = """\
@config materialized=table, schema=gold
@depends_on silver.earthquake_events
@description Significant earthquakes (M4.5+) ranked by magnitude
@col event_id: USGS earthquake identifier
@col magnitude: Richter scale magnitude
@col magnitude_class: Human-readable severity (Moderate/Strong/Major/Great)
@assert row_count > 0
@assert no_nulls(event_id)

SELECT
    event_id,
    event_time,
    magnitude,
    magnitude_class,
    place,
    region,
    latitude,
    longitude,
    depth_km,
    depth_class,
    felt_reports,
    tsunami_alert,
    significance
FROM silver.earthquake_events
WHERE magnitude >= 4.5
ORDER BY magnitude DESC, event_time DESC
"""

SAMPLE_GOLD_REGIONS_SQL = """\
@config materialized=table, schema=gold
@depends_on silver.earthquake_events
@description Regional seismic activity summary for risk assessment
@col region: Geographic region extracted from USGS place description
@col significant_events: Events with magnitude >= 5.0
@col avg_distance_to_ring_of_fire_km: Average distance from the Pacific Ring of Fire (Tokyo reference point)
@assert row_count > 0

SELECT
    region,
    count(*) AS total_events,
    round(avg(magnitude), 2) AS avg_magnitude,
    max(magnitude) AS max_magnitude,
    round(avg(depth_km), 1) AS avg_depth_km,
    sum(CASE
        WHEN magnitude >= 5.0 THEN 1 ELSE 0
    END) AS significant_events,
    sum(CASE
        WHEN tsunami_alert THEN 1 ELSE 0
    END) AS tsunami_alerts,
    min(event_date) AS first_event,
    max(event_date) AS last_event,
    round(avg(haversine_km(latitude, longitude, 35.68, 139.69)), 0)  -- Python macro: distance to Tokyo
        AS avg_distance_to_ring_of_fire_km
FROM silver.earthquake_events
GROUP BY region
HAVING count(*) >= 2
ORDER BY total_events DESC
"""

# ---------------------------------------------------------------------------
# Python SQL macro — reusable functions callable directly in SQL
# ---------------------------------------------------------------------------

SAMPLE_MACRO_GEO = '''\
"""Geospatial and seismology helpers -- callable directly in SQL.

Usage in SQL:
    SELECT classify_magnitude(6.5)            -- returns 'Strong'
    SELECT haversine_km(35.6, -117.6, 0, 0)   -- returns distance in km
"""

import math

from havn import macro


@macro
def classify_magnitude(mag: float) -> str:
    """Classify earthquake magnitude on the standard seismological scale."""
    if mag is None:
        return "Unknown"
    if mag >= 8.0:
        return "Great"
    if mag >= 7.0:
        return "Major"
    if mag >= 6.0:
        return "Strong"
    if mag >= 5.0:
        return "Moderate"
    if mag >= 4.0:
        return "Light"
    return "Minor"


@macro
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in kilometers."""
    R = 6371.0  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
'''

# ---------------------------------------------------------------------------
# Export script
# ---------------------------------------------------------------------------

SAMPLE_EXPORT_SCRIPT = '''\
"""Export earthquake analytics to CSV files."""

from pathlib import Path

output_dir = Path(__file__).parent.parent / "output"
output_dir.mkdir(exist_ok=True)

tables = [
    ("gold.earthquake_summary", "earthquake_summary.csv"),
    ("gold.top_earthquakes", "top_earthquakes.csv"),
    ("gold.region_risk", "region_risk.csv"),
]

for table, filename in tables:
    dest = str(output_dir / filename).replace("'", "''")
    db.execute(f"COPY {table} TO '{dest}' (HEADER, DELIMITER ',')")
    rows = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"Exported {rows} rows to output/{filename}")
'''

# ---------------------------------------------------------------------------
# Seed data — magnitude scale lookup table
# ---------------------------------------------------------------------------

SAMPLE_SEED_CSV = """\
class,min_magnitude,max_magnitude,description,risk_level,typical_effects
Minor,2.5,3.9,Minor earthquake,low,Often felt; rarely causes damage
Light,4.0,4.9,Light earthquake,low,Noticeable shaking of indoor objects
Moderate,5.0,5.9,Moderate earthquake,medium,Can damage poorly constructed buildings
Strong,6.0,6.9,Strong earthquake,high,Destructive in populated areas up to 160km
Major,7.0,7.9,Major earthquake,critical,Serious damage over large areas
Great,8.0,10.0,Great earthquake,critical,Can totally destroy communities near epicenter
"""

# ---------------------------------------------------------------------------
# Contracts — data quality rules
# ---------------------------------------------------------------------------

SAMPLE_FULL_REFRESH_JOB = """\
name: full-refresh
description: Full pipeline — rebuild every gold model and export the results
targets:
  - gold.*
  - export/earthquake_report.py
resolve: upstream
tags:
  - daily
enabled: true
retry: 1
retry_delay: 30
timeout_minutes: 60
# Uncomment to run automatically:
# schedules:
#   - "0 6 * * *"          # every day at 6:00 AM
#   - "every 1 day"        # or an interval, whichever you prefer
"""

SAMPLE_INCREMENTAL_JOB = """\
name: incremental
description: Quick refresh — only rebuild changed gold models and their upstream
targets:
  - gold.*
resolve: upstream
tags:
  - hourly
enabled: true
retry: 0
timeout_minutes: 30
# schedules:
#   - "0 * * * *"          # every hour on the hour
"""

SAMPLE_CONTRACTS_YML = """\
contracts:
  - name: pipeline_produces_data
    description: "The pipeline must produce earthquake summary data"
    model: gold.earthquake_summary
    severity: error
    assertions:
      - row_count > 0
      - no_nulls(event_date)
      - unique(event_date)

  - name: significant_earthquakes_valid
    description: "Top earthquakes table must contain only M4.5+ events"
    model: gold.top_earthquakes
    severity: warn
    assertions:
      - row_count > 0
      - no_nulls(event_id)
      - unique(event_id)

  - name: regions_have_data
    description: "Region risk table must have aggregated data"
    model: gold.region_risk
    severity: warn
    assertions:
      - row_count > 0
      - no_nulls(region)
"""

# ---------------------------------------------------------------------------
# Interactive notebook — explore the data after a pipeline run
# ---------------------------------------------------------------------------

SAMPLE_EXPLORE_NOTEBOOK = json.dumps({
    "title": "Earthquake Explorer",
    "cells": [
        {
            "id": "cell_1",
            "type": "markdown",
            "source": (
                "# Earthquake Explorer\n\n"
                "Interactive analysis of earthquake data. "
                "Run `havn jobs run full-refresh` first to populate the warehouse."
            ),
        },
        {
            "id": "cell_2",
            "type": "sql",
            "source": (
                "-- Overview: date range, event count, magnitude stats\n"
                "SELECT\n"
                "    count(*) AS total_events,\n"
                "    min(event_date) AS earliest,\n"
                "    max(event_date) AS latest,\n"
                "    round(avg(magnitude), 2) AS avg_magnitude,\n"
                "    max(magnitude) AS max_magnitude\n"
                "FROM silver.earthquake_events"
            ),
            "outputs": [],
        },
        {
            "id": "cell_3",
            "type": "sql",
            "source": (
                "-- Strongest earthquakes\n"
                "SELECT magnitude, magnitude_class, place, event_time, depth_class\n"
                "FROM silver.earthquake_events\n"
                "ORDER BY magnitude DESC\n"
                "LIMIT 10"
            ),
            "outputs": [],
        },
        {
            "id": "cell_4",
            "type": "sql",
            "source": (
                "-- Events by magnitude class\n"
                "SELECT\n"
                "    magnitude_class,\n"
                "    count(*) AS events,\n"
                "    round(avg(depth_km), 1) AS avg_depth_km,\n"
                "    sum(CASE WHEN tsunami_alert THEN 1 ELSE 0 END) AS tsunami_alerts\n"
                "FROM silver.earthquake_events\n"
                "GROUP BY magnitude_class\n"
                "ORDER BY min(magnitude) DESC"
            ),
            "outputs": [],
        },
        {
            "id": "cell_5",
            "type": "sql",
            "source": (
                "-- Most active regions (with distance from Ring of Fire)\n"
                "SELECT region, total_events, max_magnitude, avg_magnitude,\n"
                "       avg_distance_to_ring_of_fire_km\n"
                "FROM gold.region_risk\n"
                "ORDER BY total_events DESC\n"
                "LIMIT 10"
            ),
            "outputs": [],
        },
        {
            "id": "cell_6",
            "type": "sql",
            "source": (
                "-- Python macros in action: call classify_magnitude() and haversine_km() directly in SQL\n"
                "-- These are plain Python functions from macros/geo.py, auto-registered as DuckDB UDFs\n"
                "SELECT\n"
                "    classify_magnitude(7.5) AS major_class,\n"
                "    classify_magnitude(3.2) AS minor_class,\n"
                "    round(haversine_km(35.68, 139.69, 37.77, -122.42), 0) AS tokyo_to_sf_km"
            ),
            "outputs": [],
        },
        {
            "id": "cell_7",
            "type": "markdown",
            "source": (
                "## Next steps\n\n"
                "- Add new SQL models in `transform/` to explore different angles\n"
                "- Create Python macros in `macros/` for reusable SQL functions\n"
                "- Run `havn jobs run incremental` for a fast re-run (only changed models)\n"
                "- Run `havn diff` to see what changed in the data\n"
                "- Use `havn query \"SELECT ...\"` for quick ad-hoc queries\n"
                "- Run `havn contracts` to validate data quality rules"
            ),
        },
    ],
}, indent=2) + "\n"

# ---------------------------------------------------------------------------
# CLAUDE.md — agent instructions
# ---------------------------------------------------------------------------

CURSORRULES_TEMPLATE = """\
You are working on a havn data platform project. havn uses DuckDB + plain SQL transforms + Python ingest/export. All data in a single warehouse.duckdb file.

# SQL models go in transform/ with @decorator config:
# @config materialized=table, schema=silver
# @depends_on bronze.customers
# Folder name = default schema. No Jinja — plain SQL only.
# Incremental: @config materialized=table, schema=silver, incremental_strategy=delete+insert, unique_key=id

# Python SQL macros go in macros/ with @macro decorator:
# from havn import macro
# @macro
# def my_func(x: str) -> int: ...
# Then call directly in SQL: SELECT my_func(col) FROM table

# Python scripts go in ingest/ or export/:
# A DuckDB connection is available as `db` — just write top-level code.
# Legacy `def run(db)` scripts are also supported.

# Key commands:
# havn transform        — build SQL models (incremental change detection)
# havn run <script>     — run an ingest or export script
# havn jobs run <name>  — run a pipeline (full-refresh or incremental)
# havn query "<sql>"    — run ad-hoc SQL queries
# havn macros           — list Python SQL macros
# havn tables           — list warehouse tables
# havn diff             — diff changed models
# havn lint             — lint SQL (SQLFluff, DuckDB dialect)
# havn serve            — start web UI on :3000

# Code patterns:
# - from __future__ import annotations in all Python files
# - DuckDB connections: always try/finally with conn.close()
# - Tests: pytest tests/ — uses real temp DuckDB, no mocks

# Don't:
# - Add Jinja/templating to SQL — use Python macros for reusable logic
# - Mock DuckDB in tests
# - Modify _havn schema from user-facing code
"""

COPILOT_INSTRUCTIONS_TEMPLATE = """\
## havn — Self-Hosted Data Platform

havn uses DuckDB + plain SQL transforms + Python ingest/export. All data in a single `warehouse.duckdb` file. Data in safe waters.

### SQL models go in `transform/` with @decorator config:

```sql
@config materialized=table, schema=silver
@depends_on bronze.customers
SELECT * FROM bronze.customers WHERE active = true
```

Folder name = default schema. No Jinja — plain SQL only. Use Python macros for reusable logic.

### Incremental models:
```sql
@config materialized=table, schema=silver, incremental_strategy=delete+insert, unique_key=id
```

### Python SQL macros go in `macros/` with `@macro` decorator:
```python
from havn import macro

@macro
def my_func(x: str) -> str:
    return x.upper()
```
Then call directly in SQL: `SELECT my_func(col) FROM table`

### Python scripts go in `ingest/` or `export/`:

```python
# A DuckDB connection is available as `db` — just write top-level code
db.execute("CREATE OR REPLACE TABLE landing.x AS SELECT * FROM ...")
```

### Key commands: `havn transform`, `havn run <script>`, `havn jobs run <name>`, `havn query "<sql>"`, `havn macros`, `havn diff`, `havn lint`, `havn tables`, `havn serve`

### Code patterns:
- `from __future__ import annotations` in all Python files
- DuckDB connections: always `try/finally` with `conn.close()`
- Tests: `pytest tests/` — uses real temp DuckDB, no mocks

### Don't:
- Add Jinja/templating to SQL — use Python macros instead
- Mock DuckDB in tests
- Modify `_havn` schema from user-facing code
"""

CLAUDE_MD_TEMPLATE = """\
# CLAUDE.md — Agent Instructions for {name}

This is a havn data platform project. havn uses DuckDB for analytics, plain SQL for transforms, and Python for ingest/export.

## Commands

```bash
havn transform              # build SQL models in dependency order
havn transform --force      # force rebuild all
havn run ingest/script.py   # run a single script
havn jobs run full-refresh  # run full pipeline (seed -> ingest -> transform -> export)
havn jobs run incremental   # quick refresh (only changed models)
havn seed                   # load CSV files from seeds/ into DuckDB
havn query "SELECT 1"       # ad-hoc SQL query
havn tables                 # list warehouse objects
havn macros                 # list Python SQL macros (UDFs)
havn lint                   # lint SQL (SQLFluff, DuckDB dialect)
havn lint --fix             # auto-fix lint violations
havn diff                   # diff changed models + downstream
havn diff gold.orders       # diff a single model
havn serve                  # start web UI on :3000
havn history                # show run log
havn contracts              # evaluate data quality contracts
havn snapshot create "name" # create warehouse snapshot
havn validate               # check project structure and DAG
havn context                # generate AI-friendly project summary
havn ci generate            # generate GitHub Actions workflow
havn env list               # show available environments
```

## Project Layout

```
ingest/           Python scripts / .dpnb notebooks that load data into DuckDB
transform/
  bronze/         Light cleanup SQL (views/tables)
  silver/         Business logic, joins, enrichment
  gold/           Consumption-ready models
export/           Python scripts that export data out
macros/           Python SQL macros — functions callable directly in SQL queries
seeds/            CSV reference data (loaded with havn seed)
contracts/        YAML data quality rules (evaluated with havn contracts)
notebooks/        Interactive .dpnb notebooks for exploration
project.yml       Streams, connections, schedules, alerts
.env              Secrets (never committed)
warehouse.duckdb  The database (single file)
```

## SQL Model Convention

```sql
@config materialized=table, schema=silver
@depends_on bronze.customers, bronze.orders
@description Customer order summary with aggregates
@col customer_id: Unique customer identifier
@col order_count: Total orders including cancelled
@assert row_count > 0
@assert unique(customer_id)
@assert no_nulls(customer_id)

SELECT c.customer_id, c.name, COUNT(o.order_id) AS order_count
FROM bronze.customers c
LEFT JOIN bronze.orders o ON c.customer_id = o.customer_id
GROUP BY 1, 2
```

- `@config` sets materialization (view/table) and schema
- `@depends_on` declares upstream dependencies for DAG ordering
- `@description` documents what the model does
- `@col name: desc` documents individual columns
- `@assert` defines data quality checks (row_count, unique, no_nulls, accepted_values)
- Folder name = default schema (e.g., transform/bronze/ -> schema=bronze)
- No Jinja, no templating — plain SQL only (use Python macros for reusable logic)

### Incremental Models

For models that should only process new/changed rows:
```sql
@config materialized=table, schema=silver, incremental_strategy=delete+insert, unique_key=event_id
```

Strategies: `delete+insert` (default), `append`, `merge` (true upsert), `partition_by`.
Second pipeline run only rebuilds what changed — see `silver/earthquake_events.sql` for an example.

## Python SQL Macros

Python functions in `macros/` are auto-registered as DuckDB UDFs, callable directly in SQL:

```python
# macros/geo.py
from havn import macro

@macro
def classify_magnitude(mag: float) -> str:
    if mag >= 8.0: return "Great"
    if mag >= 7.0: return "Major"
    ...
```
```sql
-- Use in any SQL model:
SELECT event_id, classify_magnitude(magnitude) AS magnitude_class FROM bronze.earthquakes
```

- `@macro` decorator marks functions for registration
- Type hints map to DuckDB types (str->VARCHAR, int->INTEGER, float->DOUBLE)
- Run `havn macros` to list all available macros

## Python Script Convention

```python
# A DuckDB connection is available as `db` — just write top-level code
db.execute("CREATE SCHEMA IF NOT EXISTS landing")
db.execute("CREATE OR REPLACE TABLE landing.data AS SELECT * FROM ...")
```

- Scripts run as top-level code with `db` (DuckDB connection) pre-injected
- Legacy `def run(db)` scripts are still supported (backward compatible)
- `.dpnb` notebooks can also be used as ingest/export steps
- Scripts prefixed with `_` are skipped

## Data Quality

**Inline assertions** (in SQL models):
```sql
@assert row_count > 0
@assert unique(order_id)
@assert no_nulls(customer_id)
@assert accepted_values(status, ['pending', 'shipped', 'delivered'])
```

**Contracts** (in contracts/*.yml):
```yaml
contracts:
  - name: orders_valid
    model: gold.orders
    severity: error
    assertions:
      - row_count > 0
      - unique(order_id)
```

## Schemas

- `landing` — raw data from ingest scripts
- `bronze` — cleaned, deduplicated
- `silver` — business logic, joins
- `gold` — consumption-ready
- `seeds` — reference data from CSV files
- `_havn` — metadata (do not modify directly)

## Things You Can Ask Your AI Assistant

**Adding data:**
- "Load CSV files from data/customers.csv into the warehouse"
- "Create an ingest script that pulls data from our Postgres database"
- "Add a new data source for our Stripe payments API"

**Transforming data:**
- "Create a silver model that joins customers with their orders"
- "Add a gold table that shows monthly revenue by product category"
- "Create a Python macro to mask email addresses, then use it in a gold model"

**Data quality:**
- "Add assertions to check that order amounts are positive"
- "Create a contract that validates customer data completeness"
- "Set up a masking policy to redact PII columns for non-admin users"

**Querying & exploring:**
- "Show me the top 10 customers by order count"
- "What tables are in the warehouse and what columns do they have?"
- "Run a diff to see what changed since the last transform"

**Operations:**
- "Run the full pipeline and show me what happened"
- "Run the incremental stream to pick up only new data"
- "Set up a daily schedule for the full-refresh stream"
- "Create a snapshot before I make changes"
- "Generate a CI workflow for GitHub Actions"

**Tip:** Run `havn context` to generate a summary of your project that you can
paste into any AI chat (ChatGPT, Claude, etc.) for instant context.
"""
