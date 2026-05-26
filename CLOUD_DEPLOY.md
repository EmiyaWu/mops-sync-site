# Free Cloud Deployment Guide

## Architecture

- Google Cloud Scheduler triggers GitHub Actions every 5 minutes through the `workflow_dispatch` API.
- GitHub Actions still keeps its own schedule as a fallback, but Google Cloud Scheduler is the primary scheduler.
- The job always runs `python sync_once_output.py` to check MOPS and update Google Sheet.
- The public site is exported and deployed only when new MOPS rows are written, unless `force_deploy` is enabled manually.
- Google Sheet keeps the full internal dataset.
- `public/` contains only the semi-public static website.
- Cloudflare Pages deploys the static website.

## GitHub Actions Secrets

In the GitHub repository, open `Settings -> Secrets and variables -> Actions`, then add:

- `GOOGLE_SERVICE_ACCOUNT_JSON`: the full service account JSON file content
- `MOPS_SHEET_ID`: the Google Sheet ID
- `CLOUDFLARE_API_TOKEN`: Cloudflare Pages deploy token
- `CLOUDFLARE_ACCOUNT_ID`: Cloudflare account ID
- `CLOUDFLARE_PROJECT_NAME`: Cloudflare Pages project name
- `LINE_CHANNEL_ACCESS_TOKEN`: LINE Messaging API channel access token
- `LINE_NOTIFY_ENABLED`: set to `true` to send LINE notifications. The current workflow is temporarily set to `true` for LINE API testing.
- `LINE_NOTIFY_MAX_INDIVIDUAL`: maximum individual messages to send per sync, for example `10`
- `LINE_NOTIFY_INTERVAL_SECONDS`: optional minimum seconds between LINE notification sends, default `600`. The current workflow uses `30` for LINE API testing.
- `LINE_NOTIFY_ACTIVE_START_HOUR`: optional first hour for LINE notifications, default `0`
- `LINE_NOTIFY_ACTIVE_END_HOUR`: optional stop hour for LINE notifications, default `0`
- `LINE_TARGET_IDS`: optional admin fallback user IDs, separated by commas
- `LINE_SUBSCRIBERS_SHEET_ID`: private Google Sheet ID used for LINE subscriber fallback

Do not commit `service-account.json` to GitHub.

## LINE Broadcast Notifications

GitHub Actions uses `LINE_NOTIFY_MODE=broadcast`, so every user who added the LINE Official Account as a friend can receive new MOPS notifications.

New MOPS rows are first stored in `line_notify_queue` in the private subscriber Sheet. GitHub Actions still checks MOPS every 5 minutes. The workflow currently attempts LINE sending at most once every 30 seconds for testing.

LINE notifications can be sent all day by default. Setting `LINE_NOTIFY_ACTIVE_START_HOUR` and `LINE_NOTIFY_ACTIVE_END_HOUR` to the same value disables the active-hours restriction.

If LINE broadcast is rate-limited, pending notifications stay in the queue and are retried after the interval instead of being sent again immediately. Fallback recipients are loaded from:

1. `LINE_TARGET_IDS`
2. Active `user_id` rows in the private subscriber sheet configured by `LINE_SUBSCRIBERS_SHEET_ID`

The subscriber sheet must not be public. Share it only with your own account and the Google service account.

## LINE Subscriber Webhook

Use `line_webhook_worker.js` as the LINE webhook endpoint, and keep `line_webhook.gs` as the Google Sheet writer.

Flow:

```text
LINE -> Cloudflare Worker -> Google Apps Script -> private subscriber Sheet
```

LINE must call Cloudflare Worker, not Apps Script directly. Apps Script can return `302 Found` to LINE webhook verification, while the Worker returns `200 OK` directly and verifies LINE's `X-Line-Signature`.

### Google Apps Script

Use `line_webhook.gs` in a Google Apps Script project to collect LINE Messaging API user IDs automatically.

Apps Script properties:

- `LINE_CHANNEL_ACCESS_TOKEN`: LINE Messaging API channel access token
- `LINE_SUBSCRIBERS_SHEET_ID`: private Google Sheet ID for subscriber rows
- `LINE_WEBHOOK_SECRET`: random secret used in the deployed webhook URL

Deploy the Apps Script as a web app:

- Execute as: Me
- Who has access: Anyone

Then set the LINE Developers webhook URL to:

```text
https://YOUR_WORKER_NAME.YOUR_SUBDOMAIN.workers.dev/
```

### Cloudflare Worker

Create a Cloudflare Worker and paste `line_webhook_worker.js`.

Worker variables and secrets:

- `LINE_CHANNEL_SECRET`: LINE Messaging API channel secret
- `APPS_SCRIPT_WEBHOOK_URL`: full Apps Script web app URL, including the secret query string

Example `APPS_SCRIPT_WEBHOOK_URL`:

```text
https://script.google.com/macros/s/DEPLOYMENT_ID/exec?secret=LINE_WEBHOOK_SECRET_VALUE
```

After deploying the Worker, set LINE Developers `Webhook URL` to the Worker URL and click `Verify`.

## Google Cloud Scheduler

Use `GOOGLE_CLOUD_SCHEDULER.md` to create the external 5-minute scheduler.

The scheduler calls:

```text
POST https://api.github.com/repos/EmiyaWu/mops-sync-site/actions/workflows/sync-and-deploy.yml/dispatches
```

with this JSON body:

```json
{"ref":"main"}
```

This intentionally does not pass `force_deploy`, so routine 5-minute checks do not redeploy Cloudflare Pages when there are no new rows.

To manually rebuild the website without new rows, open GitHub Actions, click `Run workflow`, and enable `force_deploy`.

## Public Website Fields

The website exposes only:

- Date
- Time
- Company ID
- Company abbreviation
- Subject
- Detail content

The website does not expose:

- Data key
- Fetched-at timestamp

## Local Preview

```powershell
python mos_s.py once --export-site public
python -m http.server 8000 -d public
```

Open:

```text
http://localhost:8000
```
