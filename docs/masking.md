# Data Masking

havn provides column-level data masking to protect sensitive data. Masking is applied via pre-query SQL rewriting (with post-query fallback), preventing alias bypass and inference attacks. Policies support multiple masking methods, conditional application, and role-based exemptions.

## Masking Methods

havn ships fourteen masking methods covering general redaction, PII categories,
financial data, and analytics-safe transformations. Use the catalog API
`GET /api/masking/methods` to fetch the live list with config schemas.

### General

#### hash

Replaces the value with the first 8 characters of its SHA-256 hash.
Deterministic per value, NOT JOIN-safe (use `consistent_hash` for that).

```
"john@example.com" -> "a1b2c3d4"
```

#### redact

Replaces the value with `***`.

#### null

Replaces the value with NULL.

#### partial

Shows the first and/or last N characters, masking the middle with `*`.

```
# show_first=2, show_last=4
"555-123-4567" -> "55*******4567"
```

Configuration: `show_first` (default 0), `show_last` (default 0).

#### truncate

Shows the first N characters followed by `...`. Useful for analytics where
the column prefix is informative.

Configuration: `length` (default 3).

### PII

#### email

Hides the local part of an email address, keeps the domain.

```
"john.doe@company.com" -> "***@company.com"
```

#### phone

Keeps the last N digits of a phone number, masks the rest.

Configuration: `show_last` (default 4).

#### first_initial

Reduces a name to initials separated by periods.

```
"Jane Smith Doe" -> "J. S. D."
```

#### ip_address

Masks host octets of an IPv4 address.

```
# keep_octets=2
"192.168.1.42" -> "192.168.x.x"
```

Configuration: `keep_octets` (default 2).

### Financial

#### credit_card

PCI-DSS-compliant: masks all but the last N digits of a credit card number.

```
"4111-1111-1111-1234" -> "************1234"
```

Configuration: `show_last` (default 4).

### Analytics-safe

#### range

Buckets a numeric value into a fixed-size range string. Preserves rough
distributions for analytics while hiding precise values.

```
# bucket_size=10000
42_500 -> "40000-49999"
```

Configuration: `bucket_size` (default 10000).

#### noise

Adds deterministic random noise within `+/- percentage`. Same input always
returns the same output for a given `seed_key`.

```
# percentage=10, seed_key="salary"
50000 -> 51234   (or 48721, etc., but stable per value)
```

Configuration: `percentage` (default 10.0), `seed_key` (default "").

#### date_shift

Shifts a date or timestamp by a deterministic random number of days within
`+/- max_days`. Stable per value+seed.

Configuration: `max_days` (default 30), `seed_key` (default "").

#### consistent_hash

JOIN-safe pseudonym: same input always produces the same output. Use this
when masked columns are used as join keys.

```
# prefix="cust_", length=8
"123e4567-..." -> "cust_a1b2c3d4"
```

Configuration: `prefix` (default ""), `length` (default 8).

## Creating Masking Policies

### Via SQL Commands

havn intercepts special SQL commands in the query editor:

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

### Via REST API

```bash
# Create a policy
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
havn mask remove --id <policy_id>
```

## Policy Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `schema_name` | string | yes | Table schema |
| `table_name` | string | yes | Table name |
| `column_name` | string | yes | Column to mask |
| `method` | string | yes | One of: `hash`, `redact`, `null`, `partial`, `truncate`, `email`, `phone`, `first_initial`, `ip_address`, `credit_card`, `range`, `noise`, `date_shift`, `consistent_hash` |
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

Masking uses a two-tier approach: **pre-query SQL rewriting** (primary) with a **post-query fallback**.

### Pre-query rewriting (primary)

Before a query executes, havn parses the SQL using SQLGlot and rewrites masked columns inline with their masking expressions. This prevents alias bypass -- `SELECT email AS x` still gets masked because the rewriter operates on the parsed AST, not column names in results. All 14 masking methods are supported in the rewriter.

### Post-query fallback

When SQLGlot cannot parse a query (e.g., DuckDB-specific syntax), havn falls back to post-query masking -- applying masking functions to result columns after execution. This ensures masking is always applied, even for edge-case SQL.

### Matching behavior

- **Schema-aware matching** -- When querying a specific table (e.g., `/api/tables/{schema}/{table}/sample`), policies are matched by exact schema and table name
- **Column-name matching** -- For ad-hoc queries (`/api/query`), policies are matched by column name alone (best-effort)
- **Profile masking** -- Sample values in table profiles are also masked

## Security: Masked Column Access Restrictions

Non-exempt users are **denied** from using masked columns in filtering, sorting, joining, or grouping contexts. Specifically, the following operations on masked columns return HTTP 403:

- **WHERE** clauses -- `WHERE email = 'x'` is blocked
- **ORDER BY** clauses -- `ORDER BY email` is blocked
- **JOIN ON** conditions -- `JOIN ... ON a.email = b.email` is blocked
- **HAVING** clauses -- `HAVING email IS NOT NULL` is blocked

This prevents data exfiltration through inference attacks (e.g., binary search via WHERE filters). Exempt roles (as configured per policy) are not restricted.

## Managing Policies

### List All Policies

```bash
curl http://localhost:3000/api/masking/policies
```

Or via SQL:

```sql
SHOW MASKING POLICIES
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
curl -X DELETE http://localhost:3000/api/masking/policies/<policy_id>
```

Or via SQL:

```sql
DROP MASKING POLICY <policy_id>
```

## Policy Storage

Policies are stored in `_havn.masking_policies` in DuckDB:

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

## Related Pages

- [Auth](auth) -- RBAC roles and permissions
- [Quality](quality) -- Data quality framework
- [API Reference](api-reference) -- Masking API endpoints
- [CLI Reference](cli-reference) -- Masking CLI commands
