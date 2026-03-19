# Data Masking

havn provides column-level data masking to protect sensitive data. Masking policies are applied to query results after execution, before returning data to the client. Policies support multiple masking methods, conditional application, and role-based exemptions.

## Web UI Experience

### Masking Panel in Configure Tab

1. Go to the **Configure** tab and click **Masking**
2. The Masking panel shows all active masking policies in a table:
   - Schema, table, and column for each policy
   - Masking method (14 methods across general, PII, financial, and analytics categories)
   - Exempted roles
   - Conditional rules (if any)
3. **Add Policy** -- Click to create a new masking policy by selecting the schema, table, column, method, and exempted roles
4. **Edit Policy** -- Click any policy row to update its method, configuration, or exemptions
5. **Delete Policy** -- Remove a policy to stop masking that column

### SQL Commands in the Query Panel

You can also manage masking policies directly from the **Query** panel in the Explore tab using special SQL commands:

```sql
-- Create a policy
CREATE MASKING POLICY ON gold.customers.email METHOD hash

-- Create with role exemptions
CREATE MASKING POLICY ON gold.customers.ssn METHOD redact EXEMPT admin,editor

-- Show all policies
SHOW MASKING POLICIES

-- Drop a policy
DROP MASKING POLICY <policy_id>
```

havn intercepts these commands and translates them into masking operations.

### Masked Data in Tables Browser

When you browse tables in the **Explore** > **Tables** panel, masked columns display their masked values according to the active policies and your role. If you are exempt (e.g., admin), you see the real values.

## Masking Methods

havn provides 14 masking methods organized into four categories. Each method can be used when creating a masking policy via the API, CLI, or SQL commands.

### General

#### hash

SHA-256 hash, first 8 hex chars. Irreversible. Same input always produces the same output.

```
"john@example.com" -> "a1b2c3d4"
"Jane Smith"       -> "e5f6g7h8"
```

No configuration options.

#### redact

Replace the entire value with `***`. The simplest and most secure method.

```
"john@example.com" -> "***"
"555-1234"         -> "***"
```

No configuration options.

#### null

Replace the value with NULL. Use when the column should be completely hidden from non-exempt roles.

```
"john@example.com" -> NULL
"555-1234"         -> NULL
```

No configuration options.

#### partial

Show first and/or last N characters, mask the middle with `*`.

```
# show_first=2, show_last=5
"john@example.com" -> "jo***********e.com"

# show_first=2, show_last=4
"555-123-4567"     -> "55*******4567"
```

Configuration:
- `show_first` -- Number of characters to show from the beginning (default: 0)
- `show_last` -- Number of characters to show from the end (default: 0)

```json
{"method": "partial", "method_config": {"show_first": 2, "show_last": 4}}
```

#### truncate

Show first N characters followed by ellipsis. Simple partial visibility.

```
# length=3
"John Smith" -> "Joh..."
```

Configuration:
- `length` -- Number of visible characters (default: 3)

```json
{"method": "truncate", "method_config": {"length": 5}}
```

### PII

#### email

Hide the local part of an email address, keep the domain. Useful for analytics by email provider.

```
"john.doe@company.com" -> "***@company.com"
```

No configuration options.

#### phone

Keep last N digits of a phone number, mask the rest. Useful for verification use cases.

```
# show_last=4
"+1-555-123-4567" -> "**-***-***-4567"
```

Configuration:
- `show_last` -- Number of visible digits (default: 4)

```json
{"method": "phone", "method_config": {"show_last": 4}}
```

#### first_initial

Reduce names to initials. Semi-anonymous but still groupable.

```
"John Smith" -> "J. S."
```

No configuration options.

#### ip_address

Mask host octets of an IPv4 address, keep network prefix for geo-analytics.

```
# keep_octets=2
"192.168.1.42" -> "192.168.x.x"
```

Configuration:
- `keep_octets` -- Number of visible octets, 0-3 (default: 2)

```json
{"method": "ip_address", "method_config": {"keep_octets": 2}}
```

### Financial

#### credit_card

PCI-DSS compliant: mask all but last 4 digits.

```
# show_last=4
"4111111111111111" -> "************1111"
```

Configuration:
- `show_last` -- Number of visible digits (default: 4)

```json
{"method": "credit_card", "method_config": {"show_last": 4}}
```

### Analytics

These methods preserve analytical properties (distributions, relationships, time intervals) while masking exact values.

#### range

Bucket numeric values into ranges. Preserves distribution for aggregation-safe analytics.

```
# bucket_size=10000
47382 -> "40000-50000"
```

Configuration:
- `bucket_size` -- Size of each bucket (default: 10000)

```json
{"method": "range", "method_config": {"bucket_size": 5000}}
```

#### noise

Add deterministic random noise within +/- percentage. Same input always gets the same noise (seeded), so results are reproducible.

```
# percentage=10
47382 -> ~45200
```

Configuration:
- `percentage` -- Noise range as a percentage, +/- (default: 10.0)
- `seed_key` -- Seed string for deterministic noise (default: "")

```json
{"method": "noise", "method_config": {"percentage": 5.0, "seed_key": "revenue"}}
```

#### date_shift

Shift dates by a consistent random offset. Preserves time intervals for time-series analytics where relative ordering matters but exact dates are sensitive.

```
# max_days=30
"2024-03-15" -> "2024-03-28"
```

Supports `datetime` objects and common string formats (`YYYY-MM-DD`, `YYYY-MM-DD HH:MM:SS`).

Configuration:
- `max_days` -- Maximum shift in days, +/- (default: 30)
- `seed_key` -- Seed string for deterministic shifts (default: "")

```json
{"method": "date_shift", "method_config": {"max_days": 14, "seed_key": "orders"}}
```

#### consistent_hash

Deterministic pseudonym: same input always maps to the same output. Unlike `hash`, this is designed for JOIN-safe masking -- if two tables both have `user_id` masked with `consistent_hash` using the same config, the masked values will match and JOINs still work.

```
# prefix="usr_", length=8
"john@example.com" -> "usr_a1b2c3d4"
```

Configuration:
- `prefix` -- String prefix for the pseudonym (default: "")
- `length` -- Hash length in hex characters (default: 8)

```json
{"method": "consistent_hash", "method_config": {"prefix": "usr_", "length": 8}}
```

## Discovering Methods Programmatically

Use the `GET /api/masking/methods` endpoint to list all available methods with their descriptions, categories, example transformations, and config schemas:

```bash
curl http://localhost:3000/api/masking/methods
```

This returns the full method catalog, useful for building dynamic UIs or validating method names before creating policies.

## Creating Masking Policies

### Via REST API

```bash
# Create a hash policy
curl -X POST http://localhost:3000/api/masking/policies \
  -H "Content-Type: application/json" \
  -d '{
    "schema_name": "gold",
    "table_name": "customers",
    "column_name": "email",
    "method": "hash",
    "exempted_roles": ["admin"]
  }'

# Create a partial masking policy
curl -X POST http://localhost:3000/api/masking/policies \
  -H "Content-Type: application/json" \
  -d '{
    "schema_name": "gold",
    "table_name": "customers",
    "column_name": "phone",
    "method": "partial",
    "method_config": {"show_first": 0, "show_last": 4},
    "exempted_roles": ["admin"]
  }'
```

### Via CLI

```bash
havn mask add --schema gold --table customers --column email --method hash
havn mask add --schema gold --table customers --column ssn --method redact
havn mask list
havn mask remove <policy_id>
```

## Policy Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `schema_name` | string | yes | Table schema |
| `table_name` | string | yes | Table name |
| `column_name` | string | yes | Column to mask |
| `method` | string | yes | One of 14 methods: `hash`, `redact`, `null`, `partial`, `email`, `phone`, `credit_card`, `first_initial`, `ip_address`, `range`, `noise`, `date_shift`, `truncate`, `consistent_hash` |
| `method_config` | object | no | Method-specific config (e.g., `{"show_first": 2}`) |
| `condition_column` | string | no | Column to check for conditional masking |
| `condition_value` | string | no | Value that triggers masking |
| `exempted_roles` | list | no | Roles that see unmasked data (default: `["admin"]`) |

## Conditional Masking

Masking can be applied conditionally based on another column's value:

```bash
curl -X POST http://localhost:3000/api/masking/policies \
  -H "Content-Type: application/json" \
  -d '{
    "schema_name": "gold",
    "table_name": "customers",
    "column_name": "email",
    "method": "redact",
    "condition_column": "country",
    "condition_value": "EU",
    "exempted_roles": ["admin"]
  }'
```

This masks the `email` column only for rows where `country = 'EU'`, useful for GDPR compliance.

## Role Exemptions

By default, only users with the `admin` role see unmasked data. You can customize this per policy:

```json
{
  "exempted_roles": ["admin", "editor"]
}
```

When a user queries data:
1. havn loads all masking policies
2. For each policy, it checks if the user's role is in `exempted_roles`
3. If not exempted, the masking function is applied to matching columns

## How Masking Is Applied

Masking is applied **post-query** -- after the SQL query executes but before results are returned to the client. This means:

- **Queries run on unmasked data** -- Filters, aggregations, and joins operate on real values
- **Results are masked** -- Only the returned column values are transformed
- **Schema-aware matching** -- When querying a specific table (e.g., `/api/tables/{schema}/{table}/sample`), policies are matched by exact schema and table name
- **Column-name matching** -- For ad-hoc queries (`/api/query`), policies are matched by column name alone (best-effort)
- **Profile masking** -- Sample values in table profiles are also masked

## Managing Policies

### List All Policies

```bash
# API
curl http://localhost:3000/api/masking/policies

# SQL
SHOW MASKING POLICIES

# CLI
havn mask list
```

### Get a Specific Policy

```bash
curl http://localhost:3000/api/masking/policies/<policy_id>
```

### Update a Policy

```bash
curl -X PUT http://localhost:3000/api/masking/policies/<policy_id> \
  -H "Content-Type: application/json" \
  -d '{"method": "partial", "method_config": {"show_first": 2, "show_last": 0}}'
```

### Delete a Policy

```bash
# API
curl -X DELETE http://localhost:3000/api/masking/policies/<policy_id>

# SQL
DROP MASKING POLICY <policy_id>

# CLI
havn mask remove <policy_id>
```

## Policy Storage

Policies are stored in `_dp_internal.masking_policies` in DuckDB:

| Column | Type | Description |
|--------|------|-------------|
| `id` | VARCHAR | Auto-generated UUID |
| `schema_name` | VARCHAR | Target schema |
| `table_name` | VARCHAR | Target table |
| `column_name` | VARCHAR | Target column |
| `method` | VARCHAR | Masking method |
| `method_config` | JSON | Method configuration |
| `condition_column` | VARCHAR | Conditional column |
| `condition_value` | VARCHAR | Conditional value |
| `exempted_roles` | JSON | Roles exempt from masking |
| `created_at` | TIMESTAMP | Policy creation time |

## Best Practices

1. **Start with sensitive columns** -- Mask PII (email, phone, SSN, address) and financial data (credit card, bank account) first. Use the dedicated `email`, `phone`, and `credit_card` methods for domain-specific masking.

2. **Use consistent_hash for join keys** -- If masked data needs to be joined across tables, use `consistent_hash` with the same config. Same input always produces the same output, so JOINs still work across masked tables.

3. **Use redact for display-only fields** -- For columns that never need to be joined or filtered, `redact` is the simplest and most secure choice.

4. **Use analytics methods for aggregation columns** -- For numeric values that need approximate analytics, use `range` (bucketing) or `noise` (random perturbation). For dates, use `date_shift` to preserve time intervals.

5. **Set appropriate exemptions** -- Only exempt roles that genuinely need to see unmasked data. Most analysts can work with masked or bucketed values.

6. **Test with viewer role** -- After setting up masking, test queries as a viewer to verify that sensitive data is properly hidden.

## Related Pages

- [Auth](auth) -- RBAC roles and permissions
- [Quality](quality) -- Data quality framework
- [API Reference](api-reference) -- Masking API endpoints
- [CLI Reference](cli-reference) -- Masking CLI commands
