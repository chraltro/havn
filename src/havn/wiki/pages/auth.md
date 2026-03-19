# Authentication and Authorization

havn includes built-in authentication with role-based access control (RBAC). When enabled, users must log in to access the web UI and API. Roles control what actions each user can perform.

## Web UI Experience

### Login Screen

When auth is enabled, visiting `localhost:3000` shows a login screen. Enter your username and password to access the platform. The token is stored in the browser and used for all subsequent API requests.

### Initial Setup Wizard

On first launch with `--auth` and no users configured, the web UI shows a setup screen:

1. Enter a username, password, and display name for the first admin user
2. Click **Create Admin Account**
3. You are automatically logged in and redirected to the Overview tab

### User Management in Settings

1. Go to **Configure** > **Settings** > **Users** (requires admin role)
2. View all users with their roles, display names, and last login times
3. **Create User** -- Add new users with username, password, role (admin/editor/viewer), and display name
4. **Edit User** -- Change a user's role, display name, or password
5. **Delete User** -- Remove a user and revoke all their active tokens

### Secrets Management in Settings

1. Go to **Configure** > **Settings** > **Secrets** (requires admin role)
2. View all `.env` variable keys (values are always masked as `****`)
3. **Add Secret** -- Set a new environment variable
4. **Delete Secret** -- Remove an environment variable

## Enabling Authentication

Start the server with the `--auth` flag:

```bash
havn serve --auth
```

Without `--auth`, all endpoints are accessible without authentication (suitable for local development).

## Initial Setup

On first launch with `--auth`, no users exist. The web UI presents a setup screen to create the first admin user. Alternatively, use the API:

```bash
curl -X POST http://localhost:3000/api/auth/setup \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your-password", "role": "admin"}'
```

This creates the first admin user and returns a token:

```json
{
  "token": "abc123...",
  "username": "admin",
  "role": "admin"
}
```

The setup endpoint only works when no users exist. After the first user is created, additional users must be created by an admin.

## Roles and Permissions

havn defines three roles with cumulative permissions:

| Role | Permissions | Description |
|------|------------|-------------|
| `viewer` | `read` | Browse tables, view DAG, run queries (read-only) |
| `editor` | `read`, `write`, `execute` | All viewer permissions plus run pipelines, edit files, manage data |
| `admin` | `read`, `write`, `execute`, `manage_users`, `manage_secrets` | Full access including user management and secrets |

### Permission Details

- **read** -- View tables, run SELECT queries, browse files, view DAG, view history, view wiki
- **write** -- Edit files, save models, create versions, manage masking policies, edit wiki
- **execute** -- Run pipelines, execute scripts, trigger transforms, import data, run contracts
- **manage_users** -- Create, update, delete users
- **manage_secrets** -- View, set, delete secrets in `.env`

## Managing Users via CLI

```bash
havn users create --username analyst --password secure-pass --role viewer
havn users list
havn users delete analyst
```

## Authentication Flow

### Login

```bash
curl -X POST http://localhost:3000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your-password"}'
```

Returns:

```json
{
  "token": "abc123...",
  "username": "admin"
}
```

### Using Tokens

Include the token in the `Authorization` header for all subsequent requests:

```bash
curl http://localhost:3000/api/tables \
  -H "Authorization: Bearer abc123..."
```

### Token Lifetime

Tokens expire after 30 days by default. After expiration, the user must log in again to get a new token.

### Check Auth Status

```bash
curl http://localhost:3000/api/auth/status
```

Returns whether auth is enabled and whether initial setup is needed:

```json
{
  "auth_enabled": true,
  "needs_setup": false
}
```

### Get Current User

```bash
curl http://localhost:3000/api/auth/me \
  -H "Authorization: Bearer abc123..."
```

Returns:

```json
{
  "username": "admin",
  "role": "admin",
  "display_name": "Admin User"
}
```

## User Management via API

All user management operations require the `admin` role.

### List Users

```bash
curl http://localhost:3000/api/users \
  -H "Authorization: Bearer <admin-token>"
```

Returns:

```json
[
  {
    "username": "admin",
    "role": "admin",
    "display_name": "Admin User",
    "created_at": "2025-01-15 06:00:00",
    "last_login": "2025-01-15 12:30:00"
  }
]
```

### Create User

```bash
curl -X POST http://localhost:3000/api/users \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "analyst",
    "password": "secure-password",
    "role": "viewer",
    "display_name": "Data Analyst"
  }'
```

Username constraints: alphanumeric, underscores, dots, and hyphens. Password minimum: 4 characters.

### Update User

```bash
curl -X PUT http://localhost:3000/api/users/analyst \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"role": "editor", "display_name": "Senior Analyst"}'
```

You can update `role`, `password`, and `display_name` independently.

### Delete User

```bash
curl -X DELETE http://localhost:3000/api/users/analyst \
  -H "Authorization: Bearer <admin-token>"
```

Deleting a user also revokes all their active tokens.

## Password Security

Passwords are stored using PBKDF2 with SHA-256 and 100,000 iterations:

- A 32-byte random salt is generated per user
- The password is hashed with `PBKDF2(SHA256, password, salt, 100000)`
- Only the hash and salt are stored -- passwords are never stored in plaintext

## Rate Limiting

The login endpoint is rate-limited to prevent brute-force attacks. Each client IP is limited in its login attempt frequency.

## Secrets Management

Admin users can manage secrets (`.env` variables) through the CLI, web UI, or API.

### List Secrets

```bash
# CLI
havn secrets list

# API
curl http://localhost:3000/api/secrets \
  -H "Authorization: Bearer <admin-token>"
```

Returns keys with masked values (never exposes actual secret values).

### Set a Secret

```bash
# CLI
havn secrets set DB_PASSWORD new-password

# API
curl -X POST http://localhost:3000/api/secrets \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"key": "DB_PASSWORD", "value": "new-password"}'
```

### Delete a Secret

```bash
# CLI
havn secrets delete DB_PASSWORD

# API
curl -X DELETE http://localhost:3000/api/secrets/DB_PASSWORD \
  -H "Authorization: Bearer <admin-token>"
```

## Data Storage

Auth data is stored in the DuckDB database under `_dp_internal`:

| Table | Contents |
|-------|----------|
| `_dp_internal.users` | User accounts (username, password hash, salt, role, display name, timestamps) |
| `_dp_internal.tokens` | Active authentication tokens with expiration |

## Audit Logging

When auth is enabled, user actions are logged to `_dp_internal.audit_log`:

- Login events
- Query execution
- Pipeline runs
- File edits
- User management operations

View the audit log via API:

```bash
GET /api/audit?limit=100
```

## Related Pages

- [Masking](masking) -- Data masking with role-based exemptions
- [Configuration](configuration) -- Project configuration
- [API Reference](api-reference) -- Full API endpoint reference
