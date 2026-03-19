# Data Masking

havn provides column-level data masking to protect sensitive data. Masking policies are applied to query results after execution, before returning data to the client. Policies support multiple masking methods, conditional application, and role-based exemptions.

## Web UI Experience

### Masking Panel in Configure Tab

1. Go to the **Configure** tab and click **Masking**
2. The Masking panel shows all active masking policies in a table:
   - Schema, table, and column for each policy
   - Masking method (hash, redact, null, partial)
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

### hash

Replaces the value with the first 8 characters of its SHA-256 hash:

```
"john@example.com" -> "a1b2c3d4"
"Jane Smith"       -> "e5f6g7h8"
```

Useful for maintaining referential integrity (same input always produces same hash) while hiding actual values.

### redact

Replaces the value with `***`:

```
"john@example.com" -> "***"
"555-1234"         -> "***"
```

The simplest and most secure method -- completely hides the original value.

### null

Replaces the value with NULL:

```
"john@example.com" -> NULL
"555-1234"         -> NULL
```

Useful when downstream systems need to handle the column but shouldn't see any data.

### partial

Shows the first and/or last N characters, masking the rest with `*`:

```
# show_first=2, show_last=5
"john@example.com" -> "jo***********e.com"

# show_first=2, show_last=4
"555-123-4567"     -> "55*******4567"
```

Configuration:
- `show_first` -- Number of characters to show from the beginning (default: 0)
- `show_last` -- Number of characters to show from the end (default: 0)

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
| `method` | string | yes | `hash`, `redact`, `null`, or `partial` |
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

1. **Start with sensitive columns** -- Mask PII (email, phone, SSN, address) and financial data (credit card, bank account) first.

2. **Use hash for join keys** -- If masked data needs to be joined across tables, use `hash` to maintain referential integrity while hiding actual values.

3. **Use redact for display-only fields** -- For columns that never need to be joined or filtered, `redact` is the simplest and most secure choice.

4. **Set appropriate exemptions** -- Only exempt roles that genuinely need to see unmasked data. Most analysts can work with hashed or partial values.

5. **Test with viewer role** -- After setting up masking, test queries as a viewer to verify that sensitive data is properly hidden.

## Related Pages

- [Auth](auth) -- RBAC roles and permissions
- [Quality](quality) -- Data quality framework
- [API Reference](api-reference) -- Masking API endpoints
- [CLI Reference](cli-reference) -- Masking CLI commands
