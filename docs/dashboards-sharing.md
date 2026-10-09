# Published dashboards and scheduled reports

A dashboard can be read by people who never open the editor: as a read-only
page behind a link, inside another site, or delivered to their inbox or Slack
channel on a schedule.

Everything a viewer sees runs through the same governed read path as
`/api/query`: read-only SQL validation, column masking for the identity the
query runs as, the per-role query timeout and post-query masking. Viewers
never send SQL. They see the results of the dashboard's saved widget
queries, filtered by the dashboard's declared filters.

## Share links

Open a dashboard and choose **Share**.

| | Signed-in viewers | Public link |
|---|---|---|
| Who can open it | anyone with a havn account and read permission | anyone with the link |
| Runs as | the person viewing (their own masking) | a "view as" user or role you choose |
| Who can create it | editors and admins | admins only |
| URL | `/p/<link id>` | `/p/<secret token>` |
| Expiry | optional | optional (default 30 days in the dialog) |

Every link can be revoked at once from the same dialog; the record stays for
the audit trail. Creating, changing and revoking links are written to the
audit log as `dashboard_publish` and `dashboard_unpublish`.

### Public links

* The token is 256 bits of randomness. Only its SHA-256 is stored, so the
  link is shown **once**, when it is created. If it is lost, revoke it and
  make a new one.
* **View as** decides governance for everyone who has the link:
  * a **role** (`viewer`, `editor`, `admin`). Admins are exempt from
    masking by default, so `admin` shows raw values.
  * a **user**. The user's *current* role is read on every request: demote
    the user and the link shows less straight away; delete the user and the
    link stops working.
* Public viewers get no error detail (a failing widget says "could not be
  loaded"), and the freshness line shows when the data was built, not which
  models it came from.
* Requests are rate-limited per link and client address.
* Turn public links off for a project with `sharing.public_links: false`
  (existing links stop working too), and cap their lifetime with
  `sharing.max_public_link_days`.

### What a viewer can do

* Use the dashboard's filters and parameters. Values are checked against the
  filter's type (a date range must be dates, a number range numbers) and are
  bound as query parameters; only columns declared as dashboard filters are
  accepted. Dropdown options come from the filter's saved options query, run
  on the server.
* Switch pages, expand a widget, refresh. The page reloads its data every five
  minutes while it is visible.
* Not edit, not drill down or cross-filter by clicking charts (those build
  new filters on the fly, so they stay in the editor), not see SQL.

The page shows **Data as of**: the oldest last-build time
(`_havn.model_state`) among the models the widgets read, because a dashboard
is as fresh as its stalest input.

On a phone the widgets stack in reading order, with KPI tiles two to a row.

## Embedding

Each link in the Share dialog has an **Embed** snippet:

```html
<iframe src="https://havn.example.com/p/<key>?embed=1" title="Sales" width="100%"
        height="600" style="border:0" loading="lazy" referrerpolicy="no-referrer"></iframe>
```

`?embed=1` trims the header and footer. `&theme=havn-light` (any color theme
id) matches a light host page.

Browsers only allow the frame when the host site is listed in `project.yml`:

```yaml
sharing:
  embed:
    allowed_origins:
      - https://intranet.example.com
```

Published pages (`/p/...`) are sent with
`Content-Security-Policy: frame-ancestors 'self' <allowed origins>`,
`Referrer-Policy: no-referrer` (the URL is a credential for public links) and
`X-Robots-Tag: noindex`. Every other page keeps `X-Frame-Options: DENY`.
Origins that are not a plain `scheme://host[:port]` are ignored.

A signed-in link inside an iframe asks for a sign-in in the frame, because
browsers keep a third-party frame's storage separate from the havn tab.

## Scheduled reports

**Data › Reports** (or **Share › Schedule a report** on a dashboard) delivers a
whole dashboard, or one widget, on a cron schedule:

* **Recipients**: email addresses and Slack incoming webhooks.
* **Attachments**: a PDF, a PNG snapshot of the dashboard and a CSV of each
  widget's rows. Every report also has an inline summary with the KPI values
  and the first rows of each table.
* **Filters**: fixed values for the dashboard's declared filters.
* **Only send when**: a condition on one widget, for alert-style reports:
  its headline value is above, at least, below, at most, equal to or other
  than a number, or it returns rows / no rows. A report whose condition does
  not hold is recorded as *skipped*. **Send now** offers *Send anyway*.

A report runs as its **owner**, the person who created it, with the owner's
current role and masking, whoever presses Send. Only the owner or an admin
can edit, send, preview or delete it. If the owner is deleted the report fails
until it is recreated.

Every delivery is recorded (Reports › click the status to see the history)
and audited as `report_delivery`, with each channel's result.

### Schedules

Schedules are standard five-field cron (`0 7 * * 1-5` is weekdays at 07:00),
in the server's local time, evaluated by the scheduler that also runs
streams and jobs. Start it with `havn serve --schedule` (or `havn schedule`).
A report fires at most once per scheduled minute, even across restarts.

### Configuration

```yaml
reports:
  base_url: https://havn.example.com   # links in reports point here
  smtp:
    host: smtp.example.com
    port: 587
    username: ${SMTP_USER}              # from .env
    password: ${SMTP_PASSWORD}
    from: havn reports <reports@example.com>
    starttls: true                      # or ssl: true for port 465
  slack_webhook_url: ${SLACK_REPORTS_WEBHOOK}   # the "default" Slack target
  allowed_recipient_domains: [example.com]      # empty: any address
  max_rows_per_widget: 1000
  charts: auto                          # "off" to never draw charts
```

Slack targets on a report are `default` (the URL above, or
`alerts.slack_webhook_url`), a `${VAR}` from `.env`, or a webhook URL. Stored
URLs are shown shortened in the UI and API because a webhook URL is a secret.

A link to the dashboard is included when `base_url` is set and the dashboard
has a signed-in link.

### Rendering, with and without charts

Reports are rendered on the server without a browser.

* With matplotlib (`pip install "havn[reports]"`), chart widgets are drawn
  as images: inline in the email, as the PNG snapshot and in the PDF.
* Without it, the email has tables and KPI values, the PDF is text-only
  (KPIs and tables) and the PNG is left out with a note in the email. The
  Reports page says which mode the server is in.

Bar, column, line, area, pie, donut and scatter charts are drawn; other chart
types appear as tables.

## CLI

```bash
havn reports list                         # name, dashboard, schedule, owner, last result
havn reports send "Daily sales"           # deliver now, as the owner
havn reports send "Low stock" --force     # even if the condition does not hold
havn reports preview "Daily sales"                      # writes Daily_sales.html
havn reports preview "Daily sales" -f pdf -o out.pdf    # or png
```

When `havn serve` is running for the project, the commands go through its
API (set `HAVN_TOKEN` if auth is on); otherwise they open the warehouse.

## API

| Method | Path | Permission |
|---|---|---|
| GET/POST | `/api/dashboards/{id}/shares` | write (public links: admin) |
| GET | `/api/shares` | write |
| PATCH/DELETE | `/api/shares/{share_id}` | write (public links: admin) |
| GET | `/api/published/{key}` | none for public links, read for signed-in |
| POST | `/api/published/{key}/query` | same |
| POST | `/api/published/{key}/filters/{filter_id}/options` | same |
| GET/POST | `/api/reports` | write |
| GET/PUT/DELETE | `/api/reports/{id}` | write; changes need owner or admin |
| POST | `/api/reports/{id}/send?force=` | owner or admin |
| POST | `/api/reports/{id}/preview` | owner or admin |
| GET | `/api/reports/{id}/render?format=pdf\|png\|html` | owner or admin |
| GET | `/api/reports/capabilities` | write |

Storage: `_havn.dashboard_shares`, `_havn.reports` and
`_havn.report_deliveries`, created on first use.
