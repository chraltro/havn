# Arrow Flight SQL

For data-science workflows (pandas, polars, R, Julia) the HTTP JSON API
is 10-50× slower than Arrow once you're past ~100k rows. Flight SQL
streams Arrow record batches directly over gRPC with zero serialization
cost.

## Start the server

```bash
havn flight                                    # port 50051, no auth
HAVN_FLIGHT_TOKEN=s3cret havn flight           # bearer auth required
havn flight --host 127.0.0.1 --port 51000      # override bind
```

The server holds a long-lived DuckDB connection pool against the active
warehouse. Queries run in the `query` resource-manager category — they
show up in the UI's Active Tasks list alongside ad-hoc queries and
transforms, and can be cancelled from there.

## Client example (Python)

```python
import pyarrow.flight as flight

client = flight.FlightClient("grpc://localhost:50051")

# Token auth (if the server was started with one).
options = flight.FlightCallOptions(
    headers=[(b"authorization", b"Bearer s3cret")]
)

ticket = flight.Ticket(b"SELECT * FROM gold.orders WHERE year = 2025")
reader = client.do_get(ticket, options)
table = reader.read_all()          # pyarrow.Table

import polars as pl
df = pl.from_arrow(table)
```

## Client example (R)

```r
library(arrow)
client <- flight_connect("localhost", 50051)
tbl <- flight_get(client, "SELECT count(*) FROM gold.orders")
```

## Auth

- `--token` / `$HAVN_FLIGHT_TOKEN` sets a single bearer token.
- Accept both `Authorization: Bearer <token>` and
  `Authorization: Basic <base64(user:token)>` on the first call.
- If no token is set, anyone reaching the port can query — bind to
  `127.0.0.1` on shared machines.

## Limits

- Read-only: Flight always opens the backend with `read_only=True`.
- DuckLake catalog writers are serialized by havn's write queue; Flight
  can't accidentally create a write contention.
